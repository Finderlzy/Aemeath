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

- **嵌入无可用供应商**：本机已接入的 DeepSeek 与 EasyCLIProxyAPI 均不提供
  `/v1/embeddings`（后者经路由源码确认无该端点）。因此**记忆检索质量
  无法用真实嵌入验收**。补齐只需改配置填 key，无需改代码。

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

- **检索下限 0.5 仍未获真实校准**。本轮新增了校准工具
  （`scripts/calibrate_memory.py`）与两个不重叠的数据集，
  但缺少可用的 API 嵌入供应商，因此"同义 ≥9/10 且无关 ≥9/10"尚无结论。
  工具会用本机可用的本地嵌入模型自检：实测 `nomic-embed-text-v1.5`
  **无法完成该检索任务**（同义最低 0.651 / 无关最高 0.848，区间重叠），
  工具正确报告"不可分离"而不是给出一个看起来能用的阈值。
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
