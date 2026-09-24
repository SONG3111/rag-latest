"""Regression tests for conversation memory compaction (docs/05 §3.3).

The summarizer is always injected — the real small model is never contacted
(AGENTS.md: tests must be mock-only). What is pinned: the watermark trigger, the
coverage bookkeeping, the failure fallback that keeps the previous summary, and
the fact that a stored summary reaches the model as background context.

Since the Java-backend split compaction is a pure function over duck-typed rows
(the stateless chat request's message models qualify); the SSE-level tests drive
``/v1/chat/stream`` the way backend-java does, passing the bookmark in the
request and reading the fold back from the persist frame.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from app.services.memory import compute_compaction

pytestmark = pytest.mark.anyio


def _rows(turns: int, fat_chars: int | None = None) -> list[SimpleNamespace]:
    """Duck-typed conversation rows: role/content/tool_calls."""
    return [
        SimpleNamespace(
            role="user" if index % 2 == 0 else "assistant",
            content=("数" * fat_chars) if fat_chars else f"第{index}轮消息",
            tool_calls=None,
        )
        for index in range(turns)
    ]


def test_no_compaction_below_the_watermark():
    from app.config import get_settings

    called = []

    def summarizer(prompt: str) -> str:
        called.append(prompt)
        return "摘要"

    assert compute_compaction(_rows(10), None, 0, get_settings(), summarizer=summarizer) is None
    assert called == []


def test_compaction_folds_all_but_the_recent_window():
    from app.config import get_settings

    settings = get_settings()
    rows_total = settings.memory_compact_trigger + 10
    rows = _rows(rows_total)
    prompts: list[str] = []

    def summarizer(prompt: str) -> str:
        prompts.append(prompt)
        return "之前确认了报销上限是 5000 元。"

    result = compute_compaction(rows, None, 0, settings, summarizer=summarizer)
    assert result is not None
    summary, covered = result
    assert summary == "之前确认了报销上限是 5000 元。"
    assert covered == rows_total - settings.memory_recent_messages

    assert len(prompts) == 1
    # The recent window stays out of the summary...
    recent_index = settings.memory_compact_trigger + 9
    assert f"第{recent_index}轮消息" not in prompts[0]
    # ...and the oldest turns are in it.
    assert "第0轮消息" in prompts[0]

    # A second run with no new material must not re-summarize (idempotent):
    # the caller would pass (summary, covered) back in.
    assert compute_compaction(rows, summary, covered, settings, summarizer=summarizer) is None
    assert len(prompts) == 1

    # New turns past the window get folded in, and the old summary is part of
    # the prompt so the small model merges rather than restarts.
    rows = rows + _rows(6)
    result = compute_compaction(rows, summary, covered, settings, summarizer=summarizer)
    assert result is not None
    assert len(prompts) == 2
    assert "旧摘要" in prompts[1] and "报销上限" in prompts[1]
    assert result[1] == rows_total + 6 - settings.memory_recent_messages


def test_failed_summarization_keeps_the_previous_summary():
    """A broken summarizer returns None; the caller keeps the previous state."""
    from app.config import get_settings

    settings = get_settings()
    rows = _rows(settings.memory_compact_trigger + 2)

    def failing(prompt: str) -> str:
        raise RuntimeError("model quota")

    # No previous summary: nothing to store.
    assert compute_compaction(rows, None, 0, settings, summarizer=failing) is None
    # With a previous summary the fold fails the same way — an older summary
    # beats a broken chat turn, so the caller keeps what it had.
    assert compute_compaction(rows, "旧摘要", 0, settings, summarizer=failing) is None


def test_long_messages_are_truncated_in_the_prompt():
    from app.config import get_settings

    settings = get_settings()
    rows = [SimpleNamespace(role="user", content="长" * 5000, tool_calls=None)]
    rows += _rows(settings.memory_compact_trigger + 1)
    prompts: list[str] = []
    compute_compaction(
        rows,
        None,
        0,
        settings,
        summarizer=lambda prompt: prompts.append(prompt) or "摘要",
    )
    assert len(prompts) == 1
    assert "长" * 5000 not in prompts[0]
    assert "长" * 500 in prompts[0]


# --------------------------------------------------------------------------- #
# Token-budgeted trigger and keep boundary (pi-mono compaction / dsh compaction-basic)
# --------------------------------------------------------------------------- #
def test_fat_messages_trigger_token_pressure_below_the_count_gate():
    """25 fat turns never reach the 40-row watermark, but 20 of them in the window
    alone cost ~30k estimated tokens — pressure must fold them without the gate."""
    from app.config import get_settings

    settings = get_settings()
    rows = _rows(25, fat_chars=1500)
    prompts: list[str] = []

    result = compute_compaction(
        rows,
        None,
        0,
        settings,
        summarizer=lambda prompt: prompts.append(prompt) or "摘要",
    )
    assert result is not None

    # The keep boundary: 5 × 1500 = 7500 ≤ keep_recent_tokens(8000), a 6th
    # message would exceed it, so exactly 5 newest rows stay verbatim.
    assert result[1] == 25 - 5
    assert prompts[0].count("数" * 100) >= 1  # folded fat content is in the prompt


def test_force_compaction_folds_deeper_than_the_normal_path():
    """force=True (context-overflow recovery) halves the keep budget, so the same
    history folds more rows than the normal path would."""
    from app.config import get_settings

    settings = get_settings()
    rows = _rows(25, fat_chars=1500)
    calls: list[str] = []

    def summarizer(prompt: str) -> str:
        calls.append(prompt)
        return "摘要"

    result = compute_compaction(rows, None, 0, settings, summarizer=summarizer)
    assert result is not None and result[1] == 20

    # Forced re-run: 2 × 1500 = 3000 ≤ 4000 (halved budget), a 3rd would exceed.
    result = compute_compaction(
        rows, result[0], result[1], settings, summarizer=summarizer, force=True
    )
    assert result is not None and result[1] == 23
    assert len(calls) == 2

    # No new material beyond the bookmark: even force must not re-summarize.
    assert compute_compaction(
        rows, result[0], result[1], settings, summarizer=summarizer, force=True
    ) is None
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Structured summary template (pi-mono / dsh compaction-basic section sets)
# --------------------------------------------------------------------------- #
def test_structured_template_has_fixed_sections_and_a_bounded_length():
    from app.config import get_settings
    from app.services.memory import COMPACT_SYSTEM_PROMPT

    settings = get_settings()
    rendered = COMPACT_SYSTEM_PROMPT.format(max_chars=settings.memory_summary_max_chars)
    for section in (
        "### 用户目标与意图",
        "### 关键事实与决定",
        "### 错误与修复",
        "### 未决事项与下一步",
        "### 关键背景",
    ):
        assert section in rendered
    # The length bound comes from settings, not a hardcoded figure.
    assert str(settings.memory_summary_max_chars) in rendered
    # File-freshness facts stay out of the summary's remit by design.
    assert "时效" not in COMPACT_SYSTEM_PROMPT


def test_write_proposals_enter_the_prompt_as_summary_material():
    from app.config import get_settings

    settings = get_settings()
    rows = [
        SimpleNamespace(
            role="assistant",
            content="已经为你生成修改提案。",
            tool_calls=[
                {
                    "operation_id": "op-1",
                    "tool": "update_cells",
                    "summary": "把 B2 改为 5000",
                    "path": "报销.xlsx",
                    "diff": [{"cell": "B2", "to": 5000}],
                }
            ],
        )
    ]
    rows += _rows(settings.memory_compact_trigger + 1)
    prompts: list[str] = []
    assert compute_compaction(
        rows,
        None,
        0,
        settings,
        summarizer=lambda prompt: prompts.append(prompt) or "摘要",
    ) is not None
    assert "修改提案: update_cells（报销.xlsx） 把 B2 改为 5000" in prompts[0]


# --------------------------------------------------------------------------- #
# SSE-level: the request's summary reaches the model as background context
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


async def _stream_turn(app, payload: dict) -> list[tuple[str, dict]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/chat/stream", json=payload)
    return parse_sse_frames(response.text)


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
        frames = await _stream_turn(
            app,
            {
                "workspace_id": "ws-memory",
                "run_id": "run-memory",
                "message": "上限是多少来着？",
                "summary": "之前讨论过费用报销：单笔上限 5000 元，需要总经理审批。",
                "covered_count": 8,
            },
        )
        persist = next(payload for name, payload in frames if name == "persist")
        assert "5000" in persist["content"]

        # The summary reached the model as a system-level background notice.
        system_texts = [
            str(message.content)
            for message in scripted.inputs[0]
            if isinstance(message, SystemMessage)
        ]
        assert any("对话背景摘要" in text and "5000 元" in text for text in system_texts)


async def test_history_slicing_skips_rows_covered_by_the_summary(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bookmark decides where the replayed history starts: rows the summary
    covers are not also sent verbatim (pi's firstKeptEntryId arithmetic)."""
    from app.llm import providers

    scripted = ScriptedLLM([AIMessage(content="好的。")])
    monkeypatch.setattr(
        providers, "build_resilient_chat_model", lambda settings: scripted
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        frames = await _stream_turn(
            app,
            {
                "workspace_id": "ws-memory",
                "run_id": "run-memory",
                "message": "上限是多少来着？",
                "messages": [
                    {"role": "user" if index % 2 == 0 else "assistant", "content": f"第{index}轮消息"}
                    for index in range(30)
                ],
                "total_messages": 30,
                "summary": "早期讨论过费用报销：单笔上限 5000 元。",
                "covered_count": 20,
            },
        )
        next(payload for name, payload in frames if name == "persist")

        conversation = [
            message
            for message in scripted.inputs[0]
            if isinstance(message, (HumanMessage, AIMessage))
        ]
        # 30 request rows, 20 covered -> 第20..29轮 replay (10) + the new turn.
        assert len(conversation) == 11
        contents = [str(message.content) for message in conversation]
        assert contents[0] == "第20轮消息"
        assert contents[9] == "第29轮消息"
        assert contents[10] == "上限是多少来着？"
