# Aemeath 配置示例（无凭据，可提交）

本文件是可提交的配置样例。实际运行配置见 `config/conf.aemeath.yaml`（已排除版本管理）。

## 一、密钥通过环境变量注入

配置文件中**不写真实密钥**，只写环境变量名。上游 `read_yaml` 会展开 `${VAR}` 形式：

```yaml
openai_compatible_llm:
  llm_api_key: '${AEMEATH_LLM_API_KEY}'
```

对应环境变量：

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `AEMEATH_LLM_API_KEY` | 是 | 对话模型密钥 |
| `AEMEATH_TTS_API_KEY` | 否 | 使用 API TTS 时需要（首期默认 edge_tts 不需要） |
| `AEMEATH_VISION_API_KEY` | 否 | 屏幕理解，可与对话共用 |
| `AEMEATH_EMBEDDING_API_KEY` | 否 | 记忆嵌入，可与对话共用 |

未设置的变量会保持 `${...}` 字面量；`scripts/check_config.py` 会把它报为警告而不是崩溃。

```powershell
$env:AEMEATH_LLM_API_KEY = 'sk-...'
```

## 二、按能力替换供应商

每项能力独立配置，允许同一供应商。首期默认值选择的是**无需密钥即可跑通**的组合：

| 能力 | 首期默认 | 替换方式 |
| --- | --- | --- |
| 对话 | `openai_compatible_llm` | 改 `base_url` / `model` |
| ASR | `sherpa_onnx_asr`（本地离线） | 改 `asr_model`，如 `groq_whisper_asr` |
| TTS | `edge_tts`（本地免费） | 改 `tts_model`，如 `siliconflow_tts` |
| 视觉 | 未接入 | 见 `aemeath/adapters.py`，需 OpenAI 兼容视觉端点 |
| 嵌入 | 未接入 | 见 `aemeath/adapters.py` |
| 记忆提取 | 未接入 | 见 `aemeath/adapters.py` |

> **不要因为名字里有 "compatible" 就假定端点可用。** OpenAI-compatible 只是接口形状相似，
> 具体模型是否支持视觉、是否返回 `usage`、是否支持流式，都要实际连通验证。

## 三、Aemeath 侧参数

写在 `character_config.aemeath_config` 下，上游会忽略该段，由 `aemeath/config.py` 读取。
所有数值都有默认值，缺省不影响启动。

```yaml
character_config:
  aemeath_config:
    # 路径相对项目根目录（Aemeath/），不是相对本配置文件
    data_dir: 'data'
    log_dir: 'logs'

    memory:
      recent_turns: 12            # 注入提示词的最近轮数
      recall_limit: 5             # 每轮最多召回的记忆条数
      experience_summary_every: 10 # 每多少个完成轮次生成一次经历摘要

    proactive:
      cooldown_seconds: 900       # 两次主动搭话的最小间隔
      max_per_hour: 2             # 任意一小时最多主动搭话次数
      startup_greeting_enabled: true

    screen:
      max_edge_px: 1600           # 截图最长边上限
      min_interval_seconds: 60    # 自动观察最小间隔
      min_stable_seconds: 10      # 前台窗口需稳定的时长
      summary_max_age_seconds: 120 # 超过此时长的摘要不再当作"当前"
```

这些是计划采用的默认值，应作为配置或测试常量调整，而不是散落在代码里。

## 四、本地数据位置

| 内容 | 位置 |
| --- | --- |
| 数据库（消息、事实、经历、向量、情境状态） | `data/aemeath.sqlite3` |
| 备份目录 | `data/backups/` |
| 日志与轮次指标 | `logs/` |
| 上游运行日志 | `vendor/Open-LLM-VTuber/logs/` |

`data/`、`logs/`、`vendor/` 均已排除出版本管理。

**首期不提供自动备份恢复**：在删除传播尚未覆盖备份之前，自动恢复会把已删除的记忆装回来。
备份规则在实现删除传播之后再定义。

## 五、本地存储不等于提供商零留存

记忆检索、事实提取与嵌入都会把**本次所需输入**发送给所配置的模型 API。
本地保存不改变数据已经离开本机这一事实；选定服务商时应核对其数据留存策略。
