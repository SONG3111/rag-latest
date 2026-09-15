from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from mcp_office_server import excel_ops
from mcp_office_server.errors import (
    InvalidSheetName,
    InvalidValue,
    LastSheetError,
    MergeConflict,
    RangeError,
    SheetExists,
    SheetNotFound,
    WriteConflict,
)


def test_structure_lists_sheets_and_header(workbook_path: Path) -> None:
    structure = excel_ops.workbook_structure(workbook_path)
    assert structure["kind"] == "excel"
    names = [sheet["name"] for sheet in structure["sheets"]]
    assert names == ["销售", "汇总"]
    sales = structure["sheets"][0]
    assert sales["header"] == ["产品", "区域", "销售额"]
    assert sales["max_row"] == 4


def test_read_range_returns_values_and_digest(workbook_path: Path) -> None:
    result = excel_ops.read_range(workbook_path, "销售", start_cell="A1", end_cell="C3")
    assert result["values"][0] == ["产品", "区域", "销售额"]
    assert result["values"][2] == ["B型", "华北", 2000]
    assert result["truncated"] is False
    assert len(result["digest"]) == 64


def test_read_range_without_end_cell_reads_to_used_range(workbook_path: Path) -> None:
    """Omitting end_cell means "the rest of the sheet", not "one cell".

    Regression: the tool used to return just A1, so the model asked to read a
    populated table, saw only the header, and told the user the sheet was empty.
    """
    result = excel_ops.read_range(workbook_path, "销售", start_cell="A1")
    assert result["values"] == [
        ["产品", "区域", "销售额"],
        ["A型", "华东", 1000],
        ["B型", "华北", 2000],
        ["C型", "华南", 3000],
    ]
    assert result["end_cell"] == "C4"
    assert result["total_rows"] == 4
    assert result["sheet_max_row"] == 4
    assert result["truncated"] is False


def test_read_range_without_end_cell_respects_start_offset(workbook_path: Path) -> None:
    result = excel_ops.read_range(workbook_path, "销售", start_cell="A2")
    assert result["values"] == [
        ["A型", "华东", 1000],
        ["B型", "华北", 2000],
        ["C型", "华南", 3000],
    ]
    assert result["start_cell"] == "A2"
    assert result["end_cell"] == "C4"


def test_read_range_without_end_cell_truncates_long_sheets(workbook_path: Path) -> None:
    result = excel_ops.read_range(workbook_path, "销售", start_cell="A1", max_rows=2)
    assert result["truncated"] is True
    assert len(result["values"]) == 2
    # The reported end cell is where the caller should resume reading.
    assert result["end_cell"] == "C2"


def test_read_range_accepts_a_start_cell_that_is_already_a_range(workbook_path: Path) -> None:
    """``start_cell="A1:C2"`` is accepted, matching the upstream MCP server."""
    result = excel_ops.read_range(workbook_path, "销售", start_cell="A1:C2")
    assert result["values"] == [["产品", "区域", "销售额"], ["A型", "华东", 1000]]
    assert result["end_cell"] == "C2"


def test_read_range_expands_from_the_sheets_real_origin(tmp_path: Path) -> None:
    """A sheet whose data does not start at A1 must still read whole from A1."""
    from openpyxl import Workbook

    path = tmp_path / "offset.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "偏移"
    sheet["B3"] = "产品"
    sheet["C3"] = "销售额"
    sheet["B4"] = "A型"
    sheet["C4"] = 1000
    book.save(path)

    result = excel_ops.read_range(path, "偏移", start_cell="A1")
    assert result["start_cell"] == "B3"
    assert result["values"] == [["产品", "销售额"], ["A型", 1000]]


def test_read_range_on_an_empty_sheet_stays_a_single_cell(tmp_path: Path) -> None:
    from openpyxl import Workbook

    path = tmp_path / "empty.xlsx"
    book = Workbook()
    book.active.title = "空白"
    book.save(path)

    result = excel_ops.read_range(path, "空白")
    assert result["values"] == [[None]]
    assert result["truncated"] is False


def test_read_range_surfaces_formula_and_cached_value(workbook_path: Path) -> None:
    result = excel_ops.read_range(workbook_path, "汇总", start_cell="A1", end_cell="B2")
    formula_cell = result["values"][0][1]
    assert isinstance(formula_cell, dict)
    assert formula_cell["formula"] == "=SUM(销售!C2:C4)"


def test_read_range_unknown_sheet(workbook_path: Path) -> None:
    with pytest.raises(SheetNotFound):
        excel_ops.read_range(workbook_path, "不存在")


def test_read_range_rejects_bad_range(workbook_path: Path) -> None:
    with pytest.raises(RangeError):
        excel_ops.read_range(workbook_path, "销售", start_cell="not-a-cell")


def test_read_range_truncates_large_requests(workbook_path: Path) -> None:
    result = excel_ops.read_range(
        workbook_path, "销售", start_cell="A1", end_cell="Z100", max_rows=2, max_cols=2
    )
    assert result["truncated"] is True
    assert len(result["values"]) == 2
    assert len(result["values"][0]) == 2


