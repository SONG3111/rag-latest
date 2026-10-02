"""HTTP-level tests for the index endpoints (/v1/index, /v1/index-batch).

Everything runs against a deterministic fake embedding model — no provider is
ever contacted (AGENTS.md: tests must be mock-only). The vector store is the
real Qdrant local instance rooted at the temp data dir; storage is seeded the
way backend-java's upload would (``seed_workspace_file``).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

from tests.conftest import seed_workspace_file

pytestmark = pytest.mark.anyio


class FakeEmbeddings:
    """Deterministic stand-in; optionally slow to exercise the event-loop escape.

    Vector width follows the configured collection dimensions (default 1024),
    otherwise Qdrant rejects the upsert with a broadcast error.
    """

    def __init__(self, dimensions: int, delay: float = 0.0) -> None:
        self.dimensions = dimensions
        self.delay = delay
        self.batches: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        if self.delay:
            time.sleep(self.delay)
        return [
            [float(len(text) % 97)] + [0.0] * (self.dimensions - 1) for text in texts
        ]


@pytest.fixture()
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> FakeEmbeddings:
    from app.config import get_settings

    embeddings = FakeEmbeddings(dimensions=get_settings().embedding_dimensions)

    def _build(settings=None):
        return embeddings

    import app.api.internal as internal

    monkeypatch.setattr(internal, "build_embeddings", _build)
    return embeddings


def xlsx_bytes(rows: list[list[str]]) -> bytes:
    import io

    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


SALES_XLSX = xlsx_bytes([
    ["报销单号", "姓名", "部门", "金额"],
    ["BX-001", "张三", "销售部", "1000"],
    ["BX-002", "李四", "市场部", "2000"],
])


async def test_batch_indexes_multiple_files_and_preserves_order(
    app_with_temp_storage, fake_embeddings
):
    ws = "batch-ws"
    seed_workspace_file(ws, "a.xlsx", SALES_XLSX)
    seed_workspace_file(ws, "b.xlsx", SALES_XLSX)

    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/index-batch", json={
            "workspace_id": ws,
            "files": [
                {"file_id": "fa", "rel_path": "a.xlsx"},
                {"file_id": "fb", "rel_path": "b.xlsx"},
            ],
        })

    assert response.status_code == 200
    results = response.json()
    assert [r["status"] for r in results] == ["indexed", "indexed"]
    # 结果顺序与请求 files 一致；分块行与向量计数都非空
    assert results[0]["chunk_count"] == results[1]["chunk_count"] > 0
    assert results[0]["vector_count"] > 0
    assert all(r["chunks"] for r in results)
    # 跨文件合并嵌入：两个文件的 child 文本进同一次 embed_documents
    assert len(fake_embeddings.batches) == 1
    assert len(fake_embeddings.batches[0]) == 2 * results[0]["chunk_count"]


async def test_batch_single_file_failure_does_not_poison_the_rest(
    app_with_temp_storage, fake_embeddings
):
    ws = "partial-ws"
    seed_workspace_file(ws, "good.xlsx", SALES_XLSX)

    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/index-batch", json={
            "workspace_id": ws,
            "files": [
                {"file_id": "fbad", "rel_path": "missing.xlsx"},
                {"file_id": "fgood", "rel_path": "good.xlsx"},
            ],
        })

    assert response.status_code == 200
    results = response.json()
    assert results[0]["status"] == "failed"
    assert "missing from disk" in results[0]["error"]
    assert results[1]["status"] == "indexed"
    assert results[1]["chunk_count"] > 0


async def test_single_index_keeps_400_for_invalid_path(
    app_with_temp_storage, fake_embeddings
):
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/index", json={
            "workspace_id": "ws",
            "file_id": "f",
            "rel_path": "../escape.xlsx",
        })
    assert response.status_code == 400


async def test_health_stays_responsive_while_indexing(app_with_temp_storage, monkeypatch):
    """事件循环解锁回归：长索引期间 /health 不再被阻塞。

    此前 /v1/index 的 async handler 内全是同步调用，索引独占事件循环——
    实测 /health 从 0.08s 恶化到 1.4s（见 docs/bugs 与 bench 基线记录）。
    重活挪进 to_thread 后，health 应在慢嵌入完成前就返回。
    """
    ws = "stall-ws"
    seed_workspace_file(ws, "slow.xlsx", SALES_XLSX)
    from app.config import get_settings

    slow = FakeEmbeddings(dimensions=get_settings().embedding_dimensions, delay=1.5)

    def _build(settings=None):
        return slow

    import app.api.internal as internal

    monkeypatch.setattr(internal, "build_embeddings", _build)

    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        indexing = asyncio.create_task(
            client.post("/v1/index", json={
                "workspace_id": ws, "file_id": "f", "rel_path": "slow.xlsx"})
        )
        await asyncio.sleep(0.3)  # 索引已进入嵌入阶段
        started = time.perf_counter()
        health = await client.get("/health")
        health_latency = time.perf_counter() - started
        index_response = await indexing

    assert health.status_code == 200
    assert index_response.status_code == 200
    # 嵌入还要 >1s 才结束，health 期间必须立即返回（阈值留足余量）
    assert health_latency < 0.5, f"/health blocked for {health_latency:.2f}s during indexing"
