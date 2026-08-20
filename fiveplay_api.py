"""fiveplay_api —— 5eplay（5EPLAY）数据源模块。

提供两类数据：
- 新闻：csgo.5eplay.com/api/article 返回标准 JSON，标题已是中文，
  图片来自 oss.5eplay.com（可正常下载，无 Cloudflare 拦截）。
- 赛事：app.5eplay.com/api/tournament/session_list 返回 CS2 赛程，
  每个比赛带数字等级 mc_info.grade（grade_label 如 "S级赛事"），
  插件按配置的 grade 数字筛选（默认 1、2 即 S/A 级）。

全部接口无需鉴权，普通 UA 即可访问。
"""

from __future__ import annotations

import datetime
from typing import Any

import httpx

CST = datetime.timezone(datetime.timedelta(hours=8))  # 北京时间

NEWS_API = "https://csgo.5eplay.com/api/article"
MATCH_API = "https://app.5eplay.com/api/tournament/session_list"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


class FivePlaySource:
    """5eplay 新闻 + 赛事数据源。"""

    def __init__(self, ua: str = DEFAULT_UA, timeout: int = 25):
        self.ua = ua
        self.timeout = timeout

    async def _get_json(self, url: str, params: dict) -> dict | None:
        headers = {
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout, headers=headers
        ) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            raise RuntimeError(f"5eplay 请求失败，HTTP {r.status_code} ({url})")
        try:
            return r.json()
        except Exception as e:
            raise RuntimeError(f"5eplay 返回非 JSON 内容 ({url}): {e}")

    # ---------------- 新闻 ----------------

    async def fetch_news(self, page: int = 1, limit: int = 30) -> list[dict[str, Any]]:
        """抓取最新新闻列表。

        返回条目字段：id(jump_link)、title、link、image、pub_dt(北京时间)、hits。
        """
        data = await self._get_json(
            NEWS_API, {"page": page, "limit": limit}
        )
        if not data or not data.get("success"):
            raise RuntimeError(f"5eplay 新闻接口返回异常: {data}")
        articles = (data.get("data") or {}).get("list") or []
        items: list[dict[str, Any]] = []
        for a in articles:
            link = str(a.get("jump_link", "") or "").strip()
            if not link:
                continue
            title = str(a.get("title", "") or "").strip()
            if not title:
                continue
            images = a.get("images") or []
            img = images[0] if images and str(images[0]).strip() else ""
            pub_dt = None
            try:
                pub_dt = datetime.datetime.fromtimestamp(
                    int(a.get("dateline", 0)), tz=CST
                )
            except (TypeError, ValueError, OSError):
                pass
            items.append(
                {
                    "id": link,
                    "title": title,
                    "link": link,
                    "image": img,
                    "pub_dt": pub_dt,
                    "hits": a.get("hits", 0),
                }
            )
        return items

    # ---------------- 赛事 ----------------

    async def fetch_today_matches(self, grades: list[str]) -> list[dict[str, Any]]:
        """抓取今日（北京时间）指定等级的比赛，按开赛时间升序。

        grades: 数字等级列表，如 ["1", "2"]（S/A 级）。
        返回条目字段：time_str、team1、team2、tournament、grade_label、stage、format。
        """
        data = await self._get_json(
            MATCH_API,
            {
                "game_status": 1,
                "game_type": 1,
                "grades": ",".join(grades),
                "page": 1,
                "limit": 50,
            },
        )
        if not data or not data.get("success"):
            raise RuntimeError(f"5eplay 赛事接口返回异常: {data}")
        matches = (data.get("data") or {}).get("matches") or []

        today = datetime.datetime.now(tz=CST).date()
        result: list[dict[str, Any]] = []
        for m in matches:
            mi = m.get("mc_info") or {}
            tt = m.get("tt_info") or {}
            ts = mi.get("plan_ts")
            try:
                dt = datetime.datetime.fromtimestamp(int(ts), tz=CST)
            except (TypeError, ValueError, OSError):
                continue
            if dt.date() != today:
                continue
            t1 = (mi.get("t1_info") or {}).get("disp_name", "") or "TBD"
            t2 = (mi.get("t2_info") or {}).get("disp_name", "") or "TBD"
            fmt = "BO" + str(mi.get("format", "3")) if mi.get("format") else ""
            result.append(
                {
                    "time_str": dt.strftime("%H:%M"),
                    "team1": str(t1),
                    "team2": str(t2),
                    "tournament": str(tt.get("disp_name", "") or ""),
                    "grade_label": str(tt.get("grade_label", "") or ""),
                    "stage": str(mi.get("tt_stage", "") or ""),
                    "format": fmt,
                }
            )
        result.sort(key=lambda x: x["time_str"])
        return result