def test_update_cells_reports_before_and_after(workbook_path: Path) -> None:
    result = excel_ops.update_cells(
        workbook_path, "销售", [{"cell": "C2", "value": 1500}]
    )
    change = result["changes"][0]
    assert change == {"cell": "C2", "before": 1000, "after": 1500}

    reloaded = load_workbook(workbook_path)
    assert reloaded["销售"]["C2"].value == 1500


def test_update_cells_clears_cell_on_none(workbook_path: Path) -> None:
    excel_ops.update_cells(workbook_path, "销售", [{"cell": "C2", "value": None}])
    reloaded = load_workbook(workbook_path)
    assert reloaded["销售"]["C2"].value is None


def test_update_cells_unwraps_formula_object(workbook_path: Path) -> None:
    """Models sometimes wrap a formula as {"formula": "..."}; unwrap, don't fail."""
    result = excel_ops.update_cells(
        workbook_path, "销售", [{"cell": "C2", "value": {"formula": "=B2*2"}}]
    )
    change = result["changes"][0]
    assert change["after"] == "=B2*2"

    reloaded = load_workbook(workbook_path)
    assert reloaded["销售"]["C2"].value == "=B2*2"


def test_update_cells_requires_coordinate(workbook_path: Path) -> None:
    with pytest.raises(RangeError):
        excel_ops.update_cells(workbook_path, "销售", [{"value": 1}])


def test_update_cells_detects_stale_digest(workbook_path: Path) -> None:
    stale = excel_ops.read_range(workbook_path, "销售", end_cell="C4")["digest"]
    excel_ops.update_cells(workbook_path, "销售", [{"cell": "C2", "value": 1}])
    with pytest.raises(WriteConflict):
        excel_ops.update_cells(
            workbook_path,
            "销售",
            [{"cell": "C3", "value": 2}],
            expected_digest=stale,
        )


def test_insert_rows_shifts_content(workbook_path: Path) -> None:
    result = excel_ops.insert_rows(workbook_path, "销售", start_row=2, count=1)
    assert result["rows_after"] == result["rows_before"] + 1
    reloaded = load_workbook(workbook_path)
    assert reloaded["销售"]["A2"].value is None
    assert reloaded["销售"]["A3"].value == "A型"


def test_delete_rows_rewrites_formula_references(tmp_path: Path) -> None:
    """Regression: deleting rows shifted cells but left =C3*D3 pointing at row 3.

    openpyxl does not manage formula dependencies on row operations (documented
    limitation, issue #1273), so excel_ops rewrites references across the whole
    workbook — including cross-sheet references to the affected sheet.
    """
    path = tmp_path / "订单.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "订单"
    sheet.append(["产品", "数量", "单价", "金额"])
    for index in range(1, 6):
        sheet.append([f"P{index}", index, 10, f"=B{index + 1}*C{index + 1}"])
    summary = book.create_sheet("汇总")
    summary["A1"] = "=订单!D3"
    book.save(path)

    excel_ops.delete_rows(path, "订单", 2, 1)  # delete the first data row

    reloaded = load_workbook(path)
    moved = reloaded["订单"]
    assert moved["B2"].value == 2  # old P2 row shifted up (P1 was deleted)
    assert moved["D2"].value == "=B2*C2"
    assert moved["D3"].value == "=B3*C3"
    assert reloaded["汇总"]["A1"].value == "=订单!D2"


def test_insert_rows_rewrites_formula_references(tmp_path: Path) -> None:
    path = tmp_path / "订单.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "订单"
    sheet.append(["产品", "数量"])
    sheet.append(["P1", 5])
    sheet["B3"] = "=B2*2"
    book.save(path)

    excel_ops.insert_rows(path, "订单", 2, 1)  # insert above P1

    reloaded = load_workbook(path)
    assert reloaded["订单"]["B2"].value is None
    assert reloaded["订单"]["B4"].value == "=B3*2"


def test_delete_rows_rejects_bad_arguments(workbook_path: Path) -> None:
    with pytest.raises(RangeError):
        excel_ops.delete_rows(workbook_path, "销售", start_row=0)
    with pytest.raises(RangeError):
        excel_ops.delete_rows(workbook_path, "销售", start_row=1, count=0)


def test_set_formula_normalizes_equals_sign(workbook_path: Path) -> None:
    result = excel_ops.set_formula(workbook_path, "汇总", "B2", "SUM(销售!C2:C4)")
    assert result["changes"][0]["after"] == "=SUM(销售!C2:C4)"


def test_set_formula_rejects_empty(workbook_path: Path) -> None:
    with pytest.raises(RangeError):
        excel_ops.set_formula(workbook_path, "汇总", "B2", "   ")


def test_format_range_applies_and_reports(workbook_path: Path) -> None:
    result = excel_ops.format_range(
        workbook_path, "销售", "A1", "C1", bold=True, fill_color="FFF2CC"
    )
    assert "bold=True" in result["applied"]
    reloaded = load_workbook(workbook_path)
    assert reloaded["销售"]["A1"].font.bold is True


def test_find_text_locates_across_sheets(workbook_path: Path) -> None:
    hits = excel_ops.find_text(workbook_path, "华北")
    assert hits == [{"sheet": "销售", "cell": "B3", "value": "华北"}]


