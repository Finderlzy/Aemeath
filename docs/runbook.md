# 启动、停止与配置检查

更新：2026-09-12。本文记录**实际执行并验证通过**的命令。未验证的步骤不写在这里。

## 前置条件

| 项 | 实测值 |
| --- | --- |
| Python | 3.11.16（由 `uv python install 3.11` 安装） |
| 包管理 | uv 0.12.12 |
| 上游 | `vendor/Open-LLM-VTuber` @ v1.2.1（commit `3afa410`） |
| 客户端源码 | `vendor/Open-LLM-VTuber-Web` @ `d176e7d`（不在版本管理内） |
| Node | v24.9.0，npm 11.6.0 |
| 本机代理 | `http://127.0.0.1:7897`（访问 GitHub/PyPI/npm 必需） |

上游 `pyproject.toml` 声明 `>=3.10,<3.13`，本项目固定 3.11。

## 一次性准备

```powershell
# 1. 取得固定版本的上游源码与前端子模块
cd E:\WorkSpace\Aemeath
git -c http.proxy=http://127.0.0.1:7897 clone --depth 1 --branch v1.2.1 `
    https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git vendor/Open-LLM-VTuber
git -C vendor/Open-LLM-VTuber -c http.proxy=http://127.0.0.1:7897 `
    submodule update --init --depth 1 frontend

# 1b. 取得客户端源码（frontend/ 子模块只有预构建产物，改客户端必须要有源码）
git -c http.proxy=http://127.0.0.1:7897 clone --depth 1 --branch main `
    https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web.git vendor/Open-LLM-VTuber-Web

# 2. 按序套用四个补丁（顺序不能变，见 docs/patches/README.md）
cd vendor/Open-LLM-VTuber
git apply ..\..\docs\patches\0001-register-aemeath-agent.patch
git apply ..\..\docs\patches\0002-register-aemeath-agent-config.patch
git apply ..\..\docs\patches\0003-bridge-aemeath-runtime.patch
git apply ..\..\docs\patches\0004-fix-tls-for-conversation-endpoint.patch

# 3. 安装依赖
$env:HTTP_PROXY='http://127.0.0.1:7897'; $env:HTTPS_PROXY=$env:HTTP_PROXY
uv sync --frozen --python 3.11

# 4. 补装上游 lock 缺失或冲突的依赖（实测必需，见下）
uv pip install -r ..\..\requirements.aemeath.txt
uv pip install "torchaudio==2.6.0" `
    --index-url https://download.pytorch.org/whl/cpu `
    --extra-index-url https://pypi.org/simple
```

`requirements.aemeath.txt` 记录了各包固定版本与补装原因。

### 为什么需要第 4 步

上游 `src/open_llm_vtuber/vad/silero.py` 导入 `silero_vad`，但 `silero-vad`
不在 `uv.lock` 里，直接启动会失败：

```
Failed to initialize server context: No module named 'silero_vad'
```

且 `uv pip install silero-vad` 默认会拉 `torchaudio 2.11.0`，与锁定的
`torch 2.6.0` ABI 不兼容，导入时报 `OSError: [WinError 127]`。
必须显式降到 `torchaudio==2.6.0`。

### 屏幕捕获依赖（可选，屏幕观察真机验收前需要）

```powershell
$env:HTTP_PROXY='http://127.0.0.1:7897'; $env:HTTPS_PROXY=$env:HTTP_PROXY
uv pip install mss pygetwindow
```

未安装时后端照常启动，但 `capture` 能力为 `error` 并给出具体原因；
客户端请求截图会收到 `aemeath-screen-unavailable` 与原因。

## 构建与部署客户端

上游 `frontend/` 是 `build` 分支的**预构建产物**，不含源码。
改客户端要改 `vendor/Open-LLM-VTuber-Web` 源码再构建部署：

```powershell
cd E:\WorkSpace\Aemeath\vendor\Open-LLM-VTuber-Web
$env:HTTP_PROXY='http://127.0.0.1:7897'; $env:HTTPS_PROXY=$env:HTTP_PROXY
$env:ELECTRON_SKIP_BINARY_DOWNLOAD='1'   # 只做 web 构建，不必拉 Electron 二进制

& cmd /c "npm ci --no-audit --no-fund"   # .ps1 被执行策略拦截，走 .cmd 入口
& cmd /c "npm run build:web"

