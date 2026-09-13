# Aemeath

住在 Windows 桌面里的 AI 伙伴。

Aemeath 希望能陪你聊天、看懂你正在做的事，在相处中逐渐了解你，并根据情境决定什么时候开口、什么时候安静下来。

角色原型为《鸣潮》中的爱弥斯。项目支持简单默认人设与可替换的角色预设，独立于“大肥鱼”，不继承其角色、账号或记忆。

## 当前状态

**桌面运行链路已接通，隔离集成验证通过；真实 API 与设备验收部分完成，
视觉已通过 EasyCLIProxyAPI 接入，嵌入阻塞，语音与人工试用尚未进行。**

本轮已完成：对话、情境、记忆、屏幕观察和主动调度通过桥接层接入统一运行链路；
课堂静音与打断已接入客户端音频控制，真人听感尚未验收；聊天历史与本地记忆统一到 SQLite；
删除竞态与共享来源误删已修复；客户端回执已接入。
后台任务在真实服务中启动，主动聊天由后端调度器发起，客户端信号只请求资格检查。

真实链路验收已通过的部分：DeepSeek 对话与记忆提取真实往返；
浏览器实际驱动 10 轮中文对话全部收到回复，首段文字 P95 **1287 ms**（详细验收记录口径，10 个样本，未达 20 个样本要求）；
屏幕真机捕获可用（前台窗口取景、切换不串画面、不落盘）；
EasyCLIProxyAPI + `gemini-3.8-flash-high` 视觉接入，测试图片与真实屏幕观察均通过。

本层修出三处只在此层暴露的缺陷，其中一个使对话功能此前从未真正工作过：
上游模块被以两个名字导入两次，产生两个 `SentenceOutput` 类，模型生成的回复
在 `isinstance` 处被全部丢弃；SQLite 连接从不关闭，在 Windows 上锁住数据库文件；
对话端点忽略 TLS 设置而记忆提取不受影响，容易被误判成配置问题。

仍未完成：**嵌入在本机已接入的供应商均不提供 `/v1/embeddings`**
（DeepSeek 与 EasyCLIProxyAPI 均无该端点），记忆检索质量无法用真实嵌入验收；
EasyCLIProxyAPI 同样不提供 ASR/TTS 端点，当前仍是本地 SenseVoice 与 edge-tts，
未按 API 验收；麦克风、语音打断、课堂模式、锁屏分支、
主动聊天、失败恢复与两次人工试用均未进行。

验证层级必须区分，不能互相替代：

- **模块测试**：验证单个模块逻辑（替身模型）。
- **生产入口集成测试**：经过真实配置 schema、`AgentFactory`、桥接层、
  上游输出处理与客户端协议，只替换模型、音频设备与捕获端。
- **真实 API 与设备验收**：真实模型往返、麦克风、屏幕捕获、延迟与人工试用。
- **延迟与持续使用**：客户端单调时钟采样的 P95 与人工试用结论。

前两层已通过（244 项）。第三层部分通过并存在明确阻塞，第四层首段文字仅有初步数据，样本不足，尚不能判为验收达标。
不得把模块或替身测试写成真实场景验收结果，也不得据此声称"首期功能全部通过"。

详见 [验收记录](docs/acceptance.md)、[真实 API 与设备验收](docs/acceptance-live.md)
与 [验收操作指南](docs/acceptance-guide.md)。

## 首期目标

首期围绕一条完整交互链路展开：

> 启动桌面角色 → 打字或戴耳机直接说话 → 她回应你 → 结合屏幕接着聊 → 重启后仍记得你告诉她的事情。

具体包括：

- **自然聊天**：支持文字和语音输入，可打断回复。
- **情境交流**：告诉她“我在上课”后，后续交流改为文字。
- **本地记忆**：记录事实与经历，支持检索、纠正和遗忘。
- **屏幕感知**：按需观察当前活动，在关闭观察后停止处理。
- **主动搭话**：结合话题和情境开口，遵守冷却与不回应暂停规则。

