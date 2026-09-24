"""Pure argument helpers for the approval-gated write tools.

The approval loop itself (proposal rows, apply/reject/revert, backup/restore,
coordinate rebasing, chained digest refresh) moved to backend-java
(``OperationService`` et al.) during the Java-backend migration; what remains
here are the pure functions the *agent* needs while proposing a write — path
extraction, sandbox-root path rewriting, and the one-line Chinese summary that
rides the proposal frame to Java. ``OperationSummaries.java`` mirrors
``summarize_operation`` verbatim; the two must stay in sync.
"""

from __future__ import annotations

from typing import Any

PATH_ARGUMENTS = ("path", "filepath", "file_path")


class OperationError(RuntimeError):
    """Raised when an operation cannot be created or its arguments are invalid."""


def extract_target_path(arguments: dict[str, Any]) -> str:
    """Pull the target file path out of a tool's argument payload."""
    for key in PATH_ARGUMENTS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise OperationError(
        f"tool arguments contain no path; expected one of {', '.join(PATH_ARGUMENTS)}"
    )


def workspace_relative_path(workspace_id: str, rel_path: str) -> str:
    """Prefix a workspace-relative path with the workspace id.

    The MCP sandbox root is the shared ``workspaces`` directory, so tools address
    files as ``<workspace_id>/<file>`` and one server process serves every workspace.

    The model often copies a path straight out of ``list_files`` output, which may
    already carry the prefix (or arrive as ``<workspace_id>/<workspace_id>/file``
    after a previous rewrite), so an existing prefix is never added twice.
    """
    normalised = rel_path.replace("\\", "/").lstrip("/")
    prefix = f"{workspace_id}/"
    while normalised.startswith(prefix):
        normalised = normalised[len(prefix) :]
    return f"{workspace_id}/{normalised}"


def build_tool_arguments(workspace_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Rewrite path-bearing arguments to be relative to the sandbox root."""
    rewritten = dict(arguments)
    for key in PATH_ARGUMENTS:
        if key in rewritten and isinstance(rewritten[key], str):
            rewritten[key] = workspace_relative_path(workspace_id, rewritten[key])
    return rewritten


def summarize_operation(tool_name: str, rel_path: str, arguments: dict[str, Any]) -> str:
    """Produce the one-line Chinese summary shown on the approval card."""
    if tool_name == "update_cells":
        sheet = arguments.get("sheet_name", "")
        cells = arguments.get("updates") or []
        coords = ", ".join(str(item.get("cell")) for item in cells if isinstance(item, dict))
        return f"修改 {rel_path} 工作表「{sheet}」中的单元格：{coords}"
    if tool_name == "set_formula":
        return f"向 {rel_path} 工作表「{arguments.get('sheet_name', '')}」的 {arguments.get('cell', '')} 写入公式"
    if tool_name == "insert_rows":
        return f"在 {rel_path} 工作表「{arguments.get('sheet_name', '')}」第 {arguments.get('start_row')} 行起插入 {arguments.get('count', 1)} 行"
    if tool_name == "delete_rows":
        return f"删除 {rel_path} 工作表「{arguments.get('sheet_name', '')}」第 {arguments.get('start_row')} 行起的 {arguments.get('count', 1)} 行（不可逆）"
    if tool_name == "format_range":
        return f"为 {rel_path} 的 {arguments.get('start_cell', '')}:{arguments.get('end_cell', '')} 设置格式"
    if tool_name == "insert_columns":
        return f"在 {rel_path} 工作表「{arguments.get('sheet_name', '')}」第 {arguments.get('start_col')} 列起插入 {arguments.get('count', 1)} 列"
    if tool_name == "delete_columns":
        return f"删除 {rel_path} 工作表「{arguments.get('sheet_name', '')}」第 {arguments.get('start_col')} 列起的 {arguments.get('count', 1)} 列（不可逆）"
    if tool_name == "copy_range":
        return (
            f"把 {rel_path} 的 {arguments.get('src_sheet', '')}!{arguments.get('src_range', '')} "
            f"复制到 {arguments.get('dst_sheet', '')}!{arguments.get('dst_cell', '')}"
        )
    if tool_name == "delete_range":
        return (
            f"删除 {rel_path} 工作表「{arguments.get('sheet_name', '')}」的区域 "
            f"{arguments.get('range_text', '')}（{arguments.get('shift', 'up')} 补位，不可逆）"
        )
    if tool_name == "merge_cells":
        return f"合并 {rel_path} 工作表「{arguments.get('sheet_name', '')}」的 {arguments.get('range_text', '')}"
    if tool_name == "unmerge_cells":
        return f"取消 {rel_path} 工作表「{arguments.get('sheet_name', '')}」{arguments.get('range_text', '')} 的合并"
    if tool_name == "find_replace":
        scope = f"工作表「{arguments.get('sheet_name')}」" if arguments.get("sheet_name") else "全部工作表"
        return f"在 {rel_path} {scope}中把「{arguments.get('query', '')}」替换为「{arguments.get('replacement', '')}」"
    if tool_name == "manage_sheets":
        action = str(arguments.get("action", ""))
        action_cn = {"create": "新建", "rename": "重命名", "copy": "复制", "delete": "删除"}.get(action, action)
        target = arguments.get("new_name") or arguments.get("sheet_name") or ""
        return f"{action_cn} {rel_path} 的工作表「{target}」"
    if tool_name == "replace_text":
        return f"在 {rel_path} 中把「{arguments.get('find', '')}」替换为「{arguments.get('replace', '')}」"
    if tool_name == "update_table_cell":
        return f"修改 {rel_path} 表格 {arguments.get('table_index')} 第 {arguments.get('row')} 行第 {arguments.get('column')} 列"
    return f"对 {rel_path} 执行 {tool_name}"