# 部署到上游读取的位置
Copy-Item dist\web\assets\* ..\Open-LLM-VTuber\frontend\assets\ -Force
Copy-Item dist\web\index.html ..\Open-LLM-VTuber\frontend\index.html -Force
# 删掉旧 hash 的 js，避免累积
```

实测：源码构建出的 CSS 与预构建产物**字节相同**（`main-QEkl09-0.css`），
JS 仅 hash 不同，说明源码与产物对应。

`npx tsc --noEmit` 有 586 个预存在错误，主要在 vendored 的 Live2D WebSDK，
与本项目改动无关。判断改动是否引入错误时只筛自己改的文件：

```powershell
& cmd /c "npx tsc --noEmit -p tsconfig.web.json --composite false 2>&1" |
    Select-String "aemeath|websocket-handler|use-audio-task|use-interrupt"
```

构建走 vite + swc，不受这些 tsc 错误影响。

## 配置

- 权威配置：`config/conf.aemeath.yaml`（纳入版本管理，不含密钥）
- `scripts/run_server.py` 启动时把它复制为上游读取的 `vendor/Open-LLM-VTuber/conf.yaml`
- 密钥用 `${VAR}` 形式引用环境变量，由上游 `read_yaml` 展开

必需的环境变量：

| 变量 | 用途 |
| --- | --- |
| `AEMEATH_LLM_API_KEY` | 对话模型密钥 |
| `AEMEATH_TTS_API_KEY` | 可选，改用 API TTS 时需要 |
| `AEMEATH_EMBEDDING_API_KEY` | 记忆嵌入（配置 `providers.embedding` 后需要） |
| `AEMEATH_EXTRACTION_API_KEY` | 记忆提取（配置 `providers.extraction` 后需要） |
| `AEMEATH_VISION_API_KEY` | 屏幕理解（配置 `providers.vision` 后需要） |

Aemeath 扩展配置写在 `character_config.aemeath_config` 下（上游会忽略未知段落）：
`data_dir`、`log_dir`、`memory`、`proactive`、`screen`、`providers`。
`providers.*` 只保存 `base_url`、`model` 与**密钥所在环境变量名**，不存密钥本身。

能力状态分三种，配置错误不会静默降级为 `None` 后仍报告 ready：
`ready` / `disabled`（未配置或用户关闭）/ `error`（已配置但初始化失败）。
`scripts/status.py --json` 的 `capabilities` 段可以看到每一项及其原因。

## 配置检查（不启动服务）

```powershell
cd E:\WorkSpace\Aemeath
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_config.py
```

检查 Python 版本、上游与前端资源、依赖导入、配置 schema、监听地址、密钥与数据目录。
**致命问题退出码 1 并列出具体项；未配置密钥只算警告**，便于选定供应商前先跑通。

## 数据库迁移检查

```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_migration.py
```

对**真实** `data/aemeath.sqlite3` 打开一次并打印迁移前后的版本、行数与列，
确认既有数据保留。升级 schema 后应跑一次。

## 启动

```powershell
cd E:\WorkSpace\Aemeath
$env: HTTP_PROXY / HTTPS_PROXY = 'http://127.0.0.1:7897'
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\run_server.py
```

或先 `. .\scripts\env.ps1` 再启动，它会设好代理与路径。

启动后：

- 桌面客户端：<http://127.0.0.1:12393/>
- WebSocket：`ws://127.0.0.1:12393/client-ws`

仅监听 `127.0.0.1`，不对外暴露。

首次启动会自动下载 SenseVoice ASR 模型（约 999 MB）到
`vendor/Open-LLM-VTuber/models/`，需要几分钟；之后启动无需再下。

## 停止

服务在前台运行，`Ctrl+C` 即可。启动脚本用 `atexit` 注册了缓存清理，
退出不会遗留本次启动的服务进程。

若确需强制结束：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*run_server.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## 协议自检（服务运行中）

```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\probe_protocol.py
```

连接 `/client-ws`，依次验证：协议协商、课堂切换的帧序
（`aemeath-state` → `aemeath-clear-audio` → `full-text`）、
屏幕请求的原因回报、记忆与历史来自 SQLite。全部通过打印 `PROBE PASSED`。

客户端协议版本为 2。未协商到该版本的客户端**不会**被当作完整 Aemeath 客户端运行
（`aemeath-hello-ack` 返回 `accepted: false`）。

`ai-speak-signal` 对 Aemeath 只做资格检查：后端返回 `aemeath-proactive-decision`，
不会启动 conversation。主动轮次的唯一来源是后端调度器，客户端不能绕过它。

## 状态与记忆管理

