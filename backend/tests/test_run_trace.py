"""Regression tests for run-level tracing (docs/05 §4.1).

Pins: the collector's span mechanics (timing, error capture, clamping), the
retrieval pipeline's per-stage spans, the trace rows that survive a whole SSE
turn, and the read-back endpoints used to answer "why did this turn go wrong".
No provider is contacted anywhere (AGENTS.md: mock-only tests).
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk

from app.models import RunTrace
from app.services.tracing import TraceCollector, persist_traces

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------- #
# collector mechanics
# --------------------------------------------------------------------------- #
def test_span_records_duration_and_output():
    collector = TraceCollector("ws", run_id="run-1")
    with collector.span("agent", {"prompt": "你好"}) as span:
        span["output"] = {"text_chars": 3}
    record = collector.records[0]
    assert record["node"] == "agent"
    assert record["input"] == {"prompt": "你好"}
    assert record["output"] == {"text_chars": 3}
    assert record["error"] is None
    assert record["duration_ms"] >= 0


def test_span_records_the_error_and_reraises():
    collector = TraceCollector("ws", run_id="run-1")
    with pytest.raises(RuntimeError):
        with collector.span("tools", {"calls": ["read_range"]}):
            raise RuntimeError("file locked")
    record = collector.records[0]
    assert "file locked" in record["error"]
    assert record["duration_ms"] >= 0


def test_oversized_output_is_clamped():
    from app.services.tracing import _safe_summary

    clamped = _safe_summary({"text": "长" * 5000})
    assert "_truncated" in clamped and len(clamped["_truncated"]) < 5000
    assert _safe_summary({"a": 1}) == {"a": 1}


def test_unserializable_values_do_not_break_the_summary():
    from app.services.tracing import _safe_summary

    class Odd:
        def __repr__(self) -> str:
            return "<odd>"

    summary = _safe_summary({"obj": Odd()})
    assert "odd" in json.dumps(summary, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# retrieval pipeline spans
# --------------------------------------------------------------------------- #
def test_search_records_the_stage_spans(temp_session):
    from app.models import Chunk, DocumentFile, Workspace
    from app.retrieval.bm25 import tokenize
    from app.retrieval.pipeline import Retriever

    workspace = Workspace(name="检索埋点")
    temp_session.add(workspace)
    temp_session.flush()
    file = DocumentFile(workspace_id=workspace.id, rel_path="表.xlsx", kind="excel")
    temp_session.add(file)
    temp_session.flush()
    text = "单笔报销不得超过 5000 元"
    counts: dict[str, int] = {}
    for token in tokenize(text):
        counts[token] = counts.get(token, 0) + 1
    temp_session.add(
        Chunk(
            workspace_id=workspace.id,
            file_id=file.id,
            text=text,
            location="费用!第2行",
            token_counts=counts,
            token_length=sum(counts.values()),
        )
    )
    temp_session.flush()

    collector = TraceCollector(workspace.id, run_id="run-ret")
    retriever = Retriever(temp_session, workspace.id)
    # A token straight from the document guarantees the BM25 leg fires.
    retriever.search(next(iter(counts)), use_dense=False, use_rewrite=False, trace=collector)

    nodes = [record["node"] for record in collector.records]
    assert nodes == ["bm25", "fuse", "rerank", "select"], nodes
    assert collector.records[0]["output"]["hits"] >= 1
    assert collector.records[-1]["output"]["returned"] >= 1


# --------------------------------------------------------------------------- #
# end-to-end: a chat turn persists its trace and reports the run id
# --------------------------------------------------------------------------- #
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


async def test_chat_turn_writes_trace_rows_and_reports_run_id(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn's trace rows land in the DB and the run id comes back in `done`.

    Background tasks normally write through ``session_scope`` (the app's real
    database), which is deliberately inert under test — this test redirects it
    to the throwaway factory so the written rows can be read back through the
    trace endpoints.
    """
    import contextlib

    import app.api.routes as routes_module
    from app.llm import providers

    monkeypatch.setattr(
        providers,
        "build_resilient_chat_model",
        lambda settings: ScriptedLLM([AIMessage(content="工作区里只有一个文件。")]),
    )

    @contextlib.contextmanager
    def test_session_scope():
        session = app_with_temp_storage.state.test_session_factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(routes_module, "session_scope", test_session_scope)

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            workspace_id = (
                await client.post("/api/workspaces", json={"name": "轨迹"})
            ).json()["id"]
            response = await client.post(
                f"/api/workspaces/{workspace_id}/chat/stream",
                json={"message": "工作区里有什么文件"},
            )
            frames = parse_sse_frames(response.text)

            done = next(payload for name, payload in frames if name == "done")
            assert done["run_id"]

            # The background task ran with the response; the rows are readable.
            runs = (await client.get(f"/api/workspaces/{workspace_id}/traces")).json()
            assert [run["run_id"] for run in runs] == [done["run_id"]]

            detail = (
                await client.get(
                    f"/api/workspaces/{workspace_id}/traces/{done['run_id']}"
                )
            ).json()
            nodes = [node["node"] for node in detail["nodes"]]
            assert "agent" in nodes
            agent_node = next(node for node in detail["nodes"] if node["node"] == "agent")
            assert agent_node["output"]["text_chars"] > 0

            unknown = await client.get(
                f"/api/workspaces/{workspace_id}/traces/no-such-run"
            )
            assert unknown.status_code == 404


def test_persist_traces_writes_rows(temp_session):
    from app.models import Workspace

    workspace = Workspace(name="落库")
    temp_session.add(workspace)
    temp_session.flush()

    collector = TraceCollector(workspace.id, run_id="run-db")
    with collector.span("agent", {"prompt": "q"}) as span:
        span["output"] = {"text_chars": 2}
    assert persist_traces(temp_session, collector) == 1

    row = temp_session.query(RunTrace).one()
    assert row.run_id == "run-db" and row.node == "agent"
    assert row.input == {"prompt": "q"} and row.output == {"text_chars": 2}
