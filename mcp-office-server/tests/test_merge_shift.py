"""Regression tests: merged cell ranges must follow row/column shifts.

openpyxl moves cell values on insert_rows/delete_rows (and column variants)
but leaves merged ranges untouched (openpyxl docs, "Inserting and deleting
rows and columns"; issue #1139). Excel keeps merged headers aligned with
their data. Every case here pins one Excel behaviour that used to break:
the merge would sit over the wrong rows after the shift.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook, load_workbook

from mcp_office_server import excel_ops


def _merges(path: Path, sheet: str = "S") -> list[str]:
    wb = load_workbook(path)
    found = sorted(str(r) for r in wb[sheet].merged_cells.ranges)
    wb.close()
    return found


def _workbook_with_merge(path: Path, merge: str, anchor_value: str = "标题") -> None:
    """A sheet with one merge, data above and below it, saved to ``path``."""
    from openpyxl.utils.cell import range_boundaries

    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = "top"
    ws.cell(row=8, column=1, value="bottom")
    min_col, min_row, max_col, max_row = range_boundaries(merge)
    ws.cell(row=min_row, column=min_col, value=anchor_value)
    ws.merge_cells(merge)
    wb.save(path)


def test_insert_rows_above_merges_move_down(tmp_path: Path) -> None:
    """Inserting rows above a merged header shifts the merge down (Excel)."""
    path = tmp_path / "a.xlsx"
    _workbook_with_merge(path, "A2:B2")
    result = excel_ops.insert_rows(path, "S", 1, 2)
    assert _merges(path) == ["A4:B4"]
    assert result["merged_shifted"] == 1
    wb = load_workbook(path)
    assert wb["S"]["A4"].value == "标题"
    wb.close()


def test_insert_rows_inside_a_merge_extends_it(tmp_path: Path) -> None:
    """Excel extends a merge when rows are inserted inside it."""
    path = tmp_path / "b.xlsx"
    _workbook_with_merge(path, "A1:B2")
    excel_ops.insert_rows(path, "S", 2, 1)
    assert _merges(path) == ["A1:B3"]
    wb = load_workbook(path)
    assert wb["S"]["A1"].value == "标题"
    wb.close()


def test_delete_rows_above_merges_move_up(tmp_path: Path) -> None:
    path = tmp_path / "c.xlsx"
    _workbook_with_merge(path, "A4:B4")
    excel_ops.delete_rows(path, "S", 1, 2)
    assert _merges(path) == ["A2:B2"]
    wb = load_workbook(path)
    assert wb["S"]["A2"].value == "标题"
    wb.close()


def test_delete_rows_shrink_a_merge_partially_inside(tmp_path: Path) -> None:
    """Deleting one row out of a 3-row merge shrinks it to 2 rows (Excel)."""
    path = tmp_path / "d.xlsx"
    _workbook_with_merge(path, "A2:B4")
    excel_ops.delete_rows(path, "S", 3, 1)
    assert _merges(path) == ["A2:B3"]
    wb = load_workbook(path)
    assert wb["S"]["A2"].value == "标题"
    wb.close()


def test_delete_rows_spanning_a_merge_shrink_it_around_the_band(tmp_path: Path) -> None:
    """Merge A2:B4 with rows 2-3 deleted: what remains collapses to A2:B2."""
    path = tmp_path / "d2.xlsx"
    _workbook_with_merge(path, "A2:B4")
    excel_ops.delete_rows(path, "S", 2, 2)
    assert _merges(path) == ["A2:B2"]


def test_delete_rows_remove_a_merge_fully_inside(tmp_path: Path) -> None:
    """Deleting a merged row removes the merge instead of leaving it stale.

    Regression: the stale merge kept covering row 2 after its content moved
    away, so the header merge sat over unrelated data (and hid it in Excel).
    """
    path = tmp_path / "g.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A2"] = "标题"
    ws["B2"] = "备注"
    ws.merge_cells("A2:B2")
    ws["A5"] = 9
    wb.save(path)
    wb.close()

    result = excel_ops.delete_rows(path, "S", 2, 1)
    assert _merges(path) == []
    assert result["merged_removed"] == 1
    wb = load_workbook(path)
    assert wb["S"]["A4"].value == 9
    wb.close()


def test_insert_columns_left_of_merges_move_right(tmp_path: Path) -> None:
    path = tmp_path / "e.xlsx"
    _workbook_with_merge(path, "A1:C1")
    excel_ops.insert_columns(path, "S", 1, 1)
    assert _merges(path) == ["B1:D1"]


def test_delete_columns_shrink_a_merge_partially_inside(tmp_path: Path) -> None:
    path = tmp_path / "f.xlsx"
    _workbook_with_merge(path, "A1:C1")
    excel_ops.delete_columns(path, "S", 2, 1)
    assert _merges(path) == ["A1:B1"]


def test_merges_fully_above_the_band_are_untouched(tmp_path: Path) -> None:
    path = tmp_path / "h.xlsx"
    _workbook_with_merge(path, "A1:B1")
    excel_ops.insert_rows(path, "S", 5, 2)
    assert _merges(path) == ["A1:B1"]
    excel_ops.delete_rows(path, "S", 5, 1)
    assert _merges(path) == ["A1:B1"]


def test_multiple_merges_shift_independently(tmp_path: Path) -> None:
    """A delete that intersects one merge but not the other affects only one."""
    path = tmp_path / "i.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A2"] = "标题一"
    ws.merge_cells("A2:B2")
    ws["A5"] = "标题二"
    ws.merge_cells("A5:B5")
    wb.save(path)
    wb.close()

    excel_ops.delete_rows(path, "S", 3, 1)
    assert _merges(path) == ["A2:B2", "A4:B4"]
