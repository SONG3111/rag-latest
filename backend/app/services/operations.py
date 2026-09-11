"""The human-approval loop for document writes.

The MCP server is stateless: it will happily write the moment it is called. Approval
therefore lives here, between the agent's decision and the actual tool invocation.

The flow is:

1. The agent calls a destructive tool.
2. The tool call is intercepted and recorded as a ``proposed`` operation carrying the
   exact arguments and the pre-change values, so the user sees a real diff.
3. The graph interrupts and the UI shows the diff.
4. ``apply`` backs up the file and replays the call; ``reject`` marks the row and
   leaves the file untouched.

Because step 2 captures pre-change values by reading the file, and step 4 re-checks
the file digest, a concurrent edit cannot be silently overwritten.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DocumentFile, Operation, OperationStatus, Workspace
from ..mcp_client import McpOfficeClient, parse_tool_result
from .backup import backup_file, prune_backups, restore_file
from .files import IngestionError, resolve_workspace_path

logger = logging.getLogger(__name__)

PATH_ARGUMENTS = ("path", "filepath", "file_path")


class OperationError(RuntimeError):
    """Raised when an operation cannot be created, applied, or reverted."""


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
    if tool_name == "replace_text":
        return f"在 {rel_path} 中把「{arguments.get('find', '')}」替换为「{arguments.get('replace', '')}」"
    if tool_name == "update_table_cell":
        return f"修改 {rel_path} 表格 {arguments.get('table_index')} 第 {arguments.get('row')} 行第 {arguments.get('column')} 列"
    return f"对 {rel_path} 执行 {tool_name}"


def create_operation(
    session: Session,
    workspace_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    message_id: str | None = None,
    diff: list[dict] | None = None,
) -> Operation:
    """Record a proposed write without performing it."""
    rel_path = extract_target_path(arguments)
    operation = Operation(
        workspace_id=workspace_id,
        message_id=message_id,
        tool_name=tool_name,
        rel_path=rel_path,
        arguments=arguments,
        diff=diff or [],
        summary=summarize_operation(tool_name, rel_path, arguments),
        status=OperationStatus.proposed,
    )
    session.add(operation)
    session.flush()
    return operation


async def apply_operation(
    session: Session,
    operation: Operation,
    client: McpOfficeClient,
) -> dict[str, Any]:
    """Back up the target file, then perform the write for real."""
    if operation.status is not OperationStatus.proposed:
        raise OperationError(
            f"operation is already {operation.status.value}; nothing to apply"
        )

    tool = client.tool(operation.tool_name)
    if tool is None:
        operation.status = OperationStatus.failed
        operation.error = f"MCP tool '{operation.tool_name}' is not available"
        session.flush()
        raise OperationError(operation.error)

    source = resolve_workspace_path(operation.workspace_id, operation.rel_path)
    if not source.exists():
        operation.status = OperationStatus.failed
        operation.error = "the target file no longer exists"
        session.flush()
        raise OperationError(operation.error)

    backup = backup_file(operation.workspace_id, source)
    operation.backup_path = str(backup)

    arguments = build_tool_arguments(operation.workspace_id, operation.arguments)
    try:
        raw = await tool.ainvoke(arguments)
    except Exception as exc:
        operation.status = OperationStatus.failed
        operation.error = f"tool invocation failed: {exc}"
        session.flush()
        logger.exception("applying operation %s failed", operation.id)
        raise OperationError(operation.error) from exc

    payload = parse_tool_result(raw)
    if not payload.get("ok"):
        error = payload.get("error") or {}
        operation.status = OperationStatus.failed
        operation.error = str(error.get("message") or error or "unknown tool error")
        session.flush()
        raise OperationError(operation.error)

    result = payload.get("data") or {}
    operation.result = result
    operation.diff = result.get("changes") or operation.diff or []
    operation.status = OperationStatus.applied
    operation.resolved_at = datetime.now(timezone.utc)
    session.flush()

    prune_backups(operation.workspace_id)
    logger.info("applied operation %s (%s)", operation.id, operation.tool_name)
    return result


def reject_operation(session: Session, operation: Operation) -> None:
    """Mark a proposal as declined. The file is never touched."""
    if operation.status is not OperationStatus.proposed:
        raise OperationError(
            f"operation is already {operation.status.value}; nothing to reject"
        )
    operation.status = OperationStatus.rejected
    operation.resolved_at = datetime.now(timezone.utc)
    session.flush()


def revert_operation(session: Session, operation: Operation) -> None:
    """Restore the backup taken before an applied operation."""
    if operation.status is not OperationStatus.applied:
        raise OperationError("only applied operations can be reverted")
    if not operation.backup_path:
        raise OperationError("no backup was recorded for this operation")

    destination = resolve_workspace_path(operation.workspace_id, operation.rel_path)
    restore_file(operation.workspace_id, operation.backup_path, destination)

    operation.status = OperationStatus.rejected
    operation.error = "reverted to the pre-change backup"
    operation.resolved_at = datetime.now(timezone.utc)
    session.flush()
    logger.info("reverted operation %s", operation.id)


def pending_operations(session: Session, workspace_id: str) -> list[Operation]:
    return list(
        session.scalars(
            select(Operation)
            .where(
                Operation.workspace_id == workspace_id,
                Operation.status == OperationStatus.proposed,
            )
            .order_by(Operation.created_at.asc())
        )
    )


def operation_history(session: Session, workspace_id: str, limit: int = 100) -> list[Operation]:
    return list(
        session.scalars(
            select(Operation)
            .where(Operation.workspace_id == workspace_id)
            .order_by(Operation.created_at.desc())
            .limit(limit)
        )
    )


def record_identifies(operation: Operation, identifier: str) -> bool:
    return operation.id == identifier