def test_find_text_matches_numeric_cells(workbook_path: Path) -> None:
    """A query for a number must find numeric cells, not only text ones.

    Regression: a user asked "哪里出现了 299", find_text scanned str cells only,
    and reported the value missing while the cell held 299.0.
    """
    hits = excel_ops.find_text(workbook_path, "2000")
    assert [(hit["sheet"], hit["cell"]) for hit in hits] == [("销售", "C3")]


def test_find_text_blank_query_returns_nothing(workbook_path: Path) -> None:
    assert excel_ops.find_text(workbook_path, "   ") == []


# --------------------------------------------------------------------------- #
# insert_columns / delete_columns
# --------------------------------------------------------------------------- #
def test_insert_columns_shifts_content_and_formulas(tmp_path: Path) -> None:
    """Regression: openpyxl moves cells on insert_cols but not formula strings."""
    path = tmp_path / "报表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "报表"
    sheet.append(["产品", "区域", "销售额"])
    sheet.append(["A型", "华东", 1000])
    sheet["E2"] = "=C2*2"
    book.save(path)

    result = excel_ops.insert_columns(path, "报表", start_col=2, count=1)

    assert result["columns_after"] == result["columns_before"] + 1
    reloaded = load_workbook(path)
    moved = reloaded["报表"]
    assert moved["A2"].value == "A型"
    assert moved["B2"].value is None  # the fresh empty column
    assert moved["C2"].value == "华东"
    assert moved["F2"].value == "=D2*2"  # reference followed the shift


def test_delete_columns_rewrites_formula_references(tmp_path: Path) -> None:
    path = tmp_path / "报表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "报表"
    sheet.append(["一月", "二月", "三月"])
    sheet.append([10, 20, 30])
    sheet["E2"] = "=B2+C2"
    book.save(path)

    excel_ops.delete_columns(path, "报表", start_col=2)  # drop 二月

    reloaded = load_workbook(path)
    moved = reloaded["报表"]
    assert moved["B2"].value == 30  # 三月 moved left
    assert moved["D2"].value == "=#REF!+B2"  # the formula cell itself moved E2→D2:
    # its B2 reference died with the deleted column, C2 shifted to B2


def test_insert_delete_columns_reject_bad_arguments(workbook_path: Path) -> None:
    with pytest.raises(RangeError):
        excel_ops.insert_columns(workbook_path, "销售", start_col=0)
    with pytest.raises(RangeError):
        excel_ops.delete_columns(workbook_path, "销售", start_col=1, count=0)


def test_delete_columns_unknown_sheet(workbook_path: Path) -> None:
    with pytest.raises(SheetNotFound):
        excel_ops.insert_columns(workbook_path, "不存在", start_col=1)


# --------------------------------------------------------------------------- #
# copy_range
# --------------------------------------------------------------------------- #
def test_copy_range_moves_values_styles_and_translates_formulas(workbook_path: Path) -> None:
    """Excel-style copy: relative references translate, absolute stay put."""
    book = load_workbook(workbook_path)
    book["销售"]["D1"] = "倍数"
    book["销售"]["D2"] = "=B2&C2"  # relative, must follow the shift
    book["销售"]["E1"] = 3
    book["销售"]["E2"] = "=$E$1*C2"
    book.save(workbook_path)

    result = excel_ops.copy_range(workbook_path, "销售", "D2:E2", "销售", "D4")

    assert result["copied_cells"] == 2
    reloaded = load_workbook(workbook_path)
    assert reloaded["销售"]["D4"].value == "=B4&C4"  # rows +2
    assert reloaded["销售"]["E4"].value == "=$E$1*C4"  # absolute stays, relative shifts


def test_copy_range_across_sheets(tmp_path: Path) -> None:
    path = tmp_path / "跨表.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "源"
    sheet["A1"] = "产品"
    sheet["A2"] = "A型"
    book.create_sheet("目标")
    book.save(path)

    result = excel_ops.copy_range(path, "源", "A1:A2", "目标", "B3")

    assert result["destination"].startswith("目标!B3")
    reloaded = load_workbook(path)
    assert reloaded["目标"]["B3"].value == "产品"
    assert reloaded["目标"]["B4"].value == "A型"


def test_copy_range_rejects_merged_source_and_destination(tmp_path: Path) -> None:
    """Copying across a merge silently drops structure — refuse it instead."""
    path = tmp_path / "合并.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet["A1"] = "表头"
    sheet.merge_cells("A1:B1")
    sheet["A3"] = 1
    book.create_sheet("其他")
    book.save(path)

    with pytest.raises(MergeConflict):
        excel_ops.copy_range(path, "数据", "A1:B1", "其他", "A1")
    with pytest.raises(MergeConflict):
        excel_ops.copy_range(path, "数据", "A3:A3", "数据", "A1")  # dst hits the merge
    with pytest.raises(RangeError):
        excel_ops.copy_range(path, "数据", "A3:A3", "数据", "A1:B2")  # dst must be a cell


