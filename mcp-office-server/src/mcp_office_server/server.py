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
    # allow_nan=False keeps the envelope strict JSON: a stray Infinity would be
    # unparseable for standard JSON clients (it would raise here, but that
    # happens inside the tool's try-block and lands in the envelope backstop).
    return json.dumps({"ok": True, "data": payload}, ensure_ascii=False, default=str, allow_nan=False)


def _handle(exc: ToolError) -> str:
    return json.dumps(error_payload(exc), ensure_ascii=False, default=str, allow_nan=False)


def _unexpected(exc: Exception) -> str:
    """Envelope backstop for failures no tool expected to raise.

    The contract is that tools answer with the structured envelope instead of
    raising. Layering mirrors the upstream excel-mcp-server (domain errors
    handled per tool, everything else logged), but instead of re-raising we
    keep the surprise inside the envelope so the agent always gets parseable,
    actionable output.
    """
    logger.exception("unexpected tool failure")
    return json.dumps(
        error_payload(ToolError(f"unexpected failure: {exc}", detail=type(exc).__name__)),
        ensure_ascii=False,
        default=str,
        allow_nan=False,
    )


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=READ_ONLY)
def get_doc_structure(path: str) -> str:
    """查看一个文档的整体结构。

    Excel：返回每个 sheet 的名称、行列数、首行表头，以及合并单元格区域
    （merged_ranges，表头语义的信号）。Word：返回段落总数、表格数量、
    标题大纲（含段落下标）以及每张表的表头。

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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
        updates: 形如 [{"cell": "B3", "value": 1200}, ...] 的修改列表；value 为 null 或
            空字符串表示清空（纯空格是有效内容，按原样写入）。
            注意：以 "=" 开头的字符串会按 Excel 语义存为公式（与手工粘贴一致）；
            单元格最长 32767 字符（Excel 上限），超长会被拒绝而不是截断。
        expected_digest: 上次 read_range 返回的 digest，用于检测文件是否已被改动。

    返回每条修改的 before/after，可直接用于向用户展示变更预览。
    写入合并单元格区域时只允许写左上角格，其余位置会被拒绝并提示。
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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=WRITE)
def insert_columns(
    path: str,
    sheet_name: str,
    start_col: int,
    count: int = 1,
    expected_digest: str | None = None,
) -> str:
    """在 Excel 指定列位置**之前**插入空列，原有列整体右移，公式引用自动重写。**会写入用户的文件。**

    start_col 从 1 开始计数（A=1），count 为插入列数。
    向表格最右侧追加数据不需要本工具：直接用 update_cells 写入即可。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.insert_columns(
                resolved, sheet_name, start_col, count, expected_digest=expected_digest
            )
        )
    except ToolError as exc:
        return _handle(exc)
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=WRITE)
def delete_columns(
    path: str,
    sheet_name: str,
    start_col: int,
    count: int = 1,
    expected_digest: str | None = None,
) -> str:
    """删除 Excel 中的若干整列，右侧列左移，公式引用自动重写。**会写入用户的文件，属不可逆操作。**

    start_col 从 1 开始计数（A=1），count 为删除列数。使用前必须先向用户确认。
    只想清空某几列的值而不移动表格时，请改用 update_cells（value 传 null）。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.delete_columns(
                resolved, sheet_name, start_col, count, expected_digest=expected_digest
            )
        )
    except ToolError as exc:
        return _handle(exc)
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=WRITE)
def copy_range(
    path: str,
    src_sheet: str,
    src_range: str,
    dst_sheet: str,
    dst_cell: str,
    expected_digest: str | None = None,
) -> str:
    """整块复制 Excel 区域（值、格式、公式）到目标位置。**会写入用户的文件。**

    Args:
        src_range: 源区域，如 "A1:C10"。
        dst_cell: 目标左上角单元格（不是区域）。

    公式的相对引用会按 Excel 语义平移到新位置（绝对引用 $A$1 不变），与手工
    复制粘贴一致。改个别单元格的值用 update_cells；复制整块数据（如把明细复制
    到汇总表）用本工具。源或目标区域碰到合并单元格会报错，需先 unmerge_cells。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.copy_range(
                resolved, src_sheet, src_range, dst_sheet, dst_cell,
                expected_digest=expected_digest,
            )
        )
    except ToolError as exc:
        return _handle(exc)
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=WRITE)
def delete_range(
    path: str,
    sheet_name: str,
    range_text: str,
    shift: str = "up",
    expected_digest: str | None = None,
) -> str:
    """删除 Excel 矩形区域并让相邻内容补位。**会写入用户的文件，属不可逆操作。**

    shift="up" 时下方内容上移补位；shift="left" 时右侧内容左移补位。
    使用前必须先向用户确认。与 delete_rows/delete_columns 的分工：
    删整行/整列用它们；只删一块区域（保留周围结构）用本工具；
    只清值不移动用 update_cells（value 传 null）。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.delete_range(
                resolved, sheet_name, range_text, shift, expected_digest=expected_digest
            )
        )
    except ToolError as exc:
        return _handle(exc)
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=WRITE)
def merge_cells(
    path: str,
    sheet_name: str,
    range_text: str,
    expected_digest: str | None = None,
) -> str:
    """合并 Excel 单元格区域（如跨列标题）。**会写入用户的文件。**

    与 Excel 一致：只保留左上角单元格的值，其余值会被丢弃。重复合并同一区域
    是无害的幂等操作。取消合并用 unmerge_cells（需传完整的合并区域）。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.merge_cells(resolved, sheet_name, range_text, expected_digest=expected_digest)
        )
    except ToolError as exc:
        return _handle(exc)
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=WRITE)
def unmerge_cells(
    path: str,
    sheet_name: str,
    range_text: str,
    expected_digest: str | None = None,
) -> str:
    """取消 Excel 合并单元格。**会写入用户的文件。**

    range_text 必须与现有合并区域完全一致（可用 get_doc_structure 返回的
    merged_ranges 查询），只选中合并区域的一部分无法取消合并。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.unmerge_cells(resolved, sheet_name, range_text, expected_digest=expected_digest)
        )
    except ToolError as exc:
        return _handle(exc)
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=WRITE)
def find_replace(
    path: str,
    query: str,
    replacement: str,
    sheet_name: str | None = None,
    include_formulas: bool = False,
    expected_digest: str | None = None,
) -> str:
    """在 Excel 中按内容批量查找替换文本，返回每个被改动的单元格。**会写入用户的文件。**

    Args:
        query: 要查找的文本（大小写不敏感）。
        replacement: 替换后的文本。
        sheet_name: 限定工作表；留空则替换所有工作表。
        include_formulas: 是否同时替换公式文本中的匹配（默认 false，只处理文本单元格）。

    匹配语义与 find_text 一致。已经知道确切坐标时用 update_cells；
    改名、口径调整这类"同一处文本出现在多处"的场景用本工具。
    数字和日期单元格不会被改动。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.find_replace(
                resolved, query, replacement,
                sheet_name=sheet_name, include_formulas=include_formulas,
                expected_digest=expected_digest,
            )
        )
    except ToolError as exc:
        return _handle(exc)
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


@mcp.tool(annotations=WRITE)
def manage_sheets(
    path: str,
    action: str,
    sheet_name: str | None = None,
    new_name: str | None = None,
    expected_digest: str | None = None,
) -> str:
    """对 Excel 工作表本身执行新建/重命名/复制/删除。**会写入用户的文件，删除不可逆。**

    Args:
        action: create（新建，需 new_name）/ rename（重命名，需 sheet_name + new_name）/
            copy（复制为副本，需 sheet_name + new_name）/ delete（删除，需 sheet_name）。

    单元格内容的修改请用其他写入工具，本工具只处理工作表层级。
    重命名或删除工作表后，其他公式中指向旧表名的引用不会自动改写，
    返回结果会列出受影响的公式位置。
    """
    try:
        resolved = sandbox.resolve_document(path)
        return _ok(
            excel_ops.manage_sheets(
                resolved, action, sheet_name, new_name=new_name,
                expected_digest=expected_digest,
            )
        )
    except ToolError as exc:
        return _handle(exc)
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
    except Exception as exc:  # envelope backstop — see _unexpected
        return _unexpected(exc)


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
