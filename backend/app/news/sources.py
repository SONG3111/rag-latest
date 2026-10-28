"""Bounded, fault-tolerant news collection.

Feed parsing uses feedparser (BSD-2-Clause). URL normalization, one transient
retry, and stable-item identity adapt the source-health ideas documented by
sansan0/TrendRadar (GPL-3.0); no TrendRadar code is copied. Google News is used
only as a discovery feed; every event keeps the publisher link supplied by the
feed instead of treating the aggregator as the fact source.
"""

from __future__ import annotations

import asyncio
import calendar
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from ..config import Settings, get_settings
from ..retrieval.bm25 import tokenize
from .categories import (
    CATEGORY_KEYWORDS,
    active_category_ids,
    category_label,
    category_queries,
    custom_topic,
    direct_feeds,
)

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
    "spm",
}

_CHINANEWS_GENERAL_FEED = (
    "https://www.chinanews.com.cn/rss/scroll-news.xml",
    "中国新闻网 滚动",
)

_GLOBAL_DIRECT_FEEDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "technology": (
        ("https://techcrunch.com/feed/", "TechCrunch", "en"),
        ("https://www.theverge.com/rss/index.xml", "The Verge", "en"),
    ),
    "finance": (
        ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business", "en"),
    ),
    "world": (
        ("https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", "BBC 中文", "zh"),
        ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World", "en"),
    ),
    "health": (
        ("https://www.who.int/rss-feeds/news-english.xml", "WHO", "en"),
    ),
    "culture_sports": (
        ("https://feeds.bbci.co.uk/sport/rss.xml", "BBC Sport", "en"),
        (
            "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
            "BBC Entertainment",
            "en",
        ),
    ),
}

# Each hot-list route is fetched once. Items are then routed to one selected
# category using title/summary keywords; preferred categories are only a fallback.
_DAILY_HOT_ROUTES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ithome": ("IT之家", ("technology",)),
    "36kr": ("36氪", ("technology", "finance", "consumer")),
    "juejin": ("掘金", ("technology",)),
    "thepaper": ("澎湃新闻", ("world", "society", "finance", "culture_sports")),
    "qq-news": ("腾讯新闻", ("world", "society", "finance", "consumer")),
    "sina-news": ("新浪新闻", ("world", "society", "finance", "consumer")),
    "netease-news": ("网易新闻", ("world", "society", "finance", "consumer")),
    "weibo": ("微博热搜", ("world", "society", "culture_sports", "consumer")),
    "douyin": ("抖音热点", ("world", "society", "culture_sports", "consumer")),
}


@dataclass(frozen=True)
class RawArticle:
    category: str
    title: str
    summary: str
    url: str
    canonical_url: str
    source_name: str
    source_domain: str
    source_tier: str
    language: str
    published_at: datetime
    time_basis: str = "published"


@dataclass(frozen=True)
class FetchResult:
    articles: list[RawArticle] = field(default_factory=list)
    attempted: int = 0
    succeeded: int = 0
    errors: list[str] = field(default_factory=list)
    provider_stats: dict[str, dict[str, int]] = field(default_factory=dict)


def _plain_text(value: Any, limit: int = 1200) -> str:
    text = html.unescape(_TAG.sub(" ", str(value or "")))
    return _SPACE.sub(" ", text).strip()[:limit]


def canonical_url(value: str) -> str:
    """Normalize a feed URL and remove common tracking parameters."""

    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if not host:
            return raw.split("#", 1)[0]
        port = parts.port
        netloc = host
        if port and not (
            (parts.scheme == "http" and port == 80)
            or (parts.scheme == "https" and port == 443)
        ):
            netloc = f"{host}:{port}"
        query = [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in _TRACKING_PARAMS
        ]
        path = parts.path or "/"
        return urlunsplit(("https" if parts.scheme == "http" else parts.scheme, netloc, path, urlencode(query), ""))
    except ValueError:
        return raw.split("#", 1)[0]


