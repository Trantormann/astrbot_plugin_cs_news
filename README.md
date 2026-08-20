# astrbot_plugin_hltv_news

AstrBot 插件：定时轮询 **HLTV.org**（CS2 电竞）官方 RSS，把最新新闻自动推送到指定 QQ 群。

## 功能

- 可配置轮询间隔（默认 30 分钟，最小 5 分钟），自动抓取 HLTV 官方 RSS
- 基于 RSS 稳定唯一 ID 去重，只推送**先前未推送过**的新闻
- 单轮最多推送 3 条（可配置），优先最新
- 使用 LLM **先概括后翻译**：中文标题 + 简短中文概括（默认 ≤50 字）
  - 保留选手 ID / 真名 / 队名 / 赛事名（如 `s1mple`、`MOUZ`、`EWC`），不强行翻译
- LLM 调用直接走 provider，**不经过会话管理器、不污染主对话上下文**
- 推送格式：头图 + 标题 + 发布时间（北京时间）+ 概括 + 原文链接，QQ 群友好排版（无 Markdown）
- LLM 不可用时插件**直接报错停用**；运行中连续失败达阈值也会自动停用

## 安装

1. 将本仓库克隆到 AstrBot 的 `data/plugins/` 目录：
   ```bash
   git clone <本仓库地址> /opt/AstrBot/data/plugins/astrbot_plugin_hltv_news
   ```
2. 安装依赖（AstrBot 通常自动安装，也可手动）：
   ```bash
   pip install -r requirements.txt
   ```
3. 在 AstrBot WebUI「插件管理」中重载/启用插件。

## 配置（WebUI 插件设置）

| 配置项 | 说明 | 默认 |
|---|---|---|
| `poll_interval_minutes` | 轮询间隔（分钟），最小 5 | 30 |
| `target_sessions` | 推送目标群，每项填纯群号或完整会话 ID（`aiocqhttp:GroupMessage:群号`），可多个 | 空 |
| `llm_provider_id` | 概括/翻译用的 LLM 提供商，留空用默认对话模型 | 空 |
| `max_push_per_cycle` | 单轮最多推送条数 | 3 |
| `summary_max_chars` | 中文概括字数上限（汉字） | 50 |
| `show_publish_time` | 是否显示发布时间 | true |
| `enable_header_image` | 是否推送新闻头图 | true |
| `user_agent` | 抓取 RSS 的 UA（HLTV 反爬敏感） | 简单 UA |
| `fetch_timeout` | RSS 请求超时（秒） | 25 |
| `llm_fail_threshold` | LLM 连续失败停用阈值 | 3 |
| `data_dir` | 去重记录持久化目录 | `/opt/AstrBot/data` |

> 首次启用会**立即推送当下最新一条**新闻；之后仅推送新发布的。

## 数据

去重记录保存在 `{data_dir}/hltv_news_pushed_ids.json`，记录最近 300 条已推送新闻 ID（自动裁剪防膨胀）。

## 说明

- 仅支持 `aiocqhttp`（QQ）平台
- 网络请求使用 `httpx`（异步），依赖 `feedparser` 解析 RSS
- HLTV 对部分 User-Agent 返回 Cloudflare 挑战页，默认简单 UA 已验证可用；如遇异常可在配置中调整
