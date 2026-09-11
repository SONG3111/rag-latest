from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from mcp_office_server import excel_ops
from mcp_office_server.errors import RangeError, SheetNotFound, WriteConflict


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
