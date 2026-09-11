"""Retrieval pieces that must work without any network access."""

from __future__ import annotations

import io

import pytest
from docx import Document
from openpyxl import Workbook

from app.models import Workspace
from app.retrieval.bm25 import BM25Index, reciprocal_rank_fusion, tokenize
from app.retrieval.chunking import (
    chunk_document,
    chunk_document_groups,
    chunk_excel_groups,
    chunk_word_groups,
)
from app.retrieval.pipeline import Retriever, format_context
from app.services.files import index_file, save_upload


def test_tokenize_splits_chinese_and_latin() -> None:
    tokens = tokenize("销售额 Total 2026")
    assert "2026" in tokens
    assert "total" in tokens
    assert any("销售" in token for token in tokens)


def test_tokenize_handles_empty_input() -> None:
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_rrf_prefers_items_ranked_high_by_both_lists() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]])
    assert {item for item, _ in fused[:2]} == {"a", "b"}


def test_rrf_honours_weights() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "b"], ["b", "a"]], weights=[2.0, 0.5]))
    assert fused["a"] > fused["b"]


def test_bm25_ranks_matching_chunk_first() -> None:
    class Row:
        def __init__(self, id_, counts, length):
            self.id = id_
            self.token_counts = counts
            self.token_length = length

    rows = [
        Row("c1", {"报销": 1, "上限": 1, "5000": 1}, 3),
        Row("c2", {"出差": 1, "补贴": 1, "200": 1}, 3),
    ]
    index = BM25Index(rows)
    results = index.search("报销上限", top_k=2)
    assert results[0][0] == "c1"


def test_bm25_empty_index_returns_nothing() -> None:
    assert BM25Index([]).search("任意", top_k=5) == []


def test_chunk_excel_groups_rows_with_header(tmp_path) -> None:
    path = tmp_path / "表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet.append(["产品", "数量"])
    for index in range(20):
        sheet.append([f"P{index}", index])
    book.save(path)

    groups = list(chunk_excel_groups(path, "表.xlsx"))
    assert len(groups) >= 2

    # Children carry the header so a single row is interpretable on its own, and
    # they are addressable precisely by row number.
    first_child = groups[0].children[0]
    assert "表头：产品 | 数量" in first_child.text
    assert first_child.location == "数据!第2行"
    assert first_child.meta["row"] == 2

    # The parent spans the whole bucket and is the context handed to the model.
    assert groups[0].parent.location == "数据!第2-13行"
    assert groups[0].parent.meta["row_start"] == 2

    # One child per non-empty row, so nothing is dropped or duplicated.
    assert sum(len(group.children) for group in groups) == 20


def test_chunk_excel_parent_covers_its_children(tmp_path) -> None:
    path = tmp_path / "表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet.append(["产品", "数量"])
    for index in range(6):
        sheet.append([f"P{index}", index])
    book.save(path)

    groups = list(chunk_excel_groups(path, "表.xlsx"))
    assert len(groups) == 1
    group = groups[0]
    for child in group.children:
        row_label = child.location.split("第")[1]
        assert f"第{row_label}: " in group.parent.text


def test_chunk_word_groups_by_heading(tmp_path) -> None:
    path = tmp_path / "制度.docx"
    document = Document()
    document.add_heading("第一章", level=1)
    document.add_paragraph("内容甲")
    document.add_heading("第二章", level=1)
    document.add_paragraph("内容乙")
    document.save(str(path))

    groups = list(chunk_word_groups(path, "制度.docx"))
    assert len(groups) == 2
    assert groups[0].parent.meta["heading"] == "第一章"
    assert "章节：第一章" in groups[0].children[0].text
    assert groups[1].parent.meta["heading"] == "第二章"

    # A child cites the exact paragraph index, not a range.
    assert groups[0].children[0].location.startswith("段落 ")


