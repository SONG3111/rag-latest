"""News radar tests. All feeds and LLM behavior are deterministic mocks."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import httpx
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import select

from app.config import get_settings
from app.models import (
    NewsArticle,
    NewsDigest,
    NewsDigestStatus,
    NewsEvent,
)
from app.news.agent import NewsAgent
from app.news.schemas import NewsDigestRead
from app.news.service import (
    build_candidates,
    cleanup_news_history,
    create_manual_digest,
    digest_read,
    find_active_digest,
    generate_digest,
    save_preferences,
    search_cached_news,
    search_live_news,
    utc_now,
)
from app.news.sources import FetchResult, NewsFetcher, RawArticle, canonical_url


NOW = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)


def _article(
    title: str,
    *,
    url: str,
    source: str = "Example News",
    summary: str = "一条具有明确事实和影响的新闻摘要。",
    minutes_ago: int = 30,
) -> RawArticle:
    return RawArticle(
        category="technology",
        title=title,
        summary=summary,
        url=url,
        canonical_url=canonical_url(url),
        source_name=source,
        source_domain=canonical_url(url).split("/")[2],
        source_tier="media",
        language="zh",
        published_at=NOW - timedelta(minutes=minutes_ago),
    )


class FakeFetcher:
    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls = 0
        self.last_search_kwargs: dict[str, Any] | None = None

    async def fetch(self, **_: Any) -> FetchResult:
        self.calls += 1
        return self.result

    async def fetch_search(self, **kwargs: Any) -> FetchResult:
        self.calls += 1
        self.last_search_kwargs = kwargs
        return self.result


class ScriptedLLM:
    def __init__(self, content: list[str]) -> None:
        self.content = list(content)
        self.calls: list[list[Any]] = []
        self.bound_tools: list[Any] = []

    def bind_tools(self, tools: list[Any]):
        self.bound_tools = list(tools)
        return self

    async def astream(self, messages: list[Any]):
        self.calls.append(list(messages))
        text = self.content.pop(0) if self.content else "{}"
        yield AIMessageChunk(content=text)


def test_url_canonicalization_and_near_title_clustering() -> None:
    assert (
        canonical_url("http://Example.com/a?utm_source=x&id=7#part")
        == "https://example.com/a?id=7"
    )
    first = _article("芯片公司发布新一代 AI 芯片", url="https://a.example/1")
    second = _article(
        "芯片公司发布新一代 AI 芯片！",
        url="https://b.example/2",
        source="Other News",
        minutes_ago=20,
    )
    duplicate = _article(
        "芯片公司发布新一代 AI 芯片",
        url="https://a.example/1?utm_source=rss",
    )
    candidates = build_candidates(
        [first, second, duplicate], category="technology", limit=18
    )
    assert len(candidates) == 1
    assert candidates[0].source_count == 2


@pytest.mark.anyio
async def test_rss_fixture_parses_timestamps_and_publisher() -> None:
    rss = b"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><title>Demo</title>
      <item>
        <title>AI chip policy announced</title>
        <link>https://publisher.example/story?utm_source=rss</link>
        <description><![CDATA[<p>A verified summary.</p>]]></description>
        <pubDate>Mon, 19 Oct 2026 23:30:00 GMT</pubDate>
        <source>Publisher Desk</source>
      </item>
    </channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "feed.example"
        return httpx.Response(200, content=rss)

    fetcher = NewsFetcher(transport=httpx.MockTransport(handler))
    rows = await fetcher._fetch_rss(
        category="technology",
        url="https://feed.example/rss",
        source_hint="Fallback",
        tier="media",
        language="en",
        window_start=NOW - timedelta(hours=24),
        window_end=NOW,
    )
    assert len(rows) == 1
    assert rows[0].canonical_url == "https://publisher.example/story"
    assert rows[0].summary == "A verified summary."
    assert rows[0].source_name == "Publisher Desk"
    assert rows[0].published_at == datetime(
        2026, 10, 19, 23, 30, tzinfo=timezone.utc
    )
    assert rows[0].time_basis == "published"


@pytest.mark.anyio
async def test_chinanews_shared_feed_routes_selected_categories() -> None:
    rss = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><title>China News</title>
      <item><title>AI chips enter mass production</title>
        <link>https://www.chinanews.com.cn/tech/1.shtml</link>
        <description>semiconductor and AI development</description>
        <pubDate>Mon, 19 Oct 2026 23:30:00 GMT</pubDate></item>
      <item><title>Electric vehicle recall expands</title>
        <link>https://www.chinanews.com.cn/auto/2.shtml</link>
        <description>automakers respond to 智驾 system issue</description>
        <pubDate>Mon, 19 Oct 2026 23:20:00 GMT</pubDate></item>
    </channel></rss>""".encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.chinanews.com.cn"
        return httpx.Response(200, content=rss)

    settings = get_settings().model_copy(update={"news_hot_api_base_url": ""})
    fetcher = NewsFetcher(settings, transport=httpx.MockTransport(handler))
    rows = await fetcher._fetch_domestic_general(
        selected=["technology", "auto"],
        topics=[],
        window_start=NOW - timedelta(hours=24),
        window_end=NOW,
    )
    by_title = {row.title: row.category for row in rows}
    assert by_title["AI chips enter mass production"] == "technology"
    assert by_title["Electric vehicle recall expands"] == "auto"


