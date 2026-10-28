"""News digest generation, persistence, search, and retention.

The editorial pipeline adapts two open-source designs without copying their code:
Thysrael/Horizon (MIT) contributes the 0-10 profile rubric, threshold, topic
deduplication, and top-N quota; KKKKhazix/AIHOT (MIT) contributes source tiers,
cross-source event grouping, and independent-source heat. See the module comments
at the exact decision points below.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import session_scope
from ..llm.providers import build_chat_model, build_resilient_chat_model
from ..models import (
    NewsArticle,
    NewsDigest,
    NewsDigestStatus,
    NewsEvent,
    NewsPreference,
)
from ..retrieval.bm25 import tokenize
from .categories import (
    CATEGORY_SPECS,
    DEFAULT_CATEGORY_IDS,
    active_category_ids,
    category_label,
    normalize_custom_topics,
    validate_category_selection,
)
from .prompts import build_ranking_prompt, build_ranking_repair_prompt
from .schemas import (
    NewsDigestRead,
    NewsEventRead,
    NewsSourceRef,
)
from .sources import FetchResult, NewsFetcher, RawArticle, fallback_importance

_PROTECTED_HOSTS = {"localhost", "localhost.localdomain"}
_TITLE_NOISE = re.compile(r"[\s\-—_|:：,，.。!！?？'\"“”‘’()（）\[\]【】]+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_preferences(session: Session) -> NewsPreference:
    row = session.get(NewsPreference, "default")
    if row is not None:
        return row
    row = NewsPreference(
        id="default",
        categories=list(DEFAULT_CATEGORY_IDS),
        custom_topics=[],
    )
    session.add(row)
    session.flush()
    return row


def save_preferences(
    session: Session,
    *,
    categories: list[str],
    custom_topics: list[str],
    settings: Settings | None = None,
) -> NewsPreference:
    settings = settings or get_settings()
    categories, topics = validate_category_selection(
        categories,
        custom_topics,
        custom_limit=settings.news_custom_topic_limit,
    )
    row = default_preferences(session)
    row.categories = categories
    row.custom_topics = topics
    row.updated_at = utc_now()
    session.flush()
    return row


def _window(
    *,
    now: datetime,
    settings: Settings,
) -> tuple[datetime, datetime, str]:
    tz = ZoneInfo(settings.news_timezone)
    local = now.astimezone(tz)
    date = local.date().isoformat()
    end = now.astimezone(timezone.utc)
    start = end - timedelta(hours=max(1, settings.news_window_hours))
    return start, end, date


def create_manual_digest(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> NewsDigest:
    settings = settings or get_settings()
    preference = default_preferences(session)
    categories = list(preference.categories or DEFAULT_CATEGORY_IDS)
    topics = list(preference.custom_topics or [])
    running = find_active_digest(
        session, categories=categories, custom_topics=topics
    )
    if running is not None:
        return running
    start, end, date = _window(now=now or utc_now(), settings=settings)
    row = NewsDigest(
        edition_date=date,
        window_start=start,
        window_end=end,
        categories=categories,
        custom_topics=topics,
        trigger="manual",
        status=NewsDigestStatus.pending,
    )
    session.add(row)
    session.flush()
    return row


def find_active_digest(
    session: Session,
    *,
    categories: list[str],
    custom_topics: list[str],
) -> NewsDigest | None:
    """Find a refresh with the same selection to avoid double background jobs."""

    stale_before = utc_now() - timedelta(minutes=10)
    session.execute(
        update(NewsDigest)
        .where(
            NewsDigest.status.in_(
                [NewsDigestStatus.pending, NewsDigestStatus.running]
            ),
            NewsDigest.created_at < stale_before,
        )
        .values(
            status=NewsDigestStatus.failed,
            error="新闻任务超时，已允许重新生成",
            completed_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    session.flush()
    session.expire_all()
    return session.scalar(
        select(NewsDigest)
        .where(
            NewsDigest.status.in_(
                [NewsDigestStatus.pending, NewsDigestStatus.running]
            ),
            NewsDigest.categories == categories,
            NewsDigest.custom_topics == custom_topics,
        )
        .order_by(NewsDigest.created_at.desc())
    )


def get_digest(session: Session, digest_id: str) -> NewsDigest | None:
    return session.get(NewsDigest, digest_id)


def latest_digest(
    session: Session, *, date: str | None = None
) -> NewsDigest | None:
    statement = select(NewsDigest).where(NewsDigest.status == NewsDigestStatus.ready)
    if date:
        statement = statement.where(NewsDigest.edition_date == date)
    return session.scalar(statement.order_by(NewsDigest.completed_at.desc()).limit(1))


def fresh_digest_for_selection(
    session: Session,
    *,
    categories: list[str],
    custom_topics: list[str],
    settings: Settings | None = None,
    now: datetime | None = None,
) -> NewsDigest | None:
    """Return a ready snapshot shared by a follow-up, if it is still warm."""

    settings = settings or get_settings()
    cutoff = (now or utc_now()) - timedelta(
        minutes=max(1, settings.news_cache_minutes)
    )
    candidates = session.scalars(
        select(NewsDigest)
        .where(
            NewsDigest.status == NewsDigestStatus.ready,
            NewsDigest.completed_at >= cutoff,
        )
        .order_by(NewsDigest.completed_at.desc())
        .limit(20)
    )
    for row in candidates:
        if (
            list(row.categories or []) == categories
            and list(row.custom_topics or []) == custom_topics
        ):
            return row
    return None


def _tier_score(tier: str) -> float:
    return {"primary": 1.0, "media": 0.8, "aggregator": 0.6}.get(tier, 0.5)


def _normalized_title(value: str) -> str:
    return _TITLE_NOISE.sub("", value.casefold())


def _bigrams(value: str) -> set[str]:
    text = _normalized_title(value)
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def title_similarity(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class Candidate:
    candidate_id: str
    category: str
    articles: list[RawArticle] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.articles[0].title

    @property
    def summary(self) -> str:
        return self.articles[0].summary

    @property
    def published_at(self) -> datetime:
        return max(article.published_at for article in self.articles)

    @property
    def source_count(self) -> int:
        return len(
            {
                article.source_domain or article.source_name
                for article in self.articles
                if article.source_domain or article.source_name
            }
        )


def build_candidates(
    articles: list[RawArticle],
    *,
    category: str,
    limit: int,
) -> list[Candidate]:
    """Normalize, exact-deduplicate, then lexically cluster near-identical titles."""

    exact: dict[str, RawArticle] = {}
    for article in articles:
        if article.category != category or not article.canonical_url:
            continue
        previous = exact.get(article.canonical_url)
        if previous is None or _tier_score(article.source_tier) > _tier_score(
            previous.source_tier
        ):
            exact[article.canonical_url] = article

    ordered = sorted(
        exact.values(),
        key=lambda item: (
            _tier_score(item.source_tier),
            item.published_at,
            len(item.summary),
        ),
        reverse=True,
    )
    clusters: list[Candidate] = []
    for article in ordered:
        target: Candidate | None = None
        for candidate in clusters:
            representative = candidate.articles[0]
            hours = abs(
                (article.published_at - representative.published_at).total_seconds()
            ) / 3600
            if (
                hours <= 48
                and title_similarity(article.title, representative.title) >= 0.78
            ):
                target = candidate
                break
        if target is None:
            target = Candidate(
                candidate_id=f"c{len(clusters) + 1}",
                category=category,
            )
            clusters.append(target)
        target.articles.append(article)

    clusters.sort(
        key=lambda item: (
            item.source_count,
            _tier_score(item.articles[0].source_tier),
            item.published_at,
        ),
        reverse=True,
    )
    for index, candidate in enumerate(clusters[:limit], start=1):
        candidate.candidate_id = f"c{index}"
    return clusters[:limit]


async def _complete(llm: Any, prompt: str) -> str:
    chunks: list[Any] = []
    messages = [
        SystemMessage(content="你只输出严格 JSON，不输出解释或 Markdown 代码块。"),
        HumanMessage(content=prompt),
    ]
    async for chunk in llm.astream(messages):
        chunks.append(chunk)
    return "".join(str(getattr(chunk, "content", "") or "") for chunk in chunks)


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return value


def _fallback_events(
    candidates: list[Candidate],
    *,
    now: datetime,
    max_events: int,
    threshold: float,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for candidate in candidates[:max_events]:
        representative = candidate.articles[0]
        importance = fallback_importance(representative, now=now)
        if importance < threshold:
            continue
        events.append(
            {
                "candidate_ids": [candidate.candidate_id],
                "category": candidate.category,
                "title": representative.title,
                "original_title": representative.title,
                "summary": representative.summary[:800],
                "why_it_matters": "",
                "importance": importance,
                "confidence": 0.35,
            }
        )
    return events


async def analyze_category(
    candidates: list[Candidate],
    *,
    recent_events: list[dict[str, Any]],
    llm: Any | None,
    settings: Settings,
    now: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (events, degraded). A model failure keeps a deterministic snapshot."""

    if not candidates:
        return [], False
    if llm is None:
        return (
            _fallback_events(
                candidates,
                now=now,
                max_events=settings.news_max_events_per_category,
                threshold=settings.news_importance_threshold,
            ),
            True,
        )
    payload = [
        {
            "id": candidate.candidate_id,
            "title": candidate.title,
            "summary": candidate.summary[:800],
            "sources": [article.source_name for article in candidate.articles[:6]],
            "source_count": candidate.source_count,
            "published_at": candidate.published_at.isoformat(),
        }
        for candidate in candidates
    ]
    prompt = build_ranking_prompt(
        category_label=category_label(candidates[0].category),
        candidates=payload,
        recent_events=recent_events,
        now=now,
        timezone=settings.news_timezone,
        threshold=settings.news_importance_threshold,
        max_events=settings.news_max_events_per_category,
        candidate_limit=settings.news_candidate_limit,
    )
    try:
        raw = await _complete(llm, prompt)
        try:
            parsed = _json_object(raw)
        except Exception:
            repaired = await _complete(llm, build_ranking_repair_prompt(raw))
            parsed = _json_object(repaired)
        valid_ids = {candidate.candidate_id for candidate in candidates}
        output: list[dict[str, Any]] = []
        used: set[str] = set()
        for item in parsed.get("events") or []:
            if not isinstance(item, dict):
                continue
            ids = [
                str(candidate_id)
                for candidate_id in (item.get("candidate_ids") or [])
                if str(candidate_id) in valid_ids
            ]
            ids = [candidate_id for candidate_id in ids if candidate_id not in used]
            importance = float(item.get("importance") or 0)
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or "").strip()
            if (
                not ids
                or not title
                or importance < settings.news_importance_threshold
            ):
                continue
            used.update(ids)
            output.append(
                {
                    "candidate_ids": ids,
                    "category": candidates[0].category,
                    "title": title[:500],
                    "original_title": str(item.get("original_title") or "").strip()[:500],
                    "summary": summary[:2000],
                    "why_it_matters": str(item.get("why_it_matters") or "").strip()[:1000],
                    "importance": max(0.0, min(10.0, importance)),
                    "confidence": max(
                        0.0, min(1.0, float(item.get("confidence") or 0.6))
                    ),
                }
            )
            if len(output) >= settings.news_max_events_per_category:
                break
        output.sort(key=lambda item: -float(item["importance"]))
        return output, False
    except Exception:
        return (
            _fallback_events(
                candidates,
                now=now,
                max_events=settings.news_max_events_per_category,
                threshold=settings.news_importance_threshold,
            ),
            True,
        )


