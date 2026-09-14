# Aemeath 对上游的改动

上游固定为 `v1.2.1`（commit `3afa41014b4548a0842e9ee2f576f4b164b48886`）。
所有改动都通过本目录的补丁文件记录，便于上游升级后重新套用。

## 补丁清单与套用顺序

**必须按编号顺序套用**，每个补丁只改自己负责的文件，互不重叠：

| 顺序 | 补丁 | 文件 | 作用 |
| --- | --- | --- | --- |
| 1 | `0001-register-aemeath-agent.patch` | `src/open_llm_vtuber/agent/agent_factory.py` | 注册 `aemeath_agent` 分支，把情境管理器、记忆、屏幕摘要 provider 与桥接层接进 agent，并启动 runtime 后台任务 |
| 2 | `0002-register-aemeath-agent-config.patch` | `src/open_llm_vtuber/config_manager/agent.py` | 允许 `aemeath_agent` 通过配置校验 |
| 3 | `0003-bridge-aemeath-runtime.patch` | `service_context.py`、`websocket_handler.py`、`conversations/*` | 桥接层：协议扩展、轮次接入、输出闸门、迟到音频丢弃、SQLite 历史、主动调度与一次性问候 |
| 4 | `0004-fix-tls-for-conversation-endpoint.patch` | `src/open_llm_vtuber/agent/stateless_llm/openai_compatible_llm.py` | 对话端点使用受 `AEMEATH_TLS_INSECURE` 控制的 HTTP 客户端 |
| 5 | `0005-mount-aemeath-management-routes.patch` | `server.py` | 挂载 Aemeath 管理 API（`/aemeath/manage/`），在 frontend 通配挂载**之前**注册 |

`0001` 已包含 agent 工厂的**完整**改动（注册 + 桥接接线），不要把旧版注册补丁叠加使用。

## 套用方式

```powershell
cd vendor/Open-LLM-VTuber
git apply ..\..\docs\patches\0001-register-aemeath-agent.patch
git apply ..\..\docs\patches\0002-register-aemeath-agent-config.patch
git apply ..\..\docs\patches\0003-bridge-aemeath-runtime.patch
git apply ..\..\docs\patches\0004-fix-tls-for-conversation-endpoint.patch
git apply ..\..\docs\patches\0005-mount-aemeath-management-routes.patch
```

## 0005 的改动要点

管理 API（`aemeath/management/`）是 Aemeath 自有的 FastAPI 子应用，需要在
上游 `WebSocketServer.__init__` 里挂到同一个 app 上：

```python
try:
    from aemeath.management.routes import install_management_routes

    install_management_routes(self.app)
except ImportError as exc:
    logger.warning(...)
```

两处容易出错的地方：

1. **必须注册在 frontend 通配挂载之前。** `self.app.mount("/", StaticFiles(directory="frontend"))`
   会接管所有未匹配路径，注册顺序反了就会让管理路由被静态文件吞掉，表现为 404。
2. **`logger` 必须存在。** `server.py` 原本没有导入 `loguru` 的 logger；兜底分支里的
   `logger.warning` 一旦触发就是 `NameError`，而正常路径永远不会走到它。
   因此 `0005` 同时补了模块级 `from loguru import logger`。

补丁对 `aemeath` 采用**惰性导入**，与 `0001` 的取舍一致：上游单独运行时没有
Aemeath 代码也能启动，只是没有管理接口。

## 0004 的改动要点

OpenAI SDK 自己构造 `httpx` 客户端，**不读取**环境里的代理与 TLS 设置。
本机模型流量走本地代理，其 CA 是自签的，于是对话请求报
`CERTIFICATE_VERIFY_FAILED`；而 Aemeath 自己的适配器走统一客户端，不受影响。
结果是"记忆提取能通、对话不能通"这种容易被误判为配置问题的现象。