@pytest.mark.anyio
async def test_daily_hot_items_use_observed_time_and_aggregator_tier() -> None:
    payload = json.dumps(
        {
            "code": 200,
            "name": "36氪",
            "data": [
                {
                    "title": "芯片公司发布新模型",
                    "desc": "AI 与半导体行业最新进展",
                    "url": "https://example.com/hot/1",
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/36kr"
        return httpx.Response(200, content=payload)

    settings = get_settings().model_copy(
        update={"news_hot_api_base_url": "http://hot.local:6688"}
    )
    fetcher = NewsFetcher(settings, transport=httpx.MockTransport(handler))
    rows = await fetcher._fetch_daily_hot_route(
        route="36kr",
        selected=["technology"],
        topics=[],
        window_end=NOW,
    )
    assert len(rows) == 1
    assert rows[0].category == "technology"
    assert rows[0].source_tier == "aggregator"
    assert rows[0].published_at == NOW
    assert rows[0].time_basis == "observed"


@pytest.mark.anyio
async def test_google_is_not_requested_when_domestic_profile_is_default() -> None:
    rss = b"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><title>China News</title>
      <item><title>AI industry policy</title>
        <link>https://www.chinanews.com.cn/tech/policy.shtml</link>
        <description>technology policy</description>
        <pubDate>Mon, 19 Oct 2026 23:30:00 GMT</pubDate></item>
    </channel></rss>"""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, content=rss)

    settings = get_settings().model_copy(
        update={
            "news_hot_api_base_url": "",
            "news_enable_google": False,
            "news_enable_global": False,
        }
    )
    fetcher = NewsFetcher(settings, transport=httpx.MockTransport(handler))
    result = await fetcher.fetch(
        categories=["technology"],
        custom_topics=[],
        window_start=NOW - timedelta(hours=24),
        window_end=NOW,
    )
    assert result.succeeded == 1
    assert result.provider_stats["domestic_rss"]["succeeded"] == 1
    assert "news.google.com" not in hosts
    assert "techcrunch.com" not in hosts


@pytest.mark.anyio
async def test_daily_hot_failure_does_not_block_domestic_rss() -> None:
    rss = b"""<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><title>China News</title>
      <item><title>AI industry policy</title>
        <link>https://www.chinanews.com.cn/tech/policy.shtml</link>
        <description>technology policy</description>
        <pubDate>Mon, 19 Oct 2026 23:30:00 GMT</pubDate></item>
    </channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.chinanews.com.cn":
            return httpx.Response(200, content=rss)
        return httpx.Response(503, text="hot service unavailable")

    settings = get_settings().model_copy(
        update={"news_hot_api_base_url": "http://hot.local:6688"}
    )
    fetcher = NewsFetcher(settings, transport=httpx.MockTransport(handler))
    result = await fetcher.fetch(
        categories=["technology"],
        custom_topics=[],
        window_start=NOW - timedelta(hours=24),
        window_end=NOW,
    )
    assert result.articles
    assert result.succeeded > 0
    assert result.provider_stats["domestic_rss"]["succeeded"] == 1
    assert result.provider_stats["domestic_hot"]["failed"] > 0


@pytest.mark.anyio
async def test_generate_digest_clusters_sources_and_persists_event(
    temp_session,
) -> None:
    preference = save_preferences(
        temp_session, categories=["technology"], custom_topics=[]
    )
    digest = create_manual_digest(temp_session, now=NOW)
    fetcher = FakeFetcher(
        FetchResult(
            articles=[
                _article("芯片公司发布新一代 AI 芯片", url="https://a.example/1"),
                _article(
                    "芯片公司发布新一代 AI 芯片！",
                    url="https://b.example/2",
                    source="Other News",
                ),
            ],
            attempted=2,
            succeeded=2,
        )
    )
    llm = ScriptedLLM(
        [
            (
                '{"events":[{"candidate_ids":["c1"],"category":"technology",'
                '"title":"芯片公司发布新一代 AI 芯片","original_title":"芯片公司发布新一代 AI 芯片",'
                '"summary":"公司发布新芯片。","why_it_matters":"影响 AI 算力供给。",'
                '"importance":8.2,"confidence":0.9}]}'
            )
        ]
    )
    await generate_digest(
        temp_session,
        digest,
        fetcher=fetcher,
        llm=llm,
        settings=get_settings(),
        now=NOW,
    )
    temp_session.commit()

    assert preference.categories == ["technology"]
    assert digest.status is NewsDigestStatus.ready
    assert digest.degraded is False
    event = temp_session.scalar(select(NewsEvent))
    assert event is not None and event.source_count == 2
    payload = digest_read(temp_session, digest)
    assert payload.events[0].importance == 8.2
    assert payload.events[0].category_label == "科技"
    assert len(payload.events[0].sources) == 2
    NewsDigestRead.model_validate(payload.model_dump())


@pytest.mark.anyio
async def test_invalid_model_output_degrades_to_deterministic_snapshot(
    temp_session,
) -> None:
    save_preferences(temp_session, categories=["technology"], custom_topics=[])
    digest = create_manual_digest(temp_session, now=NOW)
    fake = FakeFetcher(
        FetchResult(
            articles=[
                _article(
                    "重要芯片政策正式发布",
                    url="https://a.example/policy",
                )
            ],
            attempted=1,
            succeeded=1,
        )
    )
    llm = ScriptedLLM(["不是 JSON", "仍然不是 JSON"])
    await generate_digest(
        temp_session,
        digest,
        fetcher=fake,
        llm=llm,
        settings=get_settings(),
        now=NOW,
    )
    temp_session.commit()
    assert digest.status is NewsDigestStatus.ready
    assert digest.degraded is True
    assert "AI 摘要" in " ".join(digest.warnings)
    assert temp_session.scalar(select(NewsEvent)) is not None
    assert len(llm.calls) == 2


@pytest.mark.anyio
async def test_all_sources_failing_marks_digest_failed(temp_session) -> None:
    save_preferences(temp_session, categories=["technology"], custom_topics=[])
    digest = create_manual_digest(temp_session, now=NOW)
    fetcher = FakeFetcher(
        FetchResult(
            articles=[],
            attempted=3,
            succeeded=0,
            errors=["a: timeout", "b: 503", "c: parse error"],
        )
    )
    await generate_digest(
        temp_session,
        digest,
        fetcher=fetcher,
        llm=ScriptedLLM([]),
        now=NOW,
    )
    assert digest.status is NewsDigestStatus.failed
    assert "所有新闻来源" in (digest.error or "")


def test_retention_cleanup_deletes_only_old_digests(temp_session) -> None:
    old = NewsDigest(
        edition_date="2026-01-01",
        window_start=NOW - timedelta(days=2),
        window_end=NOW - timedelta(days=1),
        categories=["technology"],
        custom_topics=[],
        trigger="manual",
        status=NewsDigestStatus.ready,
        created_at=NOW - timedelta(days=40),
        completed_at=NOW - timedelta(days=40),
    )
    recent = NewsDigest(
        edition_date="2026-10-20",
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        categories=["technology"],
        custom_topics=[],
        trigger="manual",
        status=NewsDigestStatus.ready,
        created_at=NOW - timedelta(days=2),
        completed_at=NOW - timedelta(days=2),
    )
    temp_session.add_all([old, recent])
    temp_session.flush()
    assert cleanup_news_history(temp_session, now=NOW) == 1
    assert temp_session.get(NewsDigest, old.id) is None
    assert temp_session.get(NewsDigest, recent.id) is not None


def test_stale_running_digest_is_failed_and_can_be_recreated(temp_session) -> None:
    save_preferences(temp_session, categories=["technology"], custom_topics=[])
    row = NewsDigest(
        edition_date="2026-10-20",
        window_start=NOW - timedelta(hours=24),
        window_end=NOW,
        categories=["technology"],
        custom_topics=[],
        trigger="manual",
        status=NewsDigestStatus.running,
        created_at=utc_now() - timedelta(minutes=11),
    )
    temp_session.add(row)
    temp_session.flush()
    assert (
        find_active_digest(
            temp_session,
            categories=["technology"],
            custom_topics=[],
        )
        is None
    )
    assert row.status is NewsDigestStatus.failed


def _seed_news_event(session) -> tuple[NewsDigest, NewsEvent, NewsArticle]:
    digest = NewsDigest(
        edition_date="2026-10-20",
        window_start=NOW - timedelta(hours=24),
        window_end=NOW,
        categories=["technology"],
        custom_topics=[],
        trigger="manual",
        status=NewsDigestStatus.ready,
        completed_at=NOW,
    )
    session.add(digest)
    session.flush()
    article = NewsArticle(
        digest_id=digest.id,
        category="technology",
        title="AI 芯片出货量创新高",
        original_title="AI chip shipments hit record",
        summary="多家研究机构确认芯片出货增长。",
        url="https://example.com/chip",
        canonical_url="https://example.com/chip",
        source_name="Example",
        source_domain="example.com",
        source_tier="media",
        language="zh",
        published_at=NOW - timedelta(hours=1),
        time_basis="observed",
    )
    session.add(article)
    session.flush()
    event = NewsEvent(
        digest_id=digest.id,
        category="technology",
        rank=1,
        title="AI 芯片出货量创新高",
        original_title="AI chip shipments hit record",
        summary="多家研究机构确认芯片出货增长。",
        why_it_matters="影响 AI 算力供给。",
        importance=8.0,
        confidence=0.9,
        source_count=1,
        article_ids=[article.id],
        published_at=article.published_at,
    )
    session.add(event)
    session.commit()
    return digest, event, article


def test_cached_search_and_digest_serialization(temp_session) -> None:
    digest, event, article = _seed_news_event(temp_session)
    results = search_cached_news(temp_session, query="芯片 出货", limit=5)
    assert results
    assert results[0]["article_id"] == article.id
    generic = search_cached_news(
        temp_session,
        query="科技 重要新闻",
        categories=["technology"],
        limit=5,
    )
    assert generic
    assert generic[0]["event_id"] == event.id
    assert generic[0]["importance"] == 8.0
    assert generic[0]["time_basis"] == "observed"
    payload = digest_read(temp_session, digest)
    assert payload.events[0].id == event.id
    assert payload.events[0].time_basis == "observed"
    assert payload.events[0].sources[0].source_name == "Example"


@pytest.mark.anyio
async def test_news_agent_checks_cache_before_live_search(temp_session) -> None:
    _, event, article = _seed_news_event(temp_session)
    llm = ScriptedLLM(
        [
            "",
            "根据缓存，AI 芯片出货量创新高 [Example, 2026-10-20](https://example.com/chip)。",
        ]
    )
    original_astream = llm.astream

    call_count = 0

    async def scripted(messages):
        nonlocal call_count
        call_count += 1
        llm.calls.append(list(messages))
        if call_count == 1:
            yield AIMessageChunk(
                content="",
                tool_calls=[
                    {
                        "name": "search_cached_news",
                        "args": {"query": "芯片"},
                        "id": "search-1",
                    }
                ],
            )
        else:
            yield AIMessageChunk(
                content=(
                    "根据缓存，AI 芯片出货量创新高 "
                    "[Example, 2026-10-20](https://example.com/chip)。"
                )
            )

    llm.astream = scripted  # type: ignore[method-assign]
    agent = NewsAgent(
        temp_session,
        selected_categories=["technology"],
        llm=llm,
    )
    events = []
    async for item in agent.astream("最近芯片新闻有什么进展？"):
        events.append(item)
    assert {tool.name for tool in llm.bound_tools} == {
        "search_cached_news",
        "search_live_news",
        "get_news_event",
        "read_news_article",
    }
    kinds = [item.type for item in events]
    assert "tool_call" in kinds
    assert "done" in kinds
    tool_result = next(item for item in events if item.type == "tool_result")
    assert article.id in tool_result.data["content"]
    system_prompt = llm.calls[0][0].content
    assert "不可信外部数据" in system_prompt
    assert "没有检索到相关报道" in system_prompt
    assert "没有收到问题" in system_prompt


@pytest.mark.anyio
async def test_news_agent_accepts_category_labels_and_json_string(temp_session) -> None:
    _seed_news_event(temp_session)
    llm = ScriptedLLM([])
    call_count = 0

    async def scripted(messages):
        nonlocal call_count
        call_count += 1
        llm.calls.append(list(messages))
        if call_count == 1:
            yield AIMessageChunk(
                content="",
                tool_calls=[
                    {
                        "name": "search_cached_news",
                        "args": {
                            "query": "芯片",
                            "categories": '["科技"]',
                        },
                        "id": "label-category",
                    }
                ],
            )
        else:
            yield AIMessageChunk(content="找到了芯片新闻。")

    llm.astream = scripted  # type: ignore[method-assign]
    agent = NewsAgent(
        temp_session,
        selected_categories=["technology"],
        llm=llm,
    )
    events = [item async for item in agent.astream("芯片有什么新闻？")]
    result = next(item for item in events if item.type == "tool_result")
    assert '"ok": true' in result.data["content"]
    assert "AI 芯片出货量创新高" in result.data["content"]


@pytest.mark.anyio
async def test_news_agent_can_targeted_live_search_when_cache_is_empty(
    temp_session,
) -> None:
    live = FakeFetcher(
        FetchResult(
            articles=[
                _article(
                    "机器人产业发布新规",
                    url="https://live.example/robot-policy",
                    source="Live Example",
                )
            ],
            attempted=2,
            succeeded=2,
        )
    )
    llm = ScriptedLLM([])
    call_count = 0

    async def scripted(messages):
        nonlocal call_count
        call_count += 1
        llm.calls.append(list(messages))
        if call_count == 1:
            yield AIMessageChunk(
                content="",
                tool_calls=[
                    {
                        "name": "search_live_news",
                        "args": {"query": "机器人 新规"},
                        "id": "live-1",
                    }
                ],
            )
        else:
            yield AIMessageChunk(content="找到一条新规报道。")

    llm.astream = scripted  # type: ignore[method-assign]
    agent = NewsAgent(
        temp_session,
        selected_categories=["technology"],
        llm=llm,
        live_fetcher=live,
    )
    events = [item async for item in agent.astream("机器人新规有什么最新消息？")]
    tool_result = next(item for item in events if item.type == "tool_result")
    assert "机器人产业发布新规" in tool_result.data["content"]
    assert live.calls == 1


@pytest.mark.anyio
async def test_live_search_respects_selected_categories_and_time_basis() -> None:
    observed = RawArticle(
        category="technology",
        title="机器人企业发布新进展",
        summary="产业进展",
        url="https://hot.example/robot",
        canonical_url="https://hot.example/robot",
        source_name="IT之家热榜",
        source_domain="hot.example",
        source_tier="aggregator",
        language="zh",
        published_at=NOW,
        time_basis="observed",
    )
    fetcher = FakeFetcher(
        FetchResult(articles=[observed], attempted=2, succeeded=2)
    )
    rows = await search_live_news(
        "机器人",
        categories=["technology", "auto"],
        fetcher=fetcher,
        limit=5,
    )
    assert rows[0]["time_basis"] == "observed"
    assert fetcher.last_search_kwargs
    assert fetcher.last_search_kwargs["categories"] == ["technology", "auto"]

