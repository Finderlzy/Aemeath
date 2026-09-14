# 增量复用调研

## v2.1 Live2D 制作复用核查（2026-09-14）

- v4 为 1254 × 1254 RGB 平面图。内置 imagegen 的抠图尝试输出了绘制的棋盘格而非 alpha，且有细节漂移，已删除。生成式输出不能不经检查直接充当分层源稿。
- 官方下载来源为 `cubism.live2d.com/editor/bin/Live2D_Cubism_Setup_5.3.00.exe` 与 `download.kde.org/stable/krita/5.3.3/krita-x64-5.3.3-setup.exe`；下载时 SHA256 与 winget 清单一致，数字签名有效。用户已安装到 D 盘，程序文件存在；这不等于启动、编辑模式、PSD 兼容或导出通过。工具链验证由 V21-T01 承接，不自动购买或激活 PRO 试用。
- 复用 Krita 分层编辑／PSD 与 Cubism Editor／Viewer，不自制 moc3 编译器。编辑器可自动操作的范围、PSD 透明效果、所用编辑模式限制与旧客户端 Core 的导出兼容性仍需小样验证；失败则阻塞完整分层／绑定并记录人工操作方案。
- `aemeath/management/live2d.py` 在规划基线 `5e0f25a` 中使用上游 `model_dict.json` 与 `character_config.live2d_model_name`，模型发现只检查 model3.json 是否存在，`is_official_model` 当前固定为 false。复用现有管理入口，在接入任务补足真实包完整性及身份判定，不能根据文件名宣称模型可运行。
- 固定客户端 `src/renderer/WebSDK/src/lappmodel.ts` 已使用 EyeBlink、LipSync、头部角度与视线参数；仍需按 [制作契约](live2d-production.md) 验证实际导出和动作叠加。SDK／Core 保持项目固定基线，不因新编辑器默认升级整个客户端。

以上为文件与源码核查，未执行 Krita／Cubism 导入导出或正式角色验收。原始参考、视频与源工程留本地，本轮入库定稿预览仅作为计划依据。

核查日期：2026-09-14。服务于 [v2 需求](requirements.md)，属于源码与文档核查，未进行学习效果、训练或桌面运行测试。现有底座固定信息继续以 [上游记录](upstream.md) 为准。

## MaiBot：人设、表达与黑话

公开仓库：[MaiM-with-u/MaiBot](https://github.com/MaiM-with-u/MaiBot)。本轮通过 GitHub 树查询定位文件，读取公开 main 分支源码；查询精确提交时 API 限流，因此本条未锁定提交，不用于直接引入代码。没有读取或复制大肥鱼的私人配置、记忆和凭据。

- [official_configs.py](https://github.com/MaiM-with-u/MaiBot/blob/main/src/config/official_configs.py)：`PersonalityConfig` 的三个主要字段是 `personality`（人格与身份）、`behavior_style`（Planner 的行动准则）、`reply_style`（说话风格）。另有备用表达风格及注入概率，不能描述为仅有三个字段。
- [config.py](https://github.com/MaiM-with-u/MaiBot/blob/main/src/config/config.py)：分别装配 `personality`、`expression`、`jargon`；人设三字段与人设／表达／黑话三个配置模块是两种不同划分。
- [expression_learner.py](https://github.com/MaiM-with-u/MaiBot/blob/main/src/learners/expression_learner.py)：独立提取表达，保留场景、风格、来源信息，有适用性检查、写入与审核记录相关机制；配置支持写入前 AI 检查。不能由这些机制推定学习质量已通过实测。
- [jargon_learner.py](https://github.com/MaiM-with-u/MaiBot/blob/main/src/learners/jargon_learner.py)：黑话学习独立运行，筛选学习来源，并排除黑话引用文本等来源，避免直接将注入内容重复当作新证据。
- 源码树有 `dashboard/src/routes/resource/expression/`、`dashboard/src/routes/resource/jargon/`，可供管理流程参考；尚未实际打开其界面或测量易用性。
- [LICENSE](https://github.com/MaiM-with-u/MaiBot/blob/main/LICENSE) 为 GPL v3 文本；当前建议借鉴概念和交互，在 Aemeath 自有模块实现，尚未决定复制或链接其代码。

采用方向：核心人设编辑与后天学习分开，学习结果有来源和用户纠正入口；用户确认人设界面采用“身份、性格、回复风格”，这与 MaiBot 的字段划分不完全相同。不复刻群聊来源、平台配置等与当前单用户桌面伙伴无关的复杂管理。用户已选择检查通过后自动应用，核心人设不自动改写。维护效率可从职责划分判断，学习质量、延迟及 API 成本必须用 Aemeath 场景实测。

对“是否高效”的结论：职责分开有利于定位和修改配置，但目前没有操作耗时或学习效果的对照证据，不能认定 MaiBot 更高效。Aemeath 借鉴其分离思路，将用户编辑入口保持为身份、性格、回复风格；不将 `behavior_style` 直接当作性格字段。学习效果在独立对话中评估，配置易用性通过完成一次修改并确认实际生效来验证。

## GPT-SoVITS：保留引擎，重做训练操作流程

仓库：[RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)。核查本地 `E:/WorkSpace/Tools/GPT-SoVITS`，提交 `48b1a01`，MIT 许可证；没有启动训练或修改服务。

本地 `webui.py` 包含前置数据工具、音频切分、ASR 与文本标注、训练集格式化、SoVITS 训练、GPT 训练和推理。上游同时提供免训练的参考音频推理路径，因此声音管理应明确区分“已有声音试听／应用”与“训练新声音”。

用户已采用分步向导组织“导入素材 → 清理切分 → 校对文字 → 训练 → 试听 → 应用”，高级入口保留原页面，完整向导属于 v2，可分阶段交付。仅内嵌原页面不能解决步骤难懂的问题。训练底层继续复用上游，暂不确定通过 Gradio 接口、独立任务进程或其他适配方式调用；须验证版本契约、进度、取消、失败重试及训练与实时语音的资源冲突后选型。

交互上提供“使用已有声音”和“训练新声音”两个入口。向导在用户开始前检查素材、依赖和资源，每一步只呈现当前必须处理的内容；GPT 与 SoVITS 的训练次序、可复用产物及预训练权重要求由固定版本的适配层处理。只有底层能可靠报告时才显示进度百分比或耗时预估。保留原页面是高级操作出口，不替代向导验收；直接打开上游页面也不代表完成了训练 API 集成。

## Open-LLM-VTuber-Web：桌面窗口基础

本地固定客户端源码的 `src/main/window-manager.ts` 已包含 Electron 透明无边框窗口、window/pet 模式及鼠标穿透；`src/main/menu-manager.ts` 有托盘管理。`package.json` 含 Windows 构建脚本。后端与客户端版本及 MIT／Live2D 许可边界沿用上游记录。

建议沿现有客户端扩展独立管理窗口与桌面呈现，共用 Aemeath 后端。仍须验证 Windows 构建、任务栏工作区域、缩放、窗口焦点、双窗口状态同步及字幕播放时序。源码存在不等于 Aemeath 桌面版已经可用。当前本地模型目录只有上游示例模型，用户确认正式模型将后续制作。
