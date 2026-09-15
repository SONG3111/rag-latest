"""Shift A1 formula references after row/column insert/delete operations.

openpyxl moves cell values on ``delete_rows``/``insert_rows`` (and the column
variants) but deliberately leaves formula strings untouched (documented
limitation, openpyxl issue #1273): a formula that read ``=C15*D15`` and moves
up to row 14 keeps referencing row 15, which now holds different data. Excel
itself rewrites the references, so this module replicates that behaviour for
the row and column operations this server exposes.

The rules follow Excel's own semantics — references track their *target*
cells, not the position of the formula. Row and column shifts are symmetric:
``axis`` selects which component of every reference moves.

* delete lines (rows or columns) ``[start, start+count)``: a reference to a
  line at/after the end of the band moves back by ``count``; a reference to a
  line inside the band becomes ``#REF!`` (a range shrinks onto the band
  edges; a range fully inside the band becomes ``#REF!``); a reference before
  the band is untouched.
* insert ``count`` lines at ``start``: a reference to a line at/after
  ``start`` moves forward by ``count``.

Only references to the *affected sheet* shift. Unqualified references belong
to the sheet the formula lives on; qualified ones (``Q1!D2`` / ``'Q 1'!D2``)
to their named sheet. The absolute marker of the shifted axis (``A$1`` for
row shifts, ``$A1`` for column shifts) never moves. Matches inside quoted
string literals are ignored, and function names such as ``LOG10`` are not
mistaken for cell references.

Known leftovers of the same openpyxl limitation, not covered here: merged
cell ranges, charts, defined names, and cross-workbook links are not
rewritten on shifts.
"""

from __future__ import annotations

import re
from typing import Literal

from openpyxl.utils import column_index_from_string, get_column_letter

ShiftOp = Literal["insert", "delete"]
ShiftAxis = Literal["row", "col"]

# Characters Excel forbids inside sheet names, plus ones that would make a bare
# qualifier ambiguous (operators, string/arg separators, whitespace, #REF!).
_BARE_SHEET = r"[^'!:;()+*/\[\]\\#&=,?\s]"

_CELL_TEXT = r"\$?[A-Z]{1,3}\$?\d{1,7}"
_QUALIFIER_TEXT = rf"(?:'[^']+'|{_BARE_SHEET}+)!"

# One reference token: optional sheet qualifier, one cell, and optionally a
# second cell after ':' (a range). Guards:
# - ``(?<![A-Za-z0-9_.])`` — not the tail of a longer identifier;
# - ``(?![\d(])`` after each cell — a row number is never followed by another
#   digit (LOG10 backtracking) and a cell is never immediately followed by '('.
_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9_.])(?:{_QUALIFIER_TEXT})?{_CELL_TEXT}(?![\d(])"
    rf"(?::(?:{_QUALIFIER_TEXT})?{_CELL_TEXT}(?![\d(])?)?"
)

_ENDPOINT = re.compile(r"^(\$?)([A-Z]{1,3})(\$?)(\d{1,7})$")
_QUOTED = re.compile(r'("(?:[^"]|"")*")')


def _points_at_affected_sheet(token: str, *, own_sheet: str, affected_sheet: str) -> bool:
    """Whether this reference token points at the sheet being shifted.

    An unqualified reference belongs to the sheet the formula lives on, so it
    only follows a shift of that same sheet — deleting rows on another sheet
    must not move it.
    """
    if "!" not in token:
        return own_sheet.casefold() == affected_sheet.casefold()
    qualifier = token.split("!", 1)[0]
    target = qualifier[1:-1].replace("''", "'") if qualifier.startswith("'") else qualifier
    return target.casefold() == affected_sheet.casefold()


def _split_endpoint(endpoint: str) -> tuple[str, str, str, bool, bool]:
    """Split an A1 endpoint into ``(qualifier, col_text, row_text, col_abs, row_abs)``.

    ``qualifier`` keeps everything before the column letter, including the
    sheet qualifier and a column ``$`` marker; ``row_text`` keeps a row ``$``.
    """
    ref = endpoint.split("!")[-1]
    m = _ENDPOINT.match(ref)
    qualifier = endpoint[: len(endpoint) - len(ref)] + ref[: m.start(2)]
    return qualifier, m.group(2), m.group(4), m.group(1) == "$", m.group(3) == "$"


def _affected_sheet_of(token: str, affected_sheet: str) -> bool:
    """Whether this token points at the sheet being shifted."""
    if "!" not in token:
        return True
    qualifier = token.split("!", 1)[0]
    target = qualifier[1:-1].replace("''", "'") if qualifier.startswith("'") else qualifier
    return target.casefold() == affected_sheet.casefold()


