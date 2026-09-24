"""Regression tests for the context-overflow compact-and-retry path.

The model is scripted and the emergency compaction's summarizer is stubbed, so
nothing here contacts a real API (AGENTS.md: tests are mock-only). What is
pinned: the retry fires exactly once, only for overflow-shaped errors, and only
before the first packet; the user sees a notice first; a persistent overflow
surfaces the error frame after that one retry; a mid-stream overflow never
retries (the streamed half-answer must not be torn).

The turn is driven through the stateless ``/v1/chat/stream`` endpoint (history
arrives in the request; the durable half of the fold rides the persist frame's
``summary`` for backend-java to store).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from app.llm.providers import ProviderError

pytestmark = pytest.mark.anyio

OVERFLOW_TEXT = (
    "模型服务返回错误（400）：This model's maximum context length is 4096 tokens. "
    "However, your messages resulted in 8192 tokens. Please reduce the length."
)


class OverflowOnceLLM:
    """First call dies with a provider-confirmed overflow, then it answers."""

    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]) -> "OverflowOnceLLM":
        return self

    async def astream(self, messages: list[Any]):
        self.calls += 1
        self.inputs.append(list(messages))
        if self.calls == 1:
            raise ProviderError(OVERFLOW_TEXT)
        yield AIMessageChunk(content="压缩后回答成功")


class OverflowAlwaysLLM(OverflowOnceLLM):
    async def astream(self, messages: list[Any]):
        self.calls += 1
        self.inputs.append(list(messages))
        raise ProviderError(OVERFLOW_TEXT)
        yield  # pragma: no cover - makes this an async generator


class TextThenOverflowLLM(OverflowOnceLLM):
    """Loop iteration 1 streams text (plus a failing tool call); iteration 2's
    request overflows. The iteration-1 text already reached the user, so the
    guard must refuse to retry even though the overflow error itself is clean."""

    async def astream(self, messages: list[Any]):
        self.calls += 1
        self.inputs.append(list(messages))
        if self.calls == 1:
            yield AIMessageChunk(
                content="先看一眼表格。",
                tool_calls=[{"name": "no_such_tool", "args": {}, "id": "call-1"}],
            )
            return
        raise ProviderError(OVERFLOW_TEXT)


def _history(turns: int) -> list[dict]:
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"第{index}轮消息"}
        for index in range(turns)
    ]


def _install(monkeypatch: pytest.MonkeyPatch, model) -> None:
    from app.llm import providers

    monkeypatch.setattr(providers, "build_resilient_chat_model", lambda settings: model)
    # The forced fold must not reach for the real rewrite channel.
    monkeypatch.setattr(
        "app.services.memory._summarize", lambda settings, prompt: "应急摘要"
    )


async def _stream_turn(
    app, workspace_id: str, message: str, messages: list[dict] | None = None
):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/stream",
            json={
                "workspace_id": workspace_id,
                "run_id": "run-overflow",
                "message": message,
                "messages": messages or [],
            },
        )
    return response.text


def _frames(body: str) -> list[tuple[str, dict]]:
    import json
    import re

    frames: list[tuple[str, dict]] = []
    for block in re.split(r"\r?\n\r?\n", body):
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


def test_is_context_overflow_error_walks_the_cause_chain():
    from app.llm.resilience import is_context_overflow_error

    assert is_context_overflow_error(RuntimeError(OVERFLOW_TEXT)) is True
    # The resilient wrapper raises the translated sentence with the vendor error
    # attached as __cause__; the detection must find the marker there.
    wrapped = ProviderError("模型服务返回错误（400）：请求被拒绝")
    wrapped.__cause__ = RuntimeError("input length exceed the limit")
    assert is_context_overflow_error(wrapped) is True
    assert is_context_overflow_error(RuntimeError("模型配额已用尽")) is False


async def test_overflow_triggers_one_forced_compaction_and_retry(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = OverflowOnceLLM()
    _install(monkeypatch, model)

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        body = await _stream_turn(
            app, "ws-overflow", "继续刚才的话题", messages=_history(25)
        )
        frames = _frames(body)

    # The user is told about the recovery, then gets a real answer.
    notices = [payload for name, payload in frames if name == "notice"]
    assert any("压缩" in payload.get("message", "") for payload in notices)
    persist = next(payload for name, payload in frames if name == "persist")
    assert persist["status"] == "ok"
    assert persist["content"] == "压缩后回答成功"

    # Exactly one retry — not a loop.
    assert model.calls == 2

    # The durable half: the forced fold moved the bookmark so the next turn
    # starts from a slimmer history. The bookmark rides the persist frame for
    # backend-java to store (26 rows = 25 request messages + this user turn,
    # 20 kept verbatim).
    assert persist["summary"] == {"text": "应急摘要", "covered_count": 6}


async def test_persistent_overflow_surfaces_the_error_after_one_retry(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = OverflowAlwaysLLM()
    _install(monkeypatch, model)

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        body = await _stream_turn(app, "ws-overflow", "再来一次")

    notices = [payload for name, payload in _frames(body) if name == "notice"]
    errors = [payload for name, payload in _frames(body) if name == "error"]
    assert len(notices) == 1
    assert len(errors) == 1
    assert model.calls == 2


async def test_overflow_with_no_user_visible_tokens_still_retries(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model yields a chunk then dies — but LangGraph drops the pending chunk
    when the stream raises, so no token frame ever reached the user. The retry is
    therefore clean: it fires (nothing was user-visible) and never re-streams
    content that would duplicate a half answer."""

    class ChunkThenDie(OverflowOnceLLM):
        async def astream(self, messages: list[Any]):
            self.calls += 1
            self.inputs.append(list(messages))
            if self.calls == 1:
                yield AIMessageChunk(content="已经流出的半句")
                raise ProviderError(OVERFLOW_TEXT)
            yield AIMessageChunk(content="压缩后回答成功")

    model = ChunkThenDie()
    _install(monkeypatch, model)

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        body = await _stream_turn(app, "ws-overflow", "再说一次")

    frames = _frames(body)
    assert model.calls == 2
    # The dropped chunk never reached the user: the only streamed text is the
    # retry's answer, and the notice frame precedes any token frame.
    tokens = [payload.get("text") for name, payload in frames if name == "token"]
    assert tokens == ["压缩后回答成功"]
    names = [name for name, _ in frames]
    assert names.index("notice") < names.index("token")
    persist = next(payload for name, payload in frames if name == "persist")
    assert persist["content"] == "压缩后回答成功"


async def test_overflow_after_streamed_text_is_not_retried(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Iteration 1's text already streamed to the user: an overflow in a later
    model call must surface as an error, not tear the answer with a retry."""
    model = TextThenOverflowLLM()
    _install(monkeypatch, model)

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        body = await _stream_turn(app, "ws-overflow", "再说一次")

    frames = _frames(body)
    # Two loop iterations happened; the guard refused a third (retry) attempt.
    assert model.calls == 2
    assert not [payload for name, payload in frames if name == "notice"]
    assert [payload for name, payload in frames if name == "error"]
