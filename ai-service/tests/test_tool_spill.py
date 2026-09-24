"""Regression tests for oversized tool-result spilling.

Unit tests pin the spill contract (threshold, preview size, keep-on-failure);
the graph-level test drives the real tools node with a scripted model and a
fake retriever, pinning the split that makes spilling safe: the *model* sees
only the preview plus retrieval guidance, while citations and the spill file
keep the complete text.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from app.agent.graph import (
    KNOWLEDGE_TOOL_NAME,
    AgentEvent,
    AgentRuntime,
    WorkspaceAgent,
)
from app.mcp_client import McpOfficeClient
from app.services.spill import spill_tool_result

FAT_TEXT = "长" * 5000


# --------------------------------------------------------------------------- #
# spill_tool_result unit contract
# --------------------------------------------------------------------------- #
def _spill_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data", raising=False)
    settings.ensure_directories()
    return settings


def test_under_threshold_returns_none(tmp_path, monkeypatch):
    settings = _spill_settings(tmp_path, monkeypatch)
    assert spill_tool_result("read_range", "短结果", workspace_id="w", settings=settings) is None
    # Exactly at the threshold is still under it.
    assert (
        spill_tool_result(
            "read_range", "字" * settings.tool_result_spill_chars, workspace_id="w", settings=settings
        )
        is None
    )


def test_oversized_result_writes_a_file_and_returns_preview_envelope(tmp_path, monkeypatch):
    settings = _spill_settings(tmp_path, monkeypatch)
    content = "数" * 9000

    envelope = spill_tool_result("search_knowledge_base", content, workspace_id="w", settings=settings)
    assert envelope is not None
    payload = json.loads(envelope)
    assert payload["ok"] is True
    assert payload["data"]["status"] == "spilled"
    assert payload["data"]["preview"] == "数" * 2000
    assert "更窄" in payload["data"]["note"]
    assert payload["data"]["spill_file"].startswith("spill/w/")

    spill_file = settings.data_dir / payload["data"]["spill_file"]
    assert spill_file.read_text(encoding="utf-8") == content


def test_storage_failure_keeps_the_full_result(tmp_path, monkeypatch):
    settings = _spill_settings(tmp_path, monkeypatch)

    def broken_write(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", broken_write)
    assert (
        spill_tool_result("t", "数" * 9000, workspace_id="w", settings=settings) is None
    )


# --------------------------------------------------------------------------- #
# Graph-level: model sees the preview, citations keep the full text
# --------------------------------------------------------------------------- #
class ScriptedLLM:
    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":
        return self

    async def astream(self, messages: list[Any]):
        self.calls.append(list(messages))
        message = self.script.pop(0)
        yield AIMessageChunk(
            content=message.content,
            tool_calls=list(getattr(message, "tool_calls", None) or []),
        )


class FakeRetriever:
    """Returns three fat hits — a payload far past the spill threshold."""

    last_run = SimpleNamespace(
        original_query="测试",
        effective_query="测试",
        rewritten=False,
        threshold_applied=0.0,
    )

    def __init__(self, *args, **kwargs) -> None:
        # The graph constructs Retriever(workspace_id, settings, corpus_loader=...).
        pass

    def search(self, query: str, top_k: int = 5, history=None, trace=None):
        return [SimpleNamespace(to_reference=lambda: {"text": FAT_TEXT, "file": "报销.xlsx", "location": "销售!A1", "score": 0.9}) for _ in range(3)]


def _call(name: str, args: dict, call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture()
def spill_agent_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Same shape as test_agent's agent_env: a workspace id + storage root."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data", raising=False)
    settings.ensure_directories()

    workspace_id = "ws-spill"
    settings.workspace_dir(workspace_id).mkdir(parents=True, exist_ok=True)
    return workspace_id


def test_knowledge_result_spills_but_citations_stay_complete(
    spill_agent_env, monkeypatch
):
    workspace_id = spill_agent_env
    monkeypatch.setattr("app.agent.graph.Retriever", FakeRetriever)
    scripted = ScriptedLLM(
        [
            _call(KNOWLEDGE_TOOL_NAME, {"query": "测试"}),
            AIMessage(content="根据检索结果回答。"),
        ]
    )

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            runtime = AgentRuntime(
                workspace_id=workspace_id,
                corpus_loader=lambda: ({}, []),
            )
            agent = WorkspaceAgent(client, llm=scripted, runtime=runtime)
            events: list[AgentEvent] = []
            async for event in agent.astream("测试"):
                events.append(event)
            return events
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    assert "error" not in [event.type for event in events]

    # The model's second view of the tool result is the envelope: preview plus
    # guidance, never the full 5000-char texts.
    tool_messages = [
        m for m in scripted.calls[1] if m.__class__.__name__ == "ToolMessage"
    ]
    payload = json.loads(tool_messages[0].content)
    assert payload["data"]["status"] == "spilled"
    assert len(payload["data"]["preview"]) == 2000
    assert FAT_TEXT not in tool_messages[0].content

    # Citations were extracted before spilling, so all three hits survive with
    # their locations (their snippet is the citation UI's 400-char cut).
    citations = next(event for event in events if event.type == "citations")
    items = citations.data["items"]
    assert len(items) == 3
    assert all(item["file"] == "报销.xlsx" for item in items)
    assert all(item["location"] == "销售!A1" for item in items)

    # The full payload landed in the session-scoped spill store.
    from app.config import get_settings

    spill_dir = get_settings().data_dir / "spill" / workspace_id
    files = list(spill_dir.iterdir())
    assert len(files) == 1
    assert FAT_TEXT in files[0].read_text(encoding="utf-8")
