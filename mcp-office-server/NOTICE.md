# Upstream attribution

This package is an independent rework rather than a fork. It keeps the layered
implementation approach of two upstream projects and replaces their tool surface,
path handling, and error semantics with a single sandboxed design.

## excel-mcp-server

- Source: https://github.com/haris-musa/excel-mcp-server
- License: MIT
- Used for: the FastMCP + openpyxl layering (`workbook` / `data` / `sheet` /
  `formatting` / `calculation` separation) and the original `_resolved_path_is_within`
  sandbox containment check, which this package generalizes to multiple roots.
  The structural Excel tools added later — `copy_range`, `delete_range`,
  `merge_cells`/`unmerge_cells`, `manage_sheets`, `insert_columns`/`delete_columns` —
  follow the semantics of this project's tool surface (reimplemented, not copied).
  `copy_range` additionally translates relative formula references to the
  destination via openpyxl's `Translator`, where the upstream copies verbatim.

## Office-Word-MCP-Server

- Source: https://github.com/GongRzhe/Office-Word-MCP-Server
- License: MIT
- Used for: the modular grouping of Word capabilities (document / content /
  formatting) that informs how the Word implementation layer is organized here.

## Borrowings from the 2026-09-13 hardening round

- **haris-musa/excel-mcp-server (MIT)** — its server layering (domain errors
  answered per tool, anything else logged) informed the envelope backstop in
  `server.py`, adapted to keep surprises inside the `{ok: false}` envelope
  instead of re-raising; its delete-side bound checks
  ("exceeds worksheet bounds") informed clamping `delete_range` to the used
  range. Its copy loop writes while reading and corrupts overlapping copies;
  our `copy_range` snapshots the block first (an improvement, not a copy).
- **openpyxl (MIT)** — the sheet-title character blacklist and the Alignment
  horizontal value set mirrored in `_validate_sheet_name` /
  `format_range`, and the 1..1048576 row / 16384 column sheet dimensions
  enforced up-front in `_check_bounds` (mirroring `Worksheet._get_cell`).
- **simpleeval (MIT, danthedeckie/simpleeval)** — the `safe_power` /
  `MAX_POWER` operand-cap pattern behind `calculator.MAX_POWER`.
- **Python `re` documentation idiom** — literal replacement via a callable
  (`pattern.subn(lambda m: replacement, s)`) in `find_replace`; `re.escape`
  must not be applied to the replacement side. No single project borrowed;
  this is the standard documented workaround for template parsing.

## Borrowings from the 2026-09-14 hardening round

- **openpyxl (MIT)** — `merge_shift.py` replicates Excel's merge-range
  behaviour on row/column shifts, which openpyxl explicitly does not manage
  (docs "Inserting and deleting rows and columns", issue #1139). The
  detach-and-remerge technique follows the standard community workaround
  (StackOverflow "Openpyxl Issue: Deleting row not moving merged cells",
  question 53162099); the band arithmetic mirrors the
  edge-collapse rules of this package's own `formula_shift.py`. The 32,767
  character cell limit and 8,192 character formula limit are Excel's
  official "Excel specifications and limits"; the control-character blacklist
  is openpyxl's own `ILLEGAL_CHARACTERS_RE` (openpyxl silently truncates
  longer strings at save time, so both are now rejected up-front).

No source files were copied verbatim. Both projects are MIT licensed, so
redistribution of derived work is permitted provided this notice and the license
text are retained.

## pycel (runtime dependency, not modified)

- Source: https://github.com/dgorissen/pycel
- License: GPL-3.0
- Used for: the `calculate` tool's workbook-mode evaluation. A formula is
  written into a scratch cell of a throwaway temp copy and recomputed by
  pycel's cell-graph engine, which supports ranges, cross-sheet references and
  formulas depending on other formulas — including workbooks written by
  openpyxl where no cached results exist.

No pycel source was copied or modified; it is consumed as an unmodified pip
dependency. It was chosen over xlcalculator because that project's 0.5 series
ships an incompatible in-progress API and its 0.4 series cannot build its
`yearfrac` dependency on Python 3.12.
