"""Regression tests for run-level tracing (docs/05 §4.1).

Pins: the collector's span mechanics (timing, error capture, clamping), the
retrieval pipeline's per-stage spans, and the trace records that ride a whole
SSE turn out on the persist frame (backend-java persists them into
``run_traces``). No provider is contacted anywhere (AGENTS.md: mock-only tests).
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk

from app.services.tracing import TraceCollector

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
def test_search_records_the_stage_spans():
    from app.retrieval.bm25 import tokenize
    from app.retrieval.pipeline import ChunkRecord, Retriever

    text = "单笔报销不得超过 5000 元"
    counts: dict[str, int] = {}
    for token in tokenize(text):
        counts[token] = counts.get(token, 0) + 1
    chunk = ChunkRecord(
        id="c1",
        file_id="f1",
        parent_id=None,
        level="child",
        text=text,
        location="费用!第2行",
        token_counts=counts,
        token_length=sum(counts.values()),
    )
    corpus = ({"f1": "表.xlsx"}, [chunk])

    collector = TraceCollector("ws-ret", run_id="run-ret")
    retriever = Retriever("ws-ret", corpus_loader=lambda: corpus)
    # A token straight from the document guarantees the BM25 leg fires.
    retriever.search(next(iter(counts)), use_dense=False, use_rewrite=False, trace=collector)

    nodes = [record["node"] for record in collector.records]
    assert nodes == ["bm25", "fuse", "rerank", "select"], nodes
    assert collector.records[0]["output"]["hits"] >= 1
    assert collector.records[-1]["output"]["returned"] >= 1


# --------------------------------------------------------------------------- #
# end-to-end: a chat turn reports its trace records on the persist frame
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


async def test_chat_turn_reports_trace_records_on_the_persist_frame(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn's trace rows ride the persist frame for backend-java to store.

    The stateless endpoint no longer writes ``run_traces`` itself; the persist
    frame carries the collector's records (Java persists them keyed by the same
    run id it generated for the request).
    """
    from app.llm import providers

    monkeypatch.setattr(
        providers,
        "build_resilient_chat_model",
        lambda settings: ScriptedLLM([AIMessage(content="工作区里只有一个文件。")]),
    )

    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/stream",
                json={
                    "workspace_id": "ws-trace",
                    "run_id": "run-trace-0001",
                    "message": "工作区里有什么文件",
                },
            )
            frames = parse_sse_frames(response.text)

            persist = next(payload for name, payload in frames if name == "persist")
            nodes = [node["node"] for node in persist["trace_nodes"]]
            assert "agent" in nodes
            agent_node = next(
                node for node in persist["trace_nodes"] if node["node"] == "agent"
            )
            assert agent_node["output"]["text_chars"] > 0
