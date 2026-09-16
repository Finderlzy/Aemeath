# Aemeath 交付验收记录

更新：2026-09-16。本文是 [project-delivery](../AGENTS.md) 职责下的**整体验收与交付证据**，
与 [验收记录](acceptance.md)（分阶段实跑证据）、[启动手册](runbook.md)（可复现命令）
互补。本文只记录**本轮实际执行**的验证，区分通过、失败、未执行与不适用。

包含：
- 首期 v1.0.0 交付验收（第十二节以前历史记录）
- **v2 完整交付验收（第十二节，2026-09-16，Issue #20 / V2-T07）**

## 一、自动化回归与基线

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 全量回归 | `.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest` | **372 passed, 7 deselected**, 2 warnings (43.04s) |
| 真实连通（默认排除） | `... -m pytest -m live_api` | **7 passed** |

`live_api` 7 项覆盖 conversation、embedding、extraction、vision、ASR、TTS、capture
七个能力，全部经**生产入口**（配置 schema → `AdapterFactory` → 对话/记忆/屏幕实际调用的适配器）
调用真实供应商，不是自构造请求。

7 项默认排除来自 `pytest.ini` 的 `addopts = -m "not live_api"`，属预期。

## 二、真实能力连通（本次实跑）

`scripts/check_config.py --config config/acceptance/conf.acceptance.yaml --live`：

| 能力 | 结果 | 实测证据 |
| --- | --- | --- |
| conversation | 通过 | `deepseek-flash` 流式返回中文 |
| embedding | 通过 | `text-embedding-bge-large-zh-v1.5` dim=1024，related=0.392 / unrelated=0.184 |
| extraction | 通过 | 提取 2 条事实，2 条均有可定位证据片段 |
| vision | 通过 | 读回测试图内 token `AEMEATH-7391` |
| asr | 通过 | 本地 `sherpa_onnx_asr`（SenseVoice）转写成功 |
| tts | 通过 | 本地 GPT-SoVITS 合成 177964 bytes |
| capture | 通过 | 抓取前台窗口 1936x1048 RGB |

> 首次执行时 vision 返回 `RateLimitError`（上游配额冷却），等待冷却后复测通过。
> 该失败是外部供应商配额状态，非产品缺陷；已记录以免被误读为随机通过。

本地依赖服务（本次启动并验证）：LM Studio `127.0.0.1:1234`（已加载 bge-large-zh-v1.5）、
GPT-SoVITS `127.0.0.1:9880`（`/openapi.json` 200，GET /tts 契约校验 400 符合预期）。
`scripts/check_gpt_sovits.py` 退出码 0。

## 三、核心用户流程端到端（真实浏览器）

在真实 Web 客户端（`http://127.0.0.1:12393/`，协议版本 2，Chromium 引擎）走通：

| 环节 | 实际操作 | 实际结果 | 判定 |
| --- | --- | --- | --- |
| 文字对话 | 输入自我介绍 | 正确回应并记住称呼 | 通过 |
| 事实提取 | 输入「记住：每天早上一杯冰美式，晚上不喝咖啡」 | 提取入库；模型主动指出与既有「拿铁」记录冲突并以当前为准 | 通过 |
| 长期记忆召回 | 追问「我早上一般喝什么？」 | 回答「冰美式。你之前说的，我记的就是这个」 | 通过 |
| 课堂静音 | 输入「我在上课了」 | `mode=class`, `voice_allowed=false`，状态落 SQLite | 通过 |
| **服务重启保持** | 实际终止服务进程并重启 | 启动日志 `mode=class`，课堂状态从 SQLite 恢复 | 通过 |
| **重启后记忆召回** | 重连后追问「你还记得我叫什么名字吗？」 | 服务端日志 `AI response: [neutral] 知远，这个我确定记得。` | 通过 |
| 下课恢复 | 输入「下课了，恢复正常说话吧」 | `mode=normal`, `voice_allowed=true` | 通过 |

