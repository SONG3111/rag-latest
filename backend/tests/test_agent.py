"""Agent behaviour tests driven by a scripted model.

Real tool-selection quality depends on the LLM, but the *control flow* around it —
which calls execute, which are gated, how many loops run — is deterministic and is
what these tests pin down. A scripted fake model makes that testable without a key.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from openpyxl import Workbook

from app.agent.graph import AgentEvent, WorkspaceAgent
from app.mcp_client import McpOfficeClient, parse_tool_result
from app.models import Workspace


class ScriptedLLM:
    """Returns a fixed sequence of AI messages, then stops.

    Implements the streaming surface the agent node uses: it yields the scripted
    message as a single chunk carrying content *and* tool_calls, which is the shape a
    real provider produces once its tool-call deltas have been aggregated.
    """

    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":
        self.bound_tools = tools
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(list(messages))
        if self.script:
            return self.script.pop(0)
        return AIMessage(content="结束")

    async def astream(self, messages: list[Any]):
        self.calls.append(list(messages))
        message = self.script.pop(0) if self.script else AIMessage(content="结束")
        yield AIMessageChunk(
            content=message.content,
            tool_calls=list(getattr(message, "tool_calls", None) or []),
        )


@pytest.fixture()
def agent_env(temp_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data", raising=False)
    settings.ensure_directories()

    workspace = Workspace(name="Agent 测试")
    temp_session.add(workspace)
    temp_session.flush()

    directory = settings.workspace_dir(workspace.id)
    directory.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["产品", "销售额"])
    sheet.append(["A型", 1000])
    book.save(directory / "销售表.xlsx")

    return workspace, directory


def _call(name: str, args: dict, call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


async def _run(
    agent: WorkspaceAgent,
    message: str = "测试",
    stale_files: list[tuple[str, str]] | None = None,
) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    async for event in agent.astream(message, stale_files=stale_files):
        events.append(event)
    return events


def test_read_only_tool_executes_without_approval(agent_env, temp_session) -> None:
    workspace, _ = agent_env
    scripted = ScriptedLLM(
        [
            _call("read_range", {"path": "销售表.xlsx", "sheet_name": "销售", "start_cell": "A1", "end_cell": "B2"}),
            AIMessage(content="这个表里有 A型，销售额 1000。"),
        ]
    )

    async def scenario():
        client = McpOfficeClient(workspace_root=__import__("app.config", fromlist=["get_settings"]).get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    kinds = [event.type for event in events]

    assert "tool_call" in kinds
    assert "proposal" not in kinds
    assert "error" not in kinds
    # The answer must reach the client as streamed tokens, not only inside `done`:
    # without `token` events the UI cannot render text until the model finishes.
    assert "token" in kinds
    streamed = "".join(
        event.data["text"] for event in events if event.type == "token"
    )
    assert streamed.strip() == "这个表里有 A型，销售额 1000。"
    done = next(event for event in events if event.type == "done")
    assert done.data["content"] == streamed.strip()

    # The graph must have been re-entered after the tool result, so the model sees it.
    assert len(scripted.calls) == 2
    tool_messages = [m for m in scripted.calls[1] if m.__class__.__name__ == "ToolMessage"]
    payload = json.loads(tool_messages[0].content)
    assert payload["ok"] is True
    assert payload["data"]["values"][0] == ["产品", "销售额"]


def test_destructive_tool_is_gated_and_does_not_write(agent_env, temp_session) -> None:
    workspace, directory = agent_env
    target = directory / "销售表.xlsx"
    before = target.read_bytes()

    scripted = ScriptedLLM(
        [
            _call(
                "update_cells",
                {
                    "path": "销售表.xlsx",
                    "sheet_name": "销售",
                    "updates": [{"cell": "B2", "value": 1500}],
                },
            ),
            AIMessage(content="我建议把 B2 从 1000 改成 1500。"),
        ]
    )

    async def scenario():
        client = McpOfficeClient(workspace_root=__import__("app.config", fromlist=["get_settings"]).get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    proposals = [event for event in events if event.type == "proposal"]

    assert len(proposals) == 1
    assert proposals[0].data["tool"] == "update_cells"
    assert proposals[0].data["path"] == "销售表.xlsx"
    # Crucially, the file is untouched until a human approves.
    assert target.read_bytes() == before


def test_proposal_carries_a_diff_from_the_current_values(agent_env, temp_session) -> None:
    workspace, _ = agent_env
    scripted = ScriptedLLM(
        [
            _call(
                "update_cells",
                {
                    "path": "销售表.xlsx",
                    "sheet_name": "销售",
                    "updates": [{"cell": "B2", "value": 2500}],
                },
            ),
            AIMessage(content="已提交修改提案。"),
        ]
    )

    async def scenario():
        client = McpOfficeClient(workspace_root=__import__("app.config", fromlist=["get_settings"]).get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    proposal = next(event for event in events if event.type == "proposal")
    diff = proposal.data["diff"]
    assert diff == [{"cell": "B2", "before": 1000, "after": 2500}]


def test_write_arguments_carry_a_digest_for_conflict_detection(
    agent_env, temp_session
) -> None:
    workspace, _ = agent_env
    scripted = ScriptedLLM(
        [
            _call(
                "update_cells",
                {
                    "path": "销售表.xlsx",
                    "sheet_name": "销售",
                    "updates": [{"cell": "B2", "value": 1}],
                },
            ),
            AIMessage(content="ok"),
        ]
    )

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            await _run(agent)
        finally:
            await client.stop()

    asyncio.run(scenario())
    from sqlalchemy import select

    from app.models import Operation

    operation = temp_session.scalar(select(Operation))
    assert operation is not None
    assert len(operation.arguments.get("expected_digest", "")) == 64


def test_unknown_tool_reports_an_error_without_crashing(agent_env, temp_session) -> None:
    workspace, _ = agent_env
    scripted = ScriptedLLM(
        [_call("nonexistent_tool", {"path": "x.xlsx"}), AIMessage(content="工具不可用。")]
    )

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    results = [event for event in events if event.type == "tool_result"]
    assert results
    assert "unknown_tool" in results[0].data["content"]


def test_iteration_ceiling_stops_a_looping_model(agent_env, temp_session, monkeypatch) -> None:
    """A model that keeps calling tools must be cut off rather than looping forever."""
    workspace, _ = agent_env
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "agent_max_iterations", 3, raising=False)

    scripted = ScriptedLLM(
        [_call("list_files", {}, f"call-{index}") for index in range(10)]
    )

    async def scenario():
        client = McpOfficeClient(workspace_root=settings.workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, settings=settings, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    assert "error" not in [event.type for event in events]
    assert len(scripted.calls) <= settings.agent_max_iterations + 1


def test_ungrounded_answer_is_sent_back_for_a_fresh_read(agent_env, temp_session) -> None:
    """An answer with numbers but no tool call is replayed history, not a reading.

    Regression: after a file was replaced, the model answered from earlier turns
    ("the sheet only has a header") and the user got a confident wrong answer.
    """
    workspace, _ = agent_env
    stale = [("销售表.xlsx", "2026-01-01 12:00:00")]
    scripted = ScriptedLLM(
        [
            AIMessage(content="销售表里 A型 的销售额是 999。"),
            _call("read_range", {"path": "销售表.xlsx", "sheet_name": "销售", "start_cell": "A1"}),
            AIMessage(content="销售表里 A型 的销售额是 1000。"),
        ]
    )

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent, stale_files=stale)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    assert "error" not in [event.type for event in events]
    assert "tool_call" in [event.type for event in events]

    # The nudge is an extra model call, and the model sees it as the latest turn.
    assert len(scripted.calls) == 3
    nudge = scripted.calls[1][-1]
    assert "系统检查" in nudge.content
    # The draft must not be replayed back as part of the replacement answer.
    assert not any(
        isinstance(message, AIMessage) and "999" in str(message.content)
        for message in scripted.calls[2]
    )
    # Only the grounded answer survives to the client.
    done = next(event for event in events if event.type == "done")
    assert done.data["content"] == "销售表里 A型 的销售额是 1000。"


def test_greeting_without_numbers_is_not_forced_through_a_tool(agent_env, temp_session) -> None:
    """The freshness guard must not tax plain conversation."""
    workspace, _ = agent_env
    stale = [("销售表.xlsx", "2026-01-01 12:00:00")]
    scripted = ScriptedLLM([AIMessage(content="你好，我可以帮你查看工作区里的文档。")])

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent, stale_files=stale)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    assert len(scripted.calls) == 1
    assert "tool_call" not in [event.type for event in events]
    done = next(event for event in events if event.type == "done")
    assert done.data["content"] == "你好，我可以帮你查看工作区里的文档。"


def test_freshness_nudge_happens_at_most_once(agent_env, temp_session) -> None:
    """A model that ignores the nudge must not loop forever."""
    workspace, _ = agent_env
    stale = [("销售表.xlsx", "2026-01-01 12:00:00")]
    scripted = ScriptedLLM(
        [
            AIMessage(content="销售表里有 3 行数据，A型 销售额 999 元。"),
            AIMessage(content="销售表里有 3 行数据，A型 销售额 999 元。"),
            AIMessage(content="销售表里有 3 行数据，A型 销售额 999 元。"),
        ]
    )

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent, stale_files=stale)
        finally:
            await client.stop()

    asyncio.run(scenario())
    assert len(scripted.calls) == 2


def test_empty_answer_is_asked_for_again(agent_env, temp_session) -> None:
    """A reasoning-only finish must be retried rather than shown as an empty bubble."""
    workspace, _ = agent_env
    scripted = ScriptedLLM(
        [
            _call("list_files", {}),
            AIMessage(content=""),  # finished with no visible text
            AIMessage(content="工作区里有 销售表.xlsx。"),
        ]
    )

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    assert len(scripted.calls) == 3
    nudge = scripted.calls[2][-1]
    assert "没有输出任何可见的文字" in nudge.content
    done = next(event for event in events if event.type == "done")
    assert done.data["content"] == "工作区里有 销售表.xlsx。"


def test_empty_answer_is_retried_only_once(agent_env, temp_session) -> None:
    """A model that stays silent must not be nudged forever."""
    workspace, _ = agent_env
    scripted = ScriptedLLM(
        [
            AIMessage(content=""),
            AIMessage(content=""),
            AIMessage(content=""),
        ]
    )

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    assert len(scripted.calls) == 2
    # The route layer turns an empty turn into an explicit message for the user.
    done = next(event for event in events if event.type == "done")
    assert done.data["content"] == ""


def test_unchanged_files_allow_reusing_the_previous_answer(agent_env, temp_session) -> None:
    """With nothing changed on disk there is no reason to force another read."""
    workspace, _ = agent_env
    scripted = ScriptedLLM([AIMessage(content="销售表中 A型 的销售额是 1000。")])

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent, stale_files=[])
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    assert len(scripted.calls) == 1
    assert "tool_call" not in [event.type for event in events]
    done = next(event for event in events if event.type == "done")
    assert done.data["content"] == "销售表中 A型 的销售额是 1000。"


def test_list_files_only_reports_the_active_workspace(agent_env, temp_session) -> None:
    """The sandbox root is shared by every workspace, but the model must not see it.

    Regression: ``list_files`` returned the whole tree, so a question about the
    current workspace also surfaced another workspace's copy of the same file —
    and stale files left on disk after a deletion — which the model then read.
    """
    workspace, _ = agent_env
    from app.config import get_settings
    from app.models import DocumentFile, IndexStatus

    settings = get_settings()
    other = Workspace(name="另一个工作区")
    temp_session.add(other)
    temp_session.flush()
    other_dir = settings.workspace_dir(other.id)
    other_dir.mkdir(parents=True, exist_ok=True)
    (other_dir / "销售表.xlsx").write_bytes((settings.workspace_dir(workspace.id) / "销售表.xlsx").read_bytes())

    # A file that exists on disk but is not part of the workspace any more: the UI does
    # not show it, so neither should the model.
    (settings.workspace_dir(workspace.id) / "已删除的表.xlsx").write_bytes(b"stale")
    temp_session.add(
        DocumentFile(
            workspace_id=workspace.id,
            rel_path="销售表.xlsx",
            kind="excel",
            status=IndexStatus.indexed,
        )
    )
    temp_session.flush()

    scripted = ScriptedLLM(
        [
            _call("list_files", {}),
            AIMessage(content="工作区里有 销售表.xlsx。"),
        ]
    )

    async def scenario():
        client = McpOfficeClient(workspace_root=settings.workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    asyncio.run(scenario())
    tool_messages = [m for m in scripted.calls[1] if m.__class__.__name__ == "ToolMessage"]
    payload = json.loads(tool_messages[0].content)

    assert payload["ok"] is True
    paths = [item["path"] for item in payload["data"]["files"]]
    assert paths == ["销售表.xlsx"]


def test_knowledge_tool_is_registered_alongside_mcp_tools(agent_env, temp_session) -> None:
    workspace, _ = agent_env

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=ScriptedLLM([]))
            return [tool.name for tool in agent.build_tools()]
        finally:
            await client.stop()

    names = asyncio.run(scenario())
    assert "search_knowledge_base" in names
    assert "update_cells" in names
    assert "read_range" in names


def test_knowledge_tool_result_is_parsed_into_citations(agent_env, temp_session, monkeypatch) -> None:
    """A retrieval call must surface citations to the UI."""
    workspace, _ = agent_env

    class FakeRetriever:
        def __init__(self, *args, **kwargs) -> None:
            from app.retrieval.pipeline import RetrievalRun

            self.last_run = RetrievalRun(
                original_query="报销上限",
                effective_query="报销金额上限是多少",
                rewritten=True,
            )

        def search(self, query: str, top_k: int = 5, **kwargs):
            from app.retrieval.pipeline import RetrievedChunk

            return [
                RetrievedChunk(
                    chunk_id="c1",
                    text="单笔报销金额不得超过 5000 元。",
                    rel_path="制度.docx",
                    location="段落 2",
                    score=0.93,
                    parent_text="文件：制度.docx\n章节：报销限额\n第二条 单笔报销金额不得超过 5000 元。",
                    parent_location="段落 1-3",
                    score_source="rerank",
                )
            ]

    monkeypatch.setattr("app.agent.graph.Retriever", FakeRetriever)

    scripted = ScriptedLLM(
        [
            _call("search_knowledge_base", {"query": "报销上限"}),
            AIMessage(content="上限是 5000 元 [制度.docx · 段落 2]。"),
        ]
    )

    async def scenario():
        from app.config import get_settings

        client = McpOfficeClient(workspace_root=get_settings().workspaces_dir)
        await client.start()
        try:
            agent = WorkspaceAgent(temp_session, workspace, client, llm=scripted)
            return await _run(agent)
        finally:
            await client.stop()

    events = asyncio.run(scenario())
    citation_events = [event for event in events if event.type == "citations"]
    assert citation_events
    assert citation_events[0].data["items"][0]["file"] == "制度.docx"
    # The citation must carry a meaningful confidence, not an ordinal fusion score.
    assert citation_events[0].data["items"][0]["score"] == 0.93
    assert citation_events[0].data["items"][0]["score_source"] == "rerank"
