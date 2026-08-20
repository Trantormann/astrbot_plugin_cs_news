# astrbot_plugin_cs_news

**CS 赛事新闻助手** —— CS2（CSGO）电竞新闻与今日赛事定时播报插件。数据源为 [5eplay（5EPLAY）中文站](https://csgo.5eplay.com)，定时轮询新闻接口抓取最新中文新闻，并在推送新闻时自动附上今日（北京时间）S/A 级赛事预告，推送到指定 QQ 群。

## 功能特性

- 每隔可配置时间轮询 5eplay 新闻 API（默认 30 分钟，最小 5 分钟）
- 基于稳定文章链接去重，只推送**先前未抓取过**的新闻
- 单轮最多推送 3 条（可配置），避免一次性刷屏
- 用 LLM 基于 5eplay 官方摘要（meta description）生成 **≤50 汉字一句话中文概括**，信息密集、不复述标题；抓取详情失败时降级为仅基于标题概括
- **自动过滤 5E 平台广告/推广**：详情页无 `CSGO新闻:` 官方摘要（如商城联名装扮、平台活动）的条目直接跳过，不计入推送也不重复请求
  - 严格保留选手 ID / 昵称 / 真名 / 战队名 / 赛事名等专有名词，不改写
- **推送新闻时自动附带今日赛事预告**：按 5eplay 数字等级筛选（默认 `["1","2"]` 即 S 级），按赛事名分组排版，每组为 `[赛事名]` 标题 + 每场 `对阵 时间` 一行
  - 数字等级可在配置中自行调整（如 `["1","2","3"]` = S+A 级）
  - 今日无符合等级的比赛时自动省略赛事部分
- 推送内容：**头图 + 中文标题 + 中文概括 + 今日赛事**，QQ 群友好排版（emoji 点缀、不用 Markdown）
  - 头图来自 5eplay 图床（oss.5eplay.com），可直接下载，默认开启；若某张图不可达会自动降级为纯文字，不影响正文
- LLM 调用走独立通道，**不影响 / 不污染主对话上下文**
- LLM 不可用或连续失败达阈值时**直接报错停用**（默认连续 3 次失败停用）
- 首次启用立即推送当前最新新闻

## 安装

1. 将本仓库克隆/放置到 AstrBot 的 `data/plugins/` 目录（或在插件市场安装）。
2. 在 AstrBot WebUI「插件管理」中启用本插件，并点击「配置」填写参数。
3. 重载插件生效。

> 依赖 `httpx`，见 `requirements.txt`。

## 配置项

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `poll_interval_minutes` | int | 30 | 轮询间隔（分钟），最小 5 |
| `target_sessions` | list | `[]` | 推送目标群。推荐每项直接填**纯 QQ 群号**（如 `123456`），插件按「显式 `platform_id` > 事件学习 > 自动探测」补全平台段，**不绑定任何平台名**；也可填 `GroupMessage:群号` 或缺平台段的写法，或**完整会话 ID**（如 `MyBot:GroupMessage:123456`）。可填多个，同时推送 |
| `platform_id` | string | `""` | 推送平台实例 ID（可空）。留空即可——在目标群执行一次 `/csnews` 会自动学习真实平台 ID；多平台机器可显式指定（如 `MyBot`）。通用环境无需设置 |
| `llm_provider_id` | string | 空 | 用于概括新闻的 LLM 提供商；留空自动使用默认对话模型 |
| `max_push_per_cycle` | int | 3 | 单轮最多推送条数（1~10） |
| `summary_max_chars` | int | 50 | 中文概括字数上限（尽可能遵守，不绝对强制） |
| `enable_header_image` | bool | `true` | 是否附带头图。5eplay 图床可直接下载；若某张图不可达会自动降级为纯文字推送，不影响正文 |
| `match_grades` | list | `["1","2"]` | 附推赛事等级（5eplay 数字分级）。分级：1/2=S级、3=A级、4=B级、5=C级。默认只推 S 级；要 S+A 级改成 `["1","2","3"]` |
| `enable_match_push` | bool | `true` | 新闻推送时是否附带今日赛事。关闭则只推新闻 |
| `user_agent` | string | `Mozilla/5.0 ...` | 抓取数据用的 UA。5eplay 接口对普通 UA 友好，保持默认即可 |
| `fetch_timeout` | int | 25 | 数据请求超时（秒） |
| `llm_fail_threshold` | int | 3 | LLM 连续失败停用阈值 |
| `data_dir` | string | `/opt/AstrBot/data` | 去重记录等持久化数据目录（勿放插件自身目录，避免更新被覆盖） |

## 手动指令

| 指令 | 说明 |
|---|---|
| `/csnews push` | 立即执行一次轮询并推送（便于调试/手动触发） |
| `/csnews status` | 查看插件运行状态、LLM 提供商、推送目标、赛事等级筛选、累计推送数 |
| `/hltv push` / `/hltv status` | 旧指令别名，功能同上 |

## 数据与去重

- 去重记录保存在 `data_dir/cs_news_state.json`，最多保留 300 条 ID，防止文件膨胀；首次升级会自动迁移旧版 `hltv_news_state.json` 的去重记录。
- 推送成功才记录 ID；LLM 概括失败/推送失败不记录，下轮自动重试。

## 数据源

- 新闻：`https://csgo.5eplay.com/api/article`（中文标题、`oss.5eplay.com` 图床）
- 赛事：`https://app.5eplay.com/api/tournament/session_list`（含 `mc_info.grade` 数字等级，如 2=S级、5=C级）
- 概括：`context.llm_generate()` 直接调用 LLM provider，不经过会话管理器，不写入主对话历史
- 推送：`context.send_message()` 主动向目标群发送「文本（可选附带已可达头图）」消息链

## 开发

- 遵循 AstrBot 插件开发规范（`Star` 基类 + `metadata.yaml` + `_conf_schema.json`）。
- 数据源独立在 `fiveplay_api.py`，便于单独测试与扩展。
- 代码用 ruff 格式化。
- 仓库采用标准插件目录结构，可直接推送到 GitHub 供插件市场分发。

## License

MIT
