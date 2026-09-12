"""Excel implementation layer built on openpyxl.

This module intentionally contains no MCP tool definitions, so it can be unit
tested and reused independently of the protocol layer. Tool wrappers in
``server.py`` are thin adapters over these functions.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.worksheet import Worksheet as OpenpyxlWorksheet

from . import formula_shift
from .errors import (
    CorruptDocument,
    FileLocked,
    RangeError,
    SheetNotFound,
    ToolError,
    WriteConflict,
)

MAX_PREVIEW_ROWS = 200
MAX_PREVIEW_COLS = 60


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def file_digest(path: Path) -> str:
    """SHA-256 of the file contents, used for optimistic write-conflict checks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_workbook(path: Path, *, read_only: bool = False, data_only: bool = False):
    """Open a workbook, converting openpyxl failures into our error type."""
    try:
        return load_workbook(path, read_only=read_only, data_only=data_only)
    except Exception as exc:  # openpyxl raises a wide variety of errors
        raise CorruptDocument(f"cannot open workbook '{path.name}': {exc}") from exc


def save_workbook(workbook, path: Path) -> None:
    """Persist the workbook, translating OS-level write failures into ToolError.

    On Windows an openpyxl save raises ``PermissionError`` when the user has the
    file open in Excel/WPS. Left uncaught, FastMCP wraps it into a plain-text
    result that the caller cannot distinguish from success.
    """
    try:
        workbook.save(path)
    except PermissionError as exc:
        raise FileLocked(
            "文件正被其他程序占用（如 Excel/WPS），请关闭后重试",
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise ToolError(f"写入文件失败: {exc}", detail=str(exc)) from exc


def _require_sheet(workbook, sheet_name: str) -> OpenpyxlWorksheet:
    if sheet_name not in workbook.sheetnames:
        raise SheetNotFound(
            f"sheet '{sheet_name}' not found; available: {', '.join(workbook.sheetnames)}"
        )
    return workbook[sheet_name]


def _parse_range(range_text: str) -> tuple[int, int, int, int]:
    """Return ``(min_col, min_row, max_col, max_row)`` for an A1-style range."""
    try:
        return range_boundaries(range_text)
    except Exception as exc:
        raise RangeError(f"invalid range '{range_text}'") from exc


def _jsonify(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _coerce_input(value: Any) -> Any:
    """Normalize values coming from the model before writing them to a cell.

    Models occasionally wrap a formula in a small object (``{"formula": "=A1*2"}``)
    instead of passing the string; the intent is unambiguous, so unwrap it rather
    than failing the whole write with openpyxl's ``Cannot convert ... to Excel``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return None if stripped == "" else value
    if isinstance(value, dict) and set(value) == {"formula"} and isinstance(value["formula"], str):
        return value["formula"]
    return value


def _check_conflict(path: Path, expected_digest: str | None) -> None:
    if not expected_digest:
        return
    actual = file_digest(path)
    if actual != expected_digest:
        raise WriteConflict(
            "the file changed on disk since it was read; re-read it before applying changes"
        )


# --------------------------------------------------------------------------- #
# read operations
# --------------------------------------------------------------------------- #
def workbook_structure(path: Path) -> dict:
    """Describe every sheet: dimensions, header row, and column widths."""
    workbook = open_workbook(path, read_only=True)
    try:
        sheets = []
        for name in workbook.sheetnames:
            sheet = workbook[name]
            header: list[Any] = []
            for row in sheet.iter_rows(min_row=1, max_row=1, max_col=min(sheet.max_column or 0, MAX_PREVIEW_COLS)):
                header = [_jsonify(cell.value) for cell in row]
                break
            sheets.append(
                {
                    "name": name,
                    "max_row": sheet.max_row or 0,
                    "max_column": sheet.max_column or 0,
                    "header": header,
                }
            )
        return {"kind": "excel", "sheets": sheets}
    finally:
        workbook.close()


def read_range(
    path: Path,
    sheet_name: str,
    *,
    start_cell: str = "A1",
    end_cell: str | None = None,
    max_rows: int = MAX_PREVIEW_ROWS,
    max_cols: int = MAX_PREVIEW_COLS,
) -> dict:
    """Read a rectangular region, returning raw values plus formula visibility.

    Values are read with ``data_only=False`` so formulas are visible to the agent
    as formula text; a cached value is reported alongside when available.
    """
    workbook = open_workbook(path, data_only=False)
    cached = open_workbook(path, data_only=True)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        cached_sheet = cached[sheet_name] if sheet_name in cached.sheetnames else None

        # Semantics ported from haris-musa/excel-mcp-server
        # (``read_excel_range_with_metadata``): a range may be passed as
        # ``start_cell="A1:C4"``, and an omitted ``end_cell`` means "expand to the
        # sheet's data", not "read one cell".
        if ":" in start_cell and not end_cell:
            start_cell, end_cell = (part.strip() for part in start_cell.split(":", 1))

        if end_cell:
            min_col, min_row, max_col, max_row = _parse_range(f"{start_cell}:{end_cell}")
        else:
            min_col, min_row, _, _ = _parse_range(start_cell)
            if start_cell.upper() == "A1":
                # A sheet whose data does not start at A1 should still read whole.
                min_row, min_col = sheet.min_row, sheet.min_column
            empty = (
                (sheet.max_row or 0) <= 1
                and (sheet.max_column or 0) <= 1
                and sheet.cell(row=1, column=1).value is None
            )
            if empty:
                max_row, max_col = min_row, min_col
            else:
                max_row = max(sheet.max_row or min_row, min_row)
                max_col = max(sheet.max_column or min_col, min_col)

        total_rows = (max_row - min_row + 1)
        total_cols = (max_col - min_col + 1)
        if total_rows <= 0 or total_cols <= 0:
            raise RangeError(f"empty range {start_cell}:{end_cell or start_cell}")

        truncated = total_rows > max_rows or total_cols > max_cols
        last_row = min(max_row, min_row + max_rows - 1)
        last_col = min(max_col, min_col + max_cols - 1)

        rows: list[list[Any]] = []
        for row_idx in range(min_row, last_row + 1):
            values: list[Any] = []
            for col_idx in range(min_col, last_col + 1):
                cell = sheet.cell(row=row_idx, column=col_idx)
                raw = cell.value
                if isinstance(raw, str) and raw.startswith("="):
                    cached_value = (
                        cached_sheet.cell(row=row_idx, column=col_idx).value
                        if cached_sheet is not None
                        else None
                    )
                    values.append(
                        {"formula": raw, "cached_value": _jsonify(cached_value)}
                    )
                else:
                    values.append(_jsonify(raw))
            rows.append(values)

        return {
            "kind": "excel",
            "sheet": sheet_name,
            # The effective range, not the request: when the caller passes A1 on a
            # sheet whose data starts at B3, the answer should say so explicitly.
            "requested_start_cell": start_cell,
            "start_cell": f"{get_column_letter(min_col)}{min_row}",
            "end_cell": f"{get_column_letter(last_col)}{last_row}",
            "values": rows,
            "truncated": truncated,
            "total_rows": total_rows,
            "total_columns": total_cols,
            "sheet_max_row": sheet.max_row or 0,
            "sheet_max_column": sheet.max_column or 0,
            "digest": file_digest(path),
        }
    finally:
        workbook.close()
        cached.close()


def find_text(path: Path, query: str, *, max_hits: int = 50) -> list[dict]:
    """Case-insensitive substring search across all sheets.

    Non-text cells are matched against their string form — the idiom openpyxl
    based servers use upstream — because a user asking "哪里出现了 299" expects
    the cell holding 299.0 to be found, not a silent miss. Dates match their
    ISO form and formulas match their formula text.
    """
    needle = query.strip().casefold()
    if not needle:
        return []
    hits: list[dict] = []
    workbook = open_workbook(path, read_only=True)
    try:
        for name in workbook.sheetnames:
            sheet = workbook[name]
            for row in sheet.iter_rows():
                for cell in row:
                    value = cell.value
                    if value is None:
                        continue
                    if needle in str(_jsonify(value)).casefold():
                        hits.append(
                            {
                                "sheet": name,
                                "cell": cell.coordinate,
                                "value": _jsonify(value),
                            }
                        )
                        if len(hits) >= max_hits:
                            return hits
        return hits
    finally:
        workbook.close()


# --------------------------------------------------------------------------- #
# write operations
# --------------------------------------------------------------------------- #
def update_cells(
    path: Path,
    sheet_name: str,
    updates: Iterable[dict],
    *,
    expected_digest: str | None = None,
) -> dict:
    """Set cell values, returning a per-cell before/after diff.

    Each update is ``{"cell": "B3", "value": ...}``. ``None`` clears a cell.
    """
    _check_conflict(path, expected_digest)
    workbook = open_workbook(path)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        changes: list[dict] = []
        for update in updates:
            coordinate = str(update.get("cell", "")).strip()
            if not coordinate:
                raise RangeError("each update requires a 'cell' coordinate")
            try:
                _parse_range(coordinate)
            except RangeError as exc:
                raise RangeError(f"invalid cell coordinate '{coordinate}'") from exc

            target = sheet[coordinate]
            before = _jsonify(target.value)
            after = _coerce_input(update.get("value"))
            target.value = after
            changes.append(
                {
                    "cell": coordinate,
                    "before": before,
                    "after": _jsonify(after),
                }
            )
        save_workbook(workbook, path)
        return {
            "kind": "excel",
            "sheet": sheet_name,
            "path": path.name,
            "changes": changes,
            "digest": file_digest(path),
        }
    finally:
        workbook.close()


def set_formula(
    path: Path,
    sheet_name: str,
    cell: str,
    formula: str,
    *,
    expected_digest: str | None = None,
) -> dict:
    """Write an Excel formula, normalizing the leading ``=``."""
    expression = formula.strip()
    if not expression:
        raise RangeError("formula must not be empty")
    if not expression.startswith("="):
        expression = f"={expression}"

    _check_conflict(path, expected_digest)
    workbook = open_workbook(path)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        target = sheet[cell]
        before = _jsonify(target.value)
        target.value = expression
        save_workbook(workbook, path)
        return {
            "kind": "excel",
            "sheet": sheet_name,
            "changes": [{"cell": cell, "before": before, "after": expression}],
            "digest": file_digest(path),
        }
    finally:
        workbook.close()


def insert_rows(
    path: Path,
    sheet_name: str,
    start_row: int,
    count: int = 1,
    *,
    expected_digest: str | None = None,
) -> dict:
    if start_row < 1:
        raise RangeError("start_row is 1-based and must be >= 1")
    if count < 1:
        raise RangeError("count must be >= 1")

    _check_conflict(path, expected_digest)
    workbook = open_workbook(path)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        before_rows = sheet.max_row or 0
        sheet.insert_rows(start_row, amount=count)
        # openpyxl moves cells but not formula strings; rewrite references the
        # way Excel does (see formula_shift).
        formula_shift.shift_workbook_formulas(
            workbook, affected_sheet=sheet.title, op="insert",
            start_row=start_row, count=count,
        )
        save_workbook(workbook, path)
        return {
            "kind": "excel",
            "sheet": sheet_name,
            "operation": "insert_rows",
            "start_row": start_row,
            "count": count,
            "rows_before": before_rows,
            "rows_after": sheet.max_row or 0,
            "digest": file_digest(path),
        }
    finally:
        workbook.close()


def delete_rows(
    path: Path,
    sheet_name: str,
    start_row: int,
    count: int = 1,
    *,
    expected_digest: str | None = None,
) -> dict:
    if start_row < 1:
        raise RangeError("start_row is 1-based and must be >= 1")
    if count < 1:
        raise RangeError("count must be >= 1")

    _check_conflict(path, expected_digest)
    workbook = open_workbook(path)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        before_rows = sheet.max_row or 0
        sheet.delete_rows(start_row, amount=count)
        formula_shift.shift_workbook_formulas(
            workbook, affected_sheet=sheet.title, op="delete",
            start_row=start_row, count=count,
        )
        save_workbook(workbook, path)
        return {
            "kind": "excel",
            "sheet": sheet_name,
            "operation": "delete_rows",
            "start_row": start_row,
            "count": count,
            "rows_before": before_rows,
            "rows_after": sheet.max_row or 0,
            "digest": file_digest(path),
        }
    finally:
        workbook.close()


def format_range(
    path: Path,
    sheet_name: str,
    start_cell: str,
    end_cell: str | None = None,
    *,
    bold: bool | None = None,
    italic: bool | None = None,
    font_color: str | None = None,
    fill_color: str | None = None,
    number_format: str | None = None,
    horizontal_alignment: str | None = None,
    expected_digest: str | None = None,
) -> dict:
    """Apply basic cell formatting to a rectangle."""
    _check_conflict(path, expected_digest)
    workbook = open_workbook(path)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        min_col, min_row, max_col, max_row = _parse_range(
            f"{start_cell}:{end_cell}" if end_cell else start_cell
        )

        applied: list[str] = []
        for row in sheet.iter_rows(
            min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col
        ):
            for cell in row:
                if bold is not None or italic is not None or font_color is not None:
                    font = cell.font
                    cell.font = Font(
                        name=font.name,
                        size=font.size,
                        bold=bold if bold is not None else font.bold,
                        italic=italic if italic is not None else font.italic,
                        color=font_color or font.color,
                    )
                if fill_color:
                    cell.fill = PatternFill(
                        start_color=fill_color, end_color=fill_color, fill_type="solid"
                    )
                if number_format:
                    cell.number_format = number_format
                if horizontal_alignment:
                    cell.alignment = Alignment(horizontal=horizontal_alignment)

        if bold is not None:
            applied.append(f"bold={bold}")
        if italic is not None:
            applied.append(f"italic={italic}")
        if font_color:
            applied.append(f"font_color={font_color}")
        if fill_color:
            applied.append(f"fill_color={fill_color}")
        if number_format:
            applied.append(f"number_format={number_format}")
        if horizontal_alignment:
            applied.append(f"alignment={horizontal_alignment}")

        save_workbook(workbook, path)
        return {
            "kind": "excel",
            "sheet": sheet_name,
            "range": f"{start_cell}:{end_cell or start_cell}",
            "applied": applied,
            "digest": file_digest(path),
        }
    finally:
        workbook.close()


__all__ = [
    "file_digest",
    "find_text",
    "format_range",
    "insert_rows",
    "read_range",
    "delete_rows",
    "set_formula",
    "update_cells",
    "workbook_structure",
]
