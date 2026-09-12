# -*- coding: utf-8 -*-
"""Tests for the ``calculate`` tool: arithmetic and workbook-mode evaluation.

The workbook mode exists because openpyxl-written files carry no cached formula
results, so ``read_range`` cannot answer "what does this formula evaluate to".
The scratch-cell + engine approach follows the established spreadsheet
evaluation libraries (see calculator.py and NOTICE.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_office_server import server
from mcp_office_server.calculator import evaluate_workbook_formula, safe_arithmetic
from mcp_office_server.errors import CalculationError


# --------------------------------------------------------------------------- #
# plain arithmetic
# --------------------------------------------------------------------------- #
def test_arithmetic_precedence_and_parentheses() -> None:
    assert safe_arithmetic("2+3*4") == 14
    assert safe_arithmetic("(2+3)*4") == 20
    assert safe_arithmetic("10/4") == 2.5
    assert safe_arithmetic("2**10") == 1024
    assert safe_arithmetic("-5+3") == -2
    assert safe_arithmetic(" 1200*98 + 128250 ") == 245850


def test_arithmetic_rejects_non_numbers() -> None:
    with pytest.raises(CalculationError):
        safe_arithmetic("1 + foo")
    with pytest.raises(CalculationError):
        safe_arithmetic("__import__('os').system('dir')")
    with pytest.raises(CalculationError):
        safe_arithmetic("2/0")
    with pytest.raises(CalculationError):
        safe_arithmetic("")


# --------------------------------------------------------------------------- #
# workbook mode
# --------------------------------------------------------------------------- #
def test_workbook_formula_over_a_range(workbook_path: Path) -> None:
    assert evaluate_workbook_formula(workbook_path, "销售", "=SUM(C2:C4)") == 6000


def test_workbook_formula_with_cross_sheet_reference(workbook_path: Path) -> None:
    # The 汇总 sheet's own cell is a formula referencing the 销售 sheet; the
    # engine must recompute the chain, not read a (missing) cached value.
    assert evaluate_workbook_formula(workbook_path, "汇总", "=SUM(销售!C2:C4)") == 6000
    assert evaluate_workbook_formula(workbook_path, "销售", "=汇总!B1") == 6000


def test_workbook_formula_without_leading_equals(workbook_path: Path) -> None:
    assert evaluate_workbook_formula(workbook_path, "销售", "C2*2 + C3") == 4000


def test_workbook_formula_reports_excel_errors(workbook_path: Path) -> None:
    with pytest.raises(CalculationError):
        evaluate_workbook_formula(workbook_path, "销售", "=NOTAFUNCTION(C2)")


def test_workbook_mode_never_touches_the_file(workbook_path: Path) -> None:
    before = workbook_path.read_bytes()
    evaluate_workbook_formula(workbook_path, "销售", "=SUM(C2:C4)*3")
    assert workbook_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# MCP tool surface
# --------------------------------------------------------------------------- #
def _invoke(expression: str, path: str | None = None, sheet: str | None = None) -> dict:
    raw = server.calculate(expression, path=path, sheet_name=sheet)
    return json.loads(raw)


def test_tool_arithmetic_without_a_file() -> None:
    payload = _invoke("(1200*98) + (860*102)")
    assert payload["ok"] is True
    assert payload["data"]["value"] == 205320


def test_tool_workbook_mode_scopes_to_the_sheet(workbook_path: Path) -> None:
    # Tools are called with workspace-relative paths, exactly as the model does.
    payload = _invoke("=SUM(C2:C4)", path="销售表.xlsx")
    assert payload["ok"] is True
    assert payload["data"]["sheet"] == "销售"  # defaults to the first sheet
    assert payload["data"]["value"] == 6000

    payload = _invoke("=SUM(销售!C2:C4)", path="销售表.xlsx", sheet="汇总")
    assert payload["ok"] is True
    assert payload["data"]["value"] == 6000


def test_tool_workbook_mode_rejects_unknown_sheet(workbook_path: Path) -> None:
    payload = _invoke("=SUM(C2:C4)", path="销售表.xlsx", sheet="不存在")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "sheet_not_found"


def test_tool_reports_calculation_failures_as_envelopes(workbook_path: Path) -> None:
    payload = _invoke("=NOTAFUNCTION(C2)", path="销售表.xlsx")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "calculation_failed"

    payload = _invoke("1 + foo")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "calculation_failed"