记忆质量（`scripts/calibrate_memory.py --dataset validation --floor 0.40`，退出码 0）：
同义召回 **10/10 PASS**、无关过滤 **9/10 PASS**、事实纠正 **5/5**、精确遗忘 **5/5**、
一句话两事实 **PASS**。与既有记录一致。

## 四、协议与集成

`scripts/probe_protocol.py` → **PROBE PASSED**：

- 协议协商 `accepted=True`、版本 2；
- 课堂切换帧序 `aemeath-state` → `aemeath-clear-audio` → `full-text`（顺序正确）；
- 记忆列表来自 SQLite（13 条）；
- 历史由 SQLite 提供（`create-new-history` / `fetch-history-list` 正常）。

一处告警不构成缺陷：probe 在**课堂模式**下发起屏幕请求故无响应——课堂态本就不采集屏幕，
属设计内行为。主动交流隔离回归 `scripts/check_proactive_flow.py` 10 项全通过
（含锁屏抑制、不抢话、不连续追问、断线重连不重复问候、失败恢复）；
该报告标注为 `isolated_test_with_doubles`，**不代表现场真机验收**。

## 五、安装与补丁可复现（干净临时目录）

在全新克隆中实际执行，不依赖本机工作副本：

| 项 | 结果 |
| --- | --- |
| 后端上游 | `v1.2.1` (commit `3afa410`) 重新克隆 |
| 后端四补丁 | `git apply --check` 全部通过；按 0001→0004 顺序套用成功 |
| 后端一致性 | **8 个改动文件 SHA256 与工作副本全部一致**（0 处差异） |
| 客户端上游 | commit `d176e7df2366952e3bacbf12cf9a8b18a4315932` |
| 客户端补丁 | `--check` 通过，套用成功 |
| 客户端一致性 | 9 个修改文件 + 2 个新增文件，**共 11 个全部 SHA256 一致** |

> 客户端文件数口径修正：此前文档记「10 个客户端改动文件」。实测补丁覆盖 **11 个**
> （9 修改 + 2 新增：`context/aemeath-context.tsx`、`services/aemeath-protocol.ts`）。
> 差异原因是用 `git diff --name-only` 统计时会漏掉新增未跟踪文件，已按补丁实际内容更正。

## 六、构建产物

| 项 | 结果 |
| --- | --- |
| `npm.cmd run build:web` | 成功，`main-BRZRBU4Q.js` (1907112 B) + `main-QEkl09-0.css` (41060 B) |
| 部署一致性 | `dist/web` 与 `vendor/Open-LLM-VTuber/frontend` 三个文件 SHA256 全部一致 |
| 客户端加载 | 重新构建并清理后，真实浏览器重新加载正常，`#root` 有内容、协议连接成功 |

**本次修复的交付缺陷**：部署目录 `frontend/assets/` 累积了 2 个历史 hash 的旧 JS
（`main-BL6esL-s.js`、`main-BrzaVU76.js`），而 `index.html` 只引用 `main-BRZRBU4Q.js`。
两者均为未跟踪的构建残留，已删除，使部署目录只保留一套与实际入口一致的产物。
`runbook.md` 早已提示需删旧 hash，本次为实际执行补齐。

## 七、文档核对

对 README 与手册逐条实际执行关键命令，不采信描述：

| 文档 | 核对项 | 结果 |
| --- | --- | --- |
| `README.md` | pytest / pytest -m live_api / check_proactive_flow / audit_latency_metrics / monitor_acceptance_progress | 全部实际可运行，命令与路径正确 |
| `README.md` | 环境依赖、补丁数量（4 后端 + 1 前端）、配置字段表、本地服务准备流程 | 与实际一致 |
| `docs/runbook.md` | 测试计数 | **原记 330 项已过期**，更正为 372 项 |
| `docs/runbook.md` | 文末「尚未验证」四项 | **已过期**（对话 API、麦克风、真机捕获、延迟目标均已补验），已更正 |
| `docs/acceptance-guide.md` | 记忆校准命令 `--floor` | **原记 0.42 与实际锁定值 0.40 不符**，已更正 |

