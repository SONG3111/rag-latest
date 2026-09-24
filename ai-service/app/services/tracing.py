"""Run-level tracing: one chat turn = one run of nodes with timing and I/O.

Data model borrowed from nageoffer/ragent (Apache-2.0, ``RagTraceContext`` /
``RagTraceNode`` / ``AgentRunTracer``, code-tree verified): a run is one user
turn, a run holds nodes (rewrite / dense / bm25 / rerank / select / agent /
tools / nudge / direct), and each node records its duration, an input/output
summary, and any error. RAGent exports the structure through OpenTelemetry;
the single-machine version buffers the same shape in memory and ships it on
the internal chat persist frame, where backend-java owns the ``run_traces``
table — "why did this turn answer wrongly" stays answerable with one query by
``run_id`` on the Java side.

Recording is deliberately side-effect free for the request: the collector only
buffers records in memory, so a broken trace can never break a chat turn —
the same fail-open rule the rest of the observability follows.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Trace rows are for diagnosis, not for replaying payloads: any single JSON blob
# larger than this is stored truncated.
_SUMMARY_CHAR_LIMIT = 2000

#: Nodes recorded by the retrieval pipeline, in execution order.
RETRIEVAL_NODES = ("rewrite", "dense", "bm25", "fuse", "rerank", "select")


def _safe_summary(value: Any) -> dict:
    """Clamp a payload into a JSON-safe dict of bounded size."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"repr": repr(value)}, ensure_ascii=False)
    if len(text) > _SUMMARY_CHAR_LIMIT:
        return {"_truncated": text[:_SUMMARY_CHAR_LIMIT]}
    return json.loads(text)


class TraceCollector:
    """Buffers the trace records of one run in memory."""

    def __init__(self, workspace_id: str, run_id: str | None = None) -> None:
        self.workspace_id = workspace_id
        self.run_id = run_id or uuid.uuid4().hex
        self._records: list[dict[str, Any]] = []

    @property
    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)

    @contextmanager
    def span(self, node: str, input_summary: dict[str, Any] | None = None) -> Iterator[dict]:
        """Time one node; ``span["output"]`` is filled by the caller inside.

        Usable from both sync (retrieval pipeline) and async (agent graph) code:
        the context manager itself never yields to the event loop, it only reads
        the wall clock on entry and exit. An exception inside the block is
        recorded on the node and re-raised — tracing never swallows errors.
        """
        record: dict[str, Any] = {
            "node": node,
            "input": _safe_summary(input_summary or {}),
            "output": {},
            "error": None,
            "duration_ms": 0,
        }
        self._records.append(record)
        started = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            record["error"] = str(exc)[:500]
            raise
        finally:
            record["duration_ms"] = int((time.perf_counter() - started) * 1000)