def test_copy_range_unknown_sheet(workbook_path: Path) -> None:
    with pytest.raises(SheetNotFound):
        excel_ops.copy_range(workbook_path, "不存在", "A1:A2", "销售", "F1")


# --------------------------------------------------------------------------- #
# delete_range
# --------------------------------------------------------------------------- #
def test_delete_range_shift_up_moves_lower_rows_into_the_hole(tmp_path: Path) -> None:
    path = tmp_path / "清单.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "清单"
    for row in (["项目", "金额"], ["甲", 1], ["乙", 2], ["丙", 3], ["丁", 4]):
        sheet.append(row)
    sheet["F2"] = "=B3+B4"
    book.save(path)

    result = excel_ops.delete_range(path, "清单", "A3:B4", shift="up")

    assert result["shift"] == "up"
    reloaded = load_workbook(path)
    moved = reloaded["清单"]
    assert [moved[f"A{r}"].value for r in range(2, 6)] == ["甲", "丁", None, None]
    assert [moved[f"B{r}"].value for r in range(2, 6)] == [1, 4, None, None]
    # A formula outside the band is untouched (documented limitation): it keeps
    # pointing at B3/B4, whose values now come from the shifted rows.
    assert moved["F2"].value == "=B3+B4"


def test_delete_range_shift_left_moves_right_cells_into_the_hole(tmp_path: Path) -> None:
    path = tmp_path / "清单.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "清单"
    sheet.append(["项目", "坏列", "一月", "二月"])
    sheet.append(["甲", None, 10, 20])
    book.save(path)

    excel_ops.delete_range(path, "清单", "B1:B2", shift="left")

    reloaded = load_workbook(path)
    moved = reloaded["清单"]
    assert [moved.cell(row=1, column=c).value for c in range(1, 5)] == ["项目", "一月", "二月", None]
    assert moved["B2"].value == 10


def test_delete_range_rejects_unknown_shift_and_merges(tmp_path: Path) -> None:
    path = tmp_path / "合并.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "数据"
    sheet["B2"] = "标题"
    sheet.merge_cells("B2:C2")
    sheet["A5"] = 1
    book.save(path)

    with pytest.raises(RangeError):
        excel_ops.delete_range(path, "数据", "A1:A2", shift="diagonal")
    with pytest.raises(MergeConflict):
        excel_ops.delete_range(path, "数据", "A1:B2", shift="up")


# --------------------------------------------------------------------------- #
# merge_cells / unmerge_cells
# --------------------------------------------------------------------------- #
def test_merge_and_unmerge_roundtrip(workbook_path: Path) -> None:
    merged = excel_ops.merge_cells(workbook_path, "销售", "A5:C5")
    assert "A5:C5" in merged["merged"]

    reloaded = load_workbook(workbook_path)
    assert "A5:C5" in [str(r) for r in reloaded["销售"].merged_cells.ranges]

    unmerged = excel_ops.unmerge_cells(workbook_path, "销售", "A5:C5")
    assert unmerged["merged"] == []


def test_merge_cells_is_idempotent_for_the_same_range(workbook_path: Path) -> None:
    first = excel_ops.merge_cells(workbook_path, "销售", "A5:C5")
    second = excel_ops.merge_cells(workbook_path, "销售", "A5:C5")
    assert second["already_merged"] == "A5:C5"
    assert second["merged"] == first["merged"]


def test_merge_cells_rejects_overlapping_merges(workbook_path: Path) -> None:
    excel_ops.merge_cells(workbook_path, "销售", "A5:C5")
    with pytest.raises(MergeConflict):
        excel_ops.merge_cells(workbook_path, "销售", "B5:D5")


def test_unmerge_requires_an_exact_match(workbook_path: Path) -> None:
    excel_ops.merge_cells(workbook_path, "销售", "A5:C5")
    with pytest.raises(RangeError):
        excel_ops.unmerge_cells(workbook_path, "销售", "A5:B5")


def test_structure_lists_merged_ranges(workbook_path: Path) -> None:
    """Merged ranges carry header semantics — surface them in the structure."""
    excel_ops.merge_cells(workbook_path, "销售", "A5:C5")
    structure = excel_ops.workbook_structure(workbook_path)
    sales = next(sheet for sheet in structure["sheets"] if sheet["name"] == "销售")
    assert "A5:C5" in sales["merged_ranges"]
    assert sales["merged_truncated"] is False


# --------------------------------------------------------------------------- #
# find_replace
# --------------------------------------------------------------------------- #
def test_find_replace_updates_text_cells_across_sheets(tmp_path: Path) -> None:
    path = tmp_path / "替换.xlsx"
    book = Workbook()
    first = book.active
    first.title = "一"
    first["A1"] = "华东区域"
    first["A2"] = 1000  # numbers keep their type
    second = book.create_sheet("二")
    second["A1"] = "华东仓"
    book.save(path)

    result = excel_ops.find_replace(path, "华东", "华南")

    assert [(c["sheet"], c["cell"]) for c in result["changes"]] == [("一", "A1"), ("二", "A1")]
    assert result["total_replacements"] == 2
    reloaded = load_workbook(path)
    assert reloaded["一"]["A1"].value == "华南区域"
    assert reloaded["一"]["A2"].value == 1000
    assert reloaded["二"]["A1"].value == "华南仓"


