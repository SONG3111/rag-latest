"""Formula reference shifting for row/column insert/delete (openpyxl limitation shim)."""

from __future__ import annotations

import pytest

from mcp_office_server.formula_shift import shift_formula_references


def shift(formula: str, *, op="delete", start: int, count: int = 1,
          own_sheet: str = "订单", affected_sheet: str = "订单", axis: str = "row"):
    return shift_formula_references(
        formula, own_sheet=own_sheet, affected_sheet=affected_sheet,
        op=op, start=start, count=count, axis=axis,
    )


# --------------------------------------------------------------------------- #
# row axis
# --------------------------------------------------------------------------- #
def test_delete_moves_later_row_references_up() -> None:
    """Regression for the reported bug: =C15*D15 must follow the row shift."""
    assert shift("=C15*D15", start=14) == "=C14*D14"
    assert shift("=C17*D17", start=14) == "=C16*D16"


def test_delete_leaves_references_above_the_band_untouched() -> None:
    assert shift("=C10*D10", start=14) == "=C10*D10"
    assert shift("=C2*D2", start=14, count=3) == "=C2*D2"


def test_delete_turns_in_band_references_into_ref_error() -> None:
    assert shift("=C14*D14", start=14) == "=#REF!*#REF!"


def test_insert_moves_references_down() -> None:
    assert shift("=C14*D14", op="insert", start=14) == "=C15*D15"
    assert shift("=C13*D13", op="insert", start=14, count=2) == "=C13*D13"


def test_row_absolute_references_do_not_move() -> None:
    assert shift("=C$14*D14", start=14) == "=C$14*#REF!"
    assert shift("=SUM(C$14:C20)", start=14) == "=SUM(C$14:C19)"


def test_cross_sheet_references_only_shift_for_the_affected_sheet() -> None:
    assert shift("=Q1!D2", own_sheet="汇总", affected_sheet="Q1", start=2) == "=Q1!#REF!"
    assert shift("=Q1!D2", own_sheet="汇总", affected_sheet="Q2", start=2) == "=Q1!D2"
    # 中文/带空格的 sheet 限定符
    assert shift("=汇总!B2", own_sheet="汇总", affected_sheet="汇总", start=2) == "=汇总!#REF!"
    assert shift("='Q 1'!D2", own_sheet="汇总", affected_sheet="Q 1", start=2) == "='Q 1'!#REF!"


def test_ranges_shrink_and_shift() -> None:
    assert shift("=SUM(B2:D10)", start=2) == "=SUM(B2:D9)"
    assert shift("=SUM(B2:D10)", start=14) == "=SUM(B2:D10)"
    assert shift("=SUM(B2:D3)", start=2, count=2) == "=SUM(#REF!)"
    assert shift("=SUM(B2:D2)", op="insert", start=5) == "=SUM(B2:D2)"
    assert shift("=SUM(B2:D2)", op="insert", start=2) == "=SUM(B3:D3)"


def test_function_names_and_string_literals_are_not_touched() -> None:
    assert shift("=LOG10(A1)+1", start=14) == "=LOG10(A1)+1"
    assert shift('=IF(A1="R14","是","否")', start=14) == '=IF(A1="R14","是","否")'
    assert shift('=IF(B2>0,SUM(C2:C9),"")', start=14) == '=IF(B2>0,SUM(C2:C9),"")'


def test_unqualified_reference_is_bound_to_the_formula_own_sheet() -> None:
    """删的是其他 sheet 的行时，本 sheet 的无限定引用不能被误平移。

    =Q1!D2+D9 里的 D9 指向汇总 sheet 自己（公式所在 sheet），所以删除
    订单 sheet 第 9 行时它必须原样保留；旧实现对无限定引用一律平移，
    会把 D9 错误改写成 #REF!。
    """
    assert shift("=Q1!D2+D9", own_sheet="汇总", affected_sheet="订单", start=14) == "=Q1!D2+D9"
    assert shift("=Q1!D2+D9", own_sheet="汇总", affected_sheet="订单", start=9) == "=Q1!D2+D9"
    # 公式就在受影响 sheet 上时，无限定引用照常平移
    assert shift("=Q1!D2+D9", own_sheet="订单", affected_sheet="订单", start=9) == "=Q1!D2+#REF!"

def test_range_endpoint_order_is_preserved() -> None:
    assert shift("=SUM(B15:B17)", start=14) == "=SUM(B14:B16)"


# --------------------------------------------------------------------------- #
# column axis
# --------------------------------------------------------------------------- #
def test_column_delete_moves_later_column_references_left() -> None:
    assert shift("=C2*E2", start=2, axis="col") == "=B2*D2"
    assert shift("=B2*D2", start=2, axis="col") == "=#REF!*C2"


def test_column_delete_turns_in_band_references_into_ref_error() -> None:
    assert shift("=C15*D15", start=3, axis="col") == "=#REF!*C15"


def test_column_insert_moves_references_right() -> None:
    assert shift("=C14*D14", op="insert", start=3, axis="col") == "=D14*E14"
    assert shift("=C14*D14", op="insert", start=5, axis="col") == "=C14*D14"


def test_column_absolute_references_do_not_move() -> None:
    # 既定语义（与行方向一致）：绝对端点钉死不动，即使指向被删的带。
    assert shift("=$C15*$D15", start=3, axis="col") == "=$C15*$D15"
    assert shift("=SUM($C2:E2)", start=4, count=2, axis="col") == "=SUM($C2:C2)"


def test_column_ranges_shrink_and_shift() -> None:
    assert shift("=SUM(B2:D2)", start=3, axis="col") == "=SUM(B2:C2)"
    assert shift("=SUM(B2:D2)", start=5, axis="col") == "=SUM(B2:D2)"
    assert shift("=SUM(C2:E2)", start=3, count=3, axis="col") == "=SUM(#REF!)"
    assert shift("=SUM(C2:C2)", op="insert", start=3, axis="col") == "=SUM(D2:D2)"
    # 高低端点塌缩对称于行方向：删 C、D 后 B:D 只剩 B
    assert shift("=SUM(B2:D2)", start=3, count=2, axis="col") == "=SUM(B2:B2)"


def test_column_shift_only_for_the_affected_sheet() -> None:
    assert shift("=Q1!C2", own_sheet="汇总", affected_sheet="Q1", start=3, axis="col") == "=Q1!#REF!"
    assert shift("=Q1!C2", own_sheet="汇总", affected_sheet="Q2", start=3, axis="col") == "=Q1!C2"
    assert shift(
        "=Q1!C2+C2", own_sheet="汇总", affected_sheet="Q1", start=3, axis="col"
    ) == "=Q1!#REF!+C2"


def test_column_shift_preserves_reversed_range_order() -> None:
    assert shift("=SUM(D2:B2)", start=4, axis="col") == "=SUM(C2:B2)"
