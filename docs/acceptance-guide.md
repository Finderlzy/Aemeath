# Aemeath 验收操作指南

本文件说明如何重复运行验收环境，以及**哪些步骤必须由人完成**。
自动可验证的部分见 [acceptance-live.md](acceptance-live.md)。

## 一、如何运行

### 1. 注入验收环境

验收环境使用独立配置、独立数据库与独立日志，不与个人记忆混用。

```powershell
cd E:\WorkSpace\Aemeath
. .\scripts\acceptance-env.ps1
```

该脚本设置：

| 变量 | 作用 |
| --- | --- |
| `AEMEATH_CONFIG` | 指向 `config/acceptance/conf.acceptance.yaml` |
| `AEMEATH_LLM_API_KEY` / `AEMEATH_EXTRACTION_API_KEY` | 真实供应商密钥 |
| `AEMEATH_TLS_INSECURE=1` | 本机代理是自签 CA，仅限本机验收使用 |
| `HTTP_PROXY` / `HTTPS_PROXY` | CLI 与 Python 都不继承 Windows 系统代理 |

脚本被 `.gitignore` 排除，因为它注入真实密钥。

> **环境脚本和服务必须在同一个 PowerShell 会话里执行。** 脚本注入的环境
> 变量只存在于当前进程；用子进程（如 `powershell -File ...`）设置环境后
> 退出，变量不会留在你的会话里，后续启动的服务读不到密钥。
>
> 若 PowerShell 执行策略拦截了 `. .\scripts\acceptance-env.ps1`，在**当前
> 进程**调整策略后再点加载，例如：
>
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> . .\scripts\acceptance-env.ps1
> ```
>
> `-Scope Process` 只影响当前会话，关闭窗口即失效。

### 2. 检查配置与连通

```powershell
$py = ".\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe"

# 只检查配置是否完整
& $py scripts\check_config.py

# 同时真实调用每个已配置的供应商
& $py scripts\check_config.py --live
```

两类结果分开报告：某能力"未配置"与"配置了但连不上"是不同的问题。

### 3. 启动服务并打开客户端

```powershell
& $py scripts\run_server.py --config config\acceptance\conf.acceptance.yaml
```

浏览器打开 <http://127.0.0.1:12393>。页面显示"已连接"表示协议协商成功。

`--config` 会贯穿两层：复制到上游的 YAML，以及服务进程内 Aemeath runtime 读取的
路径（通过 `AEMEATH_CONFIG`）。只改一层会导致上游引擎与 Aemeath 模块读到不同配置。

### 4. 查看状态与验收数据

```powershell
& $py scripts\status.py --config config\acceptance\conf.acceptance.yaml
& $py scripts\status.py --config config\acceptance\conf.acceptance.yaml --list
& $py scripts\status.py --config config\acceptance\conf.acceptance.yaml --process-pending
```

不带 `--config` 时读的是个人数据库，两者外观相同但数据不同。

### 5. 记忆校准

```powershell
# 校准集：扫描候选下限，并报告同义/无关分数是否可分离
& $py scripts\calibrate_memory.py --dataset calibration