```powershell
cd E:\WorkSpace\Aemeath
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\status.py
```

查看当前交流模式、状态版本、各项开关、记忆条数、能力状态与指标。
未配置单价时只显示用量，不猜费用。

延迟指标分两段显示，不能混用：

- `backend.*`：本进程单调时钟测得的生成阶段耗时；
- `client.*`：客户端回上报的体验耗时（提交到显示、停止说话到播放、点击停止到停止）。

样本不足 20 个时显示 `sufficient: false`；没有回执时显示
`available: false` 与 `p95: null`，**不显示为 0**。

其他子命令：

| 命令 | 作用 |
| --- | --- |
| `--list` | 列出全部有效记忆 |
| `--search 咖啡` | 按关键词搜索记忆 |
| `--forget <id前缀>` | 删除一条记忆（连同向量、来源与待办任务） |
| `--mode class` | 切换到课堂模式 |
| `--mode normal` | 恢复普通模式 |
| `--json` | 以 JSON 输出状态，便于脚本处理 |
| `--process-pending` | 处理待办的记忆提取任务 |

> Windows 控制台默认 GBK 代码页会显示中文乱码。需要正常显示时先执行
> `chcp 65001`，或直接用 `--json` 读取。

### 精确遗忘

删除一条记忆时只移除它对应的**原文片段**，同一条消息里的无关内容保留。
若该记忆没有可定位的证据片段（旧数据常见），会先做确定性定位；
定位不到则**不报告遗忘成功**，而是返回需要用户选定的来源片段。

旧的上游 JSON 历史不自动导入也不自动读取。需要一次性导入时：

```json
{ "type": "aemeath-import-legacy-history" }
```

按源文件标识幂等，导入并核对后删除原文件；
原文件无法删除时会报告"迁移未完成"，不会声称数据来源已统一。

## 测试

```powershell
cd E:\WorkSpace\Aemeath
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest
```

默认不运行真实 API 测试；需要时用 `-m live_api`。

`tests/` 下是模块单元测试，`tests/integration/` 下是**生产入口**集成测试：
后者经过真实配置 schema、`AgentFactory`、桥接层、上游输出处理与客户端协议，
只替换模型、音频设备与捕获端。新增修复应优先在 `tests/integration/` 留回归测试。

竞态用 `tests/integration/harness.py` 的 `Barrier` 精确控制发生点，不要依赖 `sleep`。

> **在受限沙箱/容器中运行**：`tests/test_phase1_conversation.py` 用
> `tempfile.mkdtemp()` 建库，部分沙箱会拒绝在新创建的临时目录里写 SQLite，
> 表现为大量 `sqlite3.OperationalError: unable to open database file`。
> 把 `TEMP`/`TMP` 指向工作区内可写目录即可，这不是产品缺陷。

## 日志

- 上游运行日志：`vendor/Open-LLM-VTuber/logs/debug_<date>.log`
- Aemeath 自有日志目录：`logs/`（已排除出版本管理）
- 轮次指标：`logs/turns.jsonl`（只在产生轮次后创建）

这些目录由运行时按需生成，不在版本管理中；收尾清理后下次启动会重新建立。

## 已验证事实

2026-09-12 实测结果：

- `scripts/check_config.py` 通过（仅密钥未配置等预期警告）
- 服务启动成功，`GET /` 返回 200；构建后的客户端 JS/CSS 均 200
- Live2D、TTS（edge_tts）、ASR（sherpa_onnx SenseVoice）、VAD（silero）均初始化成功
- Agent 初始化为 `aemeath_agent`，桥接已接到会话上下文
- `probe_protocol.py` 全部通过：协议协商 `accepted=True`；
  课堂切换帧序为 `aemeath-state` → `aemeath-clear-audio` → `full-text`
- 真实数据库 v1→v2 迁移成功，数据保留
- 干净 `v1.2.1` 检出按序套用 0001→0003 成功，7 个文件与工作副本逐字节一致
- 客户端 `npm run build:web` 成功，产物与部署文件 SHA256 相同
- `pytest` 224 项通过（160 模块 + 64 生产入口）
- 真实服务启动后后台任务确实运行（日志 `Aemeath background tasks started (2)`），
  主动调度器可在无客户端信号时自行发起一轮

**尚未验证**：真实对话模型 API 的往返质量、麦克风实际采集、屏幕真机捕获、延迟目标。
这些需要填入真实 API 配置并进行人工验收，详见 [验收记录](acceptance.md)。
