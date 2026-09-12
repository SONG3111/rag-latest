"""Formula reference shifting for row insert/delete (openpyxl limitation shim)."""

from __future__ import annotations

import pytest

from mcp_office_server.formula_shift import shift_formula_references


def shift(formula: str, *, op="delete", start_row: int, count: int = 1,
          own_sheet: str = "订单", affected_sheet: str = "订单"):
    return shift_formula_references(
        formula, own_sheet=own_sheet, affected_sheet=affected_sheet,
        op=op, start_row=start_row, count=count,
    )


def test_delete_moves_later_row_references_up() -> None:
    """Regression for the reported bug: =C15*D15 must follow the row shift."""
    assert shift("=C15*D15", start_row=14) == "=C14*D14"
    assert shift("=C17*D17", start_row=14) == "=C16*D16"


def test_delete_leaves_references_above_the_band_untouched() -> None:
    assert shift("=C10*D10", start_row=14) == "=C10*D10"
    assert shift("=C2*D2", start_row=14, count=3) == "=C2*D2"


def test_delete_turns_in_band_references_into_ref_error() -> None:
    assert shift("=C14*D14", start_row=14) == "=#REF!*#REF!"


def test_insert_moves_references_down() -> None:
    assert shift("=C14*D14", op="insert", start_row=14) == "=C15*D15"
    assert shift("=C13*D13", op="insert", start_row=14, count=2) == "=C13*D13"


def test_row_absolute_references_do_not_move() -> None:
    assert shift("=C$14*D14", start_row=14) == "=C$14*#REF!"
    assert shift("=SUM(C$14:C20)", start_row=14) == "=SUM(C$14:C19)"


def test_cross_sheet_references_only_shift_for_the_affected_sheet() -> None:
    assert shift("=Q1!D2", own_sheet="汇总", affected_sheet="Q1", start_row=2) == "=Q1!#REF!"
    assert shift("=Q1!D2", own_sheet="汇总", affected_sheet="Q2", start_row=2) == "=Q1!D2"
    # 中文/带空格的 sheet 限定符
    assert shift("=汇总!B2", own_sheet="汇总", affected_sheet="汇总", start_row=2) == "汇总!#REF!".replace("汇总!#REF!", "=汇总!#REF!")
    assert shift("='Q 1'!D2", own_sheet="汇总", affected_sheet="Q 1", start_row=2) == "='Q 1'!#REF!"


def test_ranges_shrink_and_shift() -> None:
    assert shift("=SUM(B2:D10)", start_row=2) == "=SUM(B2:D9)"
    assert shift("=SUM(B2:D10)", start_row=14) == "=SUM(B2:D10)"
    assert shift("=SUM(B2:D3)", start_row=2, count=2) == "=SUM(#REF!)"
    assert shift("=SUM(B2:D2)", op="insert", start_row=5) == "=SUM(B2:D2)"
    assert shift("=SUM(B2:D2)", op="insert", start_row=2) == "=SUM(B3:D3)"


def test_function_names_and_string_literals_are_not_touched() -> None:
    assert shift("=LOG10(A1)+1", start_row=14) == "=LOG10(A1)+1"
    assert shift('=IF(A1="R14","是","否")', start_row=14) == '=IF(A1="R14","是","否")'
    assert shift('=IF(B2>0,SUM(C2:C9),"")', start_row=14) == '=IF(B2>0,SUM(C2:C9),"")'


def test_cross_sheet_formula_on_another_sheet_keeps_its_own_refs() -> None:
    """汇总!B2 上的公式引用自己的 sheet：受影响的是 订单 时不应被平移。"""
    assert shift("=Q1!D2+D9", own_sheet="汇总", affected_sheet="订单", start_row=14) == "=Q1!D2+D9"


def test_range_endpoint_order_is_preserved() -> None:
    assert shift("=SUM(B15:B17)", start_row=14) == "=SUM(B14:B16)"
