"""Formula and arithmetic evaluation for the ``calculate`` tool.

Two entry points back one tool:

* ``safe_arithmetic`` — a plain expression such as ``1200*98 + 128250``. It is
  evaluated through a Python ``ast`` whitelist (no ``eval``), the standard
  pattern in production MCP calculator servers: only numbers, the four basic
  operations with ``// % **`` and parentheses are accepted. Operand and result
  magnitude guards follow ``safe_power``/``MAX_POWER`` from
  danthedeckie/simpleeval (MIT), which locks the power operator down "so
  expressions can't go on for ever" — ``9**9**9`` would otherwise allocate
  gigabytes before ever producing a number.
* ``evaluate_workbook_formula`` — an Excel formula such as ``=SUM(B2:D2)``
  evaluated in the context of a real workbook. This follows the approach of
  the established spreadsheet-evaluation libraries (pycel, xlcalculator):
  the formula is written into a scratch cell of a throwaway copy of the file
  and the engine recomputes it from the cell graph — which also works when
  cached results are missing, exactly the case for files written by openpyxl.

Both are read-only with respect to the user's file: the scratch copy lives in
a temp file that is deleted right after evaluation.
"""

from __future__ import annotations

import ast
import math
import operator
import tempfile
from pathlib import Path
from typing import Any

from openpyxl.utils import get_column_letter

from .errors import CalculationError
# The seven Excel error literals pycel can return as cell values. Matching the
# exact strings matters: an earlier draft compared against stripped keywords
# ("DIV0") while pycel emits Excel's own "#DIV/0!" form, so the guard never
# fired and error cells leaked to the agent as literal strings.
from .excel_ops import _require_sheet, open_workbook

_EXCEL_ERRORS = frozenset({
    "#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!",
})

# Operand cap for `**`, taken from simpleeval's MAX_POWER: any base or
# exponent larger than this is rejected before computing.
MAX_POWER = 4_000_000

# A JSON payload has to round-trip through json.dumps, which refuses integers
# beyond ~4300 digits (int-max-str-digits). Cap the *result* at a comfortable
# margin below that (2**13000 ≈ 3914 digits).
MAX_RESULT_BITS = 13_000

# Binary and unary operators the arithmetic evaluator accepts, mapped to their
# safe stdlib implementations. Anything else in the parse tree is rejected.
_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type, Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def safe_arithmetic(expression: str) -> Any:
    """Evaluate a plain arithmetic expression without ``eval``.

    Accepts numbers, ``+ - * / // % **`` and parentheses; rejects names, calls,
    strings and anything else the whitelist does not know. Division by zero in
    any form (``/``, ``//``, ``%``) and non-finite or absurdly large results
    raise ``CalculationError`` so the tool envelope stays a strict-JSON one.
    """

    def visit(node: ast.expr) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise CalculationError("division by zero in expression")
            if isinstance(node.op, (ast.FloorDiv, ast.Mod)) and right == 0:
                raise CalculationError("division by zero in expression")
            if isinstance(node.op, ast.Pow) and (
                abs(left) > MAX_POWER or abs(right) > MAX_POWER
            ):
                raise CalculationError(
                    f"exponent too large ({left} ** {right}); operands are limited to {MAX_POWER}"
                )
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](visit(node.operand))
        raise CalculationError(
            f"unsupported element in expression: {ast.dump(node)[:80]}; "
            "only numbers and arithmetic operators are allowed"
        )

    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"expression is not valid arithmetic: {expression!r}") from exc
    result = visit(tree.body)
    if isinstance(result, complex):
        raise CalculationError("expression produced a complex number")
    if isinstance(result, float) and not math.isfinite(result):
        raise CalculationError("expression produced a value too large to represent")
    if isinstance(result, int) and result.bit_length() > MAX_RESULT_BITS:
        raise CalculationError(
            "expression produced a number too large to return; "
            f"results are limited to {MAX_RESULT_BITS} bits"
        )
    return result


def evaluate_workbook_formula(
    path: Path, sheet_name: str, formula: str
) -> Any:
    """Evaluate an Excel formula inside the workbook's own context.

    The formula is placed in a scratch cell on a temp copy of the file and
    recomputed by pycel, so results come from the real cell graph: ranges,
    cross-sheet references and formulas that themselves depend on other
    formulas all work, with or without cached values.
    """
    from pycel import ExcelCompiler

    normalized = formula.strip()
    if not normalized:
        raise CalculationError("formula must not be empty")
    if not normalized.startswith("="):
        normalized = f"={normalized}"

    workbook = open_workbook(path)
    sheet = _require_sheet(workbook, sheet_name)
    scratch = f"{get_column_letter(sheet.max_column + 4)}{sheet.max_row + 10}"
    sheet[scratch] = normalized
    tmp_fd = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp_path = Path(tmp_fd.name)
    tmp_fd.close()
    try:
        workbook.save(tmp_path)
        workbook.close()
        try:
            engine = ExcelCompiler(filename=tmp_path.as_posix())
            result = engine.evaluate(f"{sheet_name}!{scratch}")
        except Exception as exc:
            raise CalculationError(f"could not evaluate {normalized!r}: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return _plain_value(result)


def _plain_value(result: Any) -> Any:
    """Coerce an engine result into something JSON- and error-friendly."""
    if isinstance(result, str) and result.strip().upper() in _EXCEL_ERRORS:
        raise CalculationError(f"formula evaluated to the Excel error {result.strip()}")
    if isinstance(result, float) and not math.isfinite(result):
        raise CalculationError("formula produced a value too large to represent")
    if hasattr(result, "item"):  # numpy scalars
        return result.item()
    return result
