# Aemeath (爱弥斯)

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
- **本地语音支持**：
  - ASR：基于 sherpa-onnx 本地运行 SenseVoice（小巧、低延迟、多语言转写）。
  - TTS：基于本地 GPT-SoVITS HTTP 服务（`http://127.0.0.1:9880/tts`，api_v2）。

---

## 二、环境前置依赖

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 x64 |
| Python | 3.11.16（由 `uv python install 3.11` 安装） |
| 包管理工具 | `uv` (>= 0.12.0) |
| Node.js | v20+ (包含 npm) |
| 代理（国内） | 本机代理（默认 `http://127.0.0.1:7897`），访问 GitHub / PyPI 必需 |

---

## 三、快速开始与安装部署

### 1. 克隆与取得依赖源码

```powershell
# 克隆 Aemeath 仓库
git clone https://github.com/Finderlzy/Aemeath.git E:\WorkSpace\Aemeath
cd E:\WorkSpace\Aemeath

# 拉取上游后端底座与前端子模块
git -c http.proxy=http://127.0.0.1:7897 clone --depth 1 --branch v1.2.1 `
    https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git vendor/Open-LLM-VTuber
git -C vendor/Open-LLM-VTuber -c http.proxy=http://127.0.0.1:7897 submodule update --init --depth 1 frontend

# 拉取客户端源码
git -c http.proxy=http://127.0.0.1:7897 clone --depth 1 --branch main `
    https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web.git vendor/Open-LLM-VTuber-Web
```

### 2. 按序套用补丁

```powershell
# 1) 后端补丁（按序套用，缺一不可）
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

# 补装 Aemeath 专用及冲突依赖
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

# 部署构建产物到后端服务目录
Copy-Item dist\web\assets\* ..\Open-LLM-VTuber\frontend\assets\ -Force
Copy-Item dist\web\index.html ..\Open-LLM-VTuber\frontend\index.html -Force
cd ..\..
```

---

## 四、配置与检查

### 1. 环境变量配置
Aemeath 运行需要注入必要的模型 API 密钥（可配置于启动会话环境）：

```powershell
$env:AEMEATH_LLM_API_KEY = "sk-..."            # 对话大模型密钥
$env:AEMEATH_EXTRACTION_API_KEY = "sk-..."     # 记忆提取模型密钥
$env:AEMEATH_EMBEDDING_API_KEY = "lm-studio"   # 嵌入服务密钥（本地服务可填占位符）
$env:AEMEATH_VISION_API_KEY = "..."            # 视觉模型密钥
$env:AEMEATH_TLS_INSECURE = "1"                # 本地代理自签 CA 放宽（如需）
```

### 2. 运行配置检查
```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_config.py
```
若需要同时探测各真实供应商连通性，可附加 `--live` 参数：
```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_config.py --live
```

---

## 五、运行服务

```powershell
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\run_server.py
```

启动后在浏览器打开桌面客户端：
- 客户端访问地址：<http://127.0.0.1:12393/>
- 服务端 WebSocket 地址：`ws://127.0.0.1:12393/client-ws`

---

## 六、自动化测试与验收

项目配备完整的回归测试套件与集成测试：

```powershell
# 运行单元与集成测试（默认排除真实外部 API 调用）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest

# 运行真实外部连通性测试（需要真实凭据）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest -m live_api

# 运行主动交流节奏与故障恢复验收
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_proactive_flow.py

# 运行记忆校准集扫描
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\calibrate_memory.py --dataset calibration
```

---

## 七、目录结构说明

- `aemeath/`：Aemeath 核心扩展模块（coordinator、situation、memory、screen、proactive、bridge 等）。
- `config/`：配置定义与模板。
- `docs/`：项目规范、需求（`requirements.md`）、架构（`architecture.md`）、验收记录（`acceptance.md`）及补丁清单（`patches/`）。
- `scripts/`：环境检查、服务拉起、状态探针、测试驱动与校准工具。
- `tests/`：pytest 自动化测试套件。
- `vendor/`：第三方依赖底座（不入版本管理）。
