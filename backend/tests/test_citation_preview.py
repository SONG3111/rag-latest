"""Regression tests for citation click-through preview (docs/05 §4.2).

The preview endpoint re-reads the original file through the real MCP subprocess
(read_range / read_paragraphs / read_table) — the same read path the agent uses —
so a workbook/docx is uploaded over the API and the returned window is asserted
verbatim. No model is involved anywhere (AGENTS.md: mock-only tests).
"""

from __future__ import annotations

import io

import pytest
from docx import Document
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

pytestmark = pytest.mark.anyio

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def workbook_bytes() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["产品", "销售额"])
    sheet.append(["A型", 1000])
    sheet.append(["B型", 2000])
    sheet.append(["C型", 3000])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def document_bytes() -> bytes:
    document = Document()
    document.add_paragraph("报销制度总则。")       # index 0
    document.add_paragraph("单笔报销不得超过 5000 元。")  # index 1
    document.add_paragraph("超出部分需总经理审批。")       # index 2
    document.add_paragraph("本制度自发布之日起执行。")     # index 3
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def table_document_bytes() -> bytes:
    document = Document()
    document.add_paragraph("附表如下。")
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "项目"
    table.rows[0].cells[1].text = "金额"
    table.rows[1].cells[0].text = "差旅费"
    table.rows[1].cells[1].text = "800"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def _workspace_with_files(app) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workspace_id = (await client.post("/api/workspaces", json={"name": "引用预览"})).json()["id"]
        uploaded = await client.post(
            f"/api/workspaces/{workspace_id}/files",
            files=[
                ("files", ("销售表.xlsx", workbook_bytes(), XLSX_MIME)),
                ("files", ("制度.docx", document_bytes(), DOCX_MIME)),
                ("files", ("附表.docx", table_document_bytes(), DOCX_MIME)),
            ],
        )
        assert uploaded.status_code == 201
        return workspace_id


async def test_excel_row_location_previews_a_window(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        workspace_id = await _workspace_with_files(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/workspaces/{workspace_id}/preview",
                params={"file": "销售表.xlsx", "location": "销售!第3行"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["kind"] == "excel"
            assert data["sheet"] == "销售"
            assert data["highlight"] == 2, "cited row sits mid-window (row 3 of 1..5)"
            rows = data["rows"]
            assert rows[1][0] == "A型" and rows[1][1] == 1000
            # Two context rows on each side, clamped at the first row.
            assert data["start_row"] == 1 and len(rows) == 5


async def test_word_paragraph_location_previews_surroundings(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        workspace_id = await _workspace_with_files(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/workspaces/{workspace_id}/preview",
                params={"file": "制度.docx", "location": "段落 1"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["kind"] == "word_paragraphs"
            assert data["highlight"] == 1
            texts = [item["text"] for item in data["paragraphs"]]
            assert any("5000" in text for text in texts)


async def test_word_table_location_previews_the_table(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        workspace_id = await _workspace_with_files(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/workspaces/{workspace_id}/preview",
                params={"file": "附表.docx", "location": "表格 0 第 2 行"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["kind"] == "word_table"
            assert data["highlight"] == 1
            assert data["rows"][1][0] == "差旅费"


async def test_unknown_location_format_is_rejected(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        workspace_id = await _workspace_with_files(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/workspaces/{workspace_id}/preview",
                params={"file": "销售表.xlsx", "location": "不知道在哪里"},
            )
            assert response.status_code == 400


def test_parse_location_supports_parent_ranges():
    from app.services.preview import parse_location

    assert parse_location("销售!第12行") == {"kind": "excel", "sheet": "销售", "row": 12}
    assert parse_location("段落 7") == {"kind": "word_paragraphs", "start": 7, "end": 7}
    assert parse_location("段落 7-9") == {"kind": "word_paragraphs", "start": 7, "end": 9}
    assert parse_location("表格 0") == {"kind": "word_table", "table": 0, "row": None}
