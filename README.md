# Aemeath (爱弥斯)

**v2.1 Live2D 制作计划**与 v2 并行推进：角色立绘已定稿，分层、绑定和正式桌面接入尚未完成。参见 [实施计划](docs/implementation-plan.md#v21-live2d-并行制作计划2026-09-14) 与 [制作契约及定稿](docs/live2d-production.md)。正式模型未完成不阻塞 v2。

首个正式源码版本：**v1.0.0**。下载与已知限制见 [GitHub Release](https://github.com/Finderlzy/Aemeath/releases/tag/v1.0.0)，验收证据见 [交付记录](docs/delivery.md)。版本字段以 `aemeath/__init__.py` 的 `__version__` 为准，发布标签使用 `vMAJOR.MINOR.PATCH`；后续按 SemVer 维护首期接口与配置兼容性。

Windows 桌面 AI 伙伴，角色原型为《鸣潮》爱弥斯。首期以 **Open-LLM-VTuber v1.2.1** 为运行底座，通过有序补丁与统一桥接层（`AemeathBridge`）深度扩展，支持本地事实与经历记忆（SQLite + 向量检索）、屏幕观察理解、主动交流节奏调度、情境状态感知（课堂静音）、本地 SenseVoice 语音识别与本地 GPT-SoVITS 语音合成。

---

## 一、架构概览

```
桌面浏览器/Web客户端  <---(WebSocket /client-ws)--->  Open-LLM-VTuber (v1.2.1)
                                                                 │
                                                       AemeathBridge 桥接层
                                                                 │
                                       ┌─────────────────────────┼─────────────────────────┐
                                       ▼                         ▼                         ▼
                               AemeathCoordinator         AemeathSituation           AemeathMemory
                                 (轮次/取消/闸门)           (课堂/静音/情境)        (SQLite + 向量检索)
                                       │                         │                         │
                                       ▼                         ▼                         ▼
                               AemeathProactive            ScreenObserver            Local Models
                               (主动搭话/冷却)          (屏幕捕获/视觉理解)    (SenseVoice / GPT-SoVITS)
```

- **统一桥接与仲裁**：所有进出站交互经由 `aemeath/coordinator.py` 和 `aemeath/bridge.py`，确保打断、课堂静音与时序闸门安全。
- **本地数据边界**：记忆、情境、历史全部存储在本地 SQLite 数据库（`data/aemeath.sqlite3`），不依赖外部云记忆服务。
- **本地语音与模型支持**：
  - ASR：基于 sherpa-onnx 本地运行 SenseVoice（离线、低延迟）。
  - TTS：基于本地 GPT-SoVITS HTTP 服务（`http://127.0.0.1:9880/tts`，api_v2）。
  - Embedding：基于本地 LM Studio 运行 `text-embedding-bge-large-zh-v1.5`（1024 维中文向量），锁定相似度阈值 `0.40`。

---

## 二、环境前置依赖

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 x64 |
| Python | 3.11.16（建议通过 `uv python install 3.11` 安装） |
| 包管理工具 | `uv` (>= 0.12.0) |
| Node.js | v20+ (包含 npm) |
| 代理（国内） | 本机代理（默认 `http://127.0.0.1:7897`），访问 GitHub / PyPI 必需 |

---

## 三、快速开始与安装部署

### 1. 克隆与取得依赖源码

```powershell
# 1) 克隆 Aemeath 仓库
git clone https://github.com/Finderlzy/Aemeath.git E:\WorkSpace\Aemeath
cd E:\WorkSpace\Aemeath
git checkout v1.0.0

# 2) 拉取上游后端底座与前端子模块（固定 v1.2.1 / commit 3afa410）
git -c http.proxy=http://127.0.0.1:7897 clone --depth 1 --branch v1.2.1 `
    https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git vendor/Open-LLM-VTuber
git -C vendor/Open-LLM-VTuber -c http.proxy=http://127.0.0.1:7897 submodule update --init --depth 1 frontend

# 3) 拉取客户端源码并固定到已验证提交 d176e7df2366952e3bacbf12cf9a8b18a4315932
git -c http.proxy=http://127.0.0.1:7897 clone `
    https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web.git vendor/Open-LLM-VTuber-Web
git -C vendor/Open-LLM-VTuber-Web checkout d176e7df2366952e3bacbf12cf9a8b18a4315932
```

### 2. 按序套用补丁 (4 后端 + 1 前端)

```powershell
# 1) 后端四个补丁（必须按顺序套用）
cd vendor/Open-LLM-VTuber
git apply ..\..\docs\patches\0001-register-aemeath-agent.patch
git apply ..\..\docs\patches\0002-register-aemeath-agent-config.patch
git apply ..\..\docs\patches\0003-bridge-aemeath-runtime.patch
git apply ..\..\docs\patches\0004-fix-tls-for-conversation-endpoint.patch
cd ..\..

# 2) 客户端补丁
cd vendor/Open-LLM-VTuber-Web
git apply ..\..\docs\patches\web\0001-aemeath-web-client.patch
cd ..\..
```

### 3. 安装 Python 依赖

```powershell
cd vendor/Open-LLM-VTuber
$env:HTTP_PROXY='http://127.0.0.1:7897'; $env:HTTPS_PROXY=$env:HTTP_PROXY
uv sync --frozen --python 3.11

# 补装 Aemeath 专用及兼容依赖
uv pip install -r ..\..\requirements.aemeath.txt
uv pip install "torchaudio==2.6.0" `
    --index-url https://download.pytorch.org/whl/cpu `
    --extra-index-url https://pypi.org/simple
cd ..\..
```

### 4. 构建与部署客户端

```powershell
cd vendor/Open-LLM-VTuber-Web
$env:HTTP_PROXY='http://127.0.0.1:7897'; $env:HTTPS_PROXY=$env:HTTP_PROXY
$env:ELECTRON_SKIP_BINARY_DOWNLOAD='1'
cmd.exe /c "npm.cmd ci --no-audit --no-fund"
cmd.exe /c "npm.cmd run build:web"

# 部署构建产物到后端静态托管目录
Copy-Item dist\web\assets\* ..\Open-LLM-VTuber\frontend\assets\ -Force
Copy-Item dist\web\index.html ..\Open-LLM-VTuber\frontend\index.html -Force
cd ..\..
```

### 5. 构建桌面程序（角色常驻桌面时使用）

上面的步骤只产出 **web 客户端**（浏览器访问）。要让角色以桌宠形态常驻 Windows 桌面，
改用：

```powershell
cd E:\WorkSpace\Aemeath
powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1
# 产物：vendor\Open-LLM-VTuber-Web\release\win-unpacked\Aemeath.exe
```

先启动后端，再运行 `Aemeath.exe`。角色窗口直接出现在所在显示器的**右下角**
（距屏幕边缘与任务栏各 12 px），托盘图标左键恢复角色、右键出菜单
（管理界面／显示角色／隐藏角色／停止发言／退出）。管理界面是**独立窗口**，
关闭它不影响角色继续对话。

> 该脚本会同时构建并部署管理页。**不要用 `npm run build:win`**：它在本机因需要为
> macOS dylib 创建符号链接而失败（缺少开发者模式或管理员权限），该步骤只服务于
> 代码签名。原因、自检项与验收命令见 [启动手册](docs/runbook.md)。

---

## 四、外部与本地模型服务准备

### 1. 本地 SenseVoice (ASR) 准备
SenseVoice 模型随上游 ASR 目录加载，模型放置在：
`vendor/Open-LLM-VTuber/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/`。
可运行麦克风识别验证脚本：
```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\verify_mic_sensevoice.py
```

### 2. 本地 GPT-SoVITS (TTS) 准备、启动与停止
Aemeath 首期正式语音方案为本地 GPT-SoVITS（`http://127.0.0.1:9880/tts`，api_v2 契约）。
- **准备模型与参考音频**：在本地部署好的 GPT-SoVITS 目录下放置爱弥斯角色模型权重与参考音频。
- **启动服务**：使用提供的启动脚本（自动执行 `/openapi.json` 健康探针，超时返回失败退出码 1）：
  ```powershell
  powershell -File scripts\start_gpt_sovits.ps1 -Port 9880 -ServiceDir "E:\WorkSpace\Tools\GPT-SoVITS"
  ```
- **检查连通性**：
  ```powershell
  .\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_gpt_sovits.py
  ```
- **停止服务**：
  ```powershell
  powershell -File scripts\stop_gpt_sovits.ps1 -Port 9880
  ```

### 3. 本地 LM Studio (Embedding) 准备
- 打开 LM Studio，搜索并下载 `text-embedding-bge-large-zh-v1.5`。
- 在“Local Server”选项卡中选择该模型，端口设为 `1234`，点击“Start Server”（监听 `http://127.0.0.1:1234/v1`）。

---

## 五、配置体系说明：默认模板 vs 实际运行配置

**特别注意**：
- `config_templates/conf.default.yaml` 与 `conf.default.yaml` 为**默认模板**，仅提供架构字段结构及上游默认 fallback。其中的 `api.example.invalid` 和 `replace-me` 是未配置的占位符，直接使用该配置无法连通外部模型。
- **实际运行配置**应使用 `conf.yaml` 或专用验收配置（如 `config/acceptance/conf.acceptance.yaml`）。

### 1. 实际配置关键字段对应表

| 模块 | 配置项路径 | 推荐实际取值 / 说明 | 对应环境变量 |
| --- | --- | --- | --- |
| 交互 Agent | `agent.agent_type` | `aemeath_agent` | - |
| 聊天模型 (LLM) | `agent.llm.provider` | `openai_compatible_llm` | - |
| 聊天端点 | `agent.llm.base_url` | 如 `http://127.0.0.1:8317/v1` | - |
| 聊天模型名 | `agent.llm.model` | 如 `deepseek-v4.1-flash` | - |
| 聊天密钥 | `agent.llm.api_key` | 可配置或置空读取环境变量 | `$env:AEMEATH_LLM_API_KEY` |
| 记忆提取模型 | `aemeath.extraction.base_url` | 如 `http://127.0.0.1:8317/v1` | - |
| 记忆提取模型名 | `aemeath.extraction.model` | 如 `deepseek-v4.1-flash` | - |
| 记忆提取密钥 | `aemeath.extraction.api_key_env` | `AEMEATH_EXTRACTION_API_KEY` | `$env:AEMEATH_EXTRACTION_API_KEY` |
| 本地嵌入端点 | `aemeath.embedding.base_url` | `http://127.0.0.1:1234/v1` | - |
| 本地嵌入模型名 | `aemeath.embedding.model` | `text-embedding-bge-large-zh-v1.5` | - |
| 嵌入服务密钥 | `aemeath.embedding.api_key_env` | `AEMEATH_EMBEDDING_API_KEY` | `$env:AEMEATH_EMBEDDING_API_KEY = "lm-studio"` |
| 视觉模型端点 | `aemeath.vision.base_url` | 视觉供应商 API 端点 | - |
| 视觉模型名 | `aemeath.vision.model` | 如 `claude-3-5-sonnet` 或 `gpt-4o` | - |
| 视觉模型密钥 | `aemeath.vision.api_key_env` | `AEMEATH_VISION_API_KEY` | `$env:AEMEATH_VISION_API_KEY` |
| 本地 ASR | `asr_model` | `sherpa_onnx_asr` | - |
| 本地 TTS | `tts_model` | `gpt_sovits_tts` | - |

> 对话、记忆提取与视觉可以共用同一个 OpenAI 兼容端点。当前实际配置三者都指向
> 本机 EasyCLIProxyAPI（`http://127.0.0.1:8317/v1`）：对话与提取用
> `deepseek-v4.1-flash`，视觉用 `gemini-3.8-flash-high`。该端点只监听回环，
> 因此 `NO_PROXY` 必须包含 `127.0.0.1`，否则回环流量会被送进外部代理而失败。

### 2. 注入环境变量与配置健康检查

```powershell
# 注入实际模型凭据
$env:AEMEATH_LLM_API_KEY = "..."          # 对话端点密钥
$env:AEMEATH_EXTRACTION_API_KEY = "..."   # 记忆提取端点密钥
$env:AEMEATH_EMBEDDING_API_KEY = "lm-studio"
$env:AEMEATH_VISION_API_KEY = "..."
$env:AEMEATH_TLS_INSECURE = "1" # 如使用代理自签证书需开启
$env:NO_PROXY = "127.0.0.1,localhost" # 回环端点必须绕过代理

# 运行配置静态与端点健康探针
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_config.py --live
```

---

## 六、启动服务与使用

```powershell
# 启动后端服务
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\run_server.py
```

若使用验收配置，需显式传入（否则读取上游 `conf.yaml`）：

```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\run_server.py `
    --config config\acceptance\conf.acceptance.yaml
```

在浏览器打开客户端：
- 客户端访问地址：<http://127.0.0.1:12393/>
- 服务端 WebSocket 地址：`ws://127.0.0.1:12393/client-ws`

### 管理界面

服务运行中打开管理页（`?page=manage` 不可省略；不带参数打开的是角色页）：

```
http://127.0.0.1:12393/?page=manage
```

导航共六项：概览、模型、人设、声音、记忆、Live2D 与桌面。界面截图见
[docs/images](docs/images/)，完整说明见 [启动手册 · 管理界面](docs/runbook.md#管理界面v2-t01)。

- **模型／人设／概览**：读写权威配置 `config/conf.aemeath.yaml`，页面区分「已保存」与
  「已生效」；凭据只存本机，接口不回显。
- **声音**：示例与自定义预设卡片，可试听与应用。应用需要本地 GPT-SoVITS 服务在运行，
  失败保留原音色。**当前没有爱弥斯的正式音色，列出的是示例音色。**
- **记忆**：搜索、来源展示、纠正与精确遗忘。遗忘只移除目标片段，同一条消息里的其他
  内容保留；若无法定位片段会要求你选定，不会谎称已遗忘。恢复备份前会提示可能恢复
  已遗忘的内容。
- **Live2D**：选择已安装模型并调整比例与位置；没有模型时页面给出明确说明，仍可完成
  配置，聊天、语音与字幕不受影响。

管理页与角色页是互斥的两个窗口：管理页不建立对话连接、不开麦克风、不播放音频。

---

## 七、自动化测试与验收套件

```powershell
# 1. 运行全量单元测试与集成测试（默认排除真实外部连通）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest

# 2. 运行真实外部连通性测试（需要配置真实服务与模型环境变量）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest -m live_api

# 3. 运行隔离环境主动交流节奏测试（报告标注为 isolated_test_with_doubles，不代表现场验收）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_proactive_flow.py

# 4. 延迟采样审计与现场采录进度（口径见 docs/acceptance-guide.md D 节）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\audit_latency_metrics.py
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\monitor_acceptance_progress.py
```

服务运行中可跑管理与记忆的端到端探针：

```powershell
# 管理 API 全端点可达性、错误码与凭据不回显
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\probe_management_v2.py

# 记忆生命周期：写入 → 纠正 → 遗忘 → 重开数据库
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\probe_memory_lifecycle.py

# 抓取管理界面六个页面的截图到 docs/images/
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\capture_manage_pages.py
```