def _recent_event_context(
    session: Session,
    *,
    category: str,
    now: datetime,
) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(NewsEvent)
            .where(
                NewsEvent.category == category,
                NewsEvent.published_at >= now - timedelta(days=7),
            )
            .order_by(NewsEvent.published_at.desc())
            .limit(16)
        )
    )
    return [
        {
            "title": row.title,
            "summary": row.summary[:300],
            "published_at": row.published_at.isoformat(),
        }
        for row in rows
    ]


async def generate_digest(
    session: Session,
    digest: NewsDigest,
    *,
    fetcher: Any | None = None,
    llm: Any | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    build_llm: bool = True,
) -> NewsDigest:
    settings = settings or get_settings()
    current = now or utc_now()
    digest.status = NewsDigestStatus.running
    digest.started_at = current
    digest.error = None
    session.flush()

    fetcher = fetcher or NewsFetcher(settings)
    fetch_result: FetchResult = await fetcher.fetch(
        categories=list(digest.categories or []),
        custom_topics=list(digest.custom_topics or []),
        window_start=digest.window_start,
        window_end=digest.window_end,
    )
    digest.source_stats = {
        "attempted": fetch_result.attempted,
        "succeeded": fetch_result.succeeded,
        "failed": len(fetch_result.errors),
        "articles": len(fetch_result.articles),
        "providers": fetch_result.provider_stats,
    }
    digest.warnings = list(fetch_result.errors)
    if fetch_result.attempted and fetch_result.succeeded == 0:
        digest.status = NewsDigestStatus.failed
        digest.error = "所有新闻来源均不可用"
        digest.completed_at = utc_now()
        session.flush()
        return digest
    if not fetch_result.articles:
        digest.status = NewsDigestStatus.ready
        digest.completed_at = utc_now()
        digest.degraded = False
        session.flush()
        return digest

    if llm is None and build_llm:
        try:
            if settings.news_model.strip():
                llm = build_chat_model(settings, model=settings.news_model.strip())
            else:
                llm = build_resilient_chat_model(settings)
        except Exception as exc:
            digest.warnings.append(f"新闻摘要模型不可用：{exc}")
            llm = None

    by_category: dict[str, list[RawArticle]] = defaultdict(list)
    for article in fetch_result.articles:
        by_category[article.category].append(article)

    degraded = llm is None
    event_rows: list[NewsEvent] = []
    rank = 0
    for category in active_category_ids(
        list(digest.categories or []), list(digest.custom_topics or [])
    ):
        candidates = build_candidates(
            by_category.get(category, []),
            category=category,
            limit=settings.news_candidate_limit,
        )
        if not candidates:
            continue
        events, category_degraded = await analyze_category(
            candidates,
            recent_events=_recent_event_context(
                session, category=category, now=current
            ),
            llm=llm,
            settings=settings,
            now=current,
        )
        degraded = degraded or category_degraded
        candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
        for event in events:
            candidate_ids = event["candidate_ids"]
            selected = [
                candidate_by_id[candidate_id]
                for candidate_id in candidate_ids
                if candidate_id in candidate_by_id
            ]
            if not selected:
                continue
            article_rows: list[NewsArticle] = []
            for candidate in selected:
                for article in candidate.articles:
                    row = NewsArticle(
                        digest_id=digest.id,
                        category=category,
                        title=article.title,
                        original_title=article.title,
                        summary=article.summary,
                        url=article.url,
                        canonical_url=article.canonical_url,
                        source_name=article.source_name,
                        source_domain=article.source_domain,
                        source_tier=article.source_tier,
                        language=article.language,
                        published_at=article.published_at,
                        time_basis=article.time_basis,
                        fetched_at=current,
                        content=article.summary,
                        content_status="feed",
                    )
                    session.add(row)
                    article_rows.append(row)
            session.flush()
            representative = max(
                article_rows,
                key=lambda row: (_tier_score(row.source_tier), row.published_at),
            )
            rank += 1
            event_rows.append(
                NewsEvent(
                    digest_id=digest.id,
                    category=category,
                    rank=rank,
                    title=event["title"],
                    original_title=event["original_title"]
                    or representative.original_title,
                    summary=event["summary"] or representative.summary,
                    why_it_matters=event["why_it_matters"],
                    importance=event["importance"],
                    confidence=event["confidence"],
                    source_count=len(
                        {
                            row.source_domain or row.source_name
                            for row in article_rows
                        }
                    ),
                    article_ids=[row.id for row in article_rows],
                    published_at=max(row.published_at for row in article_rows),
                )
            )
    session.add_all(event_rows)
    digest.degraded = degraded
    if degraded:
        digest.warnings.append("部分或全部分类未获得 AI 摘要，已显示降级排序结果。")
    digest.status = NewsDigestStatus.ready
    digest.completed_at = utc_now()
    session.flush()
    return digest