def test_find_replace_matching_is_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "替换.xlsx"
    book = Workbook()
    book.active["A1"] = "Total Amount"
    book.save(path)

    result = excel_ops.find_replace(path, "total", "net")

    assert result["changes"][0]["after"] == "net Amount"


def test_find_replace_leaves_formulas_alone_unless_asked(tmp_path: Path) -> None:
    path = tmp_path / "公式替换.xlsx"
    book = Workbook()
    data = book.active
    data.title = "报销"
    data["A1"] = 1
    summary = book.create_sheet("汇总")
    summary["A1"] = "报销单明细"
    summary["A2"] = "=SUM(报销!A1:A2)"
    book.save(path)

    # Text cells are replaced; the formula string stays untouched by default.
    result = excel_ops.find_replace(path, "报销", "费用")
    assert [(c["sheet"], c["cell"]) for c in result["changes"]] == [("汇总", "A1")]
    reloaded = load_workbook(path)
    assert reloaded["汇总"]["A2"].value == "=SUM(报销!A1:A2)"

    # With include_formulas the formula text participates too.
    result = excel_ops.find_replace(path, "报销", "费用", include_formulas=True)
    assert result["changes"][0]["cell"] == "A2"
    assert result["changes"][0]["after"] == "=SUM(费用!A1:A2)"


def test_find_replace_rejects_empty_query(workbook_path: Path) -> None:
    with pytest.raises(RangeError):
        excel_ops.find_replace(workbook_path, "  ", "x")


def test_find_replace_without_hits_does_not_touch_the_file(workbook_path: Path) -> None:
    before = excel_ops.file_digest(workbook_path)
    result = excel_ops.find_replace(workbook_path, "不存在的内容", "x")
    assert result["changes"] == []
    assert excel_ops.file_digest(workbook_path) == before


# --------------------------------------------------------------------------- #
# manage_sheets
# --------------------------------------------------------------------------- #
def test_manage_sheets_create_rename_copy_delete_roundtrip(workbook_path: Path) -> None:
    created = excel_ops.manage_sheets(workbook_path, "create", new_name="草稿")
    assert created["sheets"] == ["销售", "汇总", "草稿"]

    renamed = excel_ops.manage_sheets(workbook_path, "rename", "草稿", new_name="备份")
    assert renamed["sheets"] == ["销售", "汇总", "备份"]

    copied = excel_ops.manage_sheets(workbook_path, "copy", "销售", new_name="销售副本")
    assert copied["sheets"] == ["销售", "汇总", "备份", "销售副本"]

    deleted = excel_ops.manage_sheets(workbook_path, "delete", "备份")
    assert deleted["sheets"] == ["销售", "汇总", "销售副本"]


def test_manage_sheets_rename_reports_stale_references(tmp_path: Path) -> None:
    """openpyxl does not rewrite cross-sheet refs; the tool must warn about them."""
    path = tmp_path / "引用.xlsx"
    book = Workbook()
    book.active.title = "数据"
    book["数据"]["A1"] = 1
    summary = book.create_sheet("汇总")
    summary["A1"] = "=数据!A1*2"
    book.save(path)

    result = excel_ops.manage_sheets(path, "rename", "数据", new_name="台账")

    assert result["stale_formula_refs"] == ["汇总!A1"]
    assert "#REF!" in result["warning"]


def test_manage_sheets_delete_reports_stale_references(tmp_path: Path) -> None:
    path = tmp_path / "引用.xlsx"
    book = Workbook()
    book.active.title = "数据"
    summary = book.create_sheet("汇总")
    summary["A1"] = "=SUM(数据!A1:A9)"
    book.save(path)

    result = excel_ops.manage_sheets(path, "delete", "数据")

    assert result["stale_formula_refs"] == ["汇总!A1"]


def test_manage_sheets_rejects_duplicates_and_last_sheet(workbook_path: Path) -> None:
    with pytest.raises(SheetExists):
        excel_ops.manage_sheets(workbook_path, "create", new_name="销售")
    # a single-sheet workbook cannot lose its only sheet
    path = workbook_path.parent / "单表.xlsx"
    book = Workbook()
    book.active.title = "唯一"
    book.save(path)
    with pytest.raises(LastSheetError):
        excel_ops.manage_sheets(path, "delete", "唯一")


def test_manage_sheets_rejects_unknown_action(workbook_path: Path) -> None:
    with pytest.raises(RangeError):
        excel_ops.manage_sheets(workbook_path, "duplicate", "销售", new_name="x")


# --------------------------------------------------------------------------- #
# adversarial inputs (2026-09-13 probe round): every case below once escaped
# the tool-error envelope as a raw openpyxl/re/ZeroDivisionError, silently
# corrupted data, or hung the tool. See docs/test-report/9-13/.
# --------------------------------------------------------------------------- #
def test_update_cells_rejects_a_range_in_the_cell_field(workbook_path: Path) -> None:
    """Regression: 'A1:B2' in the cell slot indexed a tuple and crashed."""
    with pytest.raises(RangeError, match="single cell"):
        excel_ops.update_cells(workbook_path, "销售", [{"cell": "A1:B2", "value": "x"}])