`AEMEATH_TLS_INSECURE=1` 只对**这一个客户端**关闭校验，并打一条 warning，
不会静默地降低安全性。未设置时返回 `None`，交回 SDK 的默认行为，因此
普通环境（含正式部署）行为不变。

补丁同时处理 `${VAR}` 形式的密钥：环境变量缺失时**直接抛
`ValueError`**，不再有任何硬编码回退。上游 `config_manager` 本就会解析
`${VAR}`，这里只是兜底；静默 fallback 会把配置错误掩盖成"能跑"，
也曾导致真实密钥被写进补丁文件（2026-09-14 修复，见下）。

## 为什么需要改动

上游 `AgentFactory.create_agent()` 用硬编码的 `if/elif` 分支选择 agent，
没有插件注册机制；且 `AgentConfig.conversation_agent_choice` 是 `Literal[...]` 白名单。

## 设计取舍

新增分支**惰性导入** Aemeath 模块：

```python
elif conversation_agent_choice == "aemeath_agent":
    from aemeath.agent import AemeathAgent
    ...
```

好处是：不选择 `aemeath_agent` 时，上游完全不依赖 Aemeath 代码，
上游自身仍可独立运行与升级，符合"上游保留独立目录和来源信息"的要求。

## 两个配置陷阱（缺一不可，实测结论）

只加 `0001` 时，服务启动会在配置校验阶段直接失败：

```
character_config.agent_config.conversation_agent_choice
  Input should be 'basic_memory_agent', 'mem0_agent', 'hume_ai_agent' or 'letta_agent'
  [type=literal_error, input_value='aemeath_agent']
```

原因有两点，第二条容易踩：

1. `AgentConfig.conversation_agent_choice` 是 `Literal` 白名单，不含 `aemeath_agent`。
2. `AgentSettings` 没有 `aemeath_agent` 字段，且**未设置 `extra="forbid"`**，
   因此 `agent_settings.aemeath_agent` 会被 pydantic **静默丢弃**。
   `service_context.init_agent()` 传入的是 `agent_config.agent_settings.model_dump()`，
   丢失后工厂就报 `LLM provider not specified for aemeath_agent`。

> 结论：**只加配置字段不加模型类是不够的**，必须真正声明
> `aemeath_agent: Optional[AemeathAgentConfig]`。

## 0003 的改动要点

### 单一桥接层

`AemeathBridge`（`aemeath/bridge.py`）是上游与 Aemeath 的唯一接缝：

```
桌面客户端 → 上游连接与输入 → AemeathBridge → coordinator
           → agent / memory / screen / scheduler → 输出桥接与 TTS → 客户端
```

- coordinator 唯一管理轮次、取消与输出许可。
- 每条出站帧都带 `generation`、`turn_id`、`audio_slice_id`、`state_version`。
- 单用户只保留一个活动客户端；新连接接管时取消旧连接轮次。

### 会话上下文共享桥接

`ServiceContext` 会被**克隆**给每个 WebSocket 会话，而桥接是进程级单例
（一个 coordinator、一份情境、一个数据库）。
`websocket_handler._init_service_context` 因此显式把
`default_context_cache.aemeath_bridge` 赋给新上下文。
缺少这一步时桥接为空，Aemeath 输出会绕过自己的仲裁。

### 历史只走 SQLite

`history-list` / `fetch-and-set-history` / `create-new-history` / `delete-history`
对 Aemeath agent 改为读写 SQLite；上游 JSON 历史不再被写入，也不再自动读取。
其他 agent 保持原行为。

### 迟到输出丢弃

`tts_manager._deliver()` 是音频字节离开后端前的最后一点，
这里检查 `bridge.may_send_audio(turn_id)`：
合成期间切换到课堂模式或轮次被取消时，音频在此丢弃，
即使 TTS 请求本身已经来不及取消。

### 主动聊天的唯一来源

三处改动共同保证"后端调度器是主动轮次的唯一来源"：

