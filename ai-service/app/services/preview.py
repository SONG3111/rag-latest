"""Citation click-through: read the original text around a cited location.

The citation carries ``location`` — the same string the chunker wrote into
``chunks.location`` — so no new index is needed; this module only parses it back
into a read window and fetches it through the read-only MCP tools:

* Excel: ``"销售!第12行"`` → ``read_range`` over rows 10..14 (a few rows of
  context on each side), with the cited row marked for highlighting.
* Word paragraph: ``"段落 7"`` (or a range ``"段落 7-9"`` from a parent chunk)
  → ``read_paragraphs`` two paragraphs before and after.
* Word table: ``"表格 0 第 3 行"`` → ``read_table`` for the whole table, cited
  row highlighted; a bare ``"表格 0"`` (parent chunk) highlights nothing.

The two-level citation design mirrors RAGent's GroundingChunk/SourceRef split
(Apache-2.0): the chunk says *what the answer relied on*, the location resolves
*where that lives in the original file*, and this endpoint renders the latter.
"""

from __future__ import annotations

import re
from typing import Any

from ..mcp_client import McpOfficeClient, parse_tool_result

EXCEL_ROW_RE = re.compile(r"^(?P<sheet>.+?)!第(?P<row>\d+)行$")
WORD_PARAGRAPH_RE = re.compile(r"^段落 (?P<start>\d+)(?:-(?P<end>\d+))?$")
WORD_TABLE_RE = re.compile(r"^表格 (?P<table>\d+)(?: 第 (?P<row>\d+) 行)?$")

# How much surrounding context the preview window shows on each side.
_CONTEXT_ROWS = 2
_CONTEXT_PARAGRAPHS = 2
# Upper bound on columns fetched for an Excel preview window.
_PREVIEW_MAX_COL = "N"


class PreviewError(ValueError):
    """Raised when a citation location cannot be turned into a read window."""


def parse_location(location: str) -> dict[str, Any]:
    """Parse a chunk location into a structured descriptor."""
    text = (location or "").strip()
    match = EXCEL_ROW_RE.match(text)
    if match:
        return {"kind": "excel", "sheet": match.group("sheet"), "row": int(match.group("row"))}
    match = WORD_PARAGRAPH_RE.match(text)
    if match:
        return {
            "kind": "word_paragraphs",
            "start": int(match.group("start")),
            "end": int(match.group("end") or match.group("start")),
        }
    match = WORD_TABLE_RE.match(text)
    if match:
        row = int(match.group("row")) if match.group("row") else None
        return {"kind": "word_table", "table": int(match.group("table")), "row": row}
    raise PreviewError(
        f"无法识别的引用位置格式「{text}」；支持的格式：工作表!第N行 / 段落 N / 表格 N 第 N 行"
    )


async def build_preview(
    client: McpOfficeClient, workspace_id: str, rel_path: str, location: str
) -> dict[str, Any]:
    """Fetch the original text window behind one citation."""
    descriptor = parse_location(location)
    # The MCP sandbox root is the shared workspaces directory; read tools expect
    # the workspace-prefixed path, exactly like the agent's tool wrapper does.
    path = f"{workspace_id}/{rel_path}"

    if descriptor["kind"] == "excel":
        row = descriptor["row"]
        start_row = max(1, row - _CONTEXT_ROWS)
        tool = client.tool("read_range")
        if tool is None:
            raise PreviewError("文档服务未提供 read_range 工具")
        payload = parse_tool_result(
            await tool.ainvoke(
                {
                    "path": path,
                    "sheet_name": descriptor["sheet"],
                    "start_cell": f"A{start_row}",
                    "end_cell": f"{_PREVIEW_MAX_COL}{row + _CONTEXT_ROWS}",
                }
            )
        )
        if not payload.get("ok"):
            raise PreviewError(_error_text(payload))
        values = (payload.get("data") or {}).get("values") or []
        return {
            "kind": "excel",
            "file": rel_path,
            "location": location,
            "sheet": descriptor["sheet"],
            "start_row": start_row,
            "rows": values,
            # Highlight index within the returned window (0-based).
            "highlight": row - start_row,
        }

    if descriptor["kind"] == "word_paragraphs":
        start = max(0, descriptor["start"] - _CONTEXT_PARAGRAPHS)
        end = descriptor["end"] + _CONTEXT_PARAGRAPHS
        tool = client.tool("read_paragraphs")
        if tool is None:
            raise PreviewError("文档服务未提供 read_paragraphs 工具")
        payload = parse_tool_result(
            await tool.ainvoke({"path": path, "start": start, "end": end})
        )
        if not payload.get("ok"):
            raise PreviewError(_error_text(payload))
        return {
            "kind": "word_paragraphs",
            "file": rel_path,
            "location": location,
            "paragraphs": (payload.get("data") or {}).get("paragraphs") or [],
            "highlight": descriptor["start"],
        }

    tool = client.tool("read_table")
    if tool is None:
        raise PreviewError("文档服务未提供 read_table 工具")
    payload = parse_tool_result(await tool.ainvoke({"path": path, "table_index": descriptor["table"]}))
    if not payload.get("ok"):
        raise PreviewError(_error_text(payload))
    data = payload.get("data") or {}
    rows = data.get("rows") or []
    cited_row = descriptor.get("row")
    return {
        "kind": "word_table",
        "file": rel_path,
        "location": location,
        "table_index": descriptor["table"],
        "rows": rows,
        # chunking stored the 1-based display row; the table rows are 0-based.
        "highlight": cited_row - 1 if cited_row else None,
    }


def _error_text(payload: dict[str, Any]) -> str:
    error = payload.get("error") or {}
    if isinstance(error, dict):
        return str(error.get("message") or "读取原文失败")
    return "读取原文失败"