def source_domain(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def google_news_url(query: str, language: str) -> str:
    if language == "en":
        params = {"q": f"{query} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    else:
        params = {"q": f"{query} when:1d", "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    return f"https://news.google.com/rss/search?{urlencode(params, quote_via=quote_plus)}"


def _published_at(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def _entry_source(entry: Any, source_hint: str) -> str:
    source = entry.get("source") or {}
    if hasattr(source, "get"):
        title = source.get("title")
        if title:
            return _plain_text(title, 200)
    return source_hint


def _classify_domestic_item(
    text: str,
    *,
    selected: list[str],
    topics: list[str],
    preferred: tuple[str, ...] = (),
) -> str | None:
    """Route one domestic item to one category, avoiding cross-category duplicates."""

    folded = text.casefold()
    for topic in topics:
        if topic.casefold() in folded:
            return f"custom:{topic}"

    scored: list[tuple[int, int, str]] = []
    for index, category in enumerate(selected):
        hits = sum(
            1
            for keyword in CATEGORY_KEYWORDS.get(category, ())
            if keyword.casefold() in folded
        )
        if hits:
            scored.append((hits, -index, category))
    if scored:
        return max(scored)[2]
    for category in preferred:
        if category in selected:
            return category
    return None


class NewsFetcher:
    """Fetch all selected feeds with bounded concurrency and partial success."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport

    async def _get(self, url: str) -> bytes:
        last_error: Exception | None = None
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; LocalNewsRadar/1.0; "
                "+https://localhost/)"
            )
        }
        limits = httpx.Limits(
            max_connections=max(1, self.settings.news_fetch_concurrency),
            max_keepalive_connections=max(1, self.settings.news_fetch_concurrency),
        )
        async with httpx.AsyncClient(
            timeout=self.settings.news_fetch_timeout,
            follow_redirects=True,
            headers=headers,
            limits=limits,
            transport=self.transport,
        ) as client:
            for attempt in range(2):
                try:
                    response = await client.get(url)
                    if response.status_code >= 500 and attempt == 0:
                        await asyncio.sleep(0.15)
                        continue
                    response.raise_for_status()
                    content = response.content
                    if len(content) > self.settings.news_max_response_bytes:
                        raise ValueError("新闻源响应超过大小上限")
                    return content
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_error = exc
                    if attempt == 0:
                        await asyncio.sleep(0.15)
                        continue
                    raise
        raise last_error or RuntimeError("新闻源请求失败")

    async def _fetch_rss(
        self,
        *,
        category: str,
        url: str,
        source_hint: str,
        tier: str,
        language: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[RawArticle]:
        content = await self._get(url)
        parsed = feedparser.parse(content)
        items: list[RawArticle] = []
        for entry in parsed.entries:
            title = _plain_text(entry.get("title"), 500)
            link = str(entry.get("link") or "").strip()
            canonical = canonical_url(link)
            if not title or not canonical:
                continue
            published = _published_at(entry)
            # An undated item is routinely an evergreen or stale page. It is unsafe
            # to place it in a daily briefing merely because it appeared in the feed.
            if published is None:
                continue
            if published < window_start - timedelta(minutes=5) or published > window_end + timedelta(hours=2):
                continue
            published = min(published, window_end)
            source_name = _entry_source(entry, source_hint)
            domain = source_domain(link)
            # Google News wraps every publisher in a news.google.com redirect. Using
            # that host as source identity would collapse ten independent publishers
            # into one and destroy the corroboration signal.
            if domain == "news.google.com":
                domain = f"publisher:{source_name.casefold()}"
            items.append(
                RawArticle(
                    category=category,
                    title=title,
                    summary=_plain_text(
                        entry.get("summary")
                        or entry.get("description")
                        or entry.get("subtitle")
                    ),
                    url=link,
                    canonical_url=canonical,
                    source_name=source_name,
                    source_domain=domain,
                    source_tier=tier,
                    language=language,
                    published_at=published,
                )
            )
        return items

    async def _fetch_hacker_news(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[RawArticle]:
        raw = await self._get("https://hacker-news.firebaseio.com/v0/topstories.json")
        import json

        ids = json.loads(raw.decode("utf-8"))[:20]

        async def one(item_id: int) -> RawArticle | None:
            try:
                payload = json.loads(
                    (await self._get(
                        f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
                    )).decode("utf-8")
                )
            except Exception:
                return None
            title = _plain_text(payload.get("title"), 500)
            link = str(payload.get("url") or f"https://news.ycombinator.com/item?id={item_id}")
            timestamp = int(payload.get("time") or 0)
            if not title or timestamp <= 0:
                return None
            published = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if not (window_start <= published <= window_end):
                return None
            canonical = canonical_url(link)
            return RawArticle(
                category="technology",
                title=title,
                summary="",
                url=link,
                canonical_url=canonical,
                source_name="Hacker News",
                source_domain=source_domain(link),
                source_tier="aggregator",
                language="en",
                published_at=published,
            )

        results = await asyncio.gather(*(one(item_id) for item_id in ids))
        return [item for item in results if item is not None]

    async def _fetch_domestic_general(
        self,
        *,
        selected: list[str],
        topics: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> list[RawArticle]:
        """Classify one shared China News scrolling feed across selected categories."""

        url, source_name = _CHINANEWS_GENERAL_FEED
        content = await self._get(url)
        parsed = feedparser.parse(content)
        items: list[RawArticle] = []
        preferred = ("society",) if "society" in selected else ()
        for entry in parsed.entries:
            title = _plain_text(entry.get("title"), 500)
            summary = _plain_text(
                entry.get("summary")
                or entry.get("description")
                or entry.get("subtitle")
            )
            link = str(entry.get("link") or "").strip()
            canonical = canonical_url(link)
            published = _published_at(entry)
            if not title or not canonical or published is None:
                continue
            if published < window_start - timedelta(minutes=5) or published > window_end + timedelta(hours=2):
                continue
            category = _classify_domestic_item(
                f"{title} {summary}",
                selected=selected,
                topics=topics,
                preferred=preferred,
            )
            if category is None:
                continue
            items.append(
                RawArticle(
                    category=category,
                    title=title,
                    summary=summary,
                    url=link,
                    canonical_url=canonical,
                    source_name=source_name,
                    source_domain=source_domain(link),
                    source_tier="media",
                    language="zh",
                    published_at=min(published, window_end),
                    time_basis="published",
                )
            )
        return items

    async def _fetch_daily_hot_route(
        self,
        *,
        route: str,
        selected: list[str],
        topics: list[str],
        window_end: datetime,
    ) -> list[RawArticle]:
        """Fetch one self-hosted DailyHotApi route and classify its items."""

        base = self.settings.news_hot_api_base_url.strip().rstrip("/")
        if not base:
            return []
        raw = await self._get(f"{base}/{route}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"热榜接口返回非 JSON：{exc}") from exc
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ValueError("热榜接口缺少 data 数组")
        route_name, preferred = _DAILY_HOT_ROUTES[route]
        items: list[RawArticle] = []
        seen: set[str] = set()
        for entry in entries[:80]:
            if not isinstance(entry, dict):
                continue
            title = _plain_text(entry.get("title") or entry.get("name"), 500)
            summary = _plain_text(
                entry.get("desc")
                or entry.get("description")
                or entry.get("summary"),
                800,
            )
            link = str(
                entry.get("url")
                or entry.get("mobileUrl")
                or entry.get("link")
                or ""
            ).strip()
            canonical = canonical_url(link)
            if not title or not canonical or canonical in seen:
                continue
            category = _classify_domestic_item(
                f"{title} {summary}",
                selected=selected,
                topics=topics,
                preferred=preferred,
            )
            if category is None:
                continue
            seen.add(canonical)
            items.append(
                RawArticle(
                    category=category,
                    title=title,
                    summary=summary,
                    url=link,
                    canonical_url=canonical,
                    source_name=f"{route_name}热榜",
                    source_domain=source_domain(link),
                    source_tier="aggregator",
                    language="zh",
                    published_at=window_end,
                    time_basis="observed",
                )
            )
        return items

    async def fetch(
        self,
        *,
        categories: list[str],
        custom_topics: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> FetchResult:
        selected = active_category_ids(categories, custom_topics)
        built_in_selected = [item for item in selected if not item.startswith("custom:")]
        tasks: list[tuple[str, str, asyncio.Task[list[RawArticle]]]] = []

        for category in selected:
            for url, source_name in direct_feeds(category):
                tasks.append(
                    (
                        f"{category_label(category)} / {source_name}",
                        "domestic_rss",
                        asyncio.create_task(
                            self._fetch_rss(
                                category=category,
                                url=url,
                                source_hint=source_name,
                                tier="media",
                                language="zh",
                                window_start=window_start,
                                window_end=window_end,
                            )
                        ),
                    )
                )

        if selected:
            tasks.append(
                (
                    "国内共享流 / 中国新闻网",
                    "domestic_rss",
                    asyncio.create_task(
                        self._fetch_domestic_general(
                            selected=built_in_selected,
                            topics=custom_topics,
                            window_start=window_start,
                            window_end=window_end,
                        )
                    ),
                )
            )

        hot_base = self.settings.news_hot_api_base_url.strip()
        if hot_base and selected:
            for route in _DAILY_HOT_ROUTES:
                tasks.append(
                    (
                        f"国内热榜 / {_DAILY_HOT_ROUTES[route][0]}",
                        "domestic_hot",
                        asyncio.create_task(
                            self._fetch_daily_hot_route(
                                route=route,
                                selected=built_in_selected,
                                topics=custom_topics,
                                window_end=window_end,
                            )
                        ),
                    )
                )

        if self.settings.news_enable_google:
            for category in selected:
                for query, language in category_queries(category):
                    tasks.append(
                        (
                            f"{category_label(category)} / Google News {language}",
                            "google_news",
                            asyncio.create_task(
                                self._fetch_rss(
                                    category=category,
                                    url=google_news_url(query, language),
                                    source_hint="Google News",
                                    tier="aggregator",
                                    language=language,
                                    window_start=window_start,
                                    window_end=window_end,
                                )
                            ),
                        )
                    )

        if self.settings.news_enable_global:
            for category in built_in_selected:
                for url, source_name, language in _GLOBAL_DIRECT_FEEDS.get(
                    category, ()
                ):
                    tasks.append(
                        (
                            f"{category_label(category)} / {source_name}",
                            "global_news",
                            asyncio.create_task(
                                self._fetch_rss(
                                    category=category,
                                    url=url,
                                    source_hint=source_name,
                                    tier="primary" if source_name == "WHO" else "media",
                                    language=language,
                                    window_start=window_start,
                                    window_end=window_end,
                                )
                            ),
                        )
                    )

        for url in self.settings.news_extra_rss_urls:
            for category in selected:
                tasks.append(
                    (
                        f"{category_label(category)} / 自定义 RSS",
                        "extra_rss",
                        asyncio.create_task(
                            self._fetch_rss(
                                category=category,
                                url=url,
                                source_hint=source_domain(url) or "自定义 RSS",
                                tier="media",
                                language="auto",
                                window_start=window_start,
                                window_end=window_end,
                            )
                        ),
                    )
                )

        if self.settings.news_enable_global and "technology" in selected:
            tasks.append(
                (
                    "科技 / Hacker News",
                    "global_news",
                    asyncio.create_task(
                        self._fetch_hacker_news(
                            window_start=window_start, window_end=window_end
                        )
                    ),
                )
            )

        semaphore = asyncio.Semaphore(max(1, self.settings.news_fetch_concurrency))

        async def bounded(task: asyncio.Task[list[RawArticle]]) -> list[RawArticle]:
            async with semaphore:
                return await task

        articles: list[RawArticle] = []
        errors: list[str] = []
        succeeded = 0
        provider_stats: dict[str, dict[str, int]] = {}
        outcomes = await asyncio.gather(
            *(bounded(task) for _, _, task in tasks), return_exceptions=True
        )
        for (label, provider, _), outcome in zip(tasks, outcomes):
            stats = provider_stats.setdefault(
                provider, {"attempted": 0, "succeeded": 0, "failed": 0}
            )
            stats["attempted"] += 1
            if isinstance(outcome, BaseException):
                errors.append(f"{label}: {outcome}")
                stats["failed"] += 1
                continue
            succeeded += 1
            stats["succeeded"] += 1
            articles.extend(outcome)

        return FetchResult(
            articles=articles,
            attempted=len(tasks),
            succeeded=succeeded,
            errors=errors[:30],
            provider_stats=provider_stats,
        )

    async def fetch_search(
        self,
        *,
        query: str,
        categories: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> FetchResult:
        """Search domestic providers first; Google is an optional second hop."""

        builtins = [item for item in categories if not item.startswith("custom:")]
        topics = [custom_topic(item) or item[7:] for item in categories if item.startswith("custom:")]
        domestic = await self.fetch(
            categories=builtins,
            custom_topics=[item for item in topics if item],
            window_start=window_start,
            window_end=window_end,
        )
        terms = {
            part.casefold()
            for part in [
                *tokenize(query),
                *re.split(r"[\s,，、;；]+", query),
            ]
            if part.strip()
        }

        def matches(article: RawArticle) -> bool:
            text = f"{article.title} {article.summary}".casefold()
            return bool(terms) and any(term in text for term in terms)

        articles = [item for item in domestic.articles if matches(item)]
        errors = list(domestic.errors)
        provider_stats = {
            key: dict(value) for key, value in domestic.provider_stats.items()
        }
        attempted = domestic.attempted
        succeeded = domestic.succeeded

        if self.settings.news_enable_google:
            google_tasks = [
                (
                    f"Google 定向搜索 {language}",
                    asyncio.create_task(
                        self._fetch_rss(
                            category=builtins[0] if builtins else "world",
                            url=google_news_url(query, language),
                            source_hint="Google News",
                            tier="aggregator",
                            language=language,
                            window_start=window_start,
                            window_end=window_end,
                        )
                    ),
                )
                for language in ("zh", "en")
            ]
            outcomes = await asyncio.gather(
                *(task for _, task in google_tasks), return_exceptions=True
            )
            stats = provider_stats.setdefault(
                "google_news", {"attempted": 0, "succeeded": 0, "failed": 0}
            )
            for (label, _), outcome in zip(google_tasks, outcomes):
                attempted += 1
                stats["attempted"] += 1
                if isinstance(outcome, BaseException):
                    errors.append(f"{label}: {outcome}")
                    stats["failed"] += 1
                else:
                    succeeded += 1
                    stats["succeeded"] += 1
                    articles.extend(outcome)

        seen: set[str] = set()
        unique: list[RawArticle] = []
        for article in articles:
            if article.canonical_url in seen:
                continue
            seen.add(article.canonical_url)
            unique.append(article)
        return FetchResult(
            articles=unique,
            attempted=attempted,
            succeeded=succeeded,
            errors=errors[:30],
            provider_stats=provider_stats,
        )


def fallback_importance(article: RawArticle, *, now: datetime) -> float:
    tier = {"primary": 1.0, "media": 0.8, "aggregator": 0.65}.get(
        article.source_tier, 0.6
    )
    age_hours = max(0.0, (now - article.published_at).total_seconds() / 3600)
    recency = max(0.0, 1.0 - age_hours / 48)
    summary_bonus = 0.4 if len(article.summary) >= 80 else 0.0
    return round(min(10.0, 3.5 + 2.0 * tier + 2.0 * recency + summary_bonus), 1)
