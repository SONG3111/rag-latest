"""Envelope contract for the structural Excel tools at the MCP tool layer.

The implementation-level semantics are pinned in test_excel_ops.py; these tests
exercise the thin server wrappers the way the model calls them — workspace
relative paths, JSON envelopes, structured errors instead of raised exceptions.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_office_server import server


def _call(tool_name: str, **kwargs) -> dict:
    raw = getattr(server, tool_name)(**kwargs)
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# columns
# --------------------------------------------------------------------------- #
def test_insert_columns_envelope(workbook_path: Path) -> None:
    payload = _call(
        "insert_columns", path="销售表.xlsx", sheet_name="销售", start_col=2, count=1
    )
    assert payload["ok"] is True
    assert payload["data"]["operation"] == "insert_columns"
    assert payload["data"]["digest"]


def test_insert_columns_envelope_reports_bad_args(workbook_path: Path) -> None:
    payload = _call(
        "insert_columns", path="销售表.xlsx", sheet_name="销售", start_col=0
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_range"


def test_delete_columns_envelope(workbook_path: Path) -> None:
    payload = _call(
        "delete_columns", path="销售表.xlsx", sheet_name="销售", start_col=2
    )
    assert payload["ok"] is True
    assert payload["data"]["columns_after"] == payload["data"]["columns_before"] - 1


def test_delete_columns_envelope_reports_unknown_sheet(workbook_path: Path) -> None:
    payload = _call(
        "delete_columns", path="销售表.xlsx", sheet_name="不存在", start_col=1
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "sheet_not_found"


# --------------------------------------------------------------------------- #
# range copy / delete
# --------------------------------------------------------------------------- #
def test_copy_range_envelope(workbook_path: Path) -> None:
    payload = _call(
        "copy_range",
        path="销售表.xlsx", src_sheet="销售", src_range="A1:C2",
        dst_sheet="汇总", dst_cell="A1",
    )
    assert payload["ok"] is True
    assert payload["data"]["copied_cells"] == 6
    assert payload["data"]["destination"].startswith("汇总!A1")


def test_copy_range_envelope_reports_merge_conflict(workbook_path: Path) -> None:
    _call("merge_cells", path="销售表.xlsx", sheet_name="销售", range_text="A5:C5")
    payload = _call(
        "copy_range",
        path="销售表.xlsx", src_sheet="销售", src_range="A5:C5",
        dst_sheet="汇总", dst_cell="A1",
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "merge_conflict"


def test_delete_range_envelope(workbook_path: Path) -> None:
    payload = _call(
        "delete_range", path="销售表.xlsx", sheet_name="销售",
        range_text="A3:C3", shift="up",
    )
    assert payload["ok"] is True
    assert payload["data"]["shift"] == "up"


def test_delete_range_envelope_reports_bad_shift(workbook_path: Path) -> None:
    payload = _call(
        "delete_range", path="销售表.xlsx", sheet_name="销售",
        range_text="A3:C3", shift="nowhere",
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_range"


# --------------------------------------------------------------------------- #
# merge / unmerge
# --------------------------------------------------------------------------- #
def test_merge_unmerge_envelopes(workbook_path: Path) -> None:
    payload = _call("merge_cells", path="销售表.xlsx", sheet_name="销售", range_text="A5:C5")
    assert payload["ok"] is True
    assert "A5:C5" in payload["data"]["merged"]

    again = _call("merge_cells", path="销售表.xlsx", sheet_name="销售", range_text="A5:C5")
    assert again["ok"] is True
    assert again["data"]["already_merged"] == "A5:C5"

    unmerged = _call("unmerge_cells", path="销售表.xlsx", sheet_name="销售", range_text="A5:C5")
    assert unmerged["ok"] is True
    assert unmerged["data"]["merged"] == []


def test_unmerge_envelope_requires_exact_range(workbook_path: Path) -> None:
    _call("merge_cells", path="销售表.xlsx", sheet_name="销售", range_text="A5:C5")
    payload = _call("unmerge_cells", path="销售表.xlsx", sheet_name="销售", range_text="A5:B5")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_range"


# --------------------------------------------------------------------------- #
# find_replace
# --------------------------------------------------------------------------- #
def test_find_replace_envelope(workbook_path: Path) -> None:
    payload = _call(
        "find_replace", path="销售表.xlsx", query="华北", replacement="华西"
    )
    assert payload["ok"] is True
    assert payload["data"]["total_replacements"] == 1
    assert payload["data"]["changes"][0]["cell"] == "B3"


def test_find_replace_envelope_reports_empty_query(workbook_path: Path) -> None:
    payload = _call("find_replace", path="销售表.xlsx", query="  ", replacement="x")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_range"


# --------------------------------------------------------------------------- #
# manage_sheets
# --------------------------------------------------------------------------- #
def test_manage_sheets_envelope_create_and_delete(workbook_path: Path) -> None:
    created = _call(
        "manage_sheets", path="销售表.xlsx", action="create", new_name="草稿"
    )
    assert created["ok"] is True
    assert created["data"]["sheets"] == ["销售", "汇总", "草稿"]

    deleted = _call(
        "manage_sheets", path="销售表.xlsx", action="delete", sheet_name="草稿"
    )
    assert deleted["ok"] is True
    assert deleted["data"]["sheets"] == ["销售", "汇总"]


def test_manage_sheets_envelope_reports_duplicates(workbook_path: Path) -> None:
    payload = _call(
        "manage_sheets", path="销售表.xlsx", action="create", new_name="销售"
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "sheet_exists"


def test_manage_sheets_envelope_reports_unknown_action(workbook_path: Path) -> None:
    payload = _call(
        "manage_sheets", path="销售表.xlsx", action="truncate", sheet_name="销售"
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_range"


def test_get_doc_structure_lists_merged_ranges(workbook_path: Path) -> None:
    _call("merge_cells", path="销售表.xlsx", sheet_name="销售", range_text="A5:C5")
    payload = json.loads(server.get_doc_structure("销售表.xlsx"))
    sales = next(s for s in payload["data"]["sheets"] if s["name"] == "销售")
    assert "A5:C5" in sales["merged_ranges"]


# --------------------------------------------------------------------------- #
# envelope hardening (2026-09-13 probe round)
# --------------------------------------------------------------------------- #
def test_update_cells_envelope_reports_merged_region_write(workbook_path: Path) -> None:
    """Regression: writing a non-top-left merged cell raised a raw AttributeError."""
    _call("merge_cells", path="销售表.xlsx", sheet_name="销售", range_text="A5:C5")
    payload = _call(
        "update_cells", path="销售表.xlsx", sheet_name="销售",
        updates=[{"cell": "B5", "value": "表尾"}],
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "merge_conflict"
    assert "A5:C5" in payload["error"]["message"]


def test_find_replace_envelope_accepts_windows_path_replacement(workbook_path: Path) -> None:
    """Regression: a trailing backslash in the replacement crashed re.subn."""
    payload = _call(
        "find_replace", path="销售表.xlsx", query="华东", replacement="C:\\new\\dir\\"
    )
    assert payload["ok"] is True
    assert payload["data"]["changes"][0]["after"] == "C:\\new\\dir\\"


def test_unexpected_failures_stay_inside_the_envelope(
    workbook_path: Path, monkeypatch
) -> None:
    """The contract is "tools never raise": any surprise must still return JSON."""
    from mcp_office_server import excel_ops

    def explode(*args, **kwargs):
        raise RuntimeError("simulated openpyxl meltdown")

    monkeypatch.setattr(excel_ops, "save_workbook", explode)
    payload = _call(
        "update_cells", path="销售表.xlsx", sheet_name="销售",
        updates=[{"cell": "C2", "value": 1}],
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_error"
    assert "unexpected failure" in payload["error"]["message"]
