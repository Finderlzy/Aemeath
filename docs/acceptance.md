# 首期验收记录

> **T00 复核结论（2026-09-13，见第九节）**：历史真实验收的实际执行日期为 **2026-09-12**（原始报告已确证）；
> 原记载的 2026-09-14 **不成立**，系文档笔误。首段文字延迟以 **1287 ms / 10 样本**为唯一有效口径
> （原始 JSONL 复算一致）；原 README 的 518 ms **无法追溯，标为待核**。两项均**不能**用于宣称延迟验收通过。
> 测试基线已重新运行为 **244 passed**，与本文记载一致；[启动手册](runbook.md) 的 224 项为过期数据，已修正。
> 复核只做证据校准，不重跑历史真实验收、不改验收判据，也不以此宣布任何功能通过。

> 2026-09-12 文档复核：下文保留历史验收记录。
> 新发现的主动输出、回执计数及屏幕缓存缺口见 [架构复盘](architecture.md#2026-09-12-核心链路复盘)，后续任务见 [实施计划](implementation-plan.md)。其中历史“迟到摘要被拒”仅能支持已测路径，不代表并发返回后内部缓存也通过。

更新：2026-09-12（T00 于 2026-09-13 复核，见第九节）。本文件区分**模块单元测试**、**协议与集成验证（隔离替身）**、
**真实 API 与设备功能**与**延迟和持续使用体验**四层。前两层通过不代表后两层通过。
2026-09-12 补充 EasyCLIProxyAPI 视觉接入与屏幕理解验收。

> 真实 API 与设备层的实测记录见 [acceptance-live.md](acceptance-live.md)，
> 重复操作方式见 [acceptance-guide.md](acceptance-guide.md)。

## 〇、四层结果速览

| 层 | 结果 | 说明 |
| --- | --- | --- |
| 1. 模块测试 | 通过 | 244 项（2026-09-13 在 `71376f0` 复跑确认，见第九节） |
| 2. 生产入口隔离集成 | 通过 | 含 check_config 导入回归 |
| 3. 真实 API 与设备功能 | **部分通过，存在阻塞** | 对话、提取、屏幕捕获、**视觉**通过；嵌入阻塞（代理不提供端点）；语音未验收 |
| 4. 延迟与持续使用 | **未完成验收** | 首段文字 P95 1287 ms 为历史初步数据（10 样本，未达 20）；语音两项延迟与两次人工试用未进行 |

## 一、当前总体结论

> **桌面运行链路已接通，隔离集成验证通过；真实 API 与设备验收部分完成，
> 嵌入因代理不提供端点阻塞，视觉已通过 EasyCLIProxyAPI 接入，
> 语音与人工试用尚未进行。**

不能据此声称"首期功能全部通过"。
上一版"功能通过、体验未通过"的表述同样**不成立**——当时的"功能通过"
没有经过生产入口，存在模块已写但未接入链路的问题（见第三节）。

本轮真实链路验收又修出两个**只在真实链路上暴露**的缺陷，
其中一个使对话功能从未真正工作过（见第三节末尾）。

## 二、已自动化验证

命令：

```powershell
cd E:\WorkSpace\Aemeath
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest
```

结果：`244 passed, 7 deselected`（模块单元测试 + 生产入口集成测试 + check_config 导入回归，含 12 项真实链路回归）。
7 项 `live_api` 由 `pytest.ini` 的 `addopts = -m "not live_api"` 默认排除，属预期，不参与本层统计。
项数已由 T00 在 `71376f0` 重新核对（见第九节）；下表项数为 2026-09-13 实测值。

| 测试 | 项数 | 入口 | 覆盖 |
| --- | --- | --- | --- |
| `tests/test_phase1_conversation.py` | 21 | 模块 | 轮次规则、打断、失败处理、情境注入 |
| `tests/test_phase2_situation.py` | 37 | 模块 | 课堂静音、状态持久化、误触发防护 |
| `tests/test_phase3_memory.py` | 37 | 模块 | 事实/经历记忆、纠正、删除、**检索相关性下限** |
| `tests/test_phase45_screen_proactive.py` | 46 | 模块 | 屏幕观察边界、主动搭话规则、**可等待的取消** |
| `tests/test_phase6_runtime.py` | 19 | 模块 | 运行时装配、指标、用量 |
| `tests/test_check_config_import.py` | 1 | 模块 | check_config 从仓库根导入上游 |
| `tests/integration/test_classroom.py` | 10 | **生产** | 播放中切课堂、连续打断、迟到音频拒绝 |
| `tests/integration/test_memory_flow.py` | 11 | **生产** | 历史写入、两类异步回写竞态、共享来源保留 |
| `tests/integration/test_proactive_screen.py` | 22 | **生产** | 冷却/频次/未回应、观察代次失效、失败可重试、**信号不自启会话** |
| `tests/integration/test_lifecycle_metrics.py` | 21 | **生产** | 重连接管、后台任务释放、**工厂启动后台任务**、指标口径分离 |
| `tests/integration/test_real_link_regressions.py` | 12 | **生产** | **上游类同一性（回复不被丢弃）**、**SQLite 连接必然关闭**、**指标可跨进程回读**、live 探测装配 |
| `tests/integration/test_calibrate_rule.py` | 7 | **生产** | 记忆检索校准判据（同义/无关分离度规则） |

模块类合计 161，生产入口类合计 83，总计 244。

"生产入口"指测试经过真实的配置 schema 校验、`AgentFactory`、桥接层、
上游 conversation/tts_manager 输出处理与客户端协议，只替换模型、音频设备与捕获端。

### 计划指定场景的对应结果

| 计划场景 | 结果 | 测试 |
| --- | --- | --- |
| 播放中切课堂：音频停止、队列清空、迟到音频被拒、确认文字可见 | 通过 | `test_late_audio_rejected_after_switch`、`test_switch_to_class_sends_state_then_clear_then_text` |
| 连续打断及新输入：旧文字/音频不覆盖新轮次 | 通过 | `test_consecutive_interrupts_keep_latest_turn`、`test_late_text_for_cancelled_turn_is_dropped` |
| 真实装配的对话：写入历史、提取队列、召回、重启恢复 | 通过 | `test_turn_records_history_through_bridge`、`test_recall_and_restart_recovery` |
| 删除发生在提取等待期间：不重生 | 通过 | `test_delete_during_extraction_prevents_recreation` |
| 删除发生在嵌入等待期间：无孤立向量 | 通过 | `test_delete_during_embedding_leaves_no_orphan` |
| 一条消息含两个独立事实：删一个留一个 | 通过 | `test_two_facts_one_message_delete_one` |
| 历史恢复：删除内容不从工作上下文返回 | 通过 | `test_deleted_message_absent_from_restored_context` |
| 无关查询可返回空 | 通过 | `test_unrelated_query_returns_empty` |
| 主动信号连续触发：冷却与频次有效 | 通过 | `test_cooldown_blocks_second_message`、`test_hourly_limit_blocks_repeated_messages` |
| 关闭观察时视觉返回：旧摘要被丢弃 | 通过 | `test_vision_result_discarded_after_observation_disabled` |
| 重连和退出：不重复调度、不向旧连接输出 | 通过 | `test_old_connection_stops_receiving_output`、`test_background_tasks_start_and_stop` |
| Aemeath 不再写上游 JSON 历史 | 通过 | `test_no_upstream_json_history_is_written` |

## 三、本轮修复的问题（此前"功能通过"不成立的原因）

上一轮 151 项测试**没有覆盖生产入口**，以下问题是接入真实链路后才暴露的：

| 问题 | 表现 | 修复 |
| --- | --- | --- |
| 桥接未接入会话上下文 | 每个 WebSocket 会话克隆 `ServiceContext`，桥接丢失，输出绕过仲裁 | 显式把桥接赋给新上下文 |
| 轮次未注册到指标 | 生产路径从未调用 `start_turn`，指标恒为空 | `begin_user_turn` 注册轮次 |
| 用户消息未写入会话 | 记录时未传 `conversation_id`，客户端历史为空 | 传入当前会话 id |
| 数据库迁移破坏启动 | 旧库缺 `conversation_id` 列，服务启动即失败 | 迁移后再建依赖新列的索引 |
| 切换观察未清空摘要 | `reset()` 只清图像缓存，`current_summary()` 仍返回旧值 | 同时清空 `_latest` |
| 片段定位不可用 | 提取会把"我"改写为"用户"，字面匹配失败，遗忘无法定位来源 | 分句 + 相似度匹配，阈值以下要求用户选定 |
| 检索无条件返回 | 无关查询也返回相似度最低的若干条 | 增加可配置相似度下限，默认 0.5 |
| 指标口径可减出负数 | 后端用墙上时间，客户端与后端基准不同 | 后端改用单调时钟，客户端回执作为独立字段 |
| 取消任务不可等待 | `interrupt()`/`observe_user_text()` 用 `create_task()` 分离取消操作，状态变化与输出发送顺序不确定 | 取消任务纳入 `_pending_cancels` 跟踪，新增 `drain_cancellations()` 与 `observe_user_text_ordered()`，桥接在发清空指令前 `await` |
| **后台任务从未在真实服务启动** | `runtime.start()` 只在测试里被手动调用，生产代码从不调用；真实服务没有记忆任务与主动调度任务 | `AgentFactory` 创建 agent 时在有事件循环的情况下 `create_task(runtime.start())` |
| **主动聊天没有任何发起方** | `_proactive_worker` 只是 `sleep`；`request_proactive` 直接返回 `None`，注释称"由 runtime 任务驱动"，但该任务不存在；`run_proactive` 只被测试调用 | worker 改为调用 `bridge.run_proactive()`；桥接新增 `set_proactive_generator()`、`maybe_startup_greeting()`；`ServiceContext` 装配生成器 |
| **`ai-speak-signal` 绕过调度器** | 该消息仍路由到上游 `_handle_conversation_trigger`，直接启动 conversation，使客户端成为主动轮次的来源 | 改路由到 `_handle_ai_speak_signal`，只请求资格检查；非 Aemeath agent 保持原行为 |
| `or True` 无效断言 | 断言永真 | 改为真实断言 |
| 依赖 `sleep` 的取消测试 | `test_interrupt_cancels_audio` 用 `await asyncio.sleep(0.01)` 等待分离任务，只证明"某个任务最终跑了" | 改为 `await drain_cancellations()`，取消成为断言前已完成的事实 |

集成测试另修出两处替身缺陷（空 WAV 导致 `max() arg is an empty sequence`、
全零 WAV 导致 `Audio is empty or all zero`），属测试设施问题，非产品缺陷。

### 本轮（2026-09-12）真实链路验收修出的问题

以下问题**只在真实链路上暴露**，224 项模块与集成测试全部无法发现：

| 问题 | 表现 | 根因 | 修复 |
| --- | --- | --- | --- |
| **上游模块被导入两次，回复全部被丢弃** | 模型已生成回复，但每条都被丢弃，界面永久停在 `Thinking...`；日志只有 `Received unexpected item type from agent chat stream: <class '...SentenceOutput'>` | 服务端把上游加载为 `src.open_llm_vtuber.*`，Aemeath 却以 `open_llm_vtuber.*` 导入同一批文件，同一文件被加载两次，产生**两个不同的 `SentenceOutput` 类**，`isinstance` 判定失败 | Aemeath 与测试统一改用 `src.open_llm_vtuber.*` |
| **SQLite 连接从不关闭** | Windows 下数据库文件被锁住，临时目录无法删除；替换数据库与清理验收数据都会失败 | `with sqlite3.connect(...)` 是**事务**上下文管理器，只提交不关闭；`memory.py` 与 `situation.py` 共 41 处这样写 | 新增 `_connection()` 上下文管理器，提交后必然 `close()` |
| 对话端点忽略 TLS 设置 | 记忆提取能连通，对话却报 `CERTIFICATE_VERIFY_FAILED`，容易被误判为配置错误 | OpenAI SDK 自建 `httpx` 客户端，不读环境中的代理与 TLS 设置；Aemeath 自己的适配器走统一客户端因而不受影响 | 新增补丁 `0004`，对话端点使用受 `AEMEATH_TLS_INSECURE` 控制的客户端 |

第一条是本轮最重要的发现：它意味着**在本次验收之前，真实服务的对话功能从未真正工作过**，
而当时全部测试通过。这也说明"模块测试 + 隔离集成"两层通过，
仍不能推断真实链路可用。

## 四、已实测（本机运行，隔离替身）

| 项 | 证据 |
| --- | --- |
| 配置检查 | `scripts/check_config.py` 退出码 0，仅密钥/占位符警告 |
| 后端启动 | Live2D、ASR（sherpa SenseVoice）、TTS（edge_tts）、VAD（silero）均初始化成功 |
| 服务可访问 | `GET /` 返回 200；`/assets/main-FiZw243o.js` 与 CSS 均 200 |
| 协议协商 | `aemeath-hello` 返回 `aemeath-hello-ack`，`negotiated=2`、`accepted=True` |
| 课堂切换顺序 | 实测帧序 `aemeath-state` → `aemeath-clear-audio` → `full-text`，`voice_allowed=false` |
| 屏幕不可用原因 | 返回 `aemeath-screen-unavailable`，原因指明缺 `mss`/`pygetwindow` |
| 历史与记忆 | `create-new-history`、`fetch-history-list`、`aemeath-memory-list` 均走 SQLite |
| 数据库迁移 | 真实 `data/aemeath.sqlite3` 从 v1 迁到 v2，行数与内容保留 |
| 补丁可复现 | 干净 `v1.2.1` 检出按序套用 0001→0003 成功，7 个文件与工作副本逐字节一致 |
| 客户端可构建 | `npm run build:web` 成功；CSS 与 JS 均与部署产物 SHA256 相同；12 个协议消息均入包 |
| 后台任务启动 | 真实服务日志出现 `Aemeath background tasks started (2)` |
| 主动生成器装配 | 真实服务日志出现 `Aemeath proactive generator installed` |
| 信号不自启会话 | 对运行中服务发 `ai-speak-signal`，只回 `aemeath-proactive-decision`，未经过上游 conversation |
| 调度器自主发起 | 开启 proactive 后不发任何客户端信号，调度器自行生成并发出文字（因 API 为占位地址而报连接错误，属预期） |

## 五、真实 API 与设备层结果

详见 [acceptance-live.md](acceptance-live.md)。摘要：

### 已通过

- **真实对话模型往返**：DeepSeek `deepseek-flash`，浏览器实际驱动 10 轮中文对话
  **10/10 轮收到回复**；连续追问正确回忆前文，换话题未串回。
  客户端首段文字 P95 **1287 ms**（目标 ≤5 s，10 个有效样本；原始 JSONL 复算 1286.8 ms，见第九节）。
  **样本未达 20，该指标未完成验收**；原 README 的 518 ms 待核，不得引用。
- **真实记忆提取**：同一供应商，两句话提取 2 条事实且均带可定位证据片段；
  真实对话产生 11 个提取任务、写入 7 条记忆，全部 valid。
- **屏幕真机捕获**：`mss` + `pygetwindow` 已装，抓到前台窗口 1280×900 RGB 帧，
  切换窗口不串画面，连续抓帧不落盘。
- **真实视觉理解**（2026-09-12 补充）：EasyCLIProxyAPI 本机代理 +
  `gemini-3.8-flash-high`，两张测试图片均准确识别随机文字与图形颜色/形状；
  真实屏幕观察对网页/编辑器/文档各两次描述准确，切换窗口不串画面，
  reset 后拒绝迟到摘要，截图不落盘。

### 阻塞

- **嵌入服务已解决（T04 闭环）**：本机通过 LM Studio 加载 `text-embedding-bge-large-zh-v1.5`（1024 维中文向量，OpenAI 兼容端点 `http://127.0.0.1:1234/v1`），连通性与检索校准均通过。原嵌入阻塞已解除。

### 尚未验收（需人工或设备）

- **真实麦克风采集与语音链路**：VAD 加载成功，但没有真的对着话筒说话；
  10 轮语音、5 次打断、点击停止、课堂模式、重启保持、关麦停采均未做。
- **API ASR / API TTS**：EasyCLIProxyAPI 不提供音频转写与合成端点（已确认）。
  当前为本地 SenseVoice 与 edge-tts。计划要求确认选中了 API 引擎，这一点未达成。
- **屏幕视觉理解的观察项**：视觉理解已通过（见上）。仍待人工的是锁屏与
  窗口不可用分支的实际触发观察（`is_locked()` 可用但未实测）。
- **主动聊天与失败恢复**：自主发起、不抢话、课堂仅文字、
  断线重连不重复问候、失败后可继续，均未验收。
- **语音两项延迟**：停止说话到播放 10 秒、点击停止到停止播放 500 毫秒未测。
  首段文字项目前 10 个样本，未达计划要求的 20 个。
- **两次各 30 分钟人工试用**：未进行。

## 六、已知限制

- **遗忘依赖证据片段**。旧数据没有片段时先做确定性定位，
  定位不到则由记忆管理界面显示来源并要求选定片段，完成前不报告遗忘成功。
- 上游 JSON 历史**不自动导入也不自动读取**；
  需要时用 `aemeath-import-legacy-history` 做一次性幂等导入，
  源文件无法删除时会报告"迁移未完成"，不声称数据来源已统一。

## 七、环境问题与处理

| 问题 | 处理 |
| --- | --- |
| CLI 不继承 Windows 系统代理 | `scripts/env.ps1` 显式设置 `HTTP(S)_PROXY` |
| 上游 `uv.lock` 缺 `silero-vad` | 记入 `requirements.aemeath.txt` 并补装 |
| `silero-vad` 拉 `torchaudio 2.11.0` 与 `torch 2.6.0` ABI 冲突 | 固定 `torchaudio==2.6.0` |
| 上游 agent_factory 与配置 schema 不接受新 agent | 三个补丁，按序套用 |
| npm/pnpm 的 `.ps1` 被执行策略拦截 | 用 `& cmd /c "npm ..."` 调 `.cmd` 入口 |
| 上游 `frontend/` 只含预构建产物 | 另检客户端源码到 `vendor/Open-LLM-VTuber-Web`，构建后部署 |
| 无 ffmpeg（pydub 警告） | 未阻塞链路；如遇解码失败再安装 |

## 八、本轮修改的文件

| 位置 | 改动 |
| --- | --- |
| `aemeath/bridge.py` | 新增：上游与 Aemeath 的唯一桥接层 |
| `aemeath/legacy.py` | 新增：旧 JSON 历史一次性幂等导入 |
| `aemeath/config.py` | providers 配置、相似度下限、调度与提取间隔 |
| `aemeath/adapters.py` | `AdapterFactory`、`OpenAICompatibleVision`、`ExtractedFact.fragment` |
| `aemeath/memory.py` | schema v2 迁移、证据片段、精确遗忘、版本校验、检索下限 |
| `aemeath/agent.py` | 每轮读取情境、工作上下文改从 SQLite 恢复 |
| `aemeath/runtime.py` | 三态能力装配、桥接、后台任务、指标口径修正 |
| `aemeath/coordinator.py`、`situation.py`、`screen.py` | 调度器访问、状态版本、观察清空；取消任务可等待化 |
| `aemeath/bridge.py` | 主动生成器接口、`run_proactive`、一次性启动问候；取消可等待 |
| `tests/integration/` | 新增：生产入口集成测试与替身 |
| `docs/patches/0001`–`0003`、README | 补丁重新编号并登记顺序；本轮重新生成 0001 与 0003 |
| `scripts/check_migration.py`、`probe_protocol.py` | 新增：迁移与协议核查入口 |
| `scripts/gen_vision_test_images.py`、`check_vision_images.py`、`check_screen_auto.py` | 新增：视觉测试图片生成、生产视觉适配器验收、自动化屏幕理解验收 |
| `scripts/check_config.py` | 补上游根目录到 `sys.path`（修 `No module named 'src'`，回归测试覆盖） |
| `scripts/acceptance-env.ps1` | 视觉密钥注入、回环 NO_PROXY |
| `tests/test_check_config_import.py` | 新增：check_config 从仓库根可导入上游的回归测试 |
| `config/acceptance/conf.acceptance.yaml` | 视觉已启用（EasyCLIProxyAPI + `gemini-3.8-flash-high`） |
| 上游 `agent_factory.py` | 工厂创建 agent 时启动 runtime 后台任务 |
| 上游 `websocket_handler.py` | `ai-speak-signal` 改走调度器；连接后尝试一次性问候 |
| 上游 `service_context.py` | 装配主动生成器（复用同一 agent 与提示词） |

---

## 九、T00 证据复核与回归基线（2026-09-13）

本节是 [T00](https://github.com/Finderlzy/Aemeath/issues/1) 的产物：核实历史记录中口径冲突、
无法追溯原始资料的部分，并记录进入 T01–T03 修复前的真实基线。
**只做复核与基线记录**，不修改业务代码、不改验收判据，也不宣布任何功能通过。

### 9.1 复核方法

复核以**可追溯到磁盘或 git 的原始产物**为准，不以文档转述为准：

| 复核对象 | 原始证据 | 结论强度 |
| --- | --- | --- |
| 真实验收执行日期 | `logs/acceptance/turns.jsonl` 的 `logged_at`；`data/acceptance/screen-live/report-20260912-201558.json` | 确证 |
| 首段文字延迟 1287 ms | 同一 JSONL 的 10 条 `client_first_text_ms` / `first_text_ms` 复算 | 确证 |
| 518 ms | git 全历史检索 | **无法追溯 → 待核** |
| 244 项测试 | 在 `71376f0` 实跑 pytest | 确证 |
| “迟到摘要被拒”适用范围 | 阅读 `aemeath/bridge.py`、`aemeath/screen.py` 与对应测试 | 确证 |

检索方式：在**全部提交**的内容中搜索 `518`（`git grep 518 $(git rev-list --all)`），
命中仅有“来源待核”的提示行与实施计划中 T00 的任务描述，**没有任何一处是测量值**；
对已删除的原 README（`97ecf9a:README.md`）单独检索，`518` **零命中**。

### 9.2 冲突点逐项结论

**（1）执行日期 2026-09-14 vs 文档复核日期 —— 原记载不成立，实际为 2026-09-12。**

`logs/acceptance/turns.jsonl` 全部 10 条记录的 `logged_at` 落在
`2026-09-12T04:58:24Z` 至 `2026-09-12T04:59:07Z`；两个屏幕理解报告的文件时间与文件名
（`report-20260912-200444.json`、`report-20260912-201558.json`）同为 2026-09-12。
四项独立证据一致指向 2026-09-12，**2026-09-14 系笔误**。
本轮已将 `docs/acceptance-live.md` 的执行时间与相关标题改写为 2026-09-12。

**（2）首段文字延迟 518 ms vs 1287 ms —— 1287 ms 确证；518 ms 待核。**

以 `turns.jsonl` 的 10 条原始样本独立复算：

| 口径 | 样本数 | P50 | P95 | 最大 |
| --- | --- | --- | --- | --- |
| 客户端 `client_first_text_ms` | 10 | 1022.0 ms | **1286.8 ms** | 1286.8 ms |
| 后端 `first_text_ms` | 10 | 1023.0 ms | **1281.0 ms** | 1281.0 ms |

客户端 P95 = 1286.8 ms、后端 P95 = 1281.0 ms，与详细记录的 1287 / 1281 **完全吻合**，
1287 ms 口径**确证**。

**518 ms 无法追溯**：原 README 已随 `496ab34` 删除（229 行全部移除），
对该 README 的历史内容检索 `518` 为零命中；在全部提交内容中，`518` 也只出现在
“来源待核”提示与 T00 的任务描述里，**从未作为测量值存在**。
既无原始样本、也无测量区间可考，因此 518 ms **标为待核**：不能引用、不能反证，
**不得**用它宣称延迟验收通过。

差值的可能口径：文档另记有页面侧 `performance.now()` 观测 P95 **418 ms**
（提交 → DOM 首次变化），518 与 418 数值接近，可能属同类“最早变化”观测的早期版本；
但这只是推测，**无任何原始资料可证实**，故仍列为待核。

**（3）244 项测试 —— 与当前基线一致；启动手册的 224 项为过期数据。**

在基线提交 `71376f0` 实跑 `pytest` 得 **`244 passed, 7 deselected`**，
与本文记载一致，且**含 12 项真实链路回归**（`test_real_link_regressions.py` 实收 12 项）。
`224` 出现在两处，均为**过期数字**：

| 位置 | 原值 | 处理 |
| --- | --- | --- |
| `docs/runbook.md` “已验证事实” | 224 项（160 模块 + 64 生产入口） | **已更正为 244** |
| 本文第三节 | “224 项模块与集成测试全部无法发现” | 该处指**修复前**的测试规模，属历史事实，保留 |

即 224 是修复前的规模，244 是当前规模；两者不矛盾，但启动手册把它写成了当前事实，已修正。
另外，`tests/integration/test_calibrate_rule.py`（7 项）此前未登记，本次补入第二节表格。

**（4）“迟到摘要被拒”的适用范围 —— 只覆盖桥接层已测路径，不代表内部缓存也通过。**

读码确认，防止迟到写入的**代次校验位于 `aemeath/bridge.py:696-701`**：
`on_screen_request` 在 `await` 前记录 `_observation_generation`，`await` 后重新比对，
不一致则丢弃。而 `aemeath/screen.py` 的 `ScreenObserver.observe()` **自身没有代次校验**，
它在末尾无条件执行 `self._latest = observation`（`screen.py:314`）；
`reset()`（`screen.py:318-330`）只是把 `_latest` 置空。

因此：

- **已确证**：经过 `bridge.on_screen_request` 的路径，在关闭观察后返回的迟到摘要会被丢弃，
  且 `current_summary()` 为空——这正是 `test_vision_result_discarded_after_observation_disabled`
  与 `test_disabling_clears_current_summary` 覆盖的行为（后者经 `runtime.screen.current_summary()` 断言）。
- **未覆盖**：**绕过桥接、直接并发调用 `ScreenObserver.observe()`** 时，`reset()` 之后落地的
  结果仍会写回 `_latest`，内部缓存**没有**自我保护。原测试名易被读成“内部缓存也安全”，
  该测试的 docstring 亦自述其断言的是“代次在 await 后被重新检查”，即桥接层契约。

结论：历史“迟到摘要被拒”**仅能支持已测的桥接路径**，不能推断 `ScreenObserver` 内部缓存
在并发场景下安全。该缺口属 T02（屏幕观察关闭与迟到响应的生命周期）范围，本任务不修。

### 9.3 当前回归基线

修复前基线，仅供 T01–T03 对比使用，**不代表任何功能通过**。

| 项 | 值 |
| --- | --- |
| 复核日期 | 2026-09-13 |
| 代码基线 | `71376f0e5770391eeac2f74dbcbdb07f164b6fed`（`main`，clean tree） |
| 上游 | Open-LLM-VTuber v1.2.1（`3afa410`） |
| Python | 3.11.16（`vendor/Open-LLM-VTuber/.venv`） |
| 操作系统 | Windows |
| 命令 | `.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest` |
| 结果 | **244 passed, 7 deselected, 2 warnings in 17.13s**，退出码 0 |
| 失败项 | **无** |
| 排除项 | 7 项 `live_api`（需真实凭据，按 `pytest.ini` 默认排除，属预期） |
| 警告 | `audioop` DeprecationWarning、缺 ffmpeg 的 RuntimeWarning；均为环境既有，不影响结果 |

本轮**未**运行、也不在本任务范围内的层：`-m live_api` 真实供应商连通、语音与麦克风、
锁屏与窗口不可用分支、主动聊天人工观察、延迟样本补齐至 20、两次 30 分钟试用。
这些由 T04/T05/T06/T07 承接。

### 9.4 待核清单（缺原始证据，不编造）

| 条目 | 状态 | 缺口 |
| --- | --- | --- |
| 首段文字 518 ms | **待核** | 原 README 已删除且其历史内容中 `518` 零命中；来源、样本数、测量区间均不可考 |
| 原记载执行日期 2026-09-14 | **不成立** | 原始产物一致指向 2026-09-12，判定为笔误 |
| 518 与 418 ms 是否同源 | **待核** | 仅数值接近，无资料证实，不作结论 |
| “模块 160 / 生产 64”的旧拆分 | **已替换** | 现为 161 / 83（实收），旧拆分对应 224 项的历史规模 |
| 锁屏分支 `is_locked()` | **未实测** | 历史会话未锁屏，代码可用但无实测记录 |

### 9.5 复核未改动的内容

历史记录中仍有价值的部分**全部保留**：第三节与第五节的两批缺陷表、第四节校准结果、
第六节已知限制与第八节文件清单均未删改，仅校准了日期、项数与统计口径。
本次复核**未新增任何未经执行的数据**。

---

## 十、当前回归基线（GPT-SoVITS 接入后，2026-09-13）

本节记录 `task/gpt-sovits-tts-adapter` 分支合入前的实跑结果，用于替换第九节的
244 项作为**当前**基线。第九节 244 项仍是 T00 的历史记录，两者不矛盾。

### 10.1 实测数字

| 项 | 值 |
| --- | --- |
| 分支 / 提交 | `task/gpt-sovits-tts-adapter` / `45cf3fb` |
| `main` 对照 | `140368d`（T02 合并后） |
| 命令 | `.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest` |
| `main` 结果 | **288 passed, 7 deselected** |
| 分支结果 | **330 passed, 7 deselected, 2 warnings in 30.21s**，退出码 0 |
| 失败项 | **无** |
| 排除项 | 7 项 `live_api`（默认排除，属预期） |

### 10.2 新增 42 项的构成

新增两项集成测试文件，实测收集数如下（`pytest --collect-only`）：

| 文件 | 实收项数 | 覆盖 |
| --- | --- | --- |
| `tests/integration/test_gpt_sovits_engine.py` | **13** | 真实上游引擎对接本地 api_v2 替身；其中一项钉住上游默认值 `"ture"` |
| `tests/integration/test_gpt_sovits_tts.py` | **29** | `GPTSoVITSAdapter` 的参数构造、`streaming_mode` 规范化与拒绝非法值、请求契约、错误路径 |
| 合计 | **42** | — |

`288 + 42 = 330`，与实跑总数一致。集成测试单独运行为 **169 passed**。

> **口径更正**：`dccbc18` 的提交信息把 29 项误写为 13 项，`45cf3fb` 又把 13 项
> （`test_gpt_sovits_engine.py`）描述为本次新增的全部项数。两个数字实为**两个文件
> 各自**的项数，不是同一批。以 `--collect-only` 实收为准：13 + 29 = 42。

### 10.3 本节不代表的内容

- **不代表 T05 验收通过**：真实 GPT-SoVITS 服务未安装、未连通，听感未评价；
  合成播放、打断、课堂静音与延迟四项仍待真实设备验收。
- **不代表 ASR 验收通过**：ASR 目标未随本次变更，仍在 API 目标与既有阻塞记录下。
- 隔离测试不能替代真人设备验收。

## 十一、T03 屏幕驱动主动对话（2026-09-13）

本节记录 `task/T03-screen-driven-proactive` 的实跑结果。T03 修复
[架构复盘 A04](architecture.md#a04-的修复t032026-09-13接在-t01t02-之后)，
并在过程中发现一个更硬的缺陷。

### 11.1 实测数字

| 项 | 值 |
| --- | --- |
| 分支 / 提交 | `task/T03-screen-driven-proactive`（基于 `origin/main` `0471098`） |
| 命令 1 | `.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest` |
| 命令 2 | `.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest tests/integration` |
| `main` 对照（本分支起点实测） | **330 passed, 0 failed** |
| 分支结果（全量） | **347 passed, 0 failed, 0 error** |
| 分支结果（集成子集） | **186 passed, 0 failed** |
| 失败项 | **无** |
| 排除项 | 7 项 `live_api`（默认排除，属预期） |

`330 + 17 = 347`，与实跑总数一致。

### 11.2 新增 17 项的构成与驱动方式

全部在 `tests/integration/test_proactive_screen_driven.py`，经**真实链路**驱动：

```
AemeathRuntime._proactive_worker（真实定时器）
  → AemeathBridge.run_proactive（先问资格 → 按需观察 → 生成 → 发送前重检）
  → 上游 ServiceContext._install_aemeath_proactive_generator()（真实生成器入口）
  → AemeathAgent._build_messages()
  → 模型请求
```

只替换模型（`FakeLLM`）、TTS 引擎与捕获／视觉端。**全部 17 项都没有发送客户端
搭话或观察信号**，也没有绕过定时器直接调 `bridge.run_proactive` 之外的捷径；
"按需观察"因此是在真实调度下被验证的，而不是被测试手工促成的。

| 分组 | 项数 | 覆盖 |
| --- | --- | --- |
| 自动观察与引用内容 | 3 | 定时器单独触发观察；提示词含隔离窗口的**具体**摘要与来源窗口标题；不依赖手动观察帧 |
| 输出模式 | 2 | 普通模式产出可解码非空音频；课堂模式仅文字 |
| 生成前后有效性 | 6 | 开关关闭不抓屏不调用视觉；旧摘要不被引用；provider 自身守卫；观察器守卫；无来源窗口被拒；生成中用户输入丢弃候选 |
| 节流与不追问 | 2 | 相同画面不重复调用视觉；未回应后不再观察也不再搭话 |
| 切窗与保留行为 | 4 | 切窗不串画面；迟到视觉响应丢弃；回执计数唯一；用户轮次使候选退休 |

### 11.3 变异验证

三处守卫逐项改坏实现，确认测试真的会失败（结果见
[架构文档表格](architecture.md#a04-的修复t032026-09-13接在-t01t02-之后)）：

| 变异 | 捕获情况 |
| --- | --- |
| 去掉 provider 的观察开关检查 | 1 项失败 |
| 去掉来源窗口标题要求 | 1 项失败 |
| 去掉 `run_proactive` 的按需观察 | 5 项失败 |

前两次是**先变异、发现全绿、再补测试**才成立的：最初去掉开关检查时 17 项全绿，
因为 `set_switch("screen", False)` 同时清空了观察器缓存，测试无法区分是哪一层
在生效。补测只翻转情境状态并保留缓存，让 provider 自身的守卫成为唯一可能的原因。

### 11.4 修复过程中发现的更硬缺陷

`AemeathRuntime._proactive_worker()` 读取 `self.config.proactive.enabled`，
而 `ProactiveConfig` 没有该字段，第一轮循环即抛 `AttributeError`。
因此**上游主动定时器从来没有真正运行过**，"后端调度器是主动轮次的唯一来源"
在真实运行中并未成立。T01 的回归全部经 `bridge.run_proactive()` 直接驱动，
所以没有任何既有测试能暴露它——本任务的真实定时器驱动方式是发现它的前提。

### 11.5 本节不代表的内容

- **全部证据均为隔离窗口与替身**：没有真实桌面观感，没有真人体验。
- **锁屏与"窗口不可用"的自动分支未验证**（T06 承接）。
- **不代表 T05 验收通过**：未安装 GPT-SoVITS，未做真实播放与听感评价。
- 未修改默认 TTS 引擎，未新增语音后端，本节未涉及 T04–T07 范围。
- 集成测试脚手架的修正（runtime 单例注册、取消未执行的 `start()` 任务）
  属**测试基建**，用于让集成测试真的走生产接线，不是产品行为变更。


## 十二、当前开发进度验收（2026-09-13）

本节为最新汇总，较早章节保留为历史证据。结论：**核心实现与隔离回归通过，真实语音和课堂静音已有通过证据；首期整体验收仍未完成，尚不满足可复现交付条件。**

### 基线与本次实跑

- 基线：`0b1407c`，开始验收时主仓库工作树干净；vendor 后端和客户端均有本地改动，不受主仓库版本管理。
- 环境：Windows，Python 3.11.16，项目虚拟环境；前端 Vite 5.4.18。
- `vendor/Open-LLM-VTuber/.venv/Scripts/python.exe -m pytest`：**353 passed, 7 deselected**，39.87 秒。7 项真实 API 测试未执行；存在 audioop 弃用及 ffmpeg 未发现两项警告，不影响本次测试结果。
- `scripts/check_config.py`：退出码 0、6 项警告。默认配置仍含模型地址与名称占位符，当前进程未注入密钥，嵌入、提取和视觉未配置；配置检查通过不等于默认配置可以真实对话。
- 在 `vendor/Open-LLM-VTuber-Web` 执行 `npm.cmd run build:web` 成功。产物 `dist/web/assets/main-BrzaVU76.js` 与 `main-QEkl09-0.css` 和后端 frontend 中对应文件逐字节一致。未执行 Electron 打包。
- `scripts/check_gpt_sovits.py`：本机 9880 服务 OpenAPI HTTP 200，缺失参考音频请求返回预期 HTTP 400。本次只验证连通与错误契约，没有重新合成或播放。12393 当时未监听，未启动服务或重跑浏览器交互。
- 在临时干净的上游 `3afa41014b4548a0842e9ee2f576f4b164b48886` 中按顺序实际套用四个补丁，全部成功；逐文件比较时忽略 CRLF/LF 差异，发现 `tts_manager.py` 与工作副本不一致。临时副本已自动清理。

### 需求与证据

| 范围 | 当前判定 | 证据与剩余项 |
| --- | --- | --- |
| R01 文字与语音 | 核心链路已有真实通过记录 | `acceptance-live.md` 记录 SenseVoice → 模型 → GPT-SoVITS → 播放及口头打断恢复；本次未重演真人设备流程 |
| R02 课堂静音 | **通过（用户实测确认）** | 用户在本次验收明确确认“课堂静音通过了”；作为人工证据记录，不虚构轮次 ID、次数或延迟样本 |
| R03 长期记忆 | **通过（T04 闭环）** | 本地 LM Studio 接入 `text-embedding-bge-large-zh-v1.5`（1024 维），锁定下限 0.40；校准集完全分离，验收集重启后同义召回 10/10 PASS、无关过滤 9/10 PASS、事实纠正 5/5 PASS、精确遗忘 5/5 PASS、一句话两事实独立遗忘 1/1 PASS |
| R04 屏幕 | 部分通过 | 真实捕获与视觉有历史证据，生命周期隔离回归通过；锁屏、窗口不可用的真实分支仍待验收 |
| R05 自动主动交流 | 隔离通过，真实体验待验 | 定时器、屏幕上下文、音频和输出仲裁有回归；正式节奏、不抢话、不连续追问与失败恢复仍需真实完整流程 |
| R06 本地数据边界 | 既有实现与隔离回归通过 | 本次未引入云端存储；未执行全量数据流审计 |
| 延迟 | 全部达标且样本充足 | 复核本地 `logs/acceptance/turns.jsonl`：99 个总轮次、31 个取消轮次、0 个 error。三项指标样本数均 ≥20 且大幅达标：首段文字 46 样本（P50 1060.4 ms，P95 1578.0 ms ≤ 5s）；语音回复 23 样本（P50 1906.0 ms，P95 2453.0 ms ≤ 10s）；点击停止 22 样本（P50 12.5 ms，P95 12.5 ms ≤ 500ms） |
| 持续试用与交付 | 未完成 | 两次各 30 分钟人工试用无记录；干净环境启动与完整流程尚未复现 |

T05 语音操作（A1 正常语音对话、A2 播放中打断连续 5 次、A3 点击停止不再恢复、A4 播放中进课堂、A5 课堂持续静音、A6 重启保持、A7 关麦停止采集）与三项延迟采样均已闭环。

### 可复现性缺口与后续顺序

1. **补丁缺失最新修复**：工作副本 `tts_manager.py` 的全角引号等中文标点过滤改动及环境变量 key 解析未进入四个补丁。照启动手册重新安装将遗漏该修复。
2. **客户端改动未交付**：客户端有 8 个已跟踪文件修改及 2 个新增 Aemeath 文件，均位于主仓库忽略的 vendor 下；当前补丁目录只有四个后端补丁。当前构建通过不能证明新机器能取得同一客户端实现。
3. 默认配置、验收配置与本地依赖须明确区分；GPT-SoVITS 兼容修复和本地参考资源的安装复现仍需 T07 核对。
4. T04 真实长期记忆与 T05 语音与延迟指标已全部闭环；后续推进 T06 主动交流和失败恢复，再进行 T07 的复现交付与两次持续试用。

GitHub 状态本次未能实时核实：`gh` 不在 PATH，公开 REST 查询返回 403 rate limit exceeded。以上按代码和本地证据判断，不据此宣布 Issue 已关闭。本次未修改产品实现、部署、提交或推送。
