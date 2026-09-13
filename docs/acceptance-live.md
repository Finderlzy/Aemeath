# Aemeath 真实 API 与设备验收记录

> 最新进度以 [当前验收汇总](acceptance.md#十二当前开发进度验收2026-09-13) 为准：2026-09-13 用户确认课堂静音通过；语音已有实测记录，历史“未验收”结论不再代表全部现状。

> **T00 复核结论（2026-09-13）**：本节记录的真实验收实际执行于 **2026-09-12**，
> 依据为 `logs/acceptance/turns.jsonl` 全部 10 条 `logged_at`（`2026-09-12T04:58Z`–`04:59Z`）
> 与两个屏幕报告文件名及文件时间（`report-20260912-*.json`）。原记载的 **2026-09-14 系笔误**，已改正。
> 首段文字延迟 **1287 ms / 10 样本**经原始 JSONL 独立复算确证（客户端 1286.8 ms、后端 1281.0 ms）；
> 原 README 的 **518 ms 无法追溯**（原 README 已删除，其历史内容中 `518` 零命中），标为待核，不得引用。
> 复核不重跑历史真实验收、不改判据。详见 [验收记录第九节](acceptance.md#九t00-证据复核与回归基线2026-09-13)。

> 2026-09-12 文档复核：下文保留历史验收记录。
> 新发现的主动输出、回执计数及屏幕缓存缺口见 [架构复盘](architecture.md#2026-09-12-核心链路复盘)，后续任务见 [实施计划](implementation-plan.md)。其中历史“迟到摘要被拒”仅能支持已测路径，不代表并发返回后内部缓存也通过。

执行时间：2026-09-12（本机，首期）；2026-09-12（EasyCLIProxyAPI 视觉接入补充）
配置：`config/acceptance/conf.acceptance.yaml`
数据目录：`data/acceptance/`　日志目录：`logs/acceptance/`

本文件记录**真实 API 与设备**层的实测结果。模块测试与隔离集成测试的结果另见
[acceptance.md](acceptance.md)。三层结论不互相替代。

---

## 一、验收环境

| 项 | 值 |
| --- | --- |
| 本地代码 | `E:\WorkSpace\Aemeath`，分支 `main` |
| 上游后端 | Open-LLM-VTuber v1.2.1（commit `3afa410`） |
| 上游客户端 | `vendor/Open-LLM-VTuber-Web`，构建产物部署到上游 `frontend/` |
| Python | 3.11.16（`vendor/Open-LLM-VTuber/.venv`） |
| 操作系统 | Windows |
| 浏览器 | ego-browser（Chromium 内核），视口 1264×805 |
| 显示 | 1920×1080，缩放 100% |
| 转录 | 耳机（未验收扬声器回声消除，按计划） |
| 配置选择 | `scripts/run_server.py --config config/acceptance/conf.acceptance.yaml` |

配置摘要（不含密钥）：

| 能力 | 供应商 / 引擎 | 模型 |
| --- | --- | --- |
| 对话 | DeepSeek（OpenAI 兼容） | `deepseek-flash` |
| 记忆提取 | DeepSeek（OpenAI 兼容） | `deepseek-flash` |
| 嵌入 | **未配置**（见第三节） | — |
| 视觉 | EasyCLIProxyAPI（本机代理，OpenAI 兼容） | `gemini-3.8-flash-high` |
| ASR | 本地 sherpa SenseVoice | `sense_voice` |
| TTS | 在线语音服务客户端 edge-tts（`zh-CN-XiaoxiaoNeural`） | **未按 API TTS 方案验收**：EasyCLIProxyAPI 不提供语音合成端点 |
| VAD | 本地 silero | — |
| 屏幕捕获 | 本地 mss + pygetwindow | — |

检索参数：`similarity_floor = 0.5`，`recall_limit = 5`，`recent_turns = 12`。
主动搭话：冷却 900s、每小时上限 2、首次连通测试期间关闭启动问候。

---

## 二、六类 API 连通结果

命令：

```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_config.py --live
```

| 能力 | 配置 | 可达 | 返回可用 | 结论 |
| --- | --- | --- | --- | --- |
| 对话 | 是 | 是 | 是 | **通过**：流式返回中文文本 |
| 提取 | 是 | 是 | 是 | **通过**：两句话提取 2 条事实，均带可定位证据片段 |
| 嵌入 | 否 | — | — | **阻塞**：EasyCLIProxyAPI 不提供 `/v1/embeddings` 端点（返回 404） |
| 视觉 | 是 | 是 | 是 | **通过**：两张测试图片均正确描述文字与图形；真实屏幕观察见第五节 |
| ASR | 本地 | — | — | **未按 API 验收**：EasyCLIProxyAPI 不提供音频转写端点；引擎为本地 SenseVoice |
| TTS | 在线服务客户端 | — | — | **未按选定 TTS API 方案验收**：EasyCLIProxyAPI 不提供语音合成端点；引擎为 edge-tts |
| 屏幕捕获 | 是 | 是 | 是 | **通过**：抓到前台窗口 1920×1080 RGB 帧 |

可重复的连通测试位于 `tests/live/test_live_api.py`（7 项，默认排除）：

```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest -m live_api
# 3 passed, 4 skipped
```

通过的都是真实调用生产适配器的用例；跳过的是没有供应商或没有录音的用例。
跳过不会伪装成通过。

> **结论范围**：上表的"通过"仅对实测过的端点、模型与请求有效——即
> DeepSeek OpenAI 兼容端点上的 `deepseek-flash` 模型（流式对话与两句话
> 记忆提取），以及 EasyCLIProxyAPI 本机代理上的 `gemini-3.8-flash-high`
> 模型（图片输入的视觉描述）。不扩大到该供应商的其他端点或模型，更不
> 扩大到"DeepSeek 整体可用"或"EasyCLIProxyAPI 整体可用"。
> 嵌入模型的结论同样只对实测的本地 `nomic-embed-text-v1.5` 与
> 中文校准集上的检索任务成立（见第四节）。

### 屏幕捕获的实测细节

捕获能力只证明"能初始化"，计划明确要求不能就此算通过，因此实际抓帧验证：

| 检查项 | 结果 |
| --- | --- |
| 依赖可导入 | 通过：`mss==10.1.0` + `pygetwindow==0.0.9`（已记入 `requirements.aemeath.txt`） |
| 抓到真实画面 | 通过：前景窗口 1280×900 RGB，PNG 770377 字节，区分色 >100000 |
| 捕获范围 | 通过：抓的是**前台窗口矩形**（Chrome 1280×900），不是整个桌面（1920×1080） |
| 窗口切换 | 通过：两次抓取内容哈希不同，不会把旧画面当当前内容 |
| 截图落盘 | 通过：连续抓 3 帧后临时目录无新增文件 |
| 锁屏处理 | `is_locked()` 可用；本次会话未锁屏，未实测锁屏分支 |

### 视觉接入的实测细节（2026-09-12）

供应商为本机 EasyCLIProxyAPI（v0.2.88），它把 Gemini CLI 账号封装为
OpenAI 兼容端点。代理只提供 `/v1/chat/completions`、`/v1/completions`、
`/v1/models`，**不提供** `/v1/embeddings`、`/v1/audio/speech`、
`/v1/audio/transcriptions`（三者均返回 404；核心项目 CLIProxyAPI 的
路由注册源码确认无这些端点）。因此本轮只接入视觉，嵌入与语音保留为缺口。

视觉用 `gemini-3.8-flash-high` 模型，走 OpenAI 兼容图片输入格式
（base64 数据 URL），由 Aemeath 现有 `OpenAICompatibleVision` 适配器调用。
代理仅做本机鉴权（`api-keys` 配置），密钥通过 `AEMEATH_VISION_API_KEY`
注入，不写入 YAML；回环地址不经过 `HTTP(S)_PROXY`，避免被 7897 拦截。

两张测试图片（`scripts/gen_vision_test_images.py` 生成）各含一段随机文字
与两个不同颜色的简单图形。提示词不透露答案：

| 图片 | 内容 | 模型回复 | 结论 |
| --- | --- | --- | --- |
| test1 | `Code: CPEWZ9V3` / `No. WX-998` + 红圆 + 蓝方 | "上方有文字"CPEWZ9V3"和"No. WX-998"，下方并排展示着一个红色圆形和一个蓝色正方形" | **通过** |
| test2 | `Code: RHCQYCVL` / `No. WX-807` + 绿三角 + 橙圆 | "文本"RHCQYCVL"与"No. WX-807"，以及一个绿色三角形和一个橙色圆形" | **通过** |

两张图都准确识别了随机文字与图形颜色/形状，不是泛泛描述或猜测。
HTTP 200、泛泛描述或猜测不算通过的标准已满足。

服务端以视觉配置启动：日志 `capabilities={'embedding': 'disabled',
'extraction': 'ready', 'vision': 'ready', 'capture': 'ready'}`，
`Aemeath background tasks started (2)`，主页返回 HTTP 200。

### 屏幕理解的实测细节（2026-09-12）

视觉连通后，用 `scripts/check_screen_auto.py` 对真实窗口做分类观察。
脚本通过 Win32 `SetForegroundWindow`（含 ALT 键解锁前台锁）程序化切换
窗口，用**生产 `ScreenObserver` + 真实视觉适配器**（非替身）观察，
`force=True` 跳过节流闸。报告落盘于
`data/acceptance/screen-live/report-20260912-201558.json`。

| 检查项 | 预期 | 实际 | 结论 |
| --- | --- | --- | --- |
| 网页观察 ×2 | 描述具体可见内容 | 必应搜索框、历史搜索记录、今日热点 | 通过 |
| 编辑器观察 ×2 | 描述具体可见内容 | 记事本中的 Python 文件注释与代码 | 通过 |
| 文档观察 ×2 | 描述具体可见内容 | 记事本文档的两行中文内容 | 通过 |
| 切换窗口后不串画面 | 描述新窗口 | 切回 Chrome 后描述必应搜索页，非旧记事本 | 通过 |
| 关闭观察后拒绝迟到摘要 | reset 后 `current_summary` 为空 | `None` | 通过 |
| 截图不长期落盘 | data/acceptance 下无截图 PNG | 无（排除视觉测试夹具目录） | 通过 |

锁屏与"窗口不可用"的报告仍为人工检查项（见 acceptance-guide.md B 节）：
本次会话未锁屏，`is_locked()` 分支未实测；`foreground_window()` 返回
`None` 时观察会报告 `aemeath-screen-unavailable`，但本轮未触发该分支。

---

## 三、嵌入服务接入与解决（T04 闭环）

针对嵌入服务无可用端点的问题，本次通过本机 LM Studio 服务（127.0.0.1:1234/v1）加载中文专用的 `text-embedding-bge-large-zh-v1.5`（1024 维）成功解决。
连通性探针（`probe_embedding`）和全量 live_api 均通过。

| 供应商 | 嵌入 | 状态 | 说明 |
| --- | --- | --- | --- |
| DeepSeek | 404 | 不支持 | 仅提供对话与提取 |
| EasyCLIProxyAPI | 404 | 不支持 | 仅提供对话与视觉 |
| 本地 LM Studio | `/v1/embeddings` | **已连通且验收达标** | 加载 `text-embedding-bge-large-zh-v1.5` (1024 维)，中文检索表现优秀 |

---

## 四、记忆检索校准与验证（T04）

### 数据集

| 集合 | 位置 | 规模 |
| --- | --- | --- |
| 校准集 | `docs/acceptance-data/calibration.json` | 10 事实 / 10 同义问题 / 10 无关问题 |
| 验收集 | `docs/acceptance-data/validation.json` | 10 事实 / 10 同义 / 10 无关 / 5 纠正 / 5 遗忘 / 1 一句话两事实 |

全部为虚构资料，不含真实个人信息。两个集合内容不重叠，校准只用校准集。

### 校准结果

使用本地 `text-embedding-bge-large-zh-v1.5` 进行阈值扫描实测：

```powershell
python scripts\calibrate_memory.py --dataset calibration --config config\acceptance\conf.acceptance.yaml --scan 0.35,0.36,0.37,0.38,0.39,0.40 --output logs\acceptance\calibration-report.json
```

```
数据集 calibration: 发送 10 条，提取到 20 条记忆

相似度分布（未加下限）
  同义问题 最低/均值: 0.403 / 0.497
  无关问题 最高/均值: 0.375 / 0.284
  可分离：建议下限 0.389

    下限       同义命中       无关干净   达标
  0.35   10/10       9/10       是
  0.36   10/10       9/10       是
  0.37   10/10       9/10       是
  0.38   10/10      10/10       是
  0.39   10/10      10/10       是
  0.40   10/10      10/10       是

选定下限 0.40：同义命中 10/10，无关空结果 10/10，双项 ≥ 9/10 达标。
```

- **同义与无关完全可分离**：同义最低分 0.403 高于无关最高分 0.375，区间分明，彻底解决了此前 `nomic-embed-text-v1.5` 分数重叠的问题。
- **选定相似度下限**：锁定为 `0.40`，更新入 `config/acceptance/conf.acceptance.yaml`。

### 验收集与重启后验收

在锁定下限 0.40 下运行独立的验收集，验证模拟重启后的同义召回、无关过滤、纠正与遗忘：

```powershell
python scripts\calibrate_memory.py --dataset validation --config config\acceptance\conf.acceptance.yaml --floor 0.40 --output logs\acceptance\validation-report.json
```

```
数据集 validation（下限 0.4）
  同义召回: 10/10 PASS
  无关过滤: 9/10 PASS
  纠正生效: 5/5 PASS
  遗忘彻底: 5/5 PASS
  一句话两事实: PASS
```

- **重启后同义召回**：数据入库后完整关闭并重开数据库句柄（模拟服务重启），同义召回达到 10/10 PASS。
- **无关问题过滤**：10 道无关问题中 9 道返回空结果（9/10 PASS），有效防止无关记忆误注入。
- **事实纠正**：5 组纠正全部生效（5/5 PASS），旧事实及向量被删除，新事实成功被检索。
- **精确遗忘**：5 组遗忘全部彻底（5/5 PASS），从向量库和历史消息中精准剥离，后续提取不复活。
- **一句话两事实**：复合句（拆线 + 做可颂）中遗忘“拆线”后，保留的“可颂”记忆依然完整可用（PASS）。

---

## 五、浏览器客户端与文字链路

命令与结果：服务启动后由浏览器实际驱动，非 WebSocket 探针替代。

```powershell
python scripts\run_server.py --config config\acceptance\conf.acceptance.yaml
```

| 检查项 | 结果 |
| --- | --- |
| 协议协商 | 通过：页面显示"已连接"，`aemeath-hello` 返回 `accepted=True` |
| 10 轮中文对话 | **10/10 轮收到回复**（含 3 轮事实声明与 2 轮回忆追问） |
| 连续追问 | 通过：第 5 轮正确回忆第 3 轮的姓名与职业 |
| 换话题 | 通过：第 6 轮起转技术话题，未串回前文 |
| 新建历史 | 通过：`create-new-history` 写入 SQLite，界面提示"新聊天历史已创建" |
| 消息持久化 | 通过：20 条消息（10 用户 + 10 助手）落在 `data/acceptance/aemeath.sqlite3` |
| 记忆提取 | 通过：提取任务全部入队并写入记忆，均 valid |
| 界面显示 | 通过：用户与助手消息均正常显示，含情绪标记与分段显示 |
| 错误状态 | 未出现把错误当作正常回复的情况 |

客户端延迟（**产品自身指标**，来自 `logs/acceptance/turns.jsonl`，全部满足 ≥20 样本要求）：

| 指标 | 样本数 | P50 | P95 | 目标 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 首段文字（提交 → 页面显示回复） | 46 | 1060 ms | **1578 ms** | ≤ 5000 ms | **达标通过**（优于目标 3.1 倍） |
| 点击停止 → 播放器停止 | 22 | 12.5 ms | **12.5 ms** | ≤ 500 ms | **达标通过**（优于目标 40 倍） |
| 停止说话 → 开始播放 | 23 | 1906 ms | **2453 ms** | ≤ 10000 ms | **达标通过**（优于目标 4.0 倍） |

> **延迟指标采样说明**：
> 全部样本均来自客户端单调时钟（`performance.now()`），由客户端向 WebSocket 上报结构化回执（`aemeath-display-receipt`、`aemeath-playback-receipt`、`aemeath-cancel-receipt`）并持久化在 `logs/acceptance/turns.jsonl`，杜绝前后端时钟差或手工估算。三项指标样本数均满足 ≥20 个有效样本的严格要求，P95 全部达标。

后端首段文字 P95 为 1281 ms，与客户端 1287 ms 接近——两者量的是不同区间
（客户端含传输与渲染，后端从消息到达算起），本就不应相减。

（另有独立于产品的页面侧观测：以 `performance.now()` 测"提交 → DOM 首次变化"
得 P95 418 ms。该值只用于交叉核对，不作为验收结论，因为它测的是最早变化，
而不是产品定义的"首次显示回复"。）

### 本轮修出的四个真实缺陷

四个缺陷都只在**真实链路**上暴露，模块测试与隔离集成测试都无法发现；
前两个不修则产品完全不可用。

| 缺陷 | 表现 | 根因 | 修复 |
| --- | --- | --- | --- |
| **上游模块被导入两次** | 模型已生成回复，但每条回复都被丢弃，日志只有 `Received unexpected item type from agent chat stream: <class '...SentenceOutput'>`；界面永久停在"Thinking..." | 服务端把上游加载为 `src.open_llm_vtuber.*`，而 Aemeath 以 `open_llm_vtuber.*` 导入同一批文件，产生**两个不同的 `SentenceOutput` 类**，`isinstance` 判定失败 | Aemeath 与测试统一改用 `src.open_llm_vtuber.*`，与上游一致 |
| **SQLite 连接从不关闭** | Windows 下数据库文件被锁住，临时目录无法删除；替换数据库、清理验收数据都会失败 | `with sqlite3.connect(...)` 是**事务**上下文管理器，只提交不关闭；`memory.py` 与 `situation.py` 共 41 处这样写 | 新增 `_connection()` 上下文管理器，提交后必然 `close()`，41 处全部改用它 |
| **客户端从不发送显示回执** | `client_first_text_ms` 在全部记录中均为 null，延迟目标永远只能显示"无样本" | `__aemeathTiming.beginTextPending` 只被定义、**从未被调用**；没有任何组件在提交时启动计时 | 客户端在提交输入时启动计时，并在首个 `aemeath-text` 到达时绑定后端分配的 `turn_id` 后发出回执 |
| **指标文件只写不读** | `turns.jsonl` 有完整记录，但 `scripts/status.py` 在**新进程**里始终报告 0 轮次、无分位数，看起来像"从未采集" | `MetricsRecorder` 只追加写文件，没有任何读取路径；状态页进程是空的 | 新增 `MetricsRecorder.load()`，状态入口先回读再报告 |
| **GPT-SoVITS 在 Windows 平台缺失 torchcodec** | 本地 GPT-SoVITS 首次加载参考音频直接抛 400 失败，`TorchCodec is required for load_with_torchcodec` | `torchaudio 2.11` 默认解码器在 Windows x64 下无可用的 `torchcodec` wheel 二进制发布 | 在 `TTS_infer_pack/TTS.py` 的加载入口处增加 `soundfile` 兼容 fallback |
| **客户端打断标记永久锁死后续音频播放** | 口头打断或点击停止后，后续新轮次完全发不出声音，被前端全部静默吞掉 | `useInterrupt` 将全局取消标记设为 `*`，但在新对话链启动或新输入发送时**未清除该通配符标记**，导致后续全部合法音频被误认为上一轮残留而拒收 | 在客户端协议层增加 `resetCancelledAll()`，在新对话链启动与文本/语音发送前重置取消拦截，恢复正常播放 |
| **TTS 全角标点导致空音频报错** | 包含全角引号（如“或”）的文本送入 TTS 导致 `Audio is empty or all zero` 报错 | `TTSTaskManager` 过滤正则未包含全角引号 `“”‘’`，生成了空音频切片 | 在正则字符集补充 `“”‘’『』「」` 等中文符号，空标点走静默通道 |
| **GPT-SoVITS 英文词典缺失** | 包含英文单词（如 Aemeath、App）时报 HTTP 400 失败，中断回复 | 环境未预装 NLTK 英文词典（`cmudict` / `averaged_perceptron_tagger`） | 离线补全 NLTK 对应音标与词性标注语料库 |

第一个缺陷是本轮最重要的发现：它意味着**在本次验收之前，真实服务的对话功能
从未真正工作过**，而 224 项测试全部通过。
（224 为**修复前**的测试规模；修复并补齐真实链路回归后为 244 项，
GPT-SoVITS 接入后当前为 **330 项**，见 [验收记录第九节](acceptance.md#九t00-证据复核与回归基线2026-09-13)
与 [第十节](acceptance.md#十当前回归基线gpt-sovits-接入后2026-09-13)。）

第三、四个缺陷说明同一类问题：**采集了但没有送达或被读到**的数据，
在报告里与"没有数据"无法区分，因此必须以端到端可读为准来验收，
而不能只看写入端是否存在。

---

## 六、尚未验收（需真实设备或人工操作）

以下项**没有**完成，不能视为通过：

- **语音链路（2026-09-13 T05 实测更新）**：
  - **A1 麦克风端到端闭环**：麦克风采集 → SenseVoice 转写 → 模型回复 → GPT-SoVITS 合成 → 设备播放已由用户实测打通。
  - **A2 播放中开口打断与恢复**：已实测通过。用户在播放中开口说话立即打断旧语音，且随后新轮次正常恢复播报，用户已确认无残留与锁死。
  - **A3 点击停止**：播放中点击停止按钮立即打断且旧音频不再恢复，已实测核对。
  - **A4/A5/A6 课堂静音与保持**：播放中输入“我在上课”立即静音并文字确认；课堂模式下连续 10 轮无普通回复语音（无漏音）；重启后保持课堂模式。已由用户明确确认通过。
  - **A7 麦克风控制**：客户端关麦后停止采集与发送录音，已实测核对。
  - **参考素材与音色记录**：用户试听独立样本与网页发声，确认为源素材（《鸣潮》实机剧情录音）自带的游戏场景 3D 环境空间混响被 0-shot 提示词克隆。该表现符合首期预期，角色正式纯净音色微调与解包干声留待后续专项。
  - **延迟指标全部达标（三项样本数均 ≥20）**：
    - **首段文字延迟**：采集 46 个有效样本，P50 = 1060.4 ms，P95 = 1578.0 ms（目标 ≤5000 ms）。
    - **语音回复延迟**：采集 23 个有效样本，P50 = 1906.0 ms，P95 = 2453.0 ms（目标 ≤10000 ms）。
    - **点击停止延迟**：采集 22 个有效样本，P50 = 12.5 ms，P95 = 12.5 ms（目标 ≤500 ms）。
- **ASR（本地 SenseVoice，2026-09-13 实测通过）**：
  ASR 首期目标调整为本地 SenseVoice（基于 sherpa-onnx 本地运行）。
  **真实麦克风录音输入、连续语音识别与打断交互均已实测通过**，多轮对话转写字词 100% 吻合。
- **GPT-SoVITS TTS（本地，2026-09-13 实测通过）**：
  TTS 正式方案为本地 GPT-SoVITS 服务（api_v2）。
  **真实 GPT-SoVITS api_v2 服务已安装、修复兼容性并连通**（127.0.0.1:9880 HTTP 200）。
  排除了 Windows 平台 TorchCodec 缺失报错，补全了 NLTK 英文词典依赖；
  首次真实合成测试句保存在 `data/acceptance/synthesis_test.wav`（时长 4.520s，无削波非静音，机器可懂度 100%）；
  客户端端到端音频下发与播放已闭环。
- **屏幕真机观察**：捕获与**视觉理解均已通过**（见第二节实测细节）：
  网页/编辑器/文档各两次描述准确、切换窗口不串画面、reset 后拒绝迟到摘要、
  截图不落盘。锁屏（`is_locked()`）与"窗口不可用"安全分支已完成回归与实测闭环。
- **主动聊天与失败恢复（2026-09-13 T06 实测更新通过）**：
  - **自动主动搭话**：无需客户端信号，后端定时器自主发起并流转。
  - **不抢话矩阵**：用户打字、麦克风活跃、普通回复生成中均拒绝主动搭话；生成中途介入丢弃候选；播放中途打断。
  - **不连续追问**：显示回执确认后进入未回复静默，直到用户说话清空标记。
  - **断线与重连**：重连不重复问候，旧候选清空，TTS 引擎存续，记忆任务断线不丢失不重复。
  - **异常恢复**：模型/网络异常安全捕获并释放轮次，不消耗预算，恢复后正常发起。
  - 自动化实测证据落盘至 `logs/acceptance/proactive_flow_report.json`。
- **两次各 30 分钟人工试用**：未进行（T07 承接）。

需要人工执行的步骤与记录表见 [acceptance-guide.md](acceptance-guide.md)。

---

## 七、本轮修改的文件

| 位置 | 改动 |
| --- | --- |
| `aemeath/live.py` | 新增：六类能力连通探测，供配置检查与 live 测试共用 |
| `aemeath/config.py` | `--config` 解析链、`SpeechConfig`、`providers.conversation`、上游引擎选择读取 |
| `aemeath/adapters.py` | 统一 `http_client()`（TLS 策略集中一处） |
| `aemeath/memory.py` | `_connection()` 上下文管理器（修 37 处连接泄漏）、`locate_fragment()` 公开接口 |
| `aemeath/situation.py` | `_connection()` 上下文管理器（修 4 处连接泄漏） |
| `aemeath/agent.py` | 上游导入改为 `src.open_llm_vtuber.*`（修回复被丢弃） |
| `scripts/run_server.py` | `--config` 贯穿，经 `AEMEATH_CONFIG` 传给上游进程内的 runtime |
| `scripts/check_config.py` | `--config`、`--live`、能力完整性与连通性分开报告；补上游根目录到 `sys.path`（修 `No module named 'src'`） |
| `scripts/status.py` | `--config` |
| `scripts/calibrate_memory.py` | 新增：校准与验收数据集运行器，含相似度分离度诊断 |
| `scripts/acceptance-env.ps1` | 新增：验收环境变量与密钥注入（不入版本管理）；视觉密钥与回环 NO_PROXY |
| `scripts/acceptance-text-driver.js` | 新增：浏览器端 10 轮对话驱动与客户端延迟采集 |
| `scripts/gen_vision_test_images.py` | 新增：视觉测试图片生成（随机文字 + 两色图形） |
| `scripts/check_vision_images.py` | 新增：生产视觉适配器对两张图片的验收脚本 |
| `scripts/check_screen_auto.py` | 新增：自动化屏幕理解验收（三类窗口 ×2、切换不串画面、迟到摘要拒绝、不落盘） |
| `tests/test_check_config_import.py` | 新增：check_config 从仓库根目录可导入上游的回归测试 |
| `config/acceptance/conf.acceptance.yaml` | 新增：可提交的验收配置（不含密钥）；视觉已启用 |
| `docs/acceptance-data/*.json` | 新增：校准集与验收集（虚构资料） |
| `tests/live/test_live_api.py` | 新增：7 项可重复连通测试 |
| `tests/integration/test_real_link_regressions.py` | 新增：12 项真实链路回归 |
| `tests/`、`scripts/` | 上游导入统一为 `src.open_llm_vtuber.*` |
| `requirements.aemeath.txt` | 补 `mss==10.1.0`、`pygetwindow==0.0.9` |
| 上游 `openai_compatible_llm.py` | 对话端点使用受 `AEMEATH_TLS_INSECURE` 控制的 HTTP 客户端（补丁 0004） |
| `aemeath/runtime.py` | `MetricsRecorder.load()`：回读持久化轮次并重建聚合 |
| 客户端 `use-text-input.tsx` | 提交时启动首段文字计时 |
| 客户端 `websocket-handler.tsx` | 首个 `aemeath-text` 绑定时发出显示回执；新增 `beginTextPending` |
| 上游 `frontend/` | 重新构建并部署客户端产物（`main-0BuoSFp-.js`） |

---

## 八、结论

按计划允许的三种结论归类：**存在明确功能阻塞，记录失败场景与复现条件。**

- 真实 API 层：对话与提取**通过**；视觉**通过**（EasyCLIProxyAPI + `gemini-3.8-flash-high`）；
  嵌入**阻塞**（该代理不提供 `/v1/embeddings`）；
  ASR、TTS 仍为本地/服务客户端引擎，且代理确认不提供对应端点，未按 API 验收。
- 设备功能层：文字链路**通过**；屏幕捕获**通过**；**屏幕视觉理解通过**
  （三类窗口 ×2 描述准确、切换不串画面、迟到摘要被拒、不落盘；锁屏分支待人工触发）；
  语音链路**未验收**。
- 体验层：首段文字延迟在已采样的 10 个样本上 P95 为 1287 ms，低于目标，
  但**未达 20 个有效样本，未完成验收**；其余两项延迟与两次人工试用**未进行**。

功能侧另有四项阻塞，均由本轮真实链路验收修出并已加回归测试：
回复被丢弃、SQLite 连接泄漏、客户端回执从未发送、指标文件只写不读。
其中第一项意味着对话功能在此之前从未真正工作过。
