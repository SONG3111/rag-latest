"""Regression tests for followup suggestions (docs/05 §4.4/4.5).

The suggestion model is monkeypatched — no provider is ever contacted
(AGENTS.md: mock-only tests). Pinned: followups ride the SSE stream before the
terminal persist frame and vanish silently on model failure. The thumbs
feedback endpoint moved to backend-java with the rest of the message store
(see WorkspaceApiTests there); the parse-side contract stays here.
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


async def test_followups_event_precedes_the_terminal_persist_frame(
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
            response = await client.post(
                "/v1/chat/stream",
                json={
                    "workspace_id": "ws-followups",
                    "run_id": "run-followups",
                    "message": "报销的上限是多少",
                },
            )
            frames = parse_sse_frames(response.text)

            events = [name for name, _ in frames]
            assert "followups" in events
            # persist stays the terminal frame of the stateless stream.
            assert events[-1] == "persist"
            followups = next(payload for name, payload in frames if name == "followups")
            assert followups["items"][0] == "报销需要哪些材料？"
            # followups ride the stream before the terminal frame.
            assert events.index("followups") < events.index("persist")


async def test_followups_failure_is_silent(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken suggestion channel must not break the chat turn."""

    class ExplodingSuggestionLLM:
        async def ainvoke(self, messages: list[Any]) -> AIMessage:
            raise RuntimeError("suggestion channel down")

    from app.llm import providers
    from app.services import followups as followups_module

    monkeypatch.setattr(
        providers,
        "build_resilient_chat_model",
        lambda settings: ScriptedLLM([AIMessage(content="答案是 42。")]),
    )
    monkeypatch.setattr(
        followups_module,
        "build_chat_model",
        lambda settings, **kwargs: ExplodingSuggestionLLM(),
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/stream",
                json={
                    "workspace_id": "ws-followups",
                    "run_id": "run-followups",
                    "message": "答案是多少",
                },
            )
            frames = parse_sse_frames(response.text)

            events = [name for name, _ in frames]
            assert "followups" not in events
            assert events[-1] == "persist"
            assert frames[-1][1]["content"] == "答案是 42。"
