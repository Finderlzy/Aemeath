# 上游固定版本与许可

更新：2026-09-11。本文件记录 Aemeath 首期验证底座的实际固定状态。

## 底座版本

| 项 | 值 |
| --- | --- |
| 项目 | Open-LLM-VTuber |
| 仓库 | `https://github.com/Open-LLM-VTuber/Open-LLM-VTuber` |
| 标签 | `v1.2.1` |
| commit | `3afa41014b4548a0842e9ee2f576f4b164b48886` |
| 前端子模块 | `https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web` @ `06a659b114fff788cf0daaa86e484576db4975bf` |
| 本地路径 | `vendor/Open-LLM-VTuber/`（不入源码版本管理） |
| 许可证 | MIT（Copyright (c) 2025 Yi-Ting Chiu） |
| Live2D 许可 | 见上游 `LICENSE-Live2D.md`，与主许可证不同，单独核对 |

上游声明 `requires-python = ">=3.10,<3.13"`，本项目固定在 **Python 3.11**。

## 依赖锁定

- `uv.lock` 与 `pixi.lock` 由上游提供，随标签固定。
- 安装命令：`uv sync --frozen --python 3.11`（在 `vendor/Open-LLM-VTuber/` 内执行）。
- 上游 lock **不完整**：`silero-vad` 未声明，且 `torchaudio` 存在 ABI 冲突。
  必须在 `uv sync` 之后补装 `requirements.aemeath.txt`，否则无法启动。
- **开发期间不自动升级**；升级上游需显式换标签并重新验证。

上游依赖包含本地推理与音频处理库（`torch`、`sherpa-onnx`、`pydub` 等）。首期模型走 API，但**这不代表可以删除这些依赖**，它们仍被上游的 VAD/ASR/TTS 与音频处理链路使用。

## 与 Aemeath 的边界

- 上游以固定版本为基线，通过配置、自有模块和 [四个有序补丁](patches/README.md) 接入；当前工作副本包含这些修改。
- Aemeath 自有代码位于 `aemeath/`，不混入上游目录。
- 上游目录不提交到本项目版本库（`.gitignore` 已排除 `vendor/`）。
- 未复制任何既有账号、聊天历史、凭据或运行配置。

## 需要改动上游的位置

上游 `AgentFactory.create_agent()` 使用硬编码的 `if/elif` 分支选择 agent，没有插件注册机制。接入 `AemeathAgent` 必须在 `src/open_llm_vtuber/agent/agent_factory.py` 增加一个分支。该补丁在 [patches/](patches/) 下单独记录，便于上游升级后重新套用。

## 已知环境注意事项

- **代理**：本机需要 `http://127.0.0.1:7897` 才能访问 GitHub/PyPI。Windows 系统代理已配置，但 `git`、`uv`、`pip` 等 CLI 默认不继承，需显式设置。见 `scripts/env.ps1`。
- **ffmpeg**：上游 `pydub` 提示找不到 `ffmpeg`，部分音频格式转换可能受影响。首期用 edge-tts 输出的 mp3 与 sherpa 的 ASR 链路暂不受阻，如遇解码失败再安装。
