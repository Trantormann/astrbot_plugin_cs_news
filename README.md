# astrbot_plugin_hltv_news

HLTV（CS2 电竞）新闻定时播报插件。定时轮询 [HLTV 官方 RSS](https://www.hltv.org/rss/news)，抓取最新新闻，推送到指定 QQ 群。

## 功能特性

- 每隔可配置时间轮询 HLTV RSS（默认 30 分钟，最小 5 分钟）
- 基于 RSS 稳定条目 ID 去重，只推送**先前未抓取过**的新闻
- 单轮最多推送 3 条（可配置），避免一次性刷屏
- 用 LLM **先概括后翻译**：中文标题 + ≤50 汉字中文概括
  - 严格保留选手 ID / 昵称 / 真名 / 战队名 / 赛事名等英文专有名词，不强行翻译
- 推送内容：**头图 + 中文标题 + 发布时间（北京时间）+ 中文概括 + 原文链接**，QQ 群友好排版（emoji 点缀、不用 Markdown）
- LLM 调用走独立通道，**不影响 / 不污染主对话上下文**
- LLM 不可用或连续失败达阈值时**直接报错停用**（默认连续 3 次失败停用）
- 首次启用立即推送当前最新一条

## 安装

1. 将本仓库克隆/放置到 AstrBot 的 `data/plugins/` 目录（或在插件市场安装）。
2. 在 AstrBot WebUI「插件管理」中启用本插件，并点击「配置」填写参数。
3. 重载插件生效。

> 依赖 `feedparser`、`httpx`，见 `requirements.txt`。

## 配置项

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `poll_interval_minutes` | int | 30 | 轮询间隔（分钟），最小 5 |
| `target_sessions` | list | `[]` | 推送目标群。推荐每项直接填**纯 QQ 群号**（如 `123456`），插件按「显式 `platform_id` > 事件学习 > 自动探测」补全平台段，**不绑定任何平台名**；也可填 `GroupMessage:群号` 或缺平台段的写法，或**完整会话 ID**（如 `Iris:GroupMessage:123456`）。可填多个，同时推送 |
| `platform_id` | string | `""` | 推送平台实例 ID（可空）。留空即可——在目标群执行一次 `/hltv` 会自动学习真实平台 ID；多平台机器可显式指定（如 `Iris`）。通用环境无需设置 |
| `llm_provider_id` | string | 空 | 用于概括/翻译的 LLM 提供商；留空自动使用默认对话模型 |
| `max_push_per_cycle` | int | 3 | 单轮最多推送条数（1~10） |
| `summary_max_chars` | int | 50 | 中文概括字数上限（尽可能遵守，不绝对强制） |
| `show_publish_time` | bool | true | 是否显示发布时间 |
| `enable_header_image` | bool | true | 是否推送新闻头图 |
| `user_agent` | string | `Mozilla/5.0 ...` | 抓取 RSS 的 UA。HLTV 对部分 UA 返回 Cloudflare 挑战页，异常时调整 |
| `fetch_timeout` | int | 25 | RSS 请求超时（秒） |
| `llm_fail_threshold` | int | 3 | LLM 连续失败停用阈值 |
| `data_dir` | string | `/opt/AstrBot/data` | 去重记录等持久化数据目录（勿放插件自身目录，避免更新被覆盖） |

## 手动指令

| 指令 | 说明 |
|---|---|
| `/hltv push` | 立即执行一次轮询并推送（便于调试/手动触发） |
| `/hltv status` | 查看插件运行状态、LLM 提供商、推送目标、累计推送数 |

## 数据与去重

- 去重记录保存在 `data_dir/hltv_news_state.json`，最多保留 300 条 ID，防止文件膨胀。
- 推送成功才记录 ID；LLM 概括失败/推送失败不记录，下轮自动重试。

## 原理

- 抓取：`httpx` 异步请求 `https://www.hltv.org/rss/news`
- 解析：`feedparser`
- 概括/翻译：`context.llm_generate()` 直接调用 LLM provider，不经过会话管理器，不写入主对话历史
- 推送：`context.send_message()` 主动向目标群发送「头图 + 文本」消息链

## 开发

- 遵循 AstrBot 插件开发规范（`Star` 基类 + `metadata.yaml` + `_conf_schema.json`）。
- 代码用 ruff 格式化。
- 仓库采用标准插件目录结构，可直接推送到 GitHub 供插件市场分发。

## License

MIT