首期使用默认角色素材。正式爱弥斯形象、作息与卧室动画、吃药提醒、表达与黑话学习、电脑操作以及 QQ/微信互动属于后续方向。

## 技术与数据

底座采用 [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)：

- 上游版本：`v1.2.1`
- 固定 commit：`3afa41014b4548a0842e9ee2f576f4b164b48886`
- Python：`3.11`
- 依赖管理：`uv`
- 本地存储：SQLite
- 首期语义检索：本地向量与 NumPy

Aemeath 自有模块位于 `aemeath/`，通过上游补丁接入。补丁及应用方式见 [补丁说明](docs/patches/README.md)。

模型接口按能力配置，可替换供应商。目标方案使用 API 完成对话、视觉及记忆处理；当前启动验证使用过本地 SenseVoice ASR 与 edge-tts，不代表全部语音能力已经迁移或验收。

**记忆保存在用户电脑。** 聊天历史、事实、经历、索引和备份不使用云端记忆托管或自动云同步。调用模型 API 时仍会发送必要输入，提供商的数据留存取决于其服务设置。

聊天历史已统一到 SQLite：Aemeath 对话不再写入上游 JSON 历史，也不再自动读取它。
旧 JSON 历史提供一次性幂等导入入口；源文件无法删除时会报告"迁移未完成"，
不声称数据来源已统一。

删除一条记忆只移除对应的原文片段，同一条消息里的无关内容保留。
若该记忆没有可定位的证据片段（旧数据常见），会先做确定性定位；
定位不到则不报告遗忘成功，而是要求用户选定片段。

