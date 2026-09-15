"""Regression tests for the intent gate and write-tool masking (docs/05 §3.4).

The classifier's small model is injected or monkeypatched — no real provider is
ever called (AGENTS.md: tests must be mock-only). What is pinned: the
rules-first classification with its conservative direction (write beats query,
query beats chitchat when in doubt), the fail-open fallback when the model is
unavailable, the chitchat short-circuit that skips the agent loop entirely, and
the masking of write tools on lookup-intent turns.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk

from app.agent.intent import Intent, _parse_intent, classify_by_rules, classify_intent

pytestmark = pytest.mark.anyio


class FakeIntentLLM:
    """Stands in for the small classification model (ainvoke contract)."""

    def __init__(self, content: str | Exception) -> None:
        self._content = content
        self.calls: list[list[Any]] = []

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(list(messages))
        if isinstance(self._content, Exception):
            raise self._content
        return AIMessage(content=self._content)


# --------------------------------------------------------------------------- #
# rules-first classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "message,expected",
    [
        ("你好", "chitchat"),
        ("在吗", "chitchat"),
        ("谢谢！", "chitchat"),
        ("你是谁", "chitchat"),
        ("Hello", "chitchat"),
        ("你好，帮我查一下报销上限是多少", None),  # longer than the greeting rule
        ("把 B2 改成 1500", "write"),
        ("在销售表里新增一行汇总", "write"),
        ("帮我修改第 3 条条款", "write"),
        ("报销的上限是多少？", None),
        ("表格里有哪些文件", None),
    ],
)
def test_rules_cover_the_clear_cases(message: str, expected: Intent | None):
    assert classify_by_rules(message) == expected


def test_write_rule_wins_over_the_greeting_rule():
    # A "greeting" that asks for a modification must never short-circuit into a
    # direct answer: the write keyword is checked first.
    assert classify_by_rules("你好，帮我把B2改成1500") == "write"


def test_parse_intent_tolerates_prose_and_rejects_garbage():
    assert _parse_intent('{"intent": "query"}') == "query"
    assert _parse_intent('好的，结果是 {"intent": "chat"} 谢谢') == "chitchat"
    assert _parse_intent("query") is None
    assert _parse_intent("") is None
    assert _parse_intent('{"intent": "other"}') is None


async def test_model_failure_is_fail_open():
    from app.config import get_settings

    result = await classify_intent(
        "报销的上限是多少？",
        llm=FakeIntentLLM(RuntimeError("timeout")),
        settings=get_settings(),
    )
    assert result == "write", "a broken classifier must not mask tools or skip retrieval"


async def test_model_classification_maps_the_three_labels():
    from app.config import get_settings

    settings = get_settings()
    for content, expected in [
        ('{"intent": "chat"}', "chitchat"),
        ('{"intent": "query"}', "query"),
        ('{"intent": "write"}', "write"),
    ]:
        result = await classify_intent(
            "表格里 A型 的销售额是多少？", llm=FakeIntentLLM(content), settings=settings
        )
        assert result == expected


# --------------------------------------------------------------------------- #
# SSE-level routing: chitchat short-circuit and write-tool masking
# --------------------------------------------------------------------------- #
class ScriptedLLM:
    """Same contract as the other SSE tests, plus capture of bound tools."""

    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.bound_tools: list[str] | None = None

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":
        self.bound_tools = [tool.name for tool in tools]
        return self

    async def astream(self, messages: list[Any]):
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


async def _run_turn(app, workspace_id: str, message: str) -> list[tuple[str, dict]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workspaces/{workspace_id}/chat/stream",
            json={"message": message},
        )
        return parse_sse_frames(response.text)


async def _workspace(app) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return (await client.post("/api/workspaces", json={"name": "意图门控"})).json()["id"]


async def test_chitchat_is_answered_without_entering_the_tool_loop(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm import providers

    scripted = ScriptedLLM([AIMessage(content="你好！想查数据还是改文档？")])
    monkeypatch.setattr(
        providers, "build_resilient_chat_model", lambda settings: scripted
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        workspace_id = await _workspace(app)
        frames = await _run_turn(app, workspace_id, "你好")

        events = [name for name, _ in frames]
        assert "tool_call" not in events and "tool_result" not in events
        done = next(payload for name, payload in frames if name == "done")
        assert done["content"] == "你好！想查数据还是改文档？"
        # The direct path never binds tools at all — that is the whole point.
        assert scripted.bound_tools is None


async def test_lookup_intent_masks_write_tools(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agent import intent as intent_module
    from app.llm import providers

    scripted = ScriptedLLM([AIMessage(content="表格里 A型 的销售额是 1000。")])
    monkeypatch.setattr(
        providers, "build_resilient_chat_model", lambda settings: scripted
    )
    monkeypatch.setattr(
        intent_module,
        "build_chat_model",
        lambda settings, **kwargs: FakeIntentLLM('{"intent": "query"}'),
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        workspace_id = await _workspace(app)
        frames = await _run_turn(app, workspace_id, "表格里 A型 的销售额是多少？")

        events = [name for name, _ in frames]
        assert "error" not in events
        assert scripted.bound_tools is not None
        assert "update_cells" not in scripted.bound_tools, "write tools must be masked"
        assert "search_knowledge_base" in scripted.bound_tools, "retrieval stays available"

        done = next(payload for name, payload in frames if name == "done")
        assert "1000" in done["content"]


async def test_write_intent_keeps_the_full_tool_set(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm import providers

    scripted = ScriptedLLM([AIMessage(content="好的，我准备修改 B2。")])
    monkeypatch.setattr(
        providers, "build_resilient_chat_model", lambda settings: scripted
    )
    # The write keyword rule fires before any model call: even a classifier that
    # would say "query" must not mask the write tools.
    refusing_classifier = FakeIntentLLM('{"intent": "query"}')

    from app.agent import intent as intent_module

    monkeypatch.setattr(
        intent_module,
        "build_chat_model",
        lambda settings, **kwargs: refusing_classifier,
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        workspace_id = await _workspace(app)
        frames = await _run_turn(app, workspace_id, "把 B2 改成 1500")

        assert scripted.bound_tools is not None
        assert "update_cells" in scripted.bound_tools, "write intent keeps write tools"
