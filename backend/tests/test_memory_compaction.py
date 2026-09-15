"""Regression tests for conversation memory compaction (docs/05 §3.3).

The summarizer is always injected — the real small model is never contacted
(AGENTS.md: tests must be mock-only). What is pinned: the watermark trigger, the
coverage bookkeeping, the failure fallback that keeps the previous summary, and
the fact that a stored summary reaches the model as background context.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage

from app.models import ConversationSummary, Message, MessageRole, Workspace
from app.services.memory import compact_memory, load_summary

pytestmark = pytest.mark.anyio


@pytest.fixture()
def workspace_id(temp_session) -> str:
    workspace = Workspace(name="记忆压缩")
    temp_session.add(workspace)
    temp_session.flush()
    return workspace.id


def _seed_messages(session, workspace_id: str, turns: int) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(turns):
        moment = base + timedelta(minutes=index)
        session.add(
            Message(
                workspace_id=workspace_id,
                role=MessageRole.user if index % 2 == 0 else MessageRole.assistant,
                content=f"第{index}轮消息",
                created_at=moment,
            )
        )
    session.flush()


def test_no_compaction_below_the_watermark(temp_session, workspace_id):
    from app.config import get_settings

    _seed_messages(temp_session, workspace_id, turns=10)
    called = []

    def summarizer(prompt: str) -> str:
        called.append(prompt)
        return "摘要"

    assert compact_memory(temp_session, workspace_id, get_settings(), summarizer=summarizer) is False
    assert called == []
    assert load_summary(temp_session, workspace_id) is None


def test_compaction_folds_all_but_the_recent_window(temp_session, workspace_id):
    from app.config import get_settings

    settings = get_settings()
    rows_total = settings.memory_compact_trigger + 10
    _seed_messages(temp_session, workspace_id, turns=rows_total)
    prompts: list[str] = []

    def summarizer(prompt: str) -> str:
        prompts.append(prompt)
        return "之前确认了报销上限是 5000 元。"

    assert compact_memory(temp_session, workspace_id, settings, summarizer=summarizer) is True
    assert len(prompts) == 1
    # The recent window stays out of the summary...
    recent_index = settings.memory_compact_trigger + 9
    assert f"第{recent_index}轮消息" not in prompts[0]
    # ...and the oldest turns are in it.
    assert "第0轮消息" in prompts[0]

    row = temp_session.get(ConversationSummary, workspace_id)
    assert row.summary == "之前确认了报销上限是 5000 元。"
    assert row.covered_count == rows_total - settings.memory_recent_messages

    # A second run with no new material must not re-summarize (idempotent).
    assert compact_memory(temp_session, workspace_id, settings, summarizer=summarizer) is False
    assert len(prompts) == 1

    # New turns past the window get folded in, and the old summary is part of
    # the prompt so the small model merges rather than restarts.
    _seed_messages(temp_session, workspace_id, turns=6)
    assert compact_memory(temp_session, workspace_id, settings, summarizer=summarizer) is True
    assert len(prompts) == 2
    assert "旧摘要" in prompts[1] and "报销上限" in prompts[1]
    assert temp_session.get(ConversationSummary, workspace_id).covered_count == (
        rows_total + 6 - settings.memory_recent_messages
    )


def test_failed_summarization_keeps_the_previous_summary(temp_session, workspace_id):
    from app.config import get_settings

    settings = get_settings()
    _seed_messages(temp_session, workspace_id, turns=settings.memory_compact_trigger + 2)

    def failing(prompt: str) -> str:
        raise RuntimeError("model quota")

    assert compact_memory(temp_session, workspace_id, settings, summarizer=failing) is False
    assert load_summary(temp_session, workspace_id) is None

    temp_session.add(
        ConversationSummary(workspace_id=workspace_id, summary="旧摘要", covered_count=0)
    )
    temp_session.flush()
    assert compact_memory(temp_session, workspace_id, settings, summarizer=failing) is False
    # The old summary survives a failed compaction instead of being wiped.
    assert load_summary(temp_session, workspace_id) == "旧摘要"


def test_long_messages_are_truncated_in_the_prompt(temp_session, workspace_id):
    from app.config import get_settings

    settings = get_settings()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    temp_session.add(
        Message(
            workspace_id=workspace_id,
            role=MessageRole.user,
            content="长" * 5000,
            created_at=base,
        )
    )
    _seed_messages(temp_session, workspace_id, turns=settings.memory_compact_trigger + 1)
    prompts: list[str] = []
    compact_memory(
        temp_session,
        workspace_id,
        settings,
        summarizer=lambda prompt: prompts.append(prompt) or "摘要",
    )
    assert len(prompts) == 1
    assert "长" * 5000 not in prompts[0]
    assert "长" * 500 in prompts[0]


# --------------------------------------------------------------------------- #
# SSE-level: a stored summary reaches the model as background context
# --------------------------------------------------------------------------- #
class ScriptedLLM:
    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.inputs: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":
        return self

    async def astream(self, messages: list[Any]):
        self.inputs.append(list(messages))
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


async def test_stored_summary_is_injected_as_background_context(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm import providers

    scripted = ScriptedLLM([AIMessage(content="根据之前的结果，上限是 5000 元。")])
    monkeypatch.setattr(
        providers, "build_resilient_chat_model", lambda settings: scripted
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            workspace_id = (await client.post("/api/workspaces", json={"name": "摘要注入"})).json()["id"]
            session = app.state.test_session_factory()
            session.add(
                ConversationSummary(
                    workspace_id=workspace_id,
                    summary="之前讨论过费用报销：单笔上限 5000 元，需要总经理审批。",
                    covered_count=8,
                )
            )
            session.commit()

            response = await client.post(
                f"/api/workspaces/{workspace_id}/chat/stream",
                json={"message": "上限是多少来着？"},
            )
            frames = parse_sse_frames(response.text)
            done = next(payload for name, payload in frames if name == "done")
            assert "5000" in done["content"]

            # The summary reached the model as a system-level background notice.
            system_texts = [
                str(message.content)
                for message in scripted.inputs[0]
                if isinstance(message, SystemMessage)
            ]
            assert any("对话背景摘要" in text and "5000 元" in text for text in system_texts)
