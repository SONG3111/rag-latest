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


def test_chunk_excel_formula_cells_stay_indexed(tmp_path) -> None:
    """openpyxl writes formulas without cached values; the formula text must not vanish.

    Regression: a formula column written by set_formula/update_cells disappeared
    from every chunk because data_only=True reads None for uncached formulas.
    """
    path = tmp_path / "表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet.append(["产品", "数量", "金额"])
    sheet.append(["A型", 2, "=B2*C1"])
    sheet.append(["B型", 3, "=B3*C1"])
    book.save(path)

    groups = list(chunk_excel_groups(path, "表.xlsx"))
    children_text = "\n".join(child.text for group in groups for child in group.children)
    assert "金额==B2*C1" in children_text
    assert "数量=2" in children_text


def test_chunk_excel_data_starting_below_row_one_keeps_real_header(tmp_path) -> None:
    """A sheet whose data begins at B3 must use that row as the header.

    Regression: row 1 (empty) was treated as the header, so pairs degraded to
    column-letter keys and every row number was off by the leading empties.
    """
    path = tmp_path / "表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet["B3"] = "姓名"
    sheet["C3"] = "城市"
    sheet["B4"] = "陈晨"
    sheet["C4"] = "杭州"
    book.save(path)

    groups = list(chunk_excel_groups(path, "表.xlsx"))
    assert len(groups) == 1
    child = groups[0].children[0]
    assert "姓名=陈晨, 城市=杭州" in child.text
    assert child.location == "数据!第4行"


def test_chunk_excel_oversized_row_splits_by_token_budget(tmp_path) -> None:
    """An Excel row over the token budget splits exactly like a Word body.

    Regression: Excel children were cut at a character cap (1200 chars ≈
    1100+ tokens), double the embedding model's recommended retrieval size,
    because only the Word path went through the token-measured splitter.
    """
    path = tmp_path / "表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet.append(["产品", "备注"])
    sheet.append(["A型", "交付条款以书面确认为准。" * 30])  # ~420 chars > 200 budget
    book.save(path)

    groups = list(
        chunk_excel_groups(
            path,
            "表.xlsx",
            chunk_size_tokens=200,
            chunk_overlap_tokens=30,
            body_splitter=_char_body_splitter(200, 30),
        )
    )
    assert len(groups) == 1
    children = groups[0].children
    assert len(children) > 1, "a 420-char row must exceed one 200-budget child"

    header = "文件：表.xlsx\n工作表：数据\n表头：产品 | 备注\n"
    assert all(child.text.startswith(header) for child in children)
    bodies = [child.text.removeprefix(header) for child in children]
    assert all(len(body) <= 200 for body in bodies)
    assert [child.meta["part"] for child in children] == list(range(len(children)))
    assert all(child.location == "数据!第2行" for child in children)
    # Pieces are whole sentences, never cut mid-clause.
    assert all(body.endswith("。") for body in bodies)


def test_chunk_excel_stacked_tables_get_their_own_header(tmp_path) -> None:
    """A second table stacked below the first must use its own columns.

    Regression: only the sheet's first row was ever the header, so the
    stacked table's rows were labelled with the first table's columns and
    the index held semantically wrong pairs like 产品=张伟.
    """
    path = tmp_path / "表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet.append(["产品", "类别"])
    sheet.append(["显示器", "数码"])
    sheet.append([None, None])  # blank separator between the two tables
    sheet.append(["部门", "报销人"])
    sheet.append(["市场部", "张伟"])
    book.save(path)

    groups = list(chunk_excel_groups(path, "表.xlsx"))
    children_text = "\n".join(child.text for group in groups for child in group.children)
    assert "产品=显示器" in children_text
    assert "部门=市场部, 报销人=张伟" in children_text
    assert "产品=张伟" not in children_text
    assert "产品=市场部" not in children_text


def test_chunk_excel_blank_row_inside_one_table_keeps_header(tmp_path) -> None:
    """A data row after a blank row must not be mistaken for a header.

    The header sniff requires text-only cells, so a block starting with a
    numeric data row keeps the original columns instead of relabelling the
    table around it.
    """
    path = tmp_path / "表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet.append(["产品", "数量"])
    sheet.append(["显示器", 7])
    sheet.append([None, None])
    sheet.append(["打印机", 3])
    book.save(path)

    groups = list(chunk_excel_groups(path, "表.xlsx"))
    children_text = "\n".join(child.text for group in groups for child in group.children)
    assert "产品=显示器, 数量=7" in children_text
    assert "产品=打印机, 数量=3" in children_text
    assert "表头：打印机 | 数量" not in children_text


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


def test_chunk_word_custom_style_based_on_heading_starts_section(tmp_path) -> None:
    """A custom/localized style inheriting from Heading 1 opens a section.

    Regression: only style names literally starting with "Heading" counted,
    so a custom「标题 1」style (or any localized variant) merged its body
    into the previous section and citations pointed at the wrong chapter.
    """
    from docx.enum.style import WD_STYLE_TYPE

    path = tmp_path / "制度.docx"
    document = Document()
    localized = document.styles.add_style("标题 1", WD_STYLE_TYPE.PARAGRAPH)
    localized.base_style = document.styles["Heading 1"]
    document.add_paragraph("旧章节内容。")
    document.add_paragraph("报销标准", style=localized)
    document.add_paragraph("这一段属于「报销标准」章节。")
    document.save(str(path))

    groups = list(chunk_word_groups(path, "制度.docx"))
    assert len(groups) == 2
    assert groups[1].parent.meta["heading"] == "报销标准"
    assert "章节：报销标准" in groups[1].children[0].text


