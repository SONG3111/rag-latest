"""MCP tool surface for the office document server.

Only a curated subset of the underlying capability is registered. Exposing every
available operation measurably degrades tool selection accuracy in a tool-calling
agent, so the wider library surface (charts, pivot tables, document protection,
comments, footnotes) stays available in the implementation layer but unregistered
until a deployment explicitly opts in.

Every tool declares MCP standard annotations. ``readOnlyHint`` marks inspection
tools the agent may call freely; ``destructiveHint`` marks tools that mutate a file,
which the host application is expected to gate behind human approval. The server
itself holds no approval state, which keeps it reusable and stateless.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import calculator, excel_ops, sandbox, word_ops
from .errors import SheetNotFound, ToolError, error_payload

# stdio transport reserves stdout for protocol frames, so logs must go to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mcp-office] %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-office-server")

mcp = FastMCP(
    "mcp-office-server",
    instructions=(
        "Excel (.xlsx/.xlsm) 和 Word (.docx) 文档的读写工具。"
        "所有路径都是相对于当前工作区的相对路径。"
        "读取类工具可以直接调用；写入类工具会修改用户的文件，调用前请先用读取类工具确认当前内容，"
        "并向用户说明将要修改的位置与新旧值。"
    ),
)


def _ok(payload: Any) -> str:
    return json.dumps({"ok": True, "data": payload}, ensure_ascii=False, default=str)


def _handle(exc: ToolError) -> str:
    return json.dumps(error_payload(exc), ensure_ascii=False, default=str)


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
READ_ONLY_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=True, idempotentHint=True, openWorldHint=False
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)


# --------------------------------------------------------------------------- #
# read tools
# --------------------------------------------------------------------------- #
@mcp.tool(annotations=READ_ONLY_IDEMPOTENT)
def list_files() -> str:
    """列出当前工作区内所有可操作的 Excel/Word 文件（相对路径、类型、大小、修改时间）。

    当用户提到"工作区里的文件""有哪些表格"时使用本工具先确认可操作对象。
    """
    try:
        return _ok({"files": sandbox.list_documents()})
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=READ_ONLY)
def get_doc_structure(path: str) -> str:
    """查看一个文档的整体结构。

    Excel：返回每个 sheet 的名称、行列数、首行表头。
    Word：返回段落总数、表格数量、标题大纲（含段落下标）以及每张表的表头。

    在读取具体内容或修改之前，应先用本工具了解文档全貌。
    """
    try:
        resolved = sandbox.resolve_document(path)
        kind = sandbox.document_kind(resolved)
        if kind == "excel":
            return _ok(excel_ops.workbook_structure(resolved))
        return _ok(word_ops.document_structure(resolved))
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=READ_ONLY)
def read_range(
    path: str,
    sheet_name: str,
    start_cell: str = "A1",
    end_cell: str | None = None,
) -> str:
    """读取 Excel 指定区域的值，用于查看表格逐行数据。

    Args:
        path: 相对工作区的 Excel 文件路径。
        sheet_name: 工作表名称。
        start_cell: 起始单元格，如 "A1"。
        end_cell: 结束单元格，如 "F20"；留空则从 start_cell 一直读到该工作表的
            末尾（按数据区域自动截断，超过 200 行或 60 列会返回 truncated=true，
            可用返回的 end_cell 继续往后读）。读取整张表时不要传这个参数。

    公式单元格会以 {"formula": ..., "cached_value": ...} 形式返回。
    注意：本系统写入的公式没有缓存结果（openpyxl 的限制），cached_value 通常是
    null——读不到公式的计算结果（跨表引用如 =Q1!D2 同样如此）。需要公式的
    结果时请用 calculate 工具现算（传 path 与 sheet_name）。
    返回结果里的 digest 是文件当前内容指纹，后续修改时应原样回传。
    """
    try:
        resolved = sandbox.resolve_document(path)
        sheet = sheet_name or (excel_ops.workbook_structure(resolved)["sheets"][0]["name"])
        return _ok(
            excel_ops.read_range(
                resolved, sheet, start_cell=start_cell, end_cell=end_cell
            )
        )
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=READ_ONLY)
def read_paragraphs(
    path: str,
    start: int = 0,
    end: int | None = None,
) -> str:
    """读取 Word 文档的正文段落，按段落下标切片。

    Args:
        path: 相对工作区的 Word 文件路径。
        start: 起始段落下标（从 0 开始）。
        end: 结束段落下标（含）；留空表示读到文档末尾。

    返回的每条记录包含 index / text / style / is_heading，便于定位与后续替换。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(word_ops.read_paragraphs(resolved, start=start, end=end))
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=READ_ONLY)
def read_table(path: str, table_index: int = 0) -> str:
    """读取 Word 文档中某一张表格的全部单元格内容。

    Args:
        path: 相对工作区的 Word 文件路径。
        table_index: 表格序号，从 0 开始（可用 get_doc_structure 查询）。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(word_ops.read_table(resolved, table_index))
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=READ_ONLY)
def find_text(path: str, query: str) -> str:
    """在文档中查找一段文本，返回所有命中位置。

    Excel：返回 sheet 与单元格坐标。Word：返回段落下标或表格行列坐标。
    当用户说"第 X 条""关于某关键词的那行"时，先用本工具定位。
    """
    try:
        resolved = sandbox.resolve_document(path)
        kind = sandbox.document_kind(resolved)
        hits = (
            excel_ops.find_text(resolved, query)
            if kind == "excel"
            else word_ops.find_text(resolved, query)
        )
        return _ok({"query": query, "hits": hits})
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=READ_ONLY)
def calculate(
    expression: str,
    path: str | None = None,
    sheet_name: str | None = None,
) -> str:
    """计算算式或 Excel 公式的结果。**只读，不修改文件。**

    两种用法：
    - 纯算式：只传 expression，例如 "1200*98 + 128250"。
    - 工作簿公式：同时传 path（sheet_name 可选，默认第一个工作表），
      expression 写 Excel 公式，例如 "=SUM(B2:D2)"、"=Q1!D2*2"。
      会按工作簿的真实单元格重算，支持区域、跨表引用和公式套公式。

    读取工具读不到公式的计算结果（cached_value 为空），需要数值时请用本工具
    计算，再用 update_cells 把数字写入表格。不要自己心算多位数运算。
    """
    try:
        if path:
            resolved = sandbox.resolve_document(path)
            structure = excel_ops.workbook_structure(resolved)
            sheets = [s["name"] for s in structure.get("sheets", [])]
            sheet = sheet_name or (sheets[0] if sheets else None)
            if not sheet:
                raise SheetNotFound("workbook has no sheets")
            if sheet not in sheets:
                raise SheetNotFound(f"workbook has no sheet named {sheet!r}")
            value = calculator.evaluate_workbook_formula(resolved, sheet, expression)
            return _ok(
                {
                    "expression": expression,
                    "sheet": sheet,
                    "value": excel_ops._jsonify(value),
                }
            )
        value = calculator.safe_arithmetic(expression)
        return _ok({"expression": expression, "value": excel_ops._jsonify(value)})
    except ToolError as exc:
        return _handle(exc)


# --------------------------------------------------------------------------- #
# write tools
# --------------------------------------------------------------------------- #
@mcp.tool(annotations=WRITE)
def update_cells(
    path: str,
    sheet_name: str,
    updates: Iterable[dict],
    expected_digest: str | None = None,
) -> str:
    """修改 Excel 单元格的值。**会写入用户的文件。**

    Args:
        path: 相对工作区的 Excel 文件路径。
        sheet_name: 工作表名称。
        updates: 形如 [{"cell": "B3", "value": 1200}, ...] 的修改列表；value 为 null 表示清空。
        expected_digest: 上次 read_range 返回的 digest，用于检测文件是否已被改动。

    返回每条修改的 before/after，可直接用于向用户展示变更预览。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.update_cells(
                resolved, sheet_name, updates, expected_digest=expected_digest
            )
        )
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=WRITE)
def set_formula(
    path: str,
    sheet_name: str,
    cell: str,
    formula: str,
    expected_digest: str | None = None,
) -> str:
    """向 Excel 单元格写入公式。**会写入用户的文件。**

    formula 可带或不带开头的 "="，例如 "SUM(B2:B10)"。

    写入的公式不携带计算结果（openpyxl 限制）：不做重算的查看器里该单元格会
    显示为空，Excel/WPS 打开时才会重算出值。用户要求"算出来/填上/合计多少"
    这类要看到数字的场景，请先用 calculate 工具算出数值，再用 update_cells
    写入；仅当用户明确要"用公式"时才使用本工具。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.set_formula(
                resolved,
                sheet_name,
                cell,
                formula,
                expected_digest=expected_digest,
            )
        )
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=WRITE)
def insert_rows(
    path: str,
    sheet_name: str,
    start_row: int,
    count: int = 1,
    expected_digest: str | None = None,
) -> str:
    """在 Excel 指定行位置**之前**插入空行，原有行整体下移。**会写入用户的文件。**

    start_row 从 1 开始计数，count 为插入行数。
    向表格**末尾追加数据**不需要本工具：直接用 update_cells 写入最后一行数据的
    下一行即可，先插空行再写会把已有行挤到错误的位置。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.insert_rows(
                resolved, sheet_name, start_row, count, expected_digest=expected_digest
            )
        )
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=WRITE)
def delete_rows(
    path: str,
    sheet_name: str,
    start_row: int,
    count: int = 1,
    expected_digest: str | None = None,
) -> str:
    """删除 Excel 中的若干行。**会写入用户的文件，属不可逆操作。**

    start_row 从 1 开始计数，count 为删除行数。使用前必须先向用户确认。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.delete_rows(
                resolved, sheet_name, start_row, count, expected_digest=expected_digest
            )
        )
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=WRITE)
def format_range(
    path: str,
    sheet_name: str,
    start_cell: str,
    end_cell: str | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    font_color: str | None = None,
    fill_color: str | None = None,
    number_format: str | None = None,
    horizontal_alignment: str | None = None,
) -> str:
    """设置 Excel 区域的基础格式（粗体、斜体、字体色、填充色、数字格式、水平对齐）。

    **会写入用户的文件。** 颜色使用不带 "#" 的十六进制，如 "FF0000"。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.format_range(
                resolved,
                sheet_name,
                start_cell,
                end_cell,
                bold=bold,
                italic=italic,
                font_color=font_color,
                fill_color=fill_color,
                number_format=number_format,
                horizontal_alignment=horizontal_alignment,
            )
        )
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=WRITE)
def replace_text(
    path: str,
    find: str,
    replace: str,
    include_tables: bool = True,
) -> str:
    """在 Word 文档中查找并替换文本。**会写入用户的文件。**

    支持跨 run 的匹配（Word 常把一句话拆成多个 run，普通替换会漏掉）。
    默认同时处理正文段落与表格单元格。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            word_ops.replace_text(
                resolved, find, replace, include_tables=include_tables
            )
        )
    except ToolError as exc:
        return _handle(exc)


@mcp.tool(annotations=WRITE)
def update_table_cell(
    path: str,
    table_index: int,
    row: int,
    column: int,
    value: str,
) -> str:
    """修改 Word 文档中某个表格单元格的文本。**会写入用户的文件。**

    row 与 column 均从 0 开始计数。使用 get_doc_structure 或 read_table 确认坐标。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            word_ops.update_table_cell(resolved, table_index, row, column, value)
        )
    except ToolError as exc:
        return _handle(exc)


def main() -> None:
    """Entry point used both by the console script and the host application."""
    roots = sandbox.configured_roots()
    logger.info(
        "starting mcp-office-server, workspace roots: %s",
        ", ".join(str(root) for root in roots),
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
