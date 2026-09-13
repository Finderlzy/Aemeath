// Aemeath 文字链路验收驱动（10 轮真实中文对话 + 客户端延迟采样）
//
// 在 live ego-browser 会话里运行。测量口径与计划一致：
//   首段文字 = 提交输入 → 页面首次显示该轮回复（客户端单调时钟 performance.now）
//
// 结果以哨兵 JSON 输出，由调用方解析后写入验收记录。

const ROUNDS = [
  { id: "t01", text: "你好，简单介绍一下你自己吧。" },
  { id: "t02", text: "你平时是怎么记住我说过的话的？" },
  { id: "t03", text: "我叫林知远，在一家做气象数据的公司做后端。", fact: true },
  { id: "t04", text: "我习惯每天早上七点起床，先看半小时书。", fact: true },
  { id: "t05", text: "刚才我说我叫什么来着？" },
  { id: "t06", text: "换个话题吧，你觉得写代码最难的部分是什么？" },
  { id: "t07", text: "我养了一只叫墨墨的鹦鹉，它会学人说话。", fact: true },
  { id: "t08", text: "我周末一般会去爬山，最近在练耐力。" },
  { id: "t09", text: "我说过关于宠物的什么事吗？" },
  { id: "t10", text: "今天就聊到这儿，谢谢你。" },
];

/** 等待页面出现新的助手消息，返回其文本与耗时。 */
async function sendAndMeasure(page, text, turnId, timeoutMs) {
  const before = await page.evaluate(() => {
    // 记录提交前的消息容器状态，用于识别"新"消息。
    const nodes = [...document.querySelectorAll('[class*="chat"], [class*="message"], p')];
    return { count: document.body.innerText.length, snapshot: document.body.innerText };
  });

  const submitted = await page.evaluate((payload) => {
    const ta = document.querySelector('textarea');
    if (!ta) return { ok: false, reason: 'no textarea' };

    // React 受控组件：写入 value 后必须派发 input 事件，否则状态不会更新。
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value'
    ).set;
    setter.call(ta, payload.text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));

    const t0 = performance.now();
    window.__aemeath_marks = window.__aemeath_marks || {};
    window.__aemeath_marks[payload.turnId] = { submittedAt: t0, text: payload.text };

    // 回车提交，与用户实际操作一致。
    ta.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true,
    }));
    return { ok: true, submittedAt: t0 };
  }, { text, turnId });

  if (!submitted.ok) {
    return { turnId, ok: false, reason: submitted.reason, firstTextMs: null, reply: '' };
  }

  // 轮询页面直到出现新的助手文本，或超时。
  const deadline = Date.now() + timeoutMs;
  let firstTextMs = null;
  let reply = '';
  while (Date.now() < deadline) {
    const state = await page.evaluate((snapshot) => {
      const now = document.body.innerText;
      const marks = window.__aemeath_marks || {};
      const last = Object.values(marks).pop() || {};
      return { now, changed: now.length > snapshot.length, elapsed: performance.now() - (last.submittedAt || performance.now()) };
    }, before.snapshot);

    if (state.changed) {
      firstTextMs = state.elapsed;
      reply = state.now;
      break;
    }
    await new Promise((r) => setTimeout(r, 100));
  }

  // 给流式输出一点时间收尾，再取最终文本。
  await new Promise((r) => setTimeout(r, 4000));
  const finalText = await page.evaluate(() => document.body.innerText);
  const added = finalText.slice(before.snapshot.length).trim();

  return {
    turnId,
    ok: firstTextMs !== null,
    firstTextMs,
    reply: added.slice(0, 600),
    reason: firstTextMs === null ? 'timeout waiting for reply' : '',
  };
}

const results = [];
for (const round of ROUNDS) {
  const r = await sendAndMeasure(page, round.text, round.id, 60000);
  results.push({ ...r, sentText: round.text, isFact: !!round.fact });
  console.log(`[${round.id}] ${r.ok ? r.firstTextMs.toFixed(0) + 'ms' : 'FAIL'} :: ${r.reply.replace(/\n/g, ' / ').slice(0, 120)}`);
  // 轮次之间留出间隔，模拟真人节奏并避免触发限流。
  await new Promise((res) => setTimeout(res, 2500));
}

const samples = results.filter((r) => r.firstTextMs !== null).map((r) => r.firstTextMs);
const sorted = [...samples].sort((a, b) => a - b);
const p95 = sorted.length ? sorted[Math.min(sorted.length - 1, Math.ceil(0.95 * sorted.length) - 1)] : null;

console.log(JSON.stringify({
  rounds: results,
  samples,
  count: samples.length,
  p50: sorted.length ? sorted[Math.floor(sorted.length / 2)] : null,
  p95,
  failures: results.filter((r) => !r.ok).map((r) => ({ turnId: r.turnId, reason: r.reason })),
}));