async def run_digest_job(digest_id: str) -> None:
    with session_scope() as session:
        digest = session.get(NewsDigest, digest_id)
        if digest is None or digest.status not in {
            NewsDigestStatus.pending,
            NewsDigestStatus.running,
        }:
            return
        try:
            await generate_digest(session, digest)
        except Exception as exc:
            digest.status = NewsDigestStatus.failed
            digest.error = str(exc)[:2000]
            digest.completed_at = utc_now()
            session.flush()


def digest_read(session: Session, digest: NewsDigest) -> NewsDigestRead:
    articles = {
        row.id: row
        for row in session.scalars(
            select(NewsArticle).where(NewsArticle.digest_id == digest.id)
        )
    }
    events = list(
        session.scalars(
            select(NewsEvent)
            .where(NewsEvent.digest_id == digest.id)
            .order_by(NewsEvent.rank)
        )
    )
    result: list[NewsEventRead] = []
    for event in events:
        sources = [
            NewsSourceRef(
                article_id=article.id,
                source_name=article.source_name,
                source_domain=article.source_domain,
                title=article.title,
                original_title=article.original_title,
                url=article.url,
                published_at=article.published_at,
                time_basis=article.time_basis,
                language=article.language,
            )
            for article_id in (event.article_ids or [])
            if (article := articles.get(str(article_id))) is not None
        ]
        sources.sort(key=lambda item: (_tier_score(articles[item.article_id].source_tier), item.published_at), reverse=True)
        result.append(
            NewsEventRead(
                id=event.id,
                category=event.category,
                category_label=category_label(event.category),
                title=event.title,
                original_title=event.original_title,
                summary=event.summary,
                why_it_matters=event.why_it_matters,
                importance=float(event.importance),
                confidence=float(event.confidence),
                source_count=event.source_count,
                published_at=event.published_at,
                time_basis=(
                    sources[0].time_basis
                    if sources
                    else "published"
                ),
                sources=sources,
            )
        )
    return NewsDigestRead(
        id=digest.id,
        edition_date=digest.edition_date,
        window_start=digest.window_start,
        window_end=digest.window_end,
        categories=list(digest.categories or []),
        custom_topics=list(digest.custom_topics or []),
        trigger=digest.trigger,
        status=digest.status.value,
        degraded=bool(digest.degraded),
        warnings=list(digest.warnings or []),
        source_stats=dict(digest.source_stats or {}),
        error=digest.error,
        created_at=digest.created_at,
        started_at=digest.started_at,
        completed_at=digest.completed_at,
        events=result,
    )