def test_update_cells_rejects_values_excel_cannot_hold(workbook_path: Path) -> None:
    """Regression: openpyxl's "Cannot convert … to Excel" ValueError escaped raw."""
    from mcp_office_server.errors import InvalidValue

    with pytest.raises(InvalidValue):
        excel_ops.update_cells(workbook_path, "销售", [{"cell": "C2", "value": [1, 2]}])
    with pytest.raises(InvalidValue):
        excel_ops.update_cells(workbook_path, "销售", [{"cell": "C2", "value": {"a": 1}}])


def test_update_cells_rejects_writes_inside_a_merged_region(workbook_path: Path) -> None:
    """Regression: writing a MergedCell raised AttributeError; user tables have merged headers."""
    excel_ops.merge_cells(workbook_path, "销售", "A5:C5")
    with pytest.raises(MergeConflict, match="A5:C5"):
        excel_ops.update_cells(workbook_path, "销售", [{"cell": "B5", "value": "表尾"}])
    # The top-left anchor of the merge stays writable.
    result = excel_ops.update_cells(workbook_path, "销售", [{"cell": "A5", "value": "表尾"}])
    assert result["changes"][0]["after"] == "表尾"


def test_update_cells_rejects_cells_beyond_sheet_limits(workbook_path: Path) -> None:
    with pytest.raises(RangeError, match="Excel's sheet limits"):
        excel_ops.update_cells(workbook_path, "销售", [{"cell": "A1048577", "value": "x"}])


def test_set_formula_validates_the_target_cell(workbook_path: Path) -> None:
    with pytest.raises(RangeError, match="start at 1"):
        excel_ops.set_formula(workbook_path, "汇总", "A0", "SUM(C2:C4)")
    with pytest.raises(RangeError, match="invalid range"):
        excel_ops.set_formula(workbook_path, "汇总", "not-a-cell", "SUM(C2:C4)")


def test_set_formula_rejects_writes_inside_a_merged_region(workbook_path: Path) -> None:
    excel_ops.merge_cells(workbook_path, "销售", "A5:C5")
    with pytest.raises(MergeConflict):
        excel_ops.set_formula(workbook_path, "销售", "C5", "SUM(C2:C4)")


def test_read_range_normalizes_reversed_ranges(workbook_path: Path) -> None:
    """Regression: 'C3:A1' errored; Excel semantics read the same rectangle."""
    result = excel_ops.read_range(workbook_path, "销售", start_cell="C3", end_cell="A1")
    assert result["start_cell"] == "A1"
    assert result["end_cell"] == "C3"
    assert result["values"][0][0] == "产品"


def test_read_range_rejects_ranges_beyond_sheet_limits(workbook_path: Path) -> None:
    with pytest.raises(RangeError, match="Excel's sheet limits"):
        excel_ops.read_range(workbook_path, "销售", start_cell="A1", end_cell="A1048577")
    with pytest.raises(RangeError, match="whole-column"):
        excel_ops.read_range(workbook_path, "销售", start_cell="A:B")


def test_insert_rows_rejects_counts_that_push_past_excels_limit(workbook_path: Path) -> None:
    """Regression: inserting past row 1048576 produced a file Excel refuses to open."""
    with pytest.raises(RangeError, match="1048576"):
        excel_ops.insert_rows(workbook_path, "销售", start_row=1, count=2_000_000)
    with pytest.raises(RangeError):
        excel_ops.insert_rows(workbook_path, "销售", start_row=1048577, count=1)


def test_format_range_validates_alignment_and_colors(workbook_path: Path) -> None:
    """Regression: openpyxl's own ValueErrors for bad colors/alignment escaped raw."""
    with pytest.raises(RangeError, match="RGB hex"):
        excel_ops.format_range(workbook_path, "销售", "A1", fill_color="#FF0000")
    with pytest.raises(RangeError, match="RGB hex"):
        excel_ops.format_range(workbook_path, "销售", "A1", font_color="GGGGGG")
    with pytest.raises(RangeError, match="horizontal_alignment"):
        excel_ops.format_range(workbook_path, "销售", "A1", horizontal_alignment="middle")


def test_format_range_rejects_oversized_ranges(workbook_path: Path) -> None:
    """Regression: a whole-sheet format request hung the tool for minutes."""
    with pytest.raises(RangeError, match="format it in batches"):
        excel_ops.format_range(workbook_path, "销售", "A1", "XFD1048576", bold=True)


def test_format_range_normalizes_reversed_ranges(workbook_path: Path) -> None:
    """Regression: 'C1'→'A1' reported success while formatting nothing."""
    excel_ops.format_range(workbook_path, "销售", "C1", "A1", bold=True)
    reloaded = load_workbook(workbook_path)["销售"]
    assert reloaded["A1"].font.bold is True
    assert reloaded["C1"].font.bold is True


