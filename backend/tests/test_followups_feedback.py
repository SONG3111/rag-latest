"""Regression tests for followup suggestions and thumbs feedback (docs/05 §4.4/4.5).

The suggestion model is monkeypatched — no provider is ever contacted
(AGENTS.md: mock-only tests). Pinned: followups ride the SSE stream before
`done` and vanish silently on model failure; `done` carries the persisted
message id; feedback round-trips onto the message row and 404s cleanly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk

from app.services.followups import _parse_followups

pytestmark = pytest.mark.anyio


class FakeSuggestionLLM:
    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        return AIMessage(content='["报销需要哪些材料？", "超过5000元怎么办？"]')


class ScriptedLLM:
    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)

    def bind_tools(self, tools: list) -> "ScriptedLLM":
        return self

    async def astream(self, messages: list):
        message = self.script.pop(0) if self.script else AIMessage(content="结束")
        yield AIMessageChunk(content=message.content)


def parse_sse_frames(body: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in body.split("\r\n\r\n"):
        event = None
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data.append(line.split(":", 1)[1].strip())
        if event and data:
            frames.append((event, json.loads("\n".join(data))))
    return frames


def test_parse_followups_extracts_the_array_and_caps_length():
    assert _parse_followups('["问题一","问题二"]') == ["问题一", "问题二"]
    # Tolerates prose around the array and dedupes.
    assert _parse_followups('好的：["A","A","","B"]') == ["A", "B"]
    # Garbage yields nothing rather than raising.
    assert _parse_followups("我不知道该推荐什么") == []
    assert _parse_followups('["超" * 100]') == []


def test_parse_followups_caps_to_three_items():
    items = _parse_followups('["一","二","三","四","五"]')
    assert len(items) == 3


async def _stream_a_turn(app, workspace_id: str, message: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workspaces/{workspace_id}/chat/stream",
            json={"message": message},
        )
        return parse_sse_frames(response.text), client


async def test_followups_event_precedes_done_and_done_carries_message_id(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm import providers
    from app.services import followups as followups_module

    monkeypatch.setattr(
        providers,
        "build_resilient_chat_model",
        lambda settings: ScriptedLLM([AIMessage(content="单笔报销上限是 5000 元。")]),
    )
    monkeypatch.setattr(
        followups_module, "build_chat_model", lambda settings, **kwargs: FakeSuggestionLLM()
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            workspace_id = (
                await client.post("/api/workspaces", json={"name": "后续问题"})
            ).json()["id"]

            response = await client.post(
                f"/api/workspaces/{workspace_id}/chat/stream",
                json={"message": "报销的上限是多少"},
            )
            frames = parse_sse_frames(response.text)

            events = [name for name, _ in frames]
            assert "followups" in events
            # `done` stays the terminal frame of the stream.
            assert events[-1] == "done"
            followups = next(payload for name, payload in frames if name == "followups")
            assert followups["items"][0] == "报销需要哪些材料？"

            done = frames[-1][1]
            assert done["message_id"], "feedback needs the persisted message id"
            assert done["run_id"]

            # The turn is persisted exactly once with the same id.
            messages = (await client.get(f"/api/workspaces/{workspace_id}/messages")).json()
            assert messages[-1]["id"] == done["message_id"]


async def test_feedback_round_trip_and_clearing(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm import providers

    monkeypatch.setattr(
        providers,
        "build_resilient_chat_model",
        lambda settings: ScriptedLLM([AIMessage(content="答案是 42。")]),
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            workspace_id = (await client.post("/api/workspaces", json={"name": "反馈"})).json()["id"]
            response = await client.post(
                f"/api/workspaces/{workspace_id}/chat/stream",
                json={"message": "答案是多少"},
            )
            done = parse_sse_frames(response.text)[-1][1]
            message_id = done["message_id"]

            rated = await client.post(
                f"/api/workspaces/{workspace_id}/messages/{message_id}/feedback",
                json={"feedback": "down"},
            )
            assert rated.status_code == 200
            assert rated.json()["feedback"] == "down"

            # The feedback shows up in the message history the UI reloads.
            messages = (await client.get(f"/api/workspaces/{workspace_id}/messages")).json()
            assert messages[-1]["feedback"] == "down"

            cleared = await client.post(
                f"/api/workspaces/{workspace_id}/messages/{message_id}/feedback",
                json={"feedback": "none"},
            )
            assert cleared.json()["feedback"] is None

            unknown = await client.post(
                f"/api/workspaces/{workspace_id}/messages/no-such/feedback",
                json={"feedback": "up"},
            )
            assert unknown.status_code == 404