设计要求主动搭话由后端统一调度，客户端信号不能绕过仲裁。当前主动输出、回执计数和屏幕上下文接入仍有实现缺口，见 [架构复盘](docs/architecture.md#2026-09-12-核心链路复盘)。

客户端源码位于 `vendor/Open-LLM-VTuber-Web/`（固定 `d176e7d`，不入版本管理）。
上游 `frontend/` 只含预构建产物，改客户端需改源码后构建部署，见 [启动手册](docs/runbook.md)。

## 开发运行

当前面向开发验证，尚未提供完整安装包。

先按 [启动手册](docs/runbook.md) 准备固定版本上游、补丁、Python 环境与补充依赖。上游目录位于 `vendor/Open-LLM-VTuber/`，不随本项目源码提交。

配置入口为 `config/conf.aemeath.yaml`。按实际供应商填写服务地址和模型名称，密钥通过环境变量提供，不提交到源码。

需要独立验收环境（独立配置、数据库与日志，不与个人记忆混用）时使用
`config/acceptance/conf.acceptance.yaml`，各入口都接受 `--config`：

```powershell
# 检查配置，不启动服务
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_config.py

# 检查配置并真实调用每个已配置的供应商
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_config.py --live

# 启动后端（验收配置）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\run_server.py --config config\acceptance\conf.acceptance.yaml
```

`--config` 贯穿两层：复制给上游的 YAML，以及服务进程内 Aemeath runtime 读取的路径。
只改一层会让上游引擎与 Aemeath 模块读到不同配置。验收环境准备与人工验收清单见
[验收操作指南](docs/acceptance-guide.md)。

服务默认地址：

- 本地页面：http://127.0.0.1:12393/
- WebSocket：`ws://127.0.0.1:12393/client-ws`

前台运行时使用 `Ctrl+C` 停止。

配置检查成功不代表真实 API 可用；页面可以访问也不代表完整桌面交互已验收。代理按本机网络需要设置，不将开发机代理地址视为通用要求。

## 测试与诊断

```powershell
# 运行自动化测试（默认排除需要真实凭据的 live_api）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest

# 运行真实供应商连通测试
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe -m pytest -m live_api

# 查看本地状态（验收数据要带 --config）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\status.py

# 记忆检索校准（在校准集上扫描下限）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\calibrate_memory.py --dataset calibration

# 记忆检索验收（锁定下限后，在验收集上运行一次）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\calibrate_memory.py --dataset validation --floor 0.42
```

测试结果应区分模块替身测试、生产入口集成测试和真实设备验收。
`tests/` 是模块测试，`tests/integration/` 是生产入口集成测试，
`tests/live/` 是需要真实凭据的连通测试。
集成测试经过真实配置 schema、`AgentFactory`、桥接层、上游输出处理与客户端协议，
只替换模型、音频设备与捕获端。新增修复应优先在 `tests/integration/` 留回归测试。

`scripts/check_config.py --live` 把"配置是否完整"与"请求是否成功"分开报告：
某能力未配置与配置了但连不上是不同的问题。六类能力的连通探测集中在
`aemeath/live.py`，配置检查与 live 测试共用同一实现，且都调用生产适配器。

记忆校准使用两个不重叠的数据集（`docs/acceptance-data/`，全部为虚构资料）：
校准集只用于扫描下限，验收集在锁定后运行一次。达标判据是**同题命中目标事实
≥9/10 且无关问题空结果 ≥9/10 同时成立**；区间是否重叠只作诊断，不单独决定
成败。多个下限达标时依次按同义召回率、无关空结果率、下限值选择；全部不达标
则报告失败，不自动降低标准。

生产轮次与客户端回执已接通，指标按两段分别记录：后端用本进程单调时钟，
客户端回执记录体验耗时，两者不相减。样本不足 20 个时标记"不足"，
没有回执显示"不可用"而不是 0。

详细验收记录中的首段文字为 10 个真实样本（P95 1287 ms，原始数据口径待复核）；语音两项延迟仍为 0 个样本，
**不能据此判断实际播放延迟是否达标**。

其他诊断入口：

```powershell
# 服务运行中做协议自检（协商、课堂切换、屏幕原因、历史来源）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\probe_protocol.py

# 对真实数据库做一次迁移检查（版本、行数、列）
.\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\check_migration.py
```

本机数据与日志主要位于：

| 位置 | 内容 |
| --- | --- |
| `data/` | Aemeath 本地数据库及运行数据 |
| `logs/` | Aemeath 日志和轮次指标 |
| `vendor/Open-LLM-VTuber/chat_history/` | 上游 JSON 历史，不再自动读取；可一次性导入 SQLite |
| `vendor/Open-LLM-VTuber/logs/` | 上游运行日志 |

这些内容不作为源码提交。

## 项目结构

```text
Aemeath/
├─ aemeath/                    自有核心模块
├─ config/                     配置
│  └─ acceptance/              验收专用配置（不含密钥）
├─ scripts/                    启动、检查、校准与诊断脚本
├─ tests/                      模块单元测试
├─ tests/integration/          生产入口集成测试
├─ tests/live/                 真实供应商连通测试（默认排除）
├─ docs/                       需求、架构、验收和补丁说明
│  └─ acceptance-data/         记忆校准集与验收集（虚构资料）
├─ vendor/Open-LLM-VTuber/     固定版本上游，本机准备
├─ vendor/Open-LLM-VTuber-Web/ 客户端源码，构建后部署到上游 frontend/
├─ data/                       本机数据
├─ logs/                       本机日志
├─ requirements.aemeath.txt    补充依赖
└─ AGENTS.md                   项目维护指南
```

## 文档导航

- [需求](docs/requirements.md)：要做什么，以及首期与后续范围。
- [技术架构](docs/architecture.md)：模块职责、数据边界与核心链路复盘。
- [实施计划](docs/implementation-plan.md)：需求覆盖、剩余任务、依赖与完成标准。
- [启动手册](docs/runbook.md)：环境准备、配置与运行。
- [验收记录](docs/acceptance.md)：四层结果、证据与待验证项。
- [真实 API 与设备验收](docs/acceptance-live.md)：本阶段实测数据与阻塞项。
- [验收操作指南](docs/acceptance-guide.md)：重复运行方式与人工验收清单。
- [上游补丁](docs/patches/README.md)：接入改动及复现方式。
- [仓库指南](AGENTS.md)：开发与维护约定。

复用上游代码和角色素材时，分别遵循对应许可证及使用条件。
#   A e m e a t h  
 