def test_chunk_word_outline_level_starts_section(tmp_path) -> None:
    """A paragraph carrying w:outlineLvl is a heading, language-independently.

    Ported from docling's heading detection: the outline level is the OOXML
    authority on "this is a heading" and survives any style naming scheme.
    """
    try:
        from docx.oxml import parse_xml
    except ImportError:  # python-docx < 1.0 layout
        from docx.oxml.parser import parse_xml
    from docx.oxml.ns import nsdecls

    path = tmp_path / "制度.docx"
    document = Document()
    document.add_paragraph("前言内容。")
    heading = document.add_paragraph("第一章 总则")
    ppr = heading._p.get_or_add_pPr()
    ppr.append(parse_xml(f'<w:outlineLvl {nsdecls("w")} w:val="0"/>'))
    document.add_paragraph("正文。")
    document.save(str(path))

    groups = list(chunk_word_groups(path, "制度.docx"))
    assert len(groups) == 2
    assert groups[1].parent.meta["heading"] == "第一章 总则"
    assert "章节：第一章 总则" in groups[1].children[0].text


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


# --------------------------------------------------------------------------- #
# token-budgeted Word body splitting (langchain-text-splitters)
# --------------------------------------------------------------------------- #
def _char_body_splitter(chunk_size: int, chunk_overlap: int):
    """The real framework splitter measuring in characters, so tests stay hermetic."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    from app.retrieval.chunking import BODY_SEPARATORS, BodySplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=BODY_SEPARATORS,
        keep_separator="end",
        length_function=len,
    )
    return BodySplitter(splitter=splitter, measure=len)


def test_word_body_children_split_by_token_budget(tmp_path) -> None:
    path = tmp_path / "制度.docx"
    document = Document()
    document.add_heading("报销规定", level=1)
    sentence = "员工报销单据应当在费用发生后十个工作日内提交。"
    document.add_paragraph(sentence * 12)
    document.save(str(path))

    groups = list(
        chunk_word_groups(
            path,
            "制度.docx",
            chunk_size_tokens=200,
            chunk_overlap_tokens=30,
            body_splitter=_char_body_splitter(200, 30),
        )
    )
    children = [child for group in groups for child in group.children]
    assert len(children) > 1, "a 540-char paragraph must exceed one 200-budget child"

    header = "文件：制度.docx\n章节：报销规定\n"
    assert all(child.text.startswith(header) for child in children)
    bodies = [child.text.removeprefix(header) for child in children]
    assert all(len(body) <= 200 for body in bodies)
    # The header is prepended to every piece, so the *embedded* child — the
    # text that actually reaches the vector store — must fit the budget too.
    assert all(len(child.text) <= 200 for child in children)
    assert [child.meta["part"] for child in children] == list(range(len(children)))

    # Pieces are whole sentences, never cut mid-clause.
    assert all(body.endswith("。") for body in bodies)
    # Consecutive pieces share overlap text taken from the previous piece's tail.
    for previous, current in zip(bodies, bodies[1:]):
        assert current[:10] in previous


def test_word_body_splitter_reserves_the_header_budget(tmp_path) -> None:
    """The leading header counts against the budget only in the degenerate case.

    A normal budget leaves room for the body after the header is reserved; a
    pathological one (header ≈ budget) falls back to counting the header inside
    the budget rather than crowding the content out entirely.
    """
    path = tmp_path / "制度.docx"
    document = Document()
    document.add_heading("标题", level=1)
    document.add_paragraph("短句一。短句二。")
    document.save(str(path))

    groups = list(
        chunk_word_groups(
            path,
            "制度.docx",
            chunk_size_tokens=70,
            chunk_overlap_tokens=5,
            body_splitter=_char_body_splitter(70, 5),
        )
    )
    children = [child for group in groups for child in group.children]
    assert children
    assert all("文件：制度.docx" in child.text for child in children)
    assert all("短句二" in child.text for child in children)


def test_body_splitter_defaults_to_character_fallback(tmp_path) -> None:
    from app.retrieval.chunking import build_body_splitter

    # No configured path: the same recursive strategy measures characters.
    fallback = build_body_splitter(100, 10, model_path="")
    assert fallback.measure("报销制度") == 4

    # A path that exists but holds no tokenizer degrades the same way.
    empty = tmp_path / "weights"
    empty.mkdir()
    degraded = build_body_splitter(100, 10, model_path=str(empty))
    assert degraded.measure("报销制度") == 4

    from langchain_text_splitters import RecursiveCharacterTextSplitter

    assert isinstance(fallback.splitter, RecursiveCharacterTextSplitter)


def test_chunk_overlap_must_fit_inside_the_chunk_budget() -> None:
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(chunk_size_tokens=512, chunk_overlap_tokens=512)
    assert Settings(chunk_size_tokens=512, chunk_overlap_tokens=64).chunk_overlap_tokens == 64


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