def test_copy_range_snapshots_source_when_ranges_overlap(workbook_path: Path) -> None:
    """Regression: writing while reading corrupted overlapping copies.

    Excel copies a snapshot: A2:C4 → B3 must put the *original* A2:C4 block in
    B3:D5 even though the destination tramples the source.
    """
    excel_ops.copy_range(workbook_path, "销售", "A2:C4", "销售", "B3")
    reloaded = load_workbook(workbook_path)["销售"]
    got = [[reloaded.cell(row=row, column=col).value for col in range(2, 5)] for row in range(3, 6)]
    assert got == [["A型", "华东", 1000], ["B型", "华北", 2000], ["C型", "华南", 3000]]


def test_copy_range_accepts_reversed_source_range(workbook_path: Path) -> None:
    """Regression: 'C4:A2' computed negative widths and crashed."""
    result = excel_ops.copy_range(workbook_path, "销售", "C4:A2", "汇总", "A3")
    assert result["source"] == "销售!A2:C4"


def test_copy_range_rejects_destination_beyond_limits(workbook_path: Path) -> None:
    with pytest.raises(RangeError, match="Excel's sheet limits"):
        excel_ops.copy_range(workbook_path, "销售", "A1:C2", "销售", "XFC1")


def test_delete_range_clamps_to_the_sheets_used_rows(tmp_path: Path) -> None:
    """Regression: a range reaching below the data cleared rows *above* it.

    Sheet data ends at row 4; deleting A3:C6 must not touch row 2.
    """
    path = tmp_path / "清单.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "清单"
    for row in (["项目", "金额"], ["甲", 1], ["乙", 2], ["丙", 3]):
        sheet.append(row)
    book.save(path)

    excel_ops.delete_range(path, "清单", "A3:C6", shift="up")

    reloaded = load_workbook(path)["清单"]
    assert reloaded["A2"].value == "甲"  # untouched by the over-deep range
    assert reloaded["A3"].value is None  # the band itself was cleared


def test_delete_range_beyond_data_is_a_clean_noop(tmp_path: Path) -> None:
    """Regression: A10:C20 walked negative row indices and crashed."""
    path = tmp_path / "清单.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "清单"
    for row in (["项目", "金额"], ["甲", 1]):
        sheet.append(row)
    book.save(path)

    result = excel_ops.delete_range(path, "清单", "A10:C20", shift="up")
    result = excel_ops.delete_range(path, "清单", "D1:Z1", shift="left")

    reloaded = load_workbook(path)["清单"]
    assert reloaded["A2"].value == "甲"


def test_merge_cells_rejects_single_cell_ranges(workbook_path: Path) -> None:
    with pytest.raises(RangeError, match="single cell"):
        excel_ops.merge_cells(workbook_path, "销售", "B7")


def test_merge_cells_rejects_ranges_beyond_sheet_limits(workbook_path: Path) -> None:
    """Regression: A1:ZZZ1 wrote a merge past column XFD — Excel repairs the file."""
    with pytest.raises(RangeError, match="Excel's sheet limits"):
        excel_ops.merge_cells(workbook_path, "销售", "A1:ZZZ1")


def test_manage_sheets_rejects_names_excel_refuses(workbook_path: Path) -> None:
    """Regression: openpyxl's title ValueErrors escaped raw; >31 chars only warned."""
    from mcp_office_server.errors import InvalidSheetName

    with pytest.raises(InvalidSheetName, match="forbidden characters"):
        excel_ops.manage_sheets(workbook_path, "create", new_name="a[b]")
    with pytest.raises(InvalidSheetName, match="31"):
        excel_ops.manage_sheets(workbook_path, "create", new_name="超" * 40)
    with pytest.raises(InvalidSheetName):
        excel_ops.manage_sheets(workbook_path, "rename", "汇总", new_name="汇总:副本")


def test_find_replace_inserts_replacement_literally(tmp_path: Path) -> None:
    """Regression: the replacement was used as a regex *template*.

    Backslashes exploded ("bad escape") or silently expanded group references
    like ``\\g<0>``; Windows paths are the everyday casualty.
    """
    path = tmp_path / "替换.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "一"
    sheet["A1"] = "备注-甲"
    sheet["A2"] = "路径=乙目录"
    sheet["A3"] = "前缀-丙"
    book.save(path)

    excel_ops.find_replace(path, "甲", "\\g<0>新")          # group-ref-looking replacement
    excel_ops.find_replace(path, "乙目录", "C:\\new\\data")  # Windows path
    excel_ops.find_replace(path, "丙", "丁\\")               # trailing backslash

    reloaded = load_workbook(path)["一"]
    assert reloaded["A1"].value == "备注-\\g<0>新"
    assert reloaded["A2"].value == "路径=C:\\new\\data"
    assert reloaded["A3"].value == "前缀-丁\\"


