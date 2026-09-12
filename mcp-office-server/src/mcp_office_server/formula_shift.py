"""Shift A1 formula references after row insert/delete operations.

openpyxl moves cell values on ``delete_rows``/``insert_rows`` but deliberately
leaves formula strings untouched (documented limitation, openpyxl issue #1273):
a formula that read ``=C15*D15`` and moves up to row 14 keeps referencing row
15, which now holds different data. Excel itself rewrites the references, so
this module replicates that behaviour for the row operations this server
exposes.

The rules follow Excel's own semantics — references track their *target*
cells, not the position of the formula:

* delete rows ``[start, start+count)``: a reference to a row at/after the end
  of the band moves up by ``count``; a reference to a row inside the band
  becomes ``#REF!`` (a range shrinks onto the band edges; a range fully
  inside the band becomes ``#REF!``); a reference above the band is untouched.
* insert ``count`` rows at ``start``: a reference to a row at/after ``start``
  moves down by ``count``.

Only references to the *affected sheet* shift. Unqualified references belong
to the sheet the formula lives on; qualified ones (``Q1!D2`` / ``'Q 1'!D2``)
to their named sheet. Row-absolute markers (``A$1``) never move. Matches
inside quoted string literals are ignored, and function names such as
``LOG10`` are not mistaken for cell references.

Known leftovers of the same openpyxl limitation, not covered here: merged
cell ranges, charts, defined names, and cross-workbook links are not
rewritten on row shifts.
"""

from __future__ import annotations

import re
from typing import Literal

ShiftOp = Literal["insert", "delete"]

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


def _target_sheet(token: str, own_sheet: str) -> str:
    """The sheet a reference token points at: its qualifier, or the own sheet."""
    if "!" not in token:
        return own_sheet
    qualifier = token.split("!", 1)[0]
    if qualifier.startswith("'"):
        return qualifier[1:-1].replace("''", "'")
    return qualifier


def _parse_endpoint(endpoint: str) -> tuple[str, str, int, bool, str]:
    """Split ``$C$14``-style text into (prefix incl. qualifier, column, row, row-abs, raw)."""
    ref = endpoint.split("!")[-1]
    m = _ENDPOINT.match(ref)
    prefix = endpoint[: len(endpoint) - len(ref)] + ref[: m.start(2)]
    return prefix, m.group(2), int(m.group(4)), m.group(3) == "$", ref


def _shift_range(token: str, *, own_sheet: str, affected_sheet: str,
                 op: ShiftOp, start_row: int, count: int) -> str:
    left, right = token.split(":", 1)
    qualifier = left.split("!", 1)[0] if "!" in left else (
        right.split("!", 1)[0] if "!" in right else None
    )
    if qualifier is not None:
        target = qualifier[1:-1].replace("''", "'") if qualifier.startswith("'") else qualifier
        if target.casefold() != affected_sheet.casefold():
            return token

    def parse(endpoint: str) -> tuple[str, str, int, bool, str]:
        return _parse_endpoint(endpoint)

    prefix1, col1, row1, abs1, raw1 = parse(left)
    prefix2, col2, row2, abs2, raw2 = parse(right)

    # A range fully inside the deleted band loses every row it covered.
    if op == "delete" and start_row <= min(row1, row2) and max(row1, row2) < start_row + count:
        return "#REF!"

    def shifted(row: int, row_absolute: bool, *, bottom: bool) -> int:
        if row_absolute:
            return row
        if op == "insert":
            return row + count if row >= start_row else row
        band_end = start_row + count
        if row >= band_end:
            return row - count
        if row >= start_row:
            # Endpoint inside the deleted band: the range edge collapses onto
            # the band (top onto its first row, bottom onto its last row - 1).
            return start_row - 1 if bottom else start_row
        return row

    # Which endpoint forms the range's bottom edge (reversed ranges allowed).
    bottom1 = row1 >= row2
    new_row1 = shifted(row1, abs1, bottom=bottom1)
    new_row2 = shifted(row2, abs2, bottom=not bottom1)
    # Row-absolute endpoints do not move, so their original text stays as-is.
    part1 = raw1 if abs1 else f"{prefix1}{col1}{new_row1}"
    part2 = raw2 if abs2 else f"{prefix2}{col2}{new_row2}"
    return f"{part1}:{part2}"


def _shift_cell(token: str, *, own_sheet: str, affected_sheet: str,
                op: ShiftOp, start_row: int, count: int) -> str:
    qualifier = token.split("!", 1)[0] if "!" in token else None
    if qualifier is not None:
        target = qualifier[1:-1].replace("''", "'") if qualifier.startswith("'") else qualifier
        if target.casefold() != affected_sheet.casefold():
            return token

    prefix, column, row, row_absolute, raw = _parse_endpoint(token)
    if row_absolute:
        return raw
    if op == "insert":
        if row >= start_row:
            return f"{prefix}{column}{row + count}"
    else:
        if row >= start_row + count:
            return f"{prefix}{column}{row - count}"
        if row >= start_row:
            return f"{prefix}#REF!"
    return token


def shift_formula_references(
    formula: str,
    *,
    own_sheet: str,
    affected_sheet: str,
    op: ShiftOp,
    start_row: int,
    count: int,
) -> str:
    """Rewrite the A1 references in one formula string for a row shift."""
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
                start_row=start_row,
                count=count,
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
    start_row: int,
    count: int,
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
                        start_row=start_row,
                        count=count,
                    )
                    if shifted != value:
                        cell.value = shifted