def _query_terms(value: str) -> set[str]:
    return set(tokenize(value))


def search_cached_news(
    session: Session,
    *,
    query: str,
    categories: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    statement = select(NewsEvent).join(
        NewsDigest, NewsDigest.id == NewsEvent.digest_id
    )
    if categories:
        statement = statement.where(NewsEvent.category.in_(categories))
    if date_from:
        statement = statement.where(NewsDigest.edition_date >= date_from)
    if date_to:
        statement = statement.where(NewsDigest.edition_date <= date_to)
    events = list(
        session.scalars(
            statement.where(
                NewsDigest.status == NewsDigestStatus.ready,
                NewsDigest.completed_at
                >= utc_now() - timedelta(days=get_settings().news_retention_days),
            )
            .order_by(NewsEvent.published_at.desc())
            .limit(1000)
        )
    )
    terms = _query_terms(query)
    scored: list[tuple[float, NewsEvent]] = []
    for row in events:
        title_terms = _query_terms(row.title)
        summary_terms = _query_terms(row.summary + " " + row.why_it_matters)
        score = 3.0 * len(terms & title_terms) + len(terms & summary_terms)
        if query.casefold() in row.title.casefold():
            score += 5
        if score > 0:
            scored.append((score, row))
    # A category-only question such as "科技领域最重要的新闻" carries its topic in
    # the filter, not in words that also appear in every headline. Returning an empty
    # list in that case forces the model to answer generically despite a ready digest.
    if not scored and categories:
        scored = [(1.0, row) for row in events]
    scored.sort(
        key=lambda item: (
            item[0],
            float(item[1].importance),
            item[1].published_at,
        ),
        reverse=True,
    )
    if not scored:
        return []
    selected_events = scored[: max(1, min(limit, 15))]
    article_ids = {
        str(article_id)
        for _, event in selected_events
        for article_id in (event.article_ids or [])
    }
    articles = {
        row.id: row
        for row in session.scalars(
            select(NewsArticle).where(NewsArticle.id.in_(article_ids))
        )
    }
    results: list[dict[str, Any]] = []
    for score, event in selected_events:
        sources = [
            articles[article_id]
            for raw_id in (event.article_ids or [])
            if (article_id := str(raw_id)) in articles
        ]
        sources.sort(
            key=lambda row: (_tier_score(row.source_tier), row.published_at),
            reverse=True,
        )
        representative = sources[0] if sources else None
        results.append(
            {
                "event_id": event.id,
                "article_id": representative.id if representative else None,
                "title": event.title,
                "summary": event.summary[:800],
                "why_it_matters": event.why_it_matters[:500],
                "source": representative.source_name if representative else "",
                "url": representative.url if representative else "",
                "sources": [
                    {
                        "article_id": row.id,
                        "source": row.source_name,
                        "url": row.url,
                        "published_at": row.published_at.isoformat(),
                    }
                    for row in sources[:6]
                ],
                "published_at": event.published_at.isoformat(),
                "time_basis": (
                    sources[0].time_basis if sources else "published"
                ),
                "category": event.category,
                "importance": float(event.importance),
                "relevance_score": score,
            }
        )
    return results


def event_detail(session: Session, event_id: str) -> dict[str, Any] | None:
    event = session.get(NewsEvent, event_id)
    if event is None:
        return None
    articles = {
        row.id: row
        for row in session.scalars(
            select(NewsArticle).where(NewsArticle.digest_id == event.digest_id)
        )
    }
    sources = [
        {
            "article_id": row.id,
            "title": row.title,
            "source": row.source_name,
            "url": row.url,
            "published_at": row.published_at.isoformat(),
            "time_basis": row.time_basis,
            "summary": row.summary[:800],
        }
        for article_id in (event.article_ids or [])
        if (row := articles.get(str(article_id))) is not None
    ]
    return {
        "event_id": event.id,
        "category": event.category,
        "title": event.title,
        "summary": event.summary,
        "why_it_matters": event.why_it_matters,
        "importance": event.importance,
        "sources": sources,
    }


def _safe_public_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host in _PROTECTED_HOSTS or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )


