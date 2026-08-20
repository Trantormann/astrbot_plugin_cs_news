"""astrbot_plugin_hltv_news

定时轮询 HLTV.org 官方 RSS，将最新未推送的 CS2 电竞新闻推送到指定群聊。

- 稳定 RSS：https://www.hltv.org/rss/news（简单 UA，规避 Cloudflare 挑战）
- 去重：基于 RSS 的稳定唯一 id（hltvnewsXXXXX），持久化到 data 目录
- 摘要：LLM 先概括后翻译，中文标题 + 简短中文概括，保留选手 ID / 队名 / 人名
- 隔离：直接调用 LLM provider 的 text_chat，不经过会话管理器，不污染主对话上下文
- 推送：头图 + 标题 + 发布时间 + 概括 + 原文链接（QQ 群友好排版，无 Markdown）
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx

from astrbot.api import logger
from astrbot.api.all import Context, Image, MessageChain, Plain
from astrbot.api.star import Star

RSS_URL = "https://www.hltv.org/rss/news"
DEDUP_FILE = "hltv_news_pushed_ids.json"
MAX_RECORD = 300  # 去重记录条数上限，防止文件无限膨胀

SYSTEM_PROMPT = """你是 HLTV（CS2 电竞新闻站）的中文播报助手，任务是把一条英文 HLTV 新闻转成中文播报信息。

