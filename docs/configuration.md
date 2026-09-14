# Aemeath 配置示例（无凭据，可提交）

本文件是可提交的配置样例。实际运行配置见 `config/conf.aemeath.yaml`（同样受版本管理，因此只能出现 `${VAR}` 引用，不得写入字面密钥）。

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

## 三之二、管理界面与配置读写（V2-T01）

管理界面通过 Aemeath 自有的管理 API 读写**同一份权威配置**，不维护第二份副本。

| 项 | 值 |
| --- | --- |
| 路由前缀 | `/aemeath/manage/` |
| 读取 | `GET /aemeath/manage/overview` |
| 保存模型 | `POST /aemeath/manage/model` |
| 保存人设 | `POST /aemeath/manage/persona` |
| 管理页地址 | `http://127.0.0.1:12393/?page=manage` |

**已保存 vs 已生效。** 响应同时返回 `saved_revision` 与 `running_revision`，
以及 `restart_required`。保存写入权威 YAML，但引擎不会热重载，因此模型与人设改动
都标记为「已保存，重启后生效」。运行修订号取进程启动时那一份，**不会因保存而前进**，
否则界面会把未生效的改动报成已生效。

**并发修订。** 每次保存必须携带读取时的 `expected_revision`。修订号不匹配时返回
**409** 且不写文件，避免覆盖其他窗口的较新改动。

**保存失败不损坏旧配置。** 候选配置先完整校验（走上游 `validate_config`），通过后写
临时文件再 `os.replace` 原子替换；任一步失败都保留原文件。

**凭据。** 请求可以携带密钥，但它只写入本机凭据存储
（`config/credentials.yaml`，已 gitignore），配置文件里留下的是 `${VAR}` 引用。
响应**永不回显**密钥：只返回 `api_key_configured` 布尔值。

**访问限制。** 管理写接口仅接受回环来源（对端地址与 `Origin` 都按回环判定），
普通网页无法跨站改写本机设置。

### 人设：旧原文与三字段

旧配置只有 `persona_prompt` 一段自由文本。管理界面按身份／性格／回复风格三项编辑。
**旧原文不会被自动拆分**：只有用户自己保存三项后才会切换来源。首次迁移时原文被复制到
`aemeath_config.legacy_persona_prompt` 保留，因此切换后原文仍可查看，迁移可回溯。
组装提示词时只会使用其中一个来源，不会同时注入。

## 四、本地数据位置

| 内容 | 位置 |
| --- | --- |
| 数据库（消息、事实、经历、向量、情境状态） | `data/aemeath.sqlite3` |
| 备份目录 | `data/backups/` |
| 日志与轮次指标 | `logs/` |
| 上游运行日志 | `vendor/Open-LLM-VTuber/logs/` |
| 管理界面保存的凭据 | `config/credentials.yaml`（已 gitignore，不进源码管理） |

`data/`、`logs/`、`vendor/` 均已排除出版本管理。

**首期不提供自动备份恢复**：在删除传播尚未覆盖备份之前，自动恢复会把已删除的记忆装回来。
备份规则在实现删除传播之后再定义。

## 五、本地存储不等于提供商零留存

记忆检索、事实提取与嵌入都会把**本次所需输入**发送给所配置的模型 API。
本地保存不改变数据已经离开本机这一事实；选定服务商时应核对其数据留存策略。