## 八、验收矩阵

| 需求 | 场景 | 验证方式 | 结果 |
| --- | --- | --- | --- |
| R01 对话与桌面 | 文字对话、真实模型往返 | 真实浏览器 + live_api | 通过 |
| R02 课堂 | 静音、零漏音、重启保持、下课恢复 | 真实浏览器 + 重启实测 | 通过 |
| R03 本地记忆 | 提取、召回、纠正、遗忘、重启保持 | SQLite 直查 + 验收集 + 重启实测 | 通过 |
| R04 屏幕观察 | 真机捕获、视觉理解、关闭态边界 | live probe + 隔离回归 | 通过 |
| R05 主动交流 | 节奏、防重、不抢话、失败恢复 | 隔离回归（10 项） | 通过（**隔离**） |
| R06 本地数据边界 | SQLite 本地存储、不引入云端记忆 | 代码与配置核对 | 通过 |
| E01 建议能力 | 打断、纠正、精确遗忘 | 验收集 5/5 + 5/5 | 通过 |

## 九、未解决项（明确未通过/未执行）

以下项目**未经真机验证**，不计入通过；沿用用户 2026-09-13 确认的延期决定：

| 项 | 状态 | 说明 |
| --- | --- | --- |
| #7 现场锁屏采录 | **未执行** | 仅隔离回归覆盖锁屏抑制分支 |
| #8 两次 30 分钟真人试用 | **未执行** | 用户决定上线后按实际反馈跟进 |
| 语音回复延迟 ≥20 样本 | **未达样本量** | 当前 13 样本，P95 4754.8ms；用户已确认按现有样本收口 |
| 点击停止延迟 ≥20 样本 | **未达样本量** | 当前 8 样本，P95 0.8ms；同上 |
| 麦克风真人语音全链路 | **未复验** | 本轮为文字输入；语音链路沿用 T05 记录 |
| 正式角色音色与听感 | **不在本次范围** | 参考音频暂缺，首期不做音色验收 |

延期的项目不等于通过。本文不补造试用记录，也不把隔离测试写成现场验收。

## 十、发布结论

**首期交付物已就绪并通过集成、用户流程与构建产物验收。** 自动化、真实能力连通、
核心流程端到端、干净环境补丁复现、构建部署与文档一致性均已实际执行并留证。

上述验收阶段**未执行部署、未打标签、未正式发行、未更改 GitHub Issue 状态**。
`#7`/`#8` 保留为上线后跟进事项。

## 十一、首个正式版本

用户于 2026-09-13 授权正式发布。首个正式源码版本定为 **v1.0.0**，
沿用 `aemeath/__init__.py` 的 `__version__` 作为版本字段（原开发值为 `0.1.0`）。
该版本将已验收的首期接口与配置作为兼容性基线，后续按 SemVer 维护。
功能验收基线为 `4e2cd0b8937849590de3a9ef64c1969cc5b8f44c`；
发布提交仅更新版本字段和发布文档，复用上文验收证据，不声称在发布阶段重跑验收。