# --------------------------------------------------------------------------- #
# 9-14 regressions: silent truncation, control characters, bare "=" formula,
# delete_range vacated-range reporting, whitespace-only values
# --------------------------------------------------------------------------- #
def test_update_cells_rejects_strings_longer_than_excels_cell_limit(
    tmp_path: Path,
) -> None:
    """Regression: openpyxl silently truncates >32767-char strings at save.

    The tool used to report the full text while the file kept a cut-off copy —
    the report and the file diverged without any error. Excel's own cell limit
    is 32,767 characters, so the write is rejected instead.
    """
    path = tmp_path / "长文本.xlsx"
    book = Workbook()
    book.active.title = "S"
    book.active["A1"] = "seed"
    book.save(path)

    with pytest.raises(InvalidValue, match="32767"):
        excel_ops.update_cells(path, "S", [{"cell": "A1", "value": "x" * 40000}])

    # At the limit itself the write must succeed and round-trip intact.
    exactly = "y" * 32767
    excel_ops.update_cells(path, "S", [{"cell": "A1", "value": exactly}])
    reloaded = load_workbook(path)["S"]
    assert reloaded["A1"].value == exactly


def test_update_cells_rejects_control_characters(tmp_path: Path) -> None:
    """Regression: NUL and friends raised a raw IllegalCharacterError.

    They are rejected as ``invalid_value`` naming the character instead;
    \\n and \\t remain legal, matching Excel.
    """
    path = tmp_path / "控制字符.xlsx"
    book = Workbook()
    book.active.title = "S"
    book.active["A1"] = "seed"
    book.save(path)

    with pytest.raises(InvalidValue, match="control character"):
        excel_ops.update_cells(path, "S", [{"cell": "A1", "value": "bad\x00char"}])
    with pytest.raises(InvalidValue):
        excel_ops.update_cells(path, "S", [{"cell": "A1", "value": "bad\x07char"}])

    excel_ops.update_cells(path, "S", [{"cell": "A1", "value": "第一行\n第二行\t缩进"}])
    reloaded = load_workbook(path)["S"]
    assert reloaded["A1"].value == "第一行\n第二行\t缩进"


def test_update_cells_stores_whitespace_only_text_verbatim(tmp_path: Path) -> None:
    """Whitespace-only text is data, as in Excel; only "" clears a cell.

    Regression: "   " used to silently clear the cell even though the model
    sent it as a value, and Excel itself would have kept the spaces.
    """
    path = tmp_path / "空白.xlsx"
    book = Workbook()
    book.active.title = "S"
    book.active["A1"] = "keep"
    book.save(path)

    excel_ops.update_cells(path, "S", [{"cell": "A1", "value": "   "}])
    reloaded = load_workbook(path)["S"]
    assert reloaded["A1"].value == "   "

    excel_ops.update_cells(path, "S", [{"cell": "A1", "value": ""}])
    reloaded = load_workbook(path)["S"]
    assert reloaded["A1"].value is None


def test_set_formula_rejects_a_bare_equals_sign(workbook_path: Path) -> None:
    """Regression: "=" was stored as literal *text*, not a formula.

    openpyxl keeps a bare "=" as an inline string, so the tool reported a
    formula while the cell held text. Excel refuses a lone "=" too.
    """
    with pytest.raises(RangeError, match="no body"):
        excel_ops.set_formula(workbook_path, "销售", "D2", "=")
    with pytest.raises(RangeError, match="no body"):
        excel_ops.set_formula(workbook_path, "销售", "D2", "=  ")


def test_set_formula_rejects_formulas_over_excels_length_limit(
    workbook_path: Path,
) -> None:
    """Excel caps formulas at 8,192 characters; reject beyond instead of
    writing a file Excel refuses to recalculate."""
    long_formula = "=" + "A1+" * 5000 + "A1"  # 20001 chars
    with pytest.raises(RangeError, match="8192"):
        excel_ops.set_formula(workbook_path, "销售", "D2", long_formula)


def test_delete_range_reports_the_vacated_rectangle(workbook_path: Path) -> None:
    """Regression: the vacated-tail field was an inverted rectangle (A4:B3).

    It now reports, as canonical text, exactly the cells the shift left blank:
    deleting A2:B3 from a 4-row table moves row 4 up and vacates rows 3-4 in
    the deleted columns.
    """
    result = excel_ops.delete_range(workbook_path, "销售", "A2:B3", "up")
    assert result["moved_cells"] == 2
    assert result["vacated_range"] == "A3:B4"


def test_delete_range_left_reports_vacated_columns(workbook_path: Path) -> None:
    """Deleting column A from rows 2-3 shifts B,C left; old column C turns blank."""
    result = excel_ops.delete_range(workbook_path, "销售", "A2:A3", "left")
    assert result["moved_cells"] == 4
    assert result["vacated_range"] == "C2:C3"


def test_delete_range_reports_no_vacated_cells_when_none_turn_blank(
    workbook_path: Path,
) -> None:
    """A range entirely below the data deletes nothing and vacates nothing."""
    result = excel_ops.delete_range(workbook_path, "销售", "A10:C12", "up")
    assert result["moved_cells"] == 0
    assert result["vacated_range"] is None


def test_delete_range_at_the_bottom_of_data_reports_the_band_as_vacated(
    workbook_path: Path,
) -> None:
    """Deleting the last data rows blanks them: the band itself is vacated.

    Regression: with nothing below to shift in, the vacated report used to be
    null even though the deleted cells did turn blank.
    """
    result = excel_ops.delete_range(workbook_path, "销售", "A3:C4", "up")
    assert result["moved_cells"] == 0
    assert result["vacated_range"] == "A3:C4"
