"""索引链路文件日志的回归测试（AGENTS.md：mock-only，fake embeddings）。

钉住三件事：
1. 分块明细（逐 chunk 行、嵌入批次、向量写入、结果摘要）确实落到
   ``data_dir/logs/ai-service.log``；
2. 明细 logger 不向 root 传播——控制台不被逐 chunk 行刷屏；
3. text_preview 的换行转义与截断标记（保证一行日志一条记录）。
"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

from conftest import seed_workspace_file

pytestmark = pytest.mark.anyio


class FakeEmbeddings:
    """Deterministic stand-in; never contacts a provider."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
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


async def test_index_writes_chunk_details_to_log_file(
    app_with_temp_storage, fake_embeddings
):
    from app.config import get_settings

    ws = "log-ws"
    seed_workspace_file(ws, "a.xlsx", SALES_XLSX)

    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/index", json={
            "workspace_id": ws, "file_id": "fa", "rel_path": "a.xlsx"})

    assert response.status_code == 200
    log_path = get_settings().data_dir / "logs" / "ai-service.log"
    content = log_path.read_text(encoding="utf-8")

    for marker in (
        "single.start",
        "parse.start",
        "chunk.parent",
        "chunk.child",
        "parse.summary",
        "embed.texts",
        "vectors.clear",
        "vectors.written",
        "single.result",
        "file logging enabled",
    ):
        assert marker in content, f"missing log marker: {marker}"
    # 分块行携带定位与元数据：Excel 的 meta 里有 sheet 字段
    assert '"sheet"' in content
    assert "status=indexed" in content


async def test_index_batch_logs_merged_embedding_and_results(
    app_with_temp_storage, fake_embeddings
):
    from app.config import get_settings

    ws = "batch-log-ws"
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
    content = (
        (get_settings().data_dir / "logs" / "ai-service.log")
        .read_text(encoding="utf-8")
    )
    for marker in (
        "batch.start",
        "batch.parsed ws=batch-log-ws ok=1 failed=1",
        "embed.merged",
        "batch.result",
        "status=failed error=file is missing from disk",
    ):
        assert marker in content, f"missing log marker: {marker}"


async def test_detail_logger_stays_off_the_console():
    """明细只进文件：propagate=False 保证逐 chunk 行不会刷 root/控制台。"""
    from app.logging_setup import INDEX_LOGGER

    detail = logging.getLogger(INDEX_LOGGER)
    assert detail.propagate is False


def test_text_preview_flattens_and_truncates():
    from app.logging_setup import text_preview

    assert text_preview("a\nb\tc") == "a\\nb\\tc"
    long = "x" * 300
    preview = text_preview(long, limit=50)
    assert preview.startswith("x" * 50)
    assert preview.endswith("(+250 chars)")
    assert "\n" not in text_preview("line1\r\nline2")
    assert text_preview(None) == ""
