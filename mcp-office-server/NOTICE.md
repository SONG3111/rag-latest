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

## Office-Word-MCP-Server

- Source: https://github.com/GongRzhe/Office-Word-MCP-Server
- License: MIT
- Used for: the modular grouping of Word capabilities (document / content /
  formatting) that informs how the Word implementation layer is organized here.

No source files were copied verbatim. Both projects are MIT licensed, so
redistribution of derived work is permitted provided this notice and the license
text are retained.