def test_chunk_word_table_rows_are_children(tmp_path) -> None:
    path = tmp_path / "表.docx"
    document = Document()
    document.add_heading("限额", level=1)
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "费用类型"
    table.cell(0, 1).text = "限额"
    table.cell(1, 0).text = "招待费"
    table.cell(1, 1).text = "3000"
    table.cell(2, 0).text = "差旅费"
    table.cell(2, 1).text = "5000"
    document.save(str(path))

    groups = list(chunk_word_groups(path, "表.docx"))
    table_group = next(
        group for group in groups if group.parent.meta.get("table_index") == 0
    )
    assert len(table_group.children) == 3
    assert table_group.children[1].location == "表格 0 第 2 行"
    assert "招待费" in table_group.children[1].text
    assert table_group.parent.text.count("\n") >= 3


def test_chunk_document_groups_dispatches_and_flattens(tmp_path) -> None:
    excel = tmp_path / "a.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "S"
    sheet.append(["列"])
    sheet.append(["值"])
    book.save(excel)

    groups = chunk_document_groups(excel, "a.xlsx")
    assert groups
    # The flattened view is exactly the children, nothing else.
    assert chunk_document(excel, "a.xlsx") == [
        child for group in groups for child in group.children
    ]


def test_chunk_document_dispatches_by_extension(tmp_path) -> None:
    excel = tmp_path / "a.xlsx"
    book = Workbook()
    book.active.append(["列"])
    book.save(excel)
    assert chunk_document(excel, "a.xlsx")
    assert chunk_document(tmp_path / "unknown.txt", "unknown.txt") == []


def _seed(session, tmp_path, monkeypatch):
    """Create a workspace with one Excel and one Word file, indexed via BM25 only."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data", raising=False)
    settings.ensure_directories()

    workspace = Workspace(name="检索")
    session.add(workspace)
    session.flush()

    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["产品", "区域", "销售额"])
    sheet.append(["A型", "华东", 1000])
    sheet.append(["B型", "华北", 2000])
    buffer = io.BytesIO()
    book.save(buffer)
    record = save_upload(session, workspace, "销售表.xlsx", buffer.getvalue())
    index_file(session, workspace.id, record)

    document = Document()
    document.add_heading("报销制度", level=1)
    document.add_paragraph("第二条 单笔报销金额不得超过 5000 元。")
    buffer = io.BytesIO()
    document.save(buffer)
    record = save_upload(session, workspace, "制度.docx", buffer.getvalue())
    index_file(session, workspace.id, record)

    session.flush()
    return workspace


def test_keyword_retrieval_finds_the_governing_clause(
    temp_session, tmp_path, monkeypatch
) -> None:
    workspace = _seed(temp_session, tmp_path, monkeypatch)
    retriever = Retriever(temp_session, workspace.id)
    hits = retriever.search("报销上限", use_dense=False, use_rerank=False)
    assert hits
    assert hits[0].rel_path == "制度.docx"
    assert "5000" in hits[0].text


def test_keyword_retrieval_finds_table_rows(
    temp_session, tmp_path, monkeypatch
) -> None:
    workspace = _seed(temp_session, tmp_path, monkeypatch)
    retriever = Retriever(temp_session, workspace.id)
    hits = retriever.search("华北", use_dense=False, use_rerank=False)
    assert hits
    assert hits[0].rel_path == "销售表.xlsx"
    assert hits[0].location.startswith("销售!")


def test_retrieval_on_empty_workspace_returns_nothing(temp_session) -> None:
    workspace = Workspace(name="空")
    temp_session.add(workspace)
    temp_session.flush()
    assert Retriever(temp_session, workspace.id).search("任意问题") == []


def test_blank_query_is_ignored(temp_session, tmp_path, monkeypatch) -> None:
    workspace = _seed(temp_session, tmp_path, monkeypatch)
    assert Retriever(temp_session, workspace.id).search("   ", use_dense=False) == []


def test_format_context_marks_sources() -> None:
    from app.retrieval.pipeline import RetrievedChunk

    text = format_context(
        [
            RetrievedChunk(
                chunk_id="c1",
                text="正文内容",
                rel_path="制度.docx",
                location="段落 1-2",
                score=0.5,
            )
        ]
    )
    assert "制度.docx" in text
    assert "段落 1-2" in text
    assert "[资料1]" in text


def test_format_context_handles_no_hits() -> None:
    assert "没有检索到" in format_context([])