1. `agent_factory.py` 在创建 agent 时有事件循环则 `create_task(runtime.start())`，
   否则真实服务里没有记忆任务与主动调度任务（只在测试中被手动启动过）。
2. `service_context.py` 装配主动生成器：桥接决定**是否**可以说话，
   生成器负责**说什么**，两者复用同一个 agent 与提示词。
3. `websocket_handler.py` 把 `ai-speak-signal` 改路由到 `_handle_ai_speak_signal`，
   只做资格检查；连接建立后尝试一次启动问候。非 Aemeath agent 走原路径。

### 主动语音与轮次标识（T01）

主动搭话本身没有上游对话循环，因此 **`TTSTaskManager` 不会被驱动**。
补丁因此把合成引擎直接交给桥接：

- `init_tts()` 与 `init_agent()` 都调用 `aemeath_bridge.attach_tts_engine(...)`，
  两者都可能后执行，取最后生效的一方（桥接是进程级单例，引擎属于会话）。
- 桥接用同一个引擎与 `prepare_audio_payload()` 合成主动音频，
  与普通回复走同一套编码、同一套闸门。

生成器导入修正为 `src.open_llm_vtuber.agent.input_types`（**A05**）。
`open_llm_vtuber.*` 同样能导入成功——因为 `src/` 在 `sys.path` 上——
但会把同一批文件执行第二遍，产生第二个 `BatchInput` 类，
与项目"统一使用 `src.open_llm_vtuber.*`"的约定冲突。

主动轮次 id 由桥接生成并**贯穿**文字、音频与显示回执：坐标相同的 id
才能让客户端在打断后拒绝迟到音频，回执也才能对上同一条候选。

### 屏幕摘要进入提示词（T03）

`agent_factory.py` 创建 agent 时额外传入 `screen_summary_provider`：

```python
screen_summary_provider=runtime.bridge.current_screen_summary,
```

- 传的是**可调用对象**而不是摘要快照。用户可能在模型生成的任意长等待期间
  关掉观察，有效性（开关、来源窗口、时效）必须在组装提示词那一刻重新判定。
- 判定权在桥接，不在 agent：只有桥接同时知道情境开关、观察器开关、
  来源窗口与摘要时间。agent 永远不自己判断有效性，也不缓存结果。
- 未传该参数时 agent 退化为"没有屏幕上下文"，不会报错——上游独立运行或其他
  agent 不受影响。

## 验证方式