def _shift_range(token: str, *, own_sheet: str, affected_sheet: str,
                 op: ShiftOp, start: int, count: int, axis: ShiftAxis) -> str:
    left, right = token.split(":", 1)
    probe = left if "!" in left else (right if "!" in right else left)
    if not _points_at_affected_sheet(probe, own_sheet=own_sheet, affected_sheet=affected_sheet):
        return token

    qual1, col1, row1, col1_abs, row1_abs = _split_endpoint(left)
    qual2, col2, row2, col2_abs, row2_abs = _split_endpoint(right)

    if axis == "row":
        band1, band2 = int(row1), int(row2)
        abs1, abs2 = row1_abs, row2_abs
    else:
        band1 = column_index_from_string(col1)
        band2 = column_index_from_string(col2)
        abs1, abs2 = col1_abs, col2_abs

    # A range fully inside the deleted band loses every line it covered.
    if op == "delete" and start <= min(band1, band2) and max(band1, band2) < start + count:
        return "#REF!"

    def shifted(band: int, absolute: bool, *, high: bool) -> int:
        if absolute:
            return band
        if op == "insert":
            return band + count if band >= start else band
        band_end = start + count
        if band >= band_end:
            return band - count
        if band >= start:
            # Endpoint inside the deleted band: the range edge collapses onto
            # the band (the low edge onto its first line, the high edge onto
            # its last line - 1).
            return start - 1 if high else start
        return band

    # Which endpoint forms the range's high edge (reversed ranges allowed).
    high1 = band1 >= band2
    new1 = shifted(band1, abs1, high=high1)
    new2 = shifted(band2, abs2, high=not high1)

    if axis == "row":
        # Row-absolute endpoints do not move, so their original text stays.
        part1 = left if abs1 else f"{qual1}{col1}{new1}"
        part2 = right if abs2 else f"{qual2}{col2}{new2}"
    else:
        part1 = left if abs1 else f"{qual1}{get_column_letter(new1)}{row1}"
        part2 = right if abs2 else f"{qual2}{get_column_letter(new2)}{row2}"
    return f"{part1}:{part2}"


def _shift_cell(token: str, *, own_sheet: str, affected_sheet: str,
                op: ShiftOp, start: int, count: int, axis: ShiftAxis) -> str:
    if not _points_at_affected_sheet(token, own_sheet=own_sheet, affected_sheet=affected_sheet):
        return token

    qual, col_text, row_text, col_abs, row_abs = _split_endpoint(token)

    if axis == "row":
        if row_abs:
            return token
        row = int(row_text)
        if op == "insert":
            if row >= start:
                return f"{qual}{col_text}{row + count}"
        else:
            if row >= start + count:
                return f"{qual}{col_text}{row - count}"
            if row >= start:
                return f"{qual}#REF!"
        return token

    if col_abs:
        return token
    col = column_index_from_string(col_text)
    if op == "insert":
        if col >= start:
            return f"{qual}{get_column_letter(col + count)}{row_text}"
    else:
        if col >= start + count:
            return f"{qual}{get_column_letter(col - count)}{row_text}"
        if col >= start:
            return f"{qual}#REF!"
    return token


def shift_formula_references(
    formula: str,
    *,
    own_sheet: str,
    affected_sheet: str,
    op: ShiftOp,
    start: int,
    count: int,
    axis: ShiftAxis = "row",
) -> str:
    """Rewrite the A1 references in one formula string for a row/column shift."""
    if count <= 0:
        return formula
    parts: list[str] = []
    for i, segment in enumerate(_QUOTED.split(formula)):
        if i % 2 == 1:  # inside a quoted string literal — never shifted
            parts.append(segment)
            continue
        out: list[str] = []
        last = 0
        for m in _TOKEN.finditer(segment):
            out.append(segment[last : m.start()])
            token = m.group(0)
            kwargs = dict(
                own_sheet=own_sheet,
                affected_sheet=affected_sheet,
                op=op,
                start=start,
                count=count,
                axis=axis,
            )
            out.append(_shift_range(token, **kwargs) if ":" in token else _shift_cell(token, **kwargs))
            last = m.end()
        out.append(segment[last:])
        parts.append("".join(out))
    return "".join(parts)


def shift_workbook_formulas(
    workbook,
    *,
    affected_sheet: str,
    op: ShiftOp,
    start: int,
    count: int,
    axis: ShiftAxis = "row",
) -> None:
    """Apply the reference shift to every formula cell in the workbook."""
    for ws in workbook.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if isinstance(value, str) and value.startswith("="):
                    shifted = shift_formula_references(
                        value,
                        own_sheet=ws.title,
                        affected_sheet=affected_sheet,
                        op=op,
                        start=start,
                        count=count,
                        axis=axis,
                    )
                    if shifted != value:
                        cell.value = shifted
