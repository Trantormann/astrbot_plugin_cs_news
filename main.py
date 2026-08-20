"""
astrbot_plugin_hltv_news
========================
HLTV（CS2 电竞）新闻定时播报插件。

核心能力：
- 每隔可配置的时间轮询 HLTV 官方 RSS（hltv.org/rss/news），抓取最新新闻。
- 基于 RSS 稳定条目 ID 去重，只推送"先前未抓取过"的新闻；
  单轮最多推送 max_push_per_cycle 条（默认 3 条）。
- 用 LLM 先概括后翻译：中文标题 + ≤50 汉字中文概括，
  并严格保留选手 ID / 真名 / 战队名 / 赛事名等专有名词不翻译。
- 推送内容：头图 + 中文标题 + 发布时间（北京时间）+ 中文概括 + 原文链接，
  QQ 群友好排版（emoji 点缀、不用 Markdown）。
- LLM 调用走 context.llm_generate()，不经过主对话会话管理器，
  **不影响 / 不污染主对话上下文**。
- LLM 不可用或连续失败达到阈值时，直接报错并停用插件。
"""

import asyncio
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import httpx
from astrbot.api.all import MessageChain

import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

RSS_URL = "https://www.hltv.org/rss/news"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
MAX_RECORD = 300  # 去重记录最多保留的 ID 数（防膨胀）
CST = timezone(timedelta(hours=8))  # 北京时间

SYSTEM_PROMPT_TMPL = """你是 HLTV（CS2 电竞新闻站）的中文播报助手。请把给定的英文新闻标题与简介，处理成一条面向中文读者的一句话播报。

要求：
1. 将标题翻译成通顺、地道的简体中文。
2. 基于标题和简介，先理解新闻内容，再用一句中文（不超过 {max_chars} 个汉字）概括新闻核心。
3. 【最重要】必须原样保留新闻中出现的选手 ID、昵称、真名、战队名、赛事名、平台名等英文专有名词，绝不强行翻译或音译成中文。例如 s1mple、ZywOo、m0NESY、MOUZ、FURIA、Natus Vincere、G2、Esports World Cup 等保持英文原名。
4. 只输出一个 JSON 对象，不要任何解释文字，不要 markdown 代码块标记。格式如下：
{{"title_zh": "翻译后的标题", "summary_zh": "中文概括"}}"""


