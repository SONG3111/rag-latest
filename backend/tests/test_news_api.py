"""HTTP tests for news preferences, digest polling, history, and health."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from app.models import NewsDigest, NewsDigestStatus


@pytest.mark.anyio
async def test_news_preferences_and_categories(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            categories = await http.get("/api/news/categories")
            assert categories.status_code == 200
            assert len(categories.json()["categories"]) == 10
            assert categories.json()["defaults"] == [
                "technology",
                "finance",
                "world",
            ]

            initial = await http.get("/api/news/preferences")
            assert initial.status_code == 200
            assert initial.json()["categories"] == [
                "technology",
                "finance",
                "world",
            ]

            updated = await http.put(
                "/api/news/preferences",
                json={
                    "categories": ["technology", "auto"],
                    "custom_topics": ["机器人"],
                },
            )
            assert updated.status_code == 200
            assert updated.json()["custom_topics"] == ["机器人"]

            invalid = await http.put(
                "/api/news/preferences",
                json={"categories": ["unknown"], "custom_topics": []},
            )
            assert invalid.status_code == 422


@pytest.mark.anyio
async def test_refresh_is_202_and_returns_pollable_digest(
    app_with_temp_storage, monkeypatch
) -> None:
    app = app_with_temp_storage
    calls: list[str] = []

    async def fake_job(digest_id: str) -> None:
        calls.append(digest_id)

    monkeypatch.setattr("app.news.routes.run_digest_job", fake_job)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            response = await http.post("/api/news/digests/refresh")
            assert response.status_code == 202
            payload = response.json()
            assert payload["status"] in {"pending", "running"}
            assert payload["categories"] == ["technology", "finance", "world"]
            assert calls == [payload["id"]]

            duplicate = await http.post("/api/news/digests/refresh")
            assert duplicate.status_code == 202
            assert duplicate.json()["id"] == payload["id"]
            assert calls == [payload["id"]]

            polled = await http.get(f"/api/news/digests/{payload['id']}")
            assert polled.status_code == 200
            assert polled.json()["id"] == payload["id"]

            missing = await http.get("/api/news/digests/not-found")
            assert missing.status_code == 404


@pytest.mark.anyio
async def test_latest_digest_messages_and_health(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    now = datetime.now(timezone.utc)
    session = app.state.test_session_factory()
    try:
        row = NewsDigest(
            edition_date="2026-10-20",
            window_start=now - timedelta(hours=24),
            window_end=now,
            categories=["technology"],
            custom_topics=[],
            trigger="manual",
            status=NewsDigestStatus.ready,
            completed_at=now,
        )
        session.add(row)
        session.commit()
        digest_id = row.id
    finally:
        session.close()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            latest = await http.get(
                "/api/news/digests/latest?date=2026-10-20"
            )
            assert latest.status_code == 200
            assert latest.json()["id"] == digest_id
            assert latest.json()["events"] == []

            messages = await http.get("/api/news/messages")
            assert messages.status_code == 200
            assert messages.json() == []

            health = await http.get("/api/news/health")
            assert health.status_code == 200
            body = health.json()
            assert body["enabled"] is True
            assert body["generation_mode"] == "on_demand"
            assert body["cache_minutes"] == 15
            assert body["providers"]["domestic_rss"]["enabled"] is True
            assert body["providers"]["google_news"]["enabled"] is False
            assert body["providers"]["global_news"]["enabled"] is False

            global_health = await http.get("/health")
            assert global_health.json()["news"]["enabled"] is True
            assert (
                global_health.json()["news"]["generation_mode"] == "on_demand"
            )


@pytest.mark.anyio
async def test_news_question_generates_digest_before_agent_answer(
    app_with_temp_storage, monkeypatch
) -> None:
    app = app_with_temp_storage
    generated: list[str] = []

    async def fake_generate(session, digest, **_: object):
        generated.append(digest.id)
        digest.status = NewsDigestStatus.ready
        digest.completed_at = datetime.now(timezone.utc)
        session.flush()
        return digest

    class ScriptedLLM:
        def bind_tools(self, tools):
            self.tools = tools
            return self

        async def astream(self, messages):
            yield AIMessageChunk(content="已根据刚生成的新闻快照整理完成。")

    monkeypatch.setattr("app.news.routes.generate_digest", fake_generate)
    monkeypatch.setattr(
        "app.news.agent.build_resilient_chat_model",
        lambda settings: ScriptedLLM(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            response = await http.post(
                "/api/news/chat/stream",
                json={"message": "今天科技行业有什么重大新闻？"},
            )
    assert response.status_code == 200
    assert "event: digest" in response.text
    assert "event: done" in response.text
    assert "已根据刚生成的新闻快照整理完成" in response.text
    assert len(generated) == 1


@pytest.mark.anyio
async def test_news_question_reuses_recent_matching_digest(
    app_with_temp_storage, monkeypatch
) -> None:
    app = app_with_temp_storage
    now = datetime.now(timezone.utc)
    session = app.state.test_session_factory()
    try:
        session.add(
            NewsDigest(
                edition_date=now.astimezone().date().isoformat(),
                window_start=now - timedelta(hours=24),
                window_end=now,
                categories=["technology", "finance", "world"],
                custom_topics=[],
                trigger="manual",
                status=NewsDigestStatus.ready,
                completed_at=now,
            )
        )
        session.commit()
    finally:
        session.close()

    async def unexpected_generate(*args, **kwargs):
        raise AssertionError("recent digest should be reused")

    class ScriptedLLM:
        def bind_tools(self, tools):
            self.tools = tools
            return self

        async def astream(self, messages):
            yield AIMessageChunk(content="使用最近十五分钟内的新闻快照回答。")

    monkeypatch.setattr("app.news.routes.generate_digest", unexpected_generate)
    monkeypatch.setattr(
        "app.news.agent.build_resilient_chat_model",
        lambda settings: ScriptedLLM(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            response = await http.post(
                "/api/news/chat/stream",
                json={"message": "这些新闻有哪些共同影响？"},
            )
    assert response.status_code == 200
    assert "event: digest" not in response.text
    assert "使用最近十五分钟内的新闻快照回答" in response.text