async def read_article_content(
    session: Session,
    article_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    settings = settings or get_settings()
    article = session.get(NewsArticle, article_id)
    if article is None:
        return None
    if article.content_status == "extracted" and article.content:
        return {
            "article_id": article.id,
            "title": article.title,
            "source": article.source_name,
            "url": article.url,
            "published_at": article.published_at.isoformat(),
            "time_basis": article.time_basis,
            "content": article.content[:12000],
        }
    if not _safe_public_url(article.url):
        return {
            "article_id": article.id,
            "title": article.title,
            "source": article.source_name,
            "url": article.url,
            "published_at": article.published_at.isoformat(),
            "time_basis": article.time_basis,
            "content": article.summary[:4000],
            "warning": "该链接不是可安全抓取的公网地址，返回订阅摘要。",
        }
    try:
        import trafilatura

        async with httpx.AsyncClient(
            timeout=settings.news_fetch_timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 LocalNewsRadar/1.0"},
        ) as client:
            response = await client.get(article.url)
            response.raise_for_status()
            if len(response.content) > settings.news_max_response_bytes:
                raise ValueError("正文页面超过大小上限")
        extracted = trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if extracted:
            article.content = extracted[:20000]
            article.content_status = "extracted"
            session.flush()
        content = article.content or article.summary
        return {
            "article_id": article.id,
            "title": article.title,
            "source": article.source_name,
            "url": article.url,
            "published_at": article.published_at.isoformat(),
            "content": content[:12000],
        }
    except Exception as exc:
        return {
            "article_id": article.id,
            "title": article.title,
            "source": article.source_name,
            "url": article.url,
            "published_at": article.published_at.isoformat(),
            "time_basis": article.time_basis,
            "content": article.summary[:4000],
            "warning": f"正文抓取失败，返回订阅摘要：{exc}",
        }


async def search_live_news(
    query: str,
    *,
    categories: list[str] | None = None,
    fetcher: NewsFetcher | None = None,
    settings: Settings | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    fetcher = fetcher or NewsFetcher(settings)
    end = utc_now()
    result = await fetcher.fetch_search(
        query=query,
        categories=categories or ["world"],
        window_start=end - timedelta(days=7),
        window_end=end,
    )
    rows = sorted(
        result.articles,
        key=lambda item: (_tier_score(item.source_tier), item.published_at),
        reverse=True,
    )
    return [
        {
            "title": item.title,
            "summary": item.summary[:600],
            "source": item.source_name,
            "url": item.url,
            "published_at": item.published_at.isoformat(),
            "time_basis": item.time_basis,
            "category": item.category,
        }
        for item in rows[: max(1, min(limit, 15))]
    ]


def cleanup_news_history(
    session: Session,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> int:
    settings = settings or get_settings()
    cutoff = (now or utc_now()) - timedelta(days=max(1, settings.news_retention_days))
    rows = list(
        session.scalars(
            select(NewsDigest.id).where(NewsDigest.created_at < cutoff)
        )
    )
    if not rows:
        return 0
    session.execute(delete(NewsDigest).where(NewsDigest.id.in_(rows)))
    session.flush()
    return len(rows)


def list_categories() -> list[dict[str, str]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "description": item.description,
        }
        for item in CATEGORY_SPECS
    ]