历史记录称补丁已验证可复现（原记载 2026-09-14，经 [T00 复核](https://github.com/Finderlzy/Aemeath/issues/1)
判定为**笔误，实际执行于 2026-09-12**；该次复核未重跑补丁验证，下次按实施计划 T07 核对）。

**2026-09-14 密钥泄露修复后已重跑**（针对干净 `v1.2.1`，commit `3afa410`）：

- 按序 `git apply --check` 四个补丁全部成功，且可实际套用；
- 全部 **9 个被修改文件**（含 `frontend`）与工作副本**逐字节一致**
  （`openai_compatible_llm.py` 的 `git hash-object` 为 `6f1f647`）；
- `${VAR}` 三条路径实测：变量缺失抛 `ValueError`、变量存在正常解析、
  字面密钥原样透传；
- `pytest -q` 全绿（372 passed）。

## 密钥泄露事件与修复（2026-09-14）

GitGuardian 告警 `[Finderlzy/Aemeath] DeepSeek API Key exposed on GitHub`
（pushed 2026-09-13 11:24:54 UTC）确认属实。

**成因**：`0004` 的兜底逻辑写成 `os.environ.get(env_var) or "<字面密钥>"`，
生成补丁时把本机真实密钥一并写进了 `docs/patches/0004-*.patch`，随
`6c7850b` 进入 `main`（并进入 `v1.0.0` 标签）。

**修复**：

1. 删除硬编码回退，改为环境变量缺失时抛 `ValueError`（见上）；
2. **修掉一个被掩盖的 `NameError`**：`import os` 原本写在
   `_aemeath_http_client()` 函数体内，模块作用域没有 `os`，因此 `${VAR}`
   分支一旦执行就会 `NameError`——硬编码回退让这条路径从未被真正走通。
   已改为模块级 `import os`；
3. 用干净上游重新生成 `0004` 补丁（不能只做文本替换：`index` 行与 hunk 行数都会变）；
4. 轮换（吊销并重建）DeepSeek 密钥——历史重写无法撤回已被抓取的密钥，
   轮换是唯一根本缓解；
5. 重写 Git 历史清除旧值，并强制推送分支与标签；
6. 本地残留清理：`vendor/` 工作副本、`__pycache__` 字节码、`logs/` 调试日志。

**结论性约定：补丁与文档中不得出现字面密钥，`${VAR}` 是唯一允许形式。**
`scripts/check-secrets.ps1` 用于提交前扫描。

## 客户端改动与补丁（T07 纳入交付）

客户端源码位于 `vendor/Open-LLM-VTuber-Web`，固定基线为 commit `d176e7df2366952e3bacbf12cf9a8b18a4315932`。
所有客户端改动（含 8 个修改文件与 2 个 Aemeath 专属协议/上下文文件）均纳入独立补丁：

| 补丁文件 | 目标仓库 | 作用 |
| --- | --- | --- |
| `docs/patches/web/0001-aemeath-web-client.patch` | `vendor/Open-LLM-VTuber-Web` | 协议协商（v2）、显示与播放端到端回执上报、迟到音频拒绝与静音保护、Aemeath 上下文与打断联动 |
| `docs/patches/web/0002-aemeath-management-ui.patch` | `vendor/Open-LLM-VTuber-Web` | 管理界面与 `aemeath-management` API 客户端；管理窗口路由。V2-T01 含概览／模型／人设，V2-T02 增加声音／记忆／Live2D 三页 |

**web 补丁按序套用**，`0002` 基于 `0001` 之后的 `App.tsx` 生成。

> **重新生成 `0002` 时必须在 `0001` 已套用的树上做。** `0001` 自己也会改 `App.tsx`
> （加入 `AemeathProvider` 的 import 与挂载）。若在未套用 `0001` 的树上生成，补丁会把
> 那一行记成新增内容，套用时必然报 `patch does not apply`。2026-09-14 生成 V2-T02
> 版本时踩到过这个坑；最终做法是先在干净树上 `git apply 0001`，再复制管理页文件并生成
> 补丁，最后用另一个干净检出按序套用 `0001`→`0002`，核对 6 个文件与工作副本逐字节一致。

套用命令：
```powershell
cd vendor/Open-LLM-VTuber-Web
git apply ..\..\docs\patches\web\0001-aemeath-web-client.patch
git apply ..\..\docs\patches\web\0002-aemeath-management-ui.patch
```

套用后执行编译并部署到后端：
```powershell
npm.cmd run build:web
Copy-Item dist\web\assets\* ..\Open-LLM-VTuber\frontend\assets\ -Force
Copy-Item dist\web\index.html ..\Open-LLM-VTuber\frontend\index.html -Force
```

> 重新生成补丁时必须保留 LF 行尾。用 PowerShell 的 `Out-File`/`>` 会写出
> CRLF 或 UTF-16LE，导致 `git apply` 报 patch does not apply 或
> "No valid patches in input"。可靠做法是
> `cmd /c "git --no-pager diff --no-color -- <files> > patch"`。

## 升级上游的步骤

1. 切换到新的上游标签，重新 `uv sync --frozen`。
2. 按上表重新生成/核对补丁（补丁会随上游变动失效，不要假设旧补丁仍可套用）。
3. 重新运行 `python scripts/check_config.py` 与 `pytest`。
4. 更新 [docs/upstream.md](../upstream.md) 中的版本与 commit 记录。