严格按以下要求执行：
1. 把新闻标题翻译成自然流畅的中文标题。
2. 用一句话中文概括新闻的核心内容。
3. 必须保留原文中出现的选手 ID、选手真名、战队名、赛事名（如 s1mple、ZywOo、MOUZ、FURIA、Aurora、EWC、IEM 等），不要翻译或音译它们。
4. 概括尽可能简短，控制在 {max_chars} 个汉字以内（可以少于，不要多于）。
5. 只输出一个 JSON 对象，格式为：{{"title_zh": "翻译后的标题", "summary_zh": "中文概括"}}
不要输出任何解释、前后缀或多余内容。"""


class HltvNewsPlugin(Star):
    """HLTV 新闻定时播报插件。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}

        data_dir = Path(str(self.config.get("data_dir", "/opt/AstrBot/data")))
        self.dedup_path = data_dir / DEDUP_FILE

        self.interval_minutes = max(5, int(self.config.get("poll_interval_minutes", 30)))
        self.max_push = max(1, min(10, int(self.config.get("max_push_per_cycle", 3))))
        self.max_chars = int(self.config.get("summary_max_chars", 50))
        self.show_time = bool(self.config.get("show_publish_time", True))
        self.enable_image = bool(self.config.get("enable_header_image", True))
        self.ua = str(
            self.config.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        )
        self.timeout = max(5, int(self.config.get("fetch_timeout", 25)))
        self.fail_threshold = max(1, int(self.config.get("llm_fail_threshold", 3)))
        self.provider_id = str(self.config.get("llm_provider_id", "")).strip()

        self.target_sessions = self._parse_targets(self.config.get("target_sessions", []))

        self._task = None
        self._running = False
        self._provider = None
        self._llm_fail_streak = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self):
        """AstrBot 加载插件后调用。校验 LLM 与目标配置，启动轮询任务。"""
        # 1. 解析 LLM provider
        try:
            if self.provider_id:
                prov = self.context.get_provider_by_id(self.provider_id)
                if prov is None:
                    raise RuntimeError(f"配置的 LLM 提供商不存在: {self.provider_id}")
            else:
                prov = await self.context.get_using_provider_async(None)
            if prov is None:
                raise RuntimeError("当前没有可用的对话模型提供商")
            # 探测可用性
            await prov.test()
            self._provider = prov
        except Exception as e:
            logger.error(f"[hltv_news] LLM 不可用，插件停止运行: {e}")
            return

        # 2. 校验目标
        if not self.target_sessions:
            logger.error("[hltv_news] 未配置推送目标群，插件停止运行")
            return

        # 3. 启动轮询
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[hltv_news] 已启动：每 {self.interval_minutes} 分钟轮询一次，"
            f"目标群 {len(self.target_sessions)} 个，provider={self._provider.meta().id}"
        )
        await self._poll_once()  # 启动即先跑一轮（首次会立即推送最新一条）

    async def terminate(self):
        """插件卸载/停用时调用。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("[hltv_news] 已停止")

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    def _parse_targets(self, raw) -> list[str]:
        """支持纯群号或完整会话 ID；缺省平台固定为 aiocqhttp 群会话。"""
        targets = []
        if isinstance(raw, str):
            raw = [raw]
        for item in raw or []:
            item = str(item).strip()
            if not item:
                continue
            if ":" in item:
                targets.append(item)
            else:
                targets.append(f"aiocqhttp:GroupMessage:{item}")
        return targets

    # ------------------------------------------------------------------
    # 定时循环
    # ------------------------------------------------------------------
    async def _loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval_minutes * 60)
                if not self._running:
                    break
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[hltv_news] 轮询异常: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 核心轮询
    # ------------------------------------------------------------------
    async def _poll_once(self):
        xml = await self._fetch_rss()
        feed = feedparser.parse(xml)
        entries = feed.entries
        if not entries:
            logger.debug("[hltv_news] RSS 无条目")
            return

        pushed = self._load_pushed()
        fresh = [e for e in entries if str(e.get("id", "")).strip() not in pushed]
        if not fresh:
            logger.debug("[hltv_news] 没有新新闻")
            return

        # 按发布时间倒序（最新在前），只取本轮需要推送的数量
        fresh.sort(key=lambda e: e.get("published_parsed") or 0, reverse=True)
        selected = fresh[: self.max_push]
        logger.info(f"[hltv_news] 本轮待推送 {len(selected)} 条: "
                    + ", ".join(str(e.get("id")) for e in selected))

        for e in selected:
            await self._process_item(e, pushed)
            if not self._running:  # LLM 连续失败触发停用
                return

    async def _process_item(self, entry, pushed: set):
        item_id = str(entry.get("id", "")).strip()
        result = await self._llm_summarize(entry)
        if result is None:
            self._llm_fail_streak += 1
            if self._llm_fail_streak >= self.fail_threshold:
                logger.error(
                    f"[hltv_news] LLM 连续失败 {self._llm_fail_streak} 次，插件自动停用"
                )
                self._running = False
                return
            logger.warning(
                f"[hltv_news] 概括失败（连续 {self._llm_fail_streak} 次），本条跳过待下轮重试"
            )
            return

        self._llm_fail_streak = 0
        chain = self._build_chain(entry, result["title_zh"], result["summary_zh"])

        ok = True
        for session in self.target_sessions:
            try:
                sent = await self.context.send_message(session, chain)
                if not sent:
                    logger.warning(f"[hltv_news] 推送失败（找不到平台/会话）: {session}")
                    ok = False
            except Exception as e:
                logger.error(f"[hltv_news] 推送到 {session} 异常: {e}")
                ok = False

        if ok:
            pushed.add(item_id)
            self._save_pushed(pushed)
            logger.info(f"[hltv_news] 已推送并记录: {item_id}")

    # ------------------------------------------------------------------
    # RSS 抓取 / 解析
    # ------------------------------------------------------------------
    async def _fetch_rss(self) -> str:
        headers = {"User-Agent": self.ua, "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout, headers=headers, http2=False
        ) as client:
            r = await client.get(RSS_URL)
            if r.status_code != 200 or "<channel>" not in r.text[:600]:
                raise RuntimeError(
                    f"RSS 抓取异常 status={r.status_code}（可能被 Cloudflare 挑战拦截）"
                )
            return r.text

    @staticmethod
    def _get_cover(entry) -> str | None:
        mc = entry.get("media_content")
        if isinstance(mc, list) and mc and mc[0].get("url"):
            return mc[0]["url"]
        return None

    @staticmethod
    def _fmt_time(entry) -> str:
        t = entry.get("published_parsed")
        if not t:
            return ""
        dt = datetime(*t[:6], tzinfo=timezone.utc) + timedelta(hours=8)  # 北京时间
        return dt.strftime("%Y-%m-%d %H:%M")

    # ------------------------------------------------------------------
    # LLM 概括翻译（直接调 provider，不经会话管理器，不污染主对话）
    # ------------------------------------------------------------------
    async def _llm_summarize(self, entry) -> dict | None:
        title = str(entry.get("title", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        prompt = f"HLTV 新闻标题：{title}\n\n新闻简介：{summary}\n\n请按规则输出中文播报 JSON。"
        try:
            resp = await self._provider.text_chat(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT.format(max_chars=self.max_chars),
            )
            text = resp.completion_text if resp else ""
            parsed = self._parse_llm_json(text)
            if parsed:
                logger.info(
                    f"[hltv_news] 概括完成: {parsed['title_zh']} | {parsed['summary_zh']}"
                )
            return parsed
        except Exception as e:
            logger.warning(f"[hltv_news] LLM 调用异常: {e}")
            return None

    @staticmethod
    def _parse_llm_json(text: str) -> dict | None:
        if not text:
            return None
        text = text.strip()
        # 去掉可能的 ```json ... ``` 围栏
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
        if m:
            text = m.group(1).strip()
        obj = None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            m2 = re.search(r"\{.*\}", text, re.S)
            if m2:
                try:
                    obj = json.loads(m2.group(0))
                except json.JSONDecodeError:
                    obj = None
        if isinstance(obj, dict) and obj.get("title_zh") and obj.get("summary_zh"):
            return {"title_zh": str(obj["title_zh"]).strip(),
                    "summary_zh": str(obj["summary_zh"]).strip()}
        return None

    # ------------------------------------------------------------------
    # 消息构建 / 发送
    # ------------------------------------------------------------------
    def _build_chain(self, entry, title_zh: str, summary_zh: str) -> MessageChain:
        comps = []
        cover = self._get_cover(entry)
        if self.enable_image and cover:
            comps.append(Image(file=cover))

        lines = ["🏆【HLTV 播报】", f"📰 {title_zh}"]
        if self.show_time:
            lines.append(f"🕒 {self._fmt_time(entry)}（北京时间）")
        lines.append(f"📝 {summary_zh}")
        lines.append(f"🔗 {str(entry.get('link', ''))}")
        comps.append(Plain(text="\n".join(lines)))
        return MessageChain(chain=comps)

    # ------------------------------------------------------------------
    # 去重持久化
    # ------------------------------------------------------------------
    def _load_pushed(self) -> set:
        try:
            if self.dedup_path.exists():
                data = json.loads(self.dedup_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return set(str(x) for x in data)
        except Exception as e:
            logger.warning(f"[hltv_news] 读取去重记录失败: {e}")
        return set()

    def _save_pushed(self, pushed: set):
        try:
            limited = set(list(pushed)[-MAX_RECORD:])
            self.dedup_path.write_text(
                json.dumps(sorted(limited), ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"[hltv_news] 保存去重记录失败: {e}")