# 验收集：锁定下限后运行一次
& $py scripts\calibrate_memory.py --dataset validation --floor 0.42
```

加 `--output PATH` 保存 JSON，加 `--keep-db` 保留数据库以便复现。

> **不要用验收集反复调参。** 校准只在校准集上做。

### 6. 运行可重复的连通测试

```powershell
& $py -m pytest -m live_api         # 真实供应商
& $py -m pytest                      # 默认排除 live_api
```

## 二、必须人工完成的部分

以下步骤需要人对麦克风说话、听声音或评价体验，无法自动化。

### A. 语音链路（需耳机麦克风）

先启动服务并打开客户端，在页面上开启麦克风。

| # | 操作 | 预期 | 记录 |
| --- | --- | --- | --- |
| A1 | 正常说一句话 | 转写 → 回复 → 合成 → 播放完整发生 | 轮次 id、是否完整 |
| A2 | 播放中开口，连续 5 次 | 每次旧音频立即停止，新轮次接管 | 是否有残留播放 |
| A3 | 播放时点击停止 | 旧音频不再恢复 | 停止是否彻底 |
| A4 | 播放中输入"我在上课" | 立即停止，并用文字确认 | 文字是否可见 |
| A5 | 课堂模式下完成 10 轮 | 无普通回复语音 | 是否漏音 |
| A6 | 重启后检查课堂模式 | 仍为课堂模式；明确下课后恢复语音 | 状态是否持久 |
| A7 | 关闭麦克风 | 不再采集或发送录音 | 是否真的停止 |

出现残留播放、重复回复或课堂漏音时，**暂停后续体验验收**，先修复。

### B. 屏幕观察（需接入视觉供应商）

用无敏感内容的测试窗口，关闭主动搭话。

对**网页、编辑器、文档**三类各观察两次：

| 检查项 | 记录 |
| --- | --- |
| 能否描述具体可见内容 | |
| 捕获范围是否符合当前窗口约定 | |
| 切换窗口后是否把旧画面当当前内容 | |
| 最小化/锁屏/捕获失败时是否明确报告不可用 | |
| 关闭观察后是否不再捕获、是否拒绝迟到摘要 | |
| 截图是否长期落盘 | |

随后开启自动观察，检查节流与重复画面去重。
多显示器仅在确实使用时纳入，并记录显示缩放。

### C. 主动聊天与失败恢复

保持正式冷却与频次配置。

| 检查项 | 预期 |
| --- | --- |
| 无客户端主动信号 | 后端仍能自主发起 |
| 用户未回应 | 不继续搭话 |
| 用户正在说话/输入/已有回复 | 不抢话 |
| 课堂模式 | 主动消息只有文字 |
| 关闭屏幕观察 | 不声称看见当前屏幕 |
| 断开重连 | 不重复问候、不恢复旧主动候选 |
| 模拟请求失败一次 | 恢复后下一轮可继续 |
| 模拟客户端断线 | 记忆任务可恢复且不重复写入 |

### D. 延迟采样（每项 ≥20 个有效样本）

| 指标 | 起止点 | 目标 |
| --- | --- | --- |
| 首段文字 | 提交输入 → 页面首次显示回复 | P95 ≤ 5 秒 |
| 语音回复 | 停止说话 → 实际开始播放 | P95 ≤ 10 秒 |
| 点击停止 | 点击停止 → 播放器停止 | P95 ≤ 500 毫秒 |

样本必须来自客户端同一单调时钟。失败、取消与缺失回执**单独统计**，
不能只挑成功的快样本。后端 ASR/模型/TTS 耗时只用于定位瓶颈。

**采样来源标记（2026-09-13 起）**：回执会带上 `sample_source`，只有下列组合计入有效样本，
其余记录保留但不进入验收统计：

| 指标 | 有效 `sample_source` | 前置条件 |
| --- | --- | --- |
| 首段文字 | `user_text_input` | 由客户端在提交输入时开始计时 |
| 语音回复 | `microphone_vad` | 该轮来源为 `user_voice`，计时从 VAD 判定说话结束开始 |
| 点击停止 | `click_stop` | 点击时确有音频正在播放；无播放的点击不算样本 |

口径审计与进度统计：

```powershell
$py = ".\vendor\Open-LLM-VTuber\.venv\Scripts\python.exe"
& $py scripts\audit_latency_metrics.py          # 有效样本、P50/P95、待核与无效样本
& $py scripts\monitor_acceptance_progress.py    # 现场采录进度
```

### E. 两次各 30 分钟试用

**第一次：工作陪伴**
正常工作、随口聊天、分享屏幕，给她机会主动搭话；其中一次故意不回应。
记录打扰程度、上下文理解与声音体验。

**第二次：课堂与恢复**
进入课堂模式，交替打字与语音输入，测试重连、重启、记忆追问与下课恢复。
自动观察和主动聊天按实际预期配置。

两次都用简短事件表记录，只保存复现所需内容：

| 时间 | 操作 | 预期 | 实际 | 轮次 ID |
| --- | --- | --- | --- | --- |
| | | | | |

## 三、清理

验收数据保留在 `data/acceptance/`、日志在 `logs/acceptance/`，
两者都不入版本管理，可随时用于复查。

重新开始一轮干净的验收（**只删验收目录，不要用全局 `tmp*` 模式清理**）：

```powershell
Remove-Item -Recurse -Force data\acceptance, logs\acceptance
New-Item -ItemType Directory -Force data\acceptance, logs\acceptance | Out-Null
```
