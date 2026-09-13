# Aemeath 首期实施与验收计划

更新：2026-09-13。依据 [需求](requirements.md)、[架构复盘](architecture.md#2026-09-12-核心链路复盘)
和 [验收记录](acceptance.md)。任务已发布为 GitHub Issues，本文件保留**里程碑与总体顺序**；
单项任务的范围、依赖、状态和完成证据一律以 Issue 为准，在此不重复维护。

## 本轮结论与基线

首期已有功能实现，真实文字与视觉有通过记录；完整的“屏幕驱动主动语音 → 用户接续 → 课堂静音 → 重启记忆”尚未闭合。
先修接入缺口，再补真实体验证据。保留固定上游与本地数据边界，暂不扩展电脑操作、平台发送、卧室动画和学习模块。

基线为 `496ab346680966460fedd8842c9254d0f3bc5d3d`（`main`，clean tree）。
下文「2026-09-12 本机未提交工作树 / 上层仓库 HEAD `f7f634d`」是历史记录，不代表当前状态；
历史验收的原始时间与指标存在口径冲突，由 [T00](https://github.com/Finderlzy/Aemeath/issues/1) 负责复核，
历史通过记录在本轮仅作为已有证据引用。

> **T00 复核已完成（2026-09-13），结论见 [验收记录第九节](acceptance.md#九t00-证据复核与回归基线2026-09-13)。**
> 历史真实验收实际执行于 **2026-09-12**（原记 2026-09-14 为笔误）；
> 首段文字延迟以 **1287 ms / 10 样本**为有效口径，原 README 的 **518 ms 无法追溯、标为待核**；
> 修复前回归基线＝`71376f0` 上 **244 passed, 7 deselected**。
> 该基线只记录修复前事实，**不宣布任何功能通过**。

> **当前回归基线（2026-09-13，T03 完成后）：`main` 330 → 分支 347 passed, 7 deselected。**
> T03 增量 17 项，全部在 `tests/integration/test_proactive_screen_driven.py`，
> 经真实 runtime 定时器与上游主动生成器入口驱动，见
> [验收记录第十一节](acceptance.md#十一t03-屏幕驱动主动对话2026-09-13)。
> 该项只记录测试规模，**不代表 T03 已完成真机验收**；T03 的完成证据同样只覆盖隔离窗口。

## 需求覆盖

| 需求 | 当前依据 | 剩余缺口 | 对应任务 |
| --- | --- | --- | --- |
| R01 对话与桌面 | 浏览器 10 轮文字已有通过记录 | 桌面实际入口、麦克风、GPT-SoVITS 合成与可听主动输出 | [T01](https://github.com/Finderlzy/Aemeath/issues/2)、[T05](https://github.com/Finderlzy/Aemeath/issues/6)、[T07](https://github.com/Finderlzy/Aemeath/issues/8) |
| R02 课堂 | 状态、输出闸门与隔离测试已实现 | 播放中切换、主动音频、重启保持真人验证 | [T01](https://github.com/Finderlzy/Aemeath/issues/2)、[T05](https://github.com/Finderlzy/Aemeath/issues/6) |
| R03 本地记忆 | SQLite、提取与历史持久化有证据 | 可用嵌入、中文检索质量、重启后同义召回 | [T04](https://github.com/Finderlzy/Aemeath/issues/5) |
| R04 屏幕 | 手动捕获与视觉已有通过记录 | 迟到缓存、关闭后的请求、锁屏与窗口不可用 | [T02](https://github.com/Finderlzy/Aemeath/issues/3)、[T06](https://github.com/Finderlzy/Aemeath/issues/7) |
| R05 主动交流 | 定时器与资格规则已实现 | 空音频、重复计数、屏幕上下文、生成中状态变化与恢复 | [T01](https://github.com/Finderlzy/Aemeath/issues/2)、[T06](https://github.com/Finderlzy/Aemeath/issues/7) |
| R06 本地边界 | SQLite、本地索引与忽略规则 | 各项接入和复现时持续核查，不引入云端记忆 | [T02](https://github.com/Finderlzy/Aemeath/issues/3)、[T04](https://github.com/Finderlzy/Aemeath/issues/5)、[T07](https://github.com/Finderlzy/Aemeath/issues/8) |
| E01 建议能力 | 打断、纠正和精确遗忘已有隔离覆盖 | 真实打断、真实嵌入下纠正遗忘 | [T04](https://github.com/Finderlzy/Aemeath/issues/5)、[T05](https://github.com/Finderlzy/Aemeath/issues/6) |

## 里程碑与任务

| 里程碑 | 任务 |
| --- | --- |
| [M1 主动交流链路](https://github.com/Finderlzy/Aemeath/milestone/1) | [T00](https://github.com/Finderlzy/Aemeath/issues/1)、[T01](https://github.com/Finderlzy/Aemeath/issues/2)、[T02](https://github.com/Finderlzy/Aemeath/issues/3)、[T03](https://github.com/Finderlzy/Aemeath/issues/4) |
| [M2 真实语音与记忆](https://github.com/Finderlzy/Aemeath/milestone/2) | [T04](https://github.com/Finderlzy/Aemeath/issues/5)、[T05](https://github.com/Finderlzy/Aemeath/issues/6)、[T06](https://github.com/Finderlzy/Aemeath/issues/7) |
| [M3 首期交付](https://github.com/Finderlzy/Aemeath/milestone/3) | [T07](https://github.com/Finderlzy/Aemeath/issues/8) |

M1：屏幕驱动的主动交流链路接通（T01–T03），从 T01 开始；T00 同阶段校准证据。
M2：真实语音与长期记忆完成验收（T04–T06）。M3：实际交付入口与两次持续试用完成（T07）。
任务之间可独立准备供应商和验收材料，但共享桥接层与上游补丁按顺序集成。

依赖关系已在 GitHub 原生依赖字段设置，无循环：T03 依赖 T01、T02（T02 按 **T01 → T02** 串行集成共享桥接改动）；
T06 依赖 T03，其语音分支依赖 T05；T07 依赖 T00、T04、T05、T06。

| 任务 | Issue | 依赖 |
| --- | --- | --- |
| T00 复核验收证据并建立当前回归基线 | [#1](https://github.com/Finderlzy/Aemeath/issues/1) | 无 |
| T01 接通主动语音输出并统一送达计数 | [#2](https://github.com/Finderlzy/Aemeath/issues/2) | 无；与 T00 同阶段 |
| T02 修复屏幕观察关闭与迟到响应的生命周期 | [#3](https://github.com/Finderlzy/Aemeath/issues/3) | 无；与 T01 串行集成 |
| T03 接通屏幕驱动的自动主动对话 | [#4](https://github.com/Finderlzy/Aemeath/issues/4) | T01、T02 |
| T04 接入可用嵌入服务并验收真实长期记忆 | [#5](https://github.com/Finderlzy/Aemeath/issues/5) | 外部依赖：可用嵌入供应商 |
| T05 验收真实语音、打断与课堂静音 | [#6](https://github.com/Finderlzy/Aemeath/issues/6) | T01；本地 SenseVoice、本地 GPT-SoVITS 服务与音频设备 |
| T06 验收主动交流节奏与失败恢复 | [#7](https://github.com/Finderlzy/Aemeath/issues/7) | T03；语音分支依赖 T05；**已闭环** |
| T07 完成首期可复现交付与持续试用验收 | [#8](https://github.com/Finderlzy/Aemeath/issues/8) | T00、T04、T05、T06 |

T01 建议执行顺序：先从真实上游主动生成器入口建立失败回归 → 统一输出／轮次与回执语义 → 接入 TTS → 核对上游副本与补丁 → 验证普通回复及课堂分支不回退。
每个实现任务使用 `task-implement`；M3 使用 `project-delivery`。本次规划完成不表示上述任务已完成或自动获准发布。

## 验收入口与决策边界

按 [启动手册](runbook.md) 使用项目虚拟环境；真实 API 与设备操作沿用 [验收指南](acceptance-guide.md)。
使用独立验收配置、数据库与日志。不得复制大肥鱼数据、改变其服务，或为验证关闭真实工作窗口、向联系人发消息。

可直接推进 T01–T03 的代码与隔离验证，不必等待嵌入供应商。T04 需要可用嵌入服务；
T05 的语音链路采用本地 SenseVoice 与本地 GPT-SoVITS 方案。

**已确认的范围调整（2026-09-13）**：
1. **ASR 首期目标改为本地 SenseVoice**：通过 sherpa-onnx 在本地运行，麦克风转写链路已实测通，原有识别、打断、静音及延迟验收要求全部保留。
2. **TTS 正式方案为本地 GPT-SoVITS**：通过 localhost HTTP (api_v2) 接入，服务已安装连通。参考音频暂缺，首期先不做角色音色与听感验收。
3. 理由与完整边界见 [需求](requirements.md)；架构与接入实现见 [架构](architecture.md)。

- T05 的验收口径：基于本地 SenseVoice 与本地 GPT-SoVITS 服务验证语音对话、播放打断、课堂静音与延迟，**判据不放宽**（仍保留 A1–A7 与三项 ≥20 样本）。
- 音色本身不在本次完成条件内，参考音频暂缺先不做角色音色与听感验收；爱弥斯正式音色与专属训练另行确定，不编造原作设定。
- 本机服务依赖：GPT-SoVITS 服务未启动时 TTS 不可用，必须表现为明确不可用，不得静默退回其他引擎。

正式角色声音、美术与月预算在对应选型前细化。后续作息、提醒、表达学习、电脑操作和 QQ/微信渠道在 M3 后按场景规划，保留需求但不提前拆到函数。
情绪躲藏、音量检查与微信代答（F01–F03）不纳入本批 Issue。
本轮只发布任务、不改变版本、不提交或推送代码，遵循项目“不自动提交”的约定；**创建 Issue 不代表任务已实现**。