class HltvNewsPlugin(Star):
    """HLTV 新闻定时播报插件"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}

        # ---- 配置读取（带兜底，不假设字段必然存在）----
        self.interval_min = self._clamp_int("poll_interval_minutes", 30, 5, 1440)
        self.max_push = self._clamp_int("max_push_per_cycle", 3, 1, 10)
        self.summary_chars = self._clamp_int("summary_max_chars", 50, 10, 200)
        self.show_time = bool(self.config.get("show_publish_time", True))
        self.enable_img = bool(self.config.get("enable_header_image", False))
        self.ua = str(self.config.get("user_agent", DEFAULT_UA))
        self.timeout = self._clamp_int("fetch_timeout", 25, 5, 120)
        self.fail_threshold = self._clamp_int("llm_fail_threshold", 3, 1, 20)
        self.provider_id = str(self.config.get("llm_provider_id", "")).strip()

        # 推送目标：支持纯群号 或 完整会话 ID
        targets = self.config.get("target_sessions") or []
        if isinstance(targets, str):
            targets = [targets]
        self.targets = [str(t).strip() for t in targets if str(t).strip()]

        # ---- 持久化（放 data 目录，避免插件更新被覆盖）----
        data_dir = Path(str(self.config.get("data_dir", "/opt/AstrBot/data")))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = data_dir / "hltv_news_state.json"
        self._seen_ids: set[str] = set()
        self._load_state()

        # ---- 运行时状态 ----
        self._task: asyncio.Task | None = None
        self._running = False
        self._provider_id_resolved = ""
        self._consecutive_llm_fail = 0
        self._pushed_total = 0
        self._platform_id = ""  # 运行时解析到的实际平台实例 ID（如 MyBot）：
        #    优先级：配置 platform_id > 事件学习(unified_msg_origin) > 自动探测 platform_insts
        self.config_platform_id = str(self.config.get("platform_id", "") or "").strip()

    # ---------------- 生命周期 ----------------

    async def initialize(self):
        """AstrBot 加载插件后调用。检查 LLM 可用性并启动后台轮询。"""
        prov = await self._get_provider()
        if prov is None:
            logger.error(
                "[hltv_news] 未找到可用的 LLM 提供商，插件停用。"
                "请在插件配置中设置 llm_provider_id，或确认默认对话模型可用。"
            )
            self._running = False
            return
        self._provider_id_resolved = prov.meta().id
        self._platform_id = self._detect_platform_id()
        if not self._platform_id:
            logger.warning(
                "[hltv_news] 未检测到可用平台实例 ID，纯群号将回退为 aiocqhttp"
            )
        if not self.targets:
            logger.warning(
                "[hltv_news] 未配置推送目标群（target_sessions），本轮播报将只记录日志、不推送。"
            )

        logger.info(
            "[hltv_news] 已启动：轮询间隔 %s 分钟，LLM provider=%s，推送目标=%s，单轮上限 %s 条",
            self.interval_min,
            self._provider_id_resolved,
            self.targets,
            self.max_push,
        )
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def terminate(self):
        """AstrBot 卸载/停用插件时调用。"""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("[hltv_news] 已停止")

    # ---------------- 后台轮询循环 ----------------

    async def _loop(self):
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[hltv_news] 轮询异常: %s", e, exc_info=True)
            try:
                await asyncio.sleep(self.interval_min * 60)
            except asyncio.CancelledError:
                break

    async def _poll_once(self) -> int:
        """执行一次轮询，返回本轮实际推送条数。"""
        xml_text = await self._fetch_rss()
        items = self._parse_rss(xml_text)
        if not items:
            logger.info("[hltv_news] RSS 无可用条目")
            return 0

        # 按发布时间倒序（最新的在前）
        items.sort(
            key=lambda x: x["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        pending = [it for it in items if it["id"] not in self._seen_ids]
        if not pending:
            logger.debug("[hltv_news] 暂无未推送的新新闻")
            return 0

        logger.info(
            "[hltv_news] 发现 %s 条未推送新闻，本轮最多推送 %s 条",
            len(pending),
            self.max_push,
        )
        pushed = 0
        for it in pending[: self.max_push]:
            if not self._running:
                break
            # 1) LLM 概括/翻译（失败不计入已推送，下轮可重试）
            try:
                title_zh, summary_zh = await self._summarize(it["title"], it["summary"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._consecutive_llm_fail += 1
                logger.error("[hltv_news] 新闻 %s 的 LLM 概括失败: %s", it["id"], e)
                if self._consecutive_llm_fail >= self.fail_threshold:
                    logger.error(
                        "[hltv_news] LLM 连续失败 %s 次，插件停用。",
                        self.fail_threshold,
                    )
                    self._running = False
                continue
            self._consecutive_llm_fail = 0

            # 2) 推送（任一目标失败则不记 ID，下轮自动重试）
            try:
                ok = await self._push(it, title_zh, summary_zh)
                if not ok:
                    logger.warning(
                        "[hltv_news] 新闻 %s 推送未全部成功，本轮不计入已推送，下轮重试",
                        it["id"],
                    )
                    continue
                self._seen_ids.add(it["id"])
                self._pushed_total += 1
                self._save_state()
                pushed += 1
                logger.info("[hltv_news] 已推送: %s | %s", it["id"], title_zh)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "[hltv_news] 推送新闻 %s 失败: %s", it["id"], e, exc_info=True
                )

        return pushed

    # ---------------- RSS 抓取与解析 ----------------

    async def _fetch_rss(self) -> str:
        """抓取 HLTV RSS，检测 Cloudflare 挑战页。"""
        headers = {
            "User-Agent": self.ua,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout, headers=headers
        ) as client:
            r = await client.get(RSS_URL)
        if r.status_code != 200:
            raise RuntimeError(f"RSS 请求失败，HTTP {r.status_code}")
        head = r.text[:1000].lower()
        if "just a moment" in head or "<rss" not in head:
            raise RuntimeError(
                "RSS 返回了 Cloudflare 挑战页或非 RSS 内容，请检查 User-Agent 配置"
            )
        return r.text

    def _parse_rss(self, xml_text: str) -> list[dict[str, Any]]:
        """解析 RSS 为条目列表。"""
        d = feedparser.parse(xml_text)
        items = []
        for e in d.entries:
            item_id = (e.get("id") or e.get("link") or "").strip()
            if not item_id:
                continue
            media = e.get("media_content") or []
            img = media[0].get("url") if media else None
            pub_dt = None
            if e.get("published_parsed"):
                pub_dt = datetime(*e["published_parsed"][:6], tzinfo=timezone.utc)
            items.append(
                {
                    "id": item_id,
                    "title": html.unescape(e.get("title", "") or "").strip(),
                    "link": e.get("link", ""),
                    "summary": html.unescape(e.get("summary", "") or "").strip(),
                    "image": img,
                    "pub_dt": pub_dt,
                }
            )
        return items

    # ---------------- LLM 概括/翻译 ----------------

    async def _get_provider(self):
        """获取要使用的对话 provider。配置了 llm_provider_id 则用之，否则用默认对话模型。"""
        if self.provider_id:
            prov = self.context.get_provider_by_id(self.provider_id)
            if prov is None:
                logger.error(
                    "[hltv_news] 配置的 LLM 提供商 %s 不存在", self.provider_id
                )
            return prov
        return await self.context.get_using_provider_async()

    async def _summarize(self, title: str, summary: str) -> tuple[str, str]:
        """调用 LLM：先概括后翻译，返回 (中文标题, 中文概括)。

        通过 context.llm_generate() 直接调用 provider，不经过会话管理器，
        不会写入主对话历史，因此不影响主对话上下文。
        """
        system_prompt = SYSTEM_PROMPT_TMPL.format(max_chars=self.summary_chars)
        user_prompt = (
            f"新闻标题（英文）：{title}\n"
            f"新闻简介（英文）：{summary}\n"
            "请按系统要求输出 JSON。"
        )
        resp = await self.context.llm_generate(
            chat_provider_id=self._provider_id_resolved,
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
        text = (resp.completion_text or "").strip()
        data = self._parse_llm_json(text)
        if not data or not data.get("title_zh") or not data.get("summary_zh"):
            raise ValueError(f"LLM 输出无法解析为预期 JSON: {text[:300]}")
        return data["title_zh"].strip(), data["summary_zh"].strip()

    @staticmethod
    def _parse_llm_json(text: str) -> dict | None:
        """容错解析 LLM 返回的 JSON（去除 markdown 代码块标记等）。"""
        if not text:
            return None
        cleaned = re.sub(r"```(?:json)?", "", text).strip()
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

    # ---------------- 推送 ----------------

    async def _image_reachable(self, url: str) -> bool:
        """快速探测图片 URL 是否真实可下载。
        HLTV 图片 CDN 受 Cloudflare 挑战保护，普通请求常返回 403；
        探测失败时推送自动降级为纯文字，避免整条新闻发送失败。"""
        try:
            async with httpx.AsyncClient(
                timeout=8,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_UA},
            ) as c:
                r = await c.get(url)
            ct = r.headers.get("content-type", "")
            return r.status_code == 200 and (
                ct.startswith("image/") or len(r.content) > 1000
            )
        except Exception:
            return False

    async def _push(self, item: dict, title_zh: str, summary_zh: str) -> bool:
        """组装消息链并推送到所有目标群。全部成功返回 True，任一目标失败返回 False。"""
        comps = []
        img_ok = False
        if self.enable_img and item.get("image"):
            img_ok = await self._image_reachable(item["image"])
            if img_ok:
                comps.append(Image(file=item["image"]))
            else:
                logger.warning(
                    "[hltv_news] 头图不可达（HLTV CDN 可能有反爬拦截），自动降级为纯文字推送"
                )

        lines = ["【HLTV 新闻播报】📰", f"🏷 {title_zh}"]
        if self.show_time and item.get("pub_dt"):
            local = item["pub_dt"].astimezone(CST)
            lines.append(f"🕒 {local.strftime('%Y-%m-%d %H:%M')}（北京时间）")
        lines.append(f"📝 {summary_zh}")
        lines.append(f"🔗 {item['link']}")
        comps.append(Plain(text="\n".join(lines)))

        chain = MessageChain(chain=comps)
        all_ok = True
        for target in self.targets:
            session = self._normalize_session(target)
            try:
                ok = await self.context.send_message(session, chain)
            except Exception as e:
                ok = False
                logger.error(
                    "[hltv_news] 推送异常 session=%s: %s", session, e, exc_info=True
                )
            if not ok:
                all_ok = False
                logger.warning(
                    "[hltv_news] 无法匹配平台，消息未发送：目标=%s session=%s "
                    "（检测到平台ID=%s，请确认 target_sessions 填纯群号即可，"
                    "或填完整格式 <平台ID>:GroupMessage:<群号>）",
                    target,
                    session,
                    self._platform_id or "(未检测到)",
                )
        return all_ok

    def _normalize_session(self, target: str) -> str:
        """纯数字群号 → 用实际平台 ID 补全为群会话；
        缺平台段的 'GroupMessage:xxx' → 同样补全；
        其余按原样（视为完整会话 ID）。"""
        target = target.strip()
        pid = self.config_platform_id or self._platform_id or "aiocqhttp"
        if target.isdigit():
            return f"{pid}:GroupMessage:{target}"
        if target.startswith("GroupMessage:"):
            return f"{pid}:{target}"
        return target

    def _detect_platform_id(self) -> str:
        """探测实际平台实例 ID（platform.meta().id，如 'MyBot'），供拼接会话使用。
        优先 aiocqhttp 类型实例；没有则取第一个可用平台实例。"""
        try:
            insts = self.context.platform_manager.platform_insts
        except Exception:
            return ""
        for inst in insts:
            try:
                meta = inst.meta()
            except Exception:
                continue
            if meta.name == "aiocqhttp" and meta.id:
                return meta.id
        for inst in insts:
            try:
                meta = inst.meta()
            except Exception:
                continue
            if meta.id:
                return meta.id
        return ""

    # ---------------- 持久化 ----------------

    def _load_state(self):
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self._seen_ids = set(data.get("seen_ids", []))
                self._pushed_total = int(data.get("pushed_total", 0))
        except Exception as e:
            logger.warning("[hltv_news] 读取去重记录失败: %s", e)

    def _save_state(self):
        try:
            ids = list(self._seen_ids)[-MAX_RECORD:]
            payload = {"seen_ids": ids, "pushed_total": self._pushed_total}
            self.state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.error("[hltv_news] 保存去重记录失败: %s", e)

    # ---------------- 辅助 ----------------

    def _clamp_int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    # ---------------- 手动指令（便于调试/手动触发） ----------------

    @filter.command("hltv")
    async def hltv(self, event: AstrMessageEvent):
        """HLTV 新闻播报管理：/hltv push 立即轮询推送；/hltv status 查看状态。"""
        # 事件学习：从本条真实事件（unified_msg_origin 形如 "MyBot:GroupMessage:群号"）
        # 提取该群所在平台的实例 ID，优先于自动探测，保证纯群号补全准确。
        origin = getattr(event, "unified_msg_origin", "") or ""
        if ":" in origin:
            learned = origin.split(":", 1)[0]
            if learned and learned != self._platform_id:
                self._platform_id = learned
                logger.info("[hltv_news] 已从事件学习平台 ID: %s", learned)
        args = (event.message_str or "").strip().split()
        action = args[1].lower() if len(args) > 1 else "status"
        if action == "push":
            if not self._running:
                yield event.plain_result(
                    "插件当前未运行（可能 LLM 不可用已停用），请先检查日志。"
                )
                return
            try:
                n = await self._poll_once()
                yield event.plain_result(f"轮询完成，本轮推送 {n} 条。")
            except Exception as e:
                yield event.plain_result(f"轮询出错：{e}")
            return
        if action == "status":
            state = "运行中" if self._running else "已停用"
            lines = [
                f"状态：{state}",
                f"轮询间隔：{self.interval_min} 分钟",
                f"LLM 提供商：{self._provider_id_resolved or '(未解析)'}",
                f"平台 ID：{self._platform_id or '(未检测到)'}",
                f"推送目标：{self.targets or '(未配置)'}",
                f"已推送累计：{self._pushed_total} 条",
                f"去重记录：{len(self._seen_ids)} 条",
            ]
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result("用法：/hltv push 立即轮询推送；/hltv status 查看状态")
