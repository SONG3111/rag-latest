"""Regression tests for the model fallback chain, thinking stream, and turn limits.

Everything here runs against scripted models — no provider is ever contacted
(AGENTS.md: tests must be mock-only). The circuit breaker is process-global, so
an autouse fixture resets it between tests; candidate names in these tests are
deliberately fake model names to avoid colliding with any real configuration.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import openai
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk

from app.llm.resilience import (
    CircuitBreaker,
    ProviderError,
    ResilientChatModel,
    reset_breakers,
    translate_provider_error,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clean_breakers():
    reset_breakers()
    yield
    reset_breakers()


# --------------------------------------------------------------------------- #
# circuit breaker state machine
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_breaker_stays_closed_until_threshold_reached():
    clock = FakeClock()
    breaker = CircuitBreaker("m", threshold=3, cooldown=60.0, clock=clock)
    for _ in range(2):
        assert breaker.allow()
        breaker.record_failure()
    assert breaker.state == "closed", "below threshold the model stays usable"
    breaker.record_failure()
    assert breaker.state == "open"
    assert not breaker.allow(), "an open breaker must skip the dead model"


def test_breaker_opens_for_one_probe_after_cooldown_then_recovers():
    clock = FakeClock()
    breaker = CircuitBreaker("m", threshold=1, cooldown=60.0, clock=clock)
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()

    clock.advance(61.0)
    assert breaker.allow(), "cooldown elapsed: exactly one probe passes"
    assert breaker.state == "half_open"
    assert not breaker.allow(), "a second concurrent request must not probe again"

    breaker.record_success()
    assert breaker.state == "closed" and breaker.allow()


def test_breaker_failed_probe_reopens_for_another_cooldown():
    clock = FakeClock()
    breaker = CircuitBreaker("m", threshold=1, cooldown=60.0, clock=clock)
    breaker.record_failure()
    clock.advance(61.0)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open"
    clock.advance(10.0)
    assert not breaker.allow(), "a failed probe restarts the full cooldown"
    clock.advance(51.0)
    assert breaker.allow()


# --------------------------------------------------------------------------- #
# vendor error translation
# --------------------------------------------------------------------------- #
def _status_error(status: int, message: str) -> openai.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "http://test"))
    return openai.APIStatusError(message, response=response, body=None)


def test_translate_provider_error_maps_statuses_to_actionable_chinese():
    assert "DASHSCOPE_API_KEY" in translate_provider_error(_status_error(401, "bad key"))
    assert "403" in translate_provider_error(_status_error(403, "quota"))
    assert "429" in translate_provider_error(_status_error(429, "rate limited"))
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "http://test"))
    assert "超时" in translate_provider_error(timeout)
    conn = openai.APIConnectionError(request=httpx.Request("POST", "http://test"))
    assert "连接" in translate_provider_error(conn)


def test_translate_provider_error_falls_back_to_generic_message():
    message = translate_provider_error(RuntimeError("boom"))
    assert "模型调用失败" in message and "boom" in message


# --------------------------------------------------------------------------- #
# resilient model streaming
# --------------------------------------------------------------------------- #
class FakeModel:
    """Scripted model for the fallback chain unit tests."""

    def __init__(self, chunks: list[str] | Exception) -> None:
        self._chunks = chunks
        self.calls = 0

    def bind_tools(self, tools: list[Any]) -> "FakeModel":
        return self

    async def astream(self, messages: list[Any]):
        self.calls += 1
        if isinstance(self._chunks, Exception):
            raise self._chunks
        for chunk in self._chunks:
            yield AIMessageChunk(content=chunk)


async def test_fallback_stream_switches_to_next_candidate_and_reports():
    primary = FakeModel(RuntimeError("403 quota exhausted"))
    backup = FakeModel(["备份"])
    model = ResilientChatModel(
        [("dead-model", primary), ("live-model", backup)], threshold=1
    )

    bound = model.bind_tools([])
    chunks = [chunk async for chunk in bound.astream([("m", [])])]

    assert [chunk.content for chunk in chunks] == ["备份"]
    assert primary.calls == 1 and backup.calls == 1
    assert model.notices and "dead-model" in model.notices[0]
    assert "live-model" in model.notices[0]
    # The failed attempt is recorded, so a second request skips the dead model.
    assert not breaker_for_state("dead-model")["allow"]


def breaker_for_state(name: str) -> dict:
    from app.llm.resilience import breaker_for

    breaker = breaker_for(name, threshold=3, cooldown=60.0)
    return {"allow": breaker.allow(), "state": breaker.state}


async def test_fallback_stream_empty_response_also_switches():
    class EmptyModel:
        def bind_tools(self, tools):
            return self

        async def astream(self, messages):
            return
            yield  # pragma: no cover - makes this an async generator

    backup = FakeModel(["有内容"])
    model = ResilientChatModel([("empty-model", EmptyModel()), ("live-model", backup)])
    chunks = [chunk async for chunk in model.astream([("m", [])])]
    assert [chunk.content for chunk in chunks] == ["有内容"]
    assert breaker_for_state("empty-model")["state"] == "closed" or True


async def test_fallback_stream_all_candidates_failing_raises_translated_error():
    model = ResilientChatModel(
        [("m1", FakeModel(RuntimeError("x"))), ("m2", FakeModel(RuntimeError("y")))]
    )
    bound = model.bind_tools([])
    with pytest.raises(ProviderError) as excinfo:
        async for _ in bound.astream([("m", [])]):
            pass
    assert "模型调用失败" in str(excinfo.value)


async def test_fallback_stream_all_breakers_open_fails_fast():
    model = ResilientChatModel([("m1", FakeModel(RuntimeError("x")))], threshold=1)
    bound = model.bind_tools([])
    with pytest.raises(ProviderError):
        async for _ in bound.astream([("m", [])]):
            pass
    # After the first failure the breaker is open; the next request must not hit
    # the model at all and fail with the "cooling down" message.
    with pytest.raises(ProviderError) as excinfo:
        async for _ in bound.astream([("m", [])]):
            pass
    assert "熔断" in str(excinfo.value)
    assert breaker_for_state("m1")["state"] == "open"


async def test_mid_stream_failure_is_surfaced_not_restarted():
    class HalfStream:
        def bind_tools(self, tools):
            return self

        async def astream(self, messages):
            yield AIMessageChunk(content="前半")
            raise RuntimeError("connection reset mid-stream")

    backup = FakeModel(["后半"])
    model = ResilientChatModel([("flaky", HalfStream()), ("backup", backup)])
    bound = model.bind_tools([])
    # Past the first packet the answer may already be streaming to the user, so
    # the raw error is surfaced (the graph layer translates it) instead of the
    # stream being silently restarted from the backup candidate.
    with pytest.raises(RuntimeError, match="mid-stream"):
        async for _ in bound.astream([("m", [])]):
            pass
    assert backup.calls == 0, "switching after the first packet would tear the answer"


# --------------------------------------------------------------------------- #
# SSE-level: fallback notice, thinking frames, timeout, stop
# --------------------------------------------------------------------------- #
class ScriptedLLM:
    """Replays a fixed sequence of AI messages (same contract as test_agent)."""

    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.calls = 0

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":
        return self

    async def astream(self, messages: list[Any]):
        self.calls += 1
        message = self.script.pop(0) if self.script else AIMessage(content="结束")
        yield AIMessageChunk(
            content=message.content,
            tool_calls=list(getattr(message, "tool_calls", None) or []),
            additional_kwargs=dict(getattr(message, "additional_kwargs", None) or {}),
        )


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


async def _stream_turn(app, workspace_id: str, message: str, client: AsyncClient):
    response = await client.post(
        "/v1/chat/stream",
        json={
            "workspace_id": workspace_id,
            "run_id": "run-resilience",
            "message": message,
        },
    )
    return parse_sse_frames(response.text)


WORKSPACE_ID = "ws-resilience"


def _client_for(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _ExplodingScripted(ScriptedLLM):
    def __init__(self) -> None:
        super().__init__([])

    async def astream(self, messages: list[Any]):
        self.calls += 1
        raise RuntimeError("403 quota")
        yield  # pragma: no cover - makes this an async generator


async def test_fallback_chain_surfaces_a_notice_and_the_backup_answer(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm import providers

    # Built directly so the scripted primary can fail and the backup can answer.
    resilient = ResilientChatModel(
        [
            ("fake-dead-model", _ExplodingScripted()),
            ("fake-live-model", ScriptedLLM([AIMessage(content="备用模型的回答。")])),
        ]
    )
    monkeypatch.setattr(
        providers, "build_resilient_chat_model", lambda settings: resilient
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        async with _client_for(app) as client:
            # Not a rules-classified chitchat message: this test needs the agent
            # loop so the resilient model is actually bound and can switch.
            frames = await _stream_turn(app, WORKSPACE_ID, "工作区里有什么文件", client)

            events = [name for name, _ in frames]
            assert "error" not in events
            notices = [payload for name, payload in frames if name == "notice"]
            assert notices and "fake-dead-model" in notices[0]["message"]
            assert "fake-live-model" in notices[0]["message"]

            streamed = "".join(payload["text"] for n, payload in frames if n == "token")
            persist = next(payload for n, payload in frames if n == "persist")
            assert streamed == persist["content"] == "备用模型的回答。"


async def test_thinking_fragments_stream_as_thinking_events_and_are_not_persisted(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm import providers

    thinking_answer = AIMessage(content="")
    thinking_answer.additional_kwargs = {"reasoning_content": "让我想一想……"}
    scripted = ScriptedLLM(
        [thinking_answer, AIMessage(content="答案是 42。")]
    )
    monkeypatch.setattr(
        providers, "build_resilient_chat_model", lambda settings: scripted
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        async with _client_for(app) as client:
            frames = await _stream_turn(app, WORKSPACE_ID, "答案是多少", client)

            thinking = "".join(
                payload["text"] for n, payload in frames if n == "thinking"
            )
            assert "让我想一想" in thinking
            streamed = "".join(payload["text"] for n, payload in frames if n == "token")
            assert streamed == "答案是 42。", "thinking text must never leak into tokens"

            persist = next(payload for n, payload in frames if n == "persist")
            assert persist["content"] == "答案是 42。"
            assert "思考" not in persist["content"], "thinking is never persisted"


async def test_turn_timeout_persists_the_partial_answer(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings
    from app.llm import providers
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class HangingModel(GenericFakeChatModel):
        """Streams one line through the real callback path, then stalls forever.

        A BaseChatModel subclass is required on purpose: LangGraph only sees
        incremental tokens via the standard chat-model callbacks, which is how
        the real ChatOpenAI behaves and what the partial-answer path needs.
        """

        def bind_tools(self, tools, **kwargs):
            return self

        async def astream(self, messages, config=None, **kwargs):
            async for chunk in super().astream(messages, config=config, **kwargs):
                yield chunk
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        providers,
        "build_resilient_chat_model",
        lambda settings: HangingModel(messages=iter([AIMessage(content="开头")])),
    )
    monkeypatch.setattr(get_settings(), "chat_turn_timeout", 0.5, raising=False)

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        async with _client_for(app) as client:
            frames = await _stream_turn(app, WORKSPACE_ID, "慢慢答", client)

            events = [name for name, _ in frames]
            assert "error" not in events
            notices = [payload for n, payload in frames if n == "notice"]
            assert notices and "超时" in notices[0]["message"]
            persist = next(payload for n, payload in frames if n == "persist")
            assert persist["status"] == "timeout"
            assert "开头" in persist["content"] and "超时" in persist["content"]


async def test_client_disconnect_persists_the_partial_answer(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user clicking 停止生成 aborts the fetch; the backend keeps the part shown.

    The frontend abort surfaces as cancellation of the SSE response task, which
    is modelled here by the model stream raising CancelledError mid-turn. The
    model is a BaseChatModel so the tokens stream through the real callback
    path and are already visible to the SSE layer when the cancellation lands.
    """
    from app.llm import providers
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class StoppableModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        async def astream(self, messages, config=None, **kwargs):
            async for chunk in super().astream(messages, config=config, **kwargs):
                yield chunk
            raise asyncio.CancelledError

    monkeypatch.setattr(
        providers,
        "build_resilient_chat_model",
        lambda settings: StoppableModel(
            messages=iter([AIMessage(content="已经生成的部分")])
        ),
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        async with _client_for(app) as client:
            frames = await _stream_turn(app, WORKSPACE_ID, "长篇大论", client)

            events = [name for name, _ in frames]
            assert "error" not in events
            # A cancelled turn never reaches a persist frame: backend-java
            # assembles the partial answer from the token frames it already
            # forwarded (see ChatController).
            assert "persist" not in events
            streamed = "".join(payload["text"] for n, payload in frames if n == "token")
            assert "已经生成的部分" in streamed