发行载体为 [GitHub Release v1.0.0](https://github.com/Finderlzy/Aemeath/releases/tag/v1.0.0)，
提供 Git 标签对应的源码归档；安装按 README 取得固定上游、套用补丁并本机构建。
源码归档不含 `vendor/`、本地模型、运行凭据、聊天数据和 Windows 安装器。
第九节的延期、未达样本量与范围限制继续有效，Issue #7/#8 保留跟进。
实际发布状态以 GitHub Release 页面为准。

---

## 十二、v2 完整交付验收（2026-09-16，Issue #20 / V2-T07）

**任务**：[#20](https://github.com/Finderlzy/Aemeath/issues/20)（V2-T07，V2-M5 完整交付验收）。
**基线**：远端 `main` 的 `080b587`，任务分支 `issue-20-v2-delivery-acceptance`。
**性质**：按 `project-delivery` 做 v2 整体验收，不新增业务功能。前置依赖 #15、#16、#17、#19 均已关闭并在 main 分支集成。

### 1. 全量回归基线

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 全量回归 | `.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest` | **634 passed, 7 deselected**, 2 warnings (80.66s) |
| 表达学习样例与一致性 | `... -m pytest tests/integration/test_learning_samples.py tests/integration/test_learning_consistency.py` | **43 passed** (18 样例 + 25 一致性) |
| 训练向导与任务状态机 | `... -m pytest tests/integration/test_training_wizard.py tests/integration/test_training_jobs.py` | **51 passed** |

634 项基线包含 v1 首期核心、管理 API 全端点、声音预设、桌面几何/托盘/字幕、表达与黑话学习、训练接入与声音训练向导全部回归，无任何测试失败。

### 2. 真实能力与业务链路回归

- **双窗口与桌面角色**：独立管理窗口与角色桌宠窗口完全解耦（`isManagementWindow` 在最前切流，管理窗口不开麦、不播音、不广播）；`scripts/probe_desktop_v2.py` 实测桌面角色窗口 `(1478,370,430x650)` 贴合屏幕工作区右下角，配置尺寸保存与重载实时生效。
- **声音训练向导**：`scripts/probe_training_wizard.py` 实测预检通过（上游单卡 DDP 补丁已生效）；重启核对 A（进程退出有产物→succeeded）、B（进程退出无产物→failed）、C（进程存活→running）全绿；停止范围探针通过（训练进程精准终止，用户既有 9880 服务全程存活 HTTP 200）。**严谨口径重申**：18.48s 示例素材流程通过，音色质量未达标，不得冒称正式爱弥斯音色。
- **表达与黑话学习**：独立样例集 18 用例（核心人设保护、自身复述误拒修复、用户引入新表达与黑话提取）与 25 用例一致性全绿；运行时提示词与动态注入实测有效。
- **首期能力 R01–R06 与 E01**：文本对话、打断（`aemeath-clear-audio`）、课堂静音、服务重启保持、记忆召回、屏幕观察生命周期与本地 SQLite 边界全部稳态保持。

### 3. 数据迁移与备份恢复验证

在临时副本数据库上模拟旧版本 v1 (schema_version=1) 模式，经当前 `MemoryStore` 启动：
- 自动触发平滑模式迁移 `1 -> 2`；
- `messages` 自动补齐 `conversation_id`, `revision` 列；
- `memories`, `embeddings`, `pending_extraction` 自动补齐 `revision` 列；
- `memory_evidence` 自动补齐 `fragment` 列；
- **全表行数统计前后完全相等，数据零丢失**；
- 覆盖还原升级前备份文件即可完全回滚至旧版本模式，回退方案具备确定性。

### 4. 干净目录可复现安装、补丁与构建

在全新隔离目录中实际拉取上游固定版本源码，验证补丁按序套用：
- 后端 5 个补丁（`0001`–`0005`）按序 `git apply --check` 全部 0 退出码，套用成功；
- 前端 5 个补丁（`web/0001`–`web/0005`）按序 `git apply --check` 全部 0 退出码，套用成功；
- 修复了 `0001-register-aemeath-agent.patch` 行尾与格式差异问题，消除反向比对误报。

### 5. 文档与状态一致性核验

- `README.md` 与 `docs/runbook.md` 补丁数量已更新为 5 后端 + 5 前端，测试基线更新为 634 项；
- `docs/implementation-plan.md` 状态已同步核准（#14–#19 closed，#20 交付验收完成）；
- 密钥扫描脚本 `scripts/check-secrets.ps1` 扫描全部版本管理文件 100% 通过。

### 6. 未解决项说明

- **#7 现场锁屏采录** 与 **#8 两次 30 分钟真人试用**：继续保留 2026-09-13 用户确认的延期上线后跟踪状态，不计入通过，不阻塞 v2 交付。
- **正式爱弥斯音色**：保持非目标与限制说明，当前仅跑通训练向导流程与示例音色，正式音色待素材到位后另行训练。

