"""Deterministic spreadsheet statistics for the analysis agent.

Design notes
------------
* **Column addressing.** A column is named either by its header text ("销售额")
  or by a letter/number ("C", "3"). Header text resolves against a caller-chosen
  header row inside the sheet's current region — the same "header row + data
  starts below it" convention ``excel_ops.read_range`` uses — because the agent
  reads the structure first and then thinks in column *names*, not letters.
* **Values.** Read with the dual-open trick from ``read_range`` (data_only=False
  for formula text, data_only=True for cached results): a formula contributes its
  cached value when one exists, otherwise it is counted as
  ``formula_without_cache`` and left out of the numbers, so a statistic never
  reports a value the file does not actually contain.
* **Determinism.** Everything here is plain arithmetic over the cells as read —
  no sampling, no model involvement — so the same file always yields the same
  numbers and a report can be reproduced. The quantile interpolation matches
  numpy/pandas' default (linear), so numbers line up with what an analyst would
  compute elsewhere.
* **Bounded work.** Every scan stops at ``MAX_STATS_ROWS``/``MAX_HEADER_SCAN_COLS``
  and flags ``truncated`` rather than spinning on a pathological sheet.

This module contains no MCP tool definitions (mirroring ``excel_ops``), so it is
unit-testable without the protocol layer; ``server.py`` wraps it in thin tools.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from openpyxl.utils import column_index_from_string, get_column_letter

from .errors import ColumnNotFound, InvalidValue
from .excel_ops import _jsonify, _require_sheet, open_workbook

MAX_STATS_ROWS = 200_000
MAX_HEADER_SCAN_COLS = 200
MAX_OUTPUT_COLS = 20
MAX_SAMPLE_VALUES = 10
DEFAULT_TOP_N = 20
MAX_TOP_N = 200
AGG_FUNCTIONS = ("sum", "mean", "count", "min", "max", "median")

_LETTER_RE_SEGMENT = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _is_letter_reference(text: str) -> bool:
    """True for a bare column letter such as "C" or "AB" (Excel-style)."""
    stripped = text.strip().upper()
    if not stripped or len(stripped) > 3:
        return False
    return all(char in _LETTER_RE_SEGMENT for char in stripped)


def _is_number_reference(text: str) -> bool:
    stripped = text.strip()
    return stripped.isdigit() and int(stripped) >= 1


def _header_names(sheet, header_row: int) -> list[tuple[int, str]]:
    """``(column_index, header_text)`` pairs for the header row, blanks skipped."""
    last_col = min(sheet.max_column or 0, MAX_HEADER_SCAN_COLS)
    names: list[tuple[int, str]] = []
    for index in range(1, last_col + 1):
        value = sheet.cell(row=header_row, column=index).value
        if value is None:
            continue
        text = str(_jsonify(value)).strip()
        if text:
            names.append((index, text))
    return names


def _resolve_column(sheet, column: str, header_row: int) -> tuple[int, str, str]:
    """Resolve ``column`` to ``(index, letter, display_name)``.

    Accepts a column letter ("C"), a 1-based number ("3"), or a header name
    ("销售额", matched case-sensitively against the header row). A header name
    that matches nothing raises ``ColumnNotFound`` listing what *is* available —
    the same self-correcting shape the other tools use on bad input.
    """
    text = "" if column is None else str(column)
    if _is_letter_reference(text):
        index = column_index_from_string(text.strip().upper())
        return index, get_column_letter(index), get_column_letter(index)
    if _is_number_reference(text):
        index = int(text.strip())
        return index, get_column_letter(index), get_column_letter(index)

    wanted = text.strip()
    if not wanted:
        raise ColumnNotFound("column must not be empty")
    for index, name in _header_names(sheet, header_row):
        if name == wanted:
            return index, get_column_letter(index), name
    available = [name for _, name in _header_names(sheet, header_row)]
    raise ColumnNotFound(
        f"column {wanted!r} not found in header row {header_row}; "
        f"available columns: {', '.join(available) if available else '(none)'}"
    )


def _display_value(raw: Any, cached: Any) -> Any:
    """What to show for a cell: the cached result of a formula, else the raw value."""
    if isinstance(raw, str) and raw.startswith("="):
        return _jsonify(cached)
    return _jsonify(raw)


def _is_formula(raw: Any) -> bool:
    return isinstance(raw, str) and raw.startswith("=")


def _is_number(value: Any) -> bool:
    """True for a real number; booleans are Excel TRUE/FALSE, not magnitudes."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile — the numpy/pandas default."""
    if not values:
        raise ValueError("quantile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    fraction = position - low
    return float(ordered[low]) + (float(ordered[high]) - float(ordered[low])) * fraction


def _collect_column(
    sheet,
    cached_sheet,
    index: int,
    header_row: int,
) -> dict:
    """Read one column's data rows, separating numbers from everything else.

    Returns ``numbers`` (floats), ``missing`` (blank cells), ``non_numeric``
    (samples of non-blank, non-numeric values), ``formula_without_cache`` (a
    formula whose cached result is missing — the number cannot be trusted and
    ``calculate`` should be used instead) and ``rows_scanned``.
    """
    numbers: list[float] = []
    missing = 0
    non_numeric: list[Any] = []
    formula_without_cache = 0
    last_row = min(sheet.max_row or 0, header_row + MAX_STATS_ROWS)

    for row in range(header_row + 1, last_row + 1):
        raw = sheet.cell(row=row, column=index).value
        cached = (
            cached_sheet.cell(row=row, column=index).value
            if cached_sheet is not None
            else None
        )
        if _is_formula(raw):
            if isinstance(cached, bool) or not isinstance(cached, (int, float)):
                formula_without_cache += 1
                continue
            numbers.append(float(cached))
            continue
        if raw is None:
            missing += 1
            continue
        if isinstance(raw, bool) or isinstance(raw, (datetime, date)):
            if len(non_numeric) < MAX_SAMPLE_VALUES:
                non_numeric.append(_jsonify(raw))
            continue
        if isinstance(raw, (int, float)):
            if isinstance(raw, float) and math.isnan(raw):
                missing += 1
                continue
            numbers.append(float(raw))
            continue
        if len(non_numeric) < MAX_SAMPLE_VALUES:
            non_numeric.append(_jsonify(raw))

    return {
        "numbers": numbers,
        "missing": missing,
        "non_numeric": non_numeric,
        "formula_without_cache": formula_without_cache,
        "rows_scanned": max(0, last_row - header_row),
        "truncated": (sheet.max_row or 0) > last_row,
    }


def _formula_hint(payload: dict) -> str | None:
    if payload.get("formula_without_cache"):
        return (
            f"该列有 {payload['formula_without_cache']} 个公式单元格没有缓存结果，"
            "已从统计中排除；需要这些数值请改用 calculate 工具。"
        )
    return None


# --------------------------------------------------------------------------- #
# read-only statistics
# --------------------------------------------------------------------------- #
def describe_column(
    path: Path,
    sheet_name: str,
    column: str,
    header_row: int = 1,
) -> dict:
    """Summary statistics for one numeric column."""
    workbook = open_workbook(path)
    cached = open_workbook(path, data_only=True)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        cached_sheet = cached[sheet_name] if sheet_name in cached.sheetnames else None
        index, letter, name = _resolve_column(sheet, column, header_row)
        collected = _collect_column(sheet, cached_sheet, index, header_row)
        numbers = collected["numbers"]

        payload: dict[str, Any] = {
            "kind": "excel",
            "sheet": sheet_name,
            "column": name,
            "column_letter": letter,
            "header_row": header_row,
            "count": len(numbers),
            "missing": collected["missing"],
            "formula_without_cache": collected["formula_without_cache"],
            "non_numeric_samples": collected["non_numeric"],
            "rows_scanned": collected["rows_scanned"],
            "truncated": collected["truncated"],
        }
        if numbers:
            payload.update(
                {
                    "sum": float(sum(numbers)),
                    "mean": float(statistics.fmean(numbers)),
                    "median": float(statistics.median(numbers)),
                    "min": float(min(numbers)),
                    "max": float(max(numbers)),
                    "stdev": (
                        float(statistics.stdev(numbers)) if len(numbers) >= 2 else None
                    ),
                    "q1": _quantile(numbers, 0.25),
                    "q3": _quantile(numbers, 0.75),
                }
            )
            hint = _formula_hint(collected)
            if hint:
                payload["hint"] = hint
            return payload

        # A column with no readable number cannot be described. Reporting zeros here
        # would be worse than failing: the agent would quote 0 as if it were data.
        raise InvalidValue(
            f"column {name!r} has no numeric values to describe "
            f"(blank cells: {collected['missing']}, "
            f"non-numeric cells: {len(collected['non_numeric'])}, "
            f"formula cells without a cached result: {collected['formula_without_cache']}); "
            "check the column name and header_row, or use calculate for formula-backed values"
        )
    finally:
        workbook.close()
        cached.close()


def aggregate(
    path: Path,
    sheet_name: str,
    group_by: str,
    value_column: str | None = None,
    agg: str = "sum",
    header_row: int = 1,
    top_n: int = DEFAULT_TOP_N,
    sort: str = "desc",
) -> dict:
    """Group rows by one column and aggregate a value column.

    With no ``value_column`` this is a frequency count of the grouping column
    (``agg`` is then forced to ``count``), which is how a plain "每个区域有多少条"
    question is answered.
    """
    agg = (agg or "sum").strip().lower()
    if agg not in AGG_FUNCTIONS:
        raise InvalidValue(
            f"unknown aggregation {agg!r}; expected one of {', '.join(AGG_FUNCTIONS)}"
        )
    direction = "desc" if (sort or "desc").strip().lower() != "asc" else "asc"
    top_n = max(1, min(int(top_n or DEFAULT_TOP_N), MAX_TOP_N))

    workbook = open_workbook(path)
    cached = open_workbook(path, data_only=True)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        cached_sheet = cached[sheet_name] if sheet_name in cached.sheetnames else None
        group_index, group_letter, group_name = _resolve_column(sheet, group_by, header_row)
        if value_column:
            value_index, value_letter, value_name = _resolve_column(
                sheet, value_column, header_row
            )
        else:
            value_index, value_letter, value_name = None, None, None
            agg = "count"

        buckets: dict[str, dict[str, Any]] = {}
        blank_groups = 0
        non_numeric_values = 0
        last_row = min(sheet.max_row or 0, header_row + MAX_STATS_ROWS)

        for row in range(header_row + 1, last_row + 1):
            raw_key = sheet.cell(row=row, column=group_index).value
            if raw_key is None or (isinstance(raw_key, str) and not raw_key.strip()):
                blank_groups += 1
                continue
            key = str(_jsonify(raw_key))
            bucket = buckets.setdefault(key, {"keys": key, "numbers": [], "rows": 0})
            bucket["rows"] += 1

            if value_index is None:
                continue
            raw_value = sheet.cell(row=row, column=value_index).value
            if raw_value is None:
                continue
            number: float | None = None
            if isinstance(raw_value, str) and raw_value.startswith("="):
                cached_value = (
                    cached_sheet.cell(row=row, column=value_index).value
                    if cached_sheet is not None
                    else None
                )
                if not isinstance(cached_value, bool) and isinstance(
                    cached_value, (int, float)
                ):
                    number = float(cached_value)
            elif not isinstance(raw_value, bool) and isinstance(raw_value, (int, float)):
                number = float(raw_value)
            if number is None:
                non_numeric_values += 1
                continue
            bucket["numbers"].append(number)

        def _aggregate_bucket(bucket: dict[str, Any]) -> float | None:
            numbers = bucket["numbers"]
            if agg == "count":
                return float(bucket["rows"] if value_index is None else len(numbers))
            if not numbers:
                return None
            if agg == "sum":
                return float(sum(numbers))
            if agg == "mean":
                return float(statistics.fmean(numbers))
            if agg == "min":
                return float(min(numbers))
            if agg == "max":
                return float(max(numbers))
            return float(statistics.median(numbers))

        groups = [
            {"key": bucket["keys"], "value": _aggregate_bucket(bucket), "count": bucket["rows"]}
            for bucket in buckets.values()
        ]
        # Groups with no numeric value (empty aggregation) cannot be ranked, so they
        # are listed after the ranked ones instead of jumping to the top when the
        # sort direction flips.
        ranked = [item for item in groups if item["value"] is not None]
        unranked = [item for item in groups if item["value"] is None]
        ranked.sort(key=lambda item: float(item["value"]), reverse=(direction == "desc"))
        groups = ranked + unranked

        total_groups = len(groups)
        truncated = total_groups > top_n
        groups = groups[:top_n]

        payload: dict[str, Any] = {
            "kind": "excel",
            "sheet": sheet_name,
            "group_by": group_name,
            "group_by_letter": group_letter,
            "value_column": value_name,
            "value_column_letter": value_letter,
            "agg": agg,
            "groups": groups,
            "group_count": total_groups,
            "truncated": truncated,
            "rows_scanned": max(0, last_row - header_row),
            "skipped": {"blank_group": blank_groups, "non_numeric_value": non_numeric_values},
        }
        if value_index is not None:
            all_numbers = [
                number for bucket in buckets.values() for number in bucket["numbers"]
            ]
            payload["total_value"] = float(sum(all_numbers)) if all_numbers else None
        return payload
    finally:
        workbook.close()
        cached.close()


def sort_by(
    path: Path,
    sheet_name: str,
    by_column: str,
    descending: bool = True,
    top_n: int = DEFAULT_TOP_N,
    header_row: int = 1,
    columns: Sequence[str] | None = None,
) -> dict:
    """Rank data rows by one column and return the top N, as a table."""
    top_n = max(1, min(int(top_n or DEFAULT_TOP_N), MAX_TOP_N))

    workbook = open_workbook(path)
    cached = open_workbook(path, data_only=True)
    try:
        sheet = _require_sheet(workbook, sheet_name)
        cached_sheet = cached[sheet_name] if sheet_name in cached.sheetnames else None
        order_index, order_letter, order_name = _resolve_column(sheet, by_column, header_row)

        if columns:
            selected = [
                _resolve_column(sheet, name, header_row) for name in columns
            ][:MAX_OUTPUT_COLS]
        else:
            last_col = min(sheet.max_column or 0, MAX_OUTPUT_COLS)
            selected = [
                (index, get_column_letter(index), str(_jsonify(
                    sheet.cell(row=header_row, column=index).value
                )).strip() or get_column_letter(index))
                for index in range(1, last_col + 1)
            ]
        if not any(index == order_index for index, _, _ in selected):
            selected.append((order_index, order_letter, order_name))

        rows: list[dict[str, Any]] = []
        skipped_blank = 0
        last_row = min(sheet.max_row or 0, header_row + MAX_STATS_ROWS)

        for row in range(header_row + 1, last_row + 1):
            raw_order = sheet.cell(row=row, column=order_index).value
            order_value = _display_value(
                raw_order,
                cached_sheet.cell(row=row, column=order_index).value
                if cached_sheet is not None
                else None,
            )
            if order_value is None or (isinstance(order_value, str) and not order_value.strip()):
                skipped_blank += 1
                continue
            entry: dict[str, Any] = {"_sort": order_value}
            for index, _letter, name in selected:
                raw = sheet.cell(row=row, column=index).value
                cached_value = (
                    cached_sheet.cell(row=row, column=index).value
                    if cached_sheet is not None
                    else None
                )
                entry[name] = _display_value(raw, cached_value)
            rows.append(entry)

        # Numbers rank first whatever the direction: a text value sitting in a
        # numeric column ("待定") must never displace the real top-N. Text rows are
        # kept in the output, just ordered after the numbers.
        numeric_rows = [item for item in rows if _is_number(item["_sort"])]
        other_rows = [item for item in rows if not _is_number(item["_sort"])]
        numeric_rows.sort(key=lambda item: float(item["_sort"]), reverse=bool(descending))
        other_rows.sort(
            key=lambda item: str(item["_sort"]).casefold(), reverse=bool(descending)
        )
        ordered_rows = numeric_rows + other_rows

        total_rows = len(ordered_rows)
        truncated = total_rows > top_n
        output: list[dict[str, Any]] = []
        for rank, entry in enumerate(ordered_rows[:top_n], start=1):
            output.append({"rank": rank, **{key: value for key, value in entry.items() if key != "_sort"}})

        return {
            "kind": "excel",
            "sheet": sheet_name,
            "order_by": order_name,
            "order_by_letter": order_letter,
            "descending": bool(descending),
            "columns": [name for _index, _letter, name in selected],
            "rows": output,
            "row_count": total_rows,
            "truncated": truncated,
            "rows_scanned": max(0, last_row - header_row),
            "skipped": {
                "blank_order_value": skipped_blank,
                "non_numeric_order_value": len(other_rows),
            },
        }
    finally:
        workbook.close()
        cached.close()
