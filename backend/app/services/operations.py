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
import re
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

_CELL_RE = re.compile(r"^([A-Za-z]{1,3})(\d+)$")


def _shift_formula_text(formula: str, *, sheet_name: Any, kind: str, start: int, count: int) -> str:
    """Rewrite the A1 references inside a not-yet-written formula string.

    A pending ``set_formula`` was authored against the pre-shift layout, so the
    references it makes must follow the same row movement as its target cell.
    This reuses the MCP server's own shifter — the exact logic that keeps
    already-written formulas correct — instead of a second implementation.
    """
    from mcp_office_server.formula_shift import shift_formula_references

    sheet = str(sheet_name or "")
    op = "delete" if kind == "delete" else "insert"
    return shift_formula_references(
        formula, own_sheet=sheet, affected_sheet=sheet, op=op, start_row=start, count=count
    )


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
    if operation.status not in (OperationStatus.proposed, OperationStatus.failed):
        raise OperationError(
            f"operation is already {operation.status.value}; nothing to apply"
        )
    # A failed apply may be retried — the usual causes (file locked by Excel, a
    # conflict that another edit has since resolved) are transient, and the
    # digest check below still guards the retry against a changed file.

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
    operation.error = None
    operation.resolved_at = datetime.now(timezone.utc)

    # Rebase planning re-reads cell values through the MCP server; it must run
    # before the first DB write so this request never holds the SQLite write
    # lock across a network round-trip. The plan is persisted before the digest
    # chain refresh so the refresh lands on the rebased arguments last — the
    # digest must be the newest value in the args, never a stale one restored
    # by the plan. One short flush then persists everything.
    plan = await _plan_rebase(session, operation, client)
    _apply_rebase_plan(plan)
    _refresh_chained_digests(session, operation, result.get("digest"))
    session.flush()
    prune_backups(operation.workspace_id)
    logger.info("applied operation %s (%s)", operation.id, operation.tool_name)
    return result


def _refresh_chained_digests(
    session: Session, applied: Operation, new_digest: Any
) -> None:
    """Point same-file follow-up proposals at the file's new digest.

    Proposals written in one turn share the digest captured at proposal time,
    so applying the first one invalidates every later proposal on that file and
    the user's second confirmation fails with a write conflict. The follow-ups
    were approved against the pre-edit state on purpose, so their lock moves to
    the digest the applied write just produced. An external edit that happens
    after this refresh still conflicts, which is the race the lock is for.
    """
    if not isinstance(new_digest, str) or not new_digest:
        return
    for other in pending_operations(session, applied.workspace_id):
        if other.id == applied.id or other.rel_path != applied.rel_path:
            continue
        if other.arguments.get("expected_digest"):
            other.arguments = {**other.arguments, "expected_digest": new_digest}


def _rebase_arguments(
    tool_name: str,
    args: dict[str, Any],
    *,
    kind: str,
    start: int,
    count: int,
) -> tuple[dict[str, Any], bool] | None:
    """Shift a pending proposal's row coordinates past an applied row change.

    ``kind="delete"`` means ``count`` rows were removed starting at ``start``;
    ``kind="insert"`` means ``count`` rows were added at ``start``. Returns the
    updated arguments plus whether anything moved, or None when the proposal's
    own target no longer exists and it must be discarded rather than relocated.
    """
    dead = False
    moved = False

    def row_after(row: int) -> int | None:
        if kind == "delete":
            if start <= row < start + count:
                return None
            return row - count if row >= start + count else row
        return row + count if row >= start else row

    def shift_coordinate(value: Any) -> Any:
        nonlocal dead, moved
        match = _CELL_RE.match(str(value or "").strip())
        if not match:
            return value
        row = row_after(int(match.group(2)))
        if row is None:
            dead = True
            return value
        shifted = f"{match.group(1)}{row}"
        if shifted != str(value).strip():
            moved = True
        return shifted

    def shift_value(value: Any) -> Any:
        nonlocal moved
        if isinstance(value, dict) and set(value) == {"formula"} and isinstance(value["formula"], str):
            # Models sometimes wrap formulas as {"formula": "=A1*2"}; the MCP
            # layer unwraps that at write time, so shift the inner reference.
            shifted = _shift_formula_text(
                value["formula"], sheet_name=args.get("sheet_name"), kind=kind, start=start, count=count
            )
            if shifted != value["formula"]:
                moved = True
            return {**value, "formula": shifted}
        if isinstance(value, str) and value.startswith("="):
            shifted = _shift_formula_text(
                value, sheet_name=args.get("sheet_name"), kind=kind, start=start, count=count
            )
            if shifted != value:
                moved = True
            return shifted
        return value

    if tool_name in ("delete_rows", "insert_rows"):
        try:
            band_start = int(args.get("start_row"))
        except (TypeError, ValueError):
            return args, False
        band_count = int(args.get("count", 1) or 1)
        if kind == "delete":
            if band_start >= start + count:
                return {**args, "start_row": band_start - count}, True
            if tool_name == "insert_rows" and band_start >= start:
                # The insertion point collapsed into the deleted band; the
                # closest legal position is the band start itself.
                return {**args, "start_row": start}, True
            if band_start + band_count <= start:
                return args, False
            return None  # the pending band overlaps what was already deleted
        # kind == "insert"
        if tool_name == "delete_rows" and band_start < start < band_start + band_count:
            return None  # rows landed inside the pending band; it is no longer contiguous
        if band_start >= start:
            return {**args, "start_row": band_start + count}, True
        return args, False

    if tool_name == "update_cells":
        updates = args.get("updates")
        if isinstance(updates, list):
            shifted = []
            for item in updates:
                entry = dict(item) if isinstance(item, dict) else item
                if isinstance(entry, dict) and "cell" in entry:
                    entry["cell"] = shift_coordinate(entry.get("cell"))
                    entry["value"] = shift_value(entry.get("value"))
                shifted.append(entry)
            if dead:
                return None
            return {**args, "updates": shifted}, moved
        return args, False

    if tool_name == "set_formula":
        cell = shift_coordinate(args.get("cell"))
        formula = shift_value(args.get("formula"))
        if dead:
            return None
        return {**args, "cell": cell, "formula": formula}, moved

    if tool_name == "format_range":
        start_cell = shift_coordinate(args.get("start_cell"))
        end_cell = shift_coordinate(args.get("end_cell"))
        if dead:
            return None
        return {**args, "start_cell": start_cell, "end_cell": end_cell}, moved

    # Word tools carry no spreadsheet row coordinates; nothing to move.
    return args, False


async def _refresh_diff_before(
    client: McpOfficeClient,
    workspace_id: str,
    operation: Operation,
    args: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rebuild a rebased proposal's diff from the current file.

    The diff captured at proposal time describes rows that have since moved;
    the approval card should show what is actually in the targeted cells now,
    keyed by the rebased coordinates in ``args``. Network only — the caller
    persists the result, so this never runs inside a write transaction.
    """
    updates = (args or {}).get("updates")
    if not isinstance(updates, list) or not updates:
        return operation.diff or []
    tool = client.tool("read_range")
    if tool is None:
        return operation.diff or []
    sheet = (args or {}).get("sheet_name")
    path = workspace_relative_path(workspace_id, operation.rel_path)
    refreshed: list[dict[str, Any]] = []
    for update in updates:
        cell = str(update.get("cell", "")).strip() if isinstance(update, dict) else ""
        after = update.get("value") if isinstance(update, dict) else None
        before: Any = None
        if cell:
            try:
                payload = parse_tool_result(
                    await tool.ainvoke(
                        {"path": path, "sheet_name": sheet, "start_cell": cell}
                    )
                )
                values = ((payload.get("data") or {}).get("values") or [[None]])[0][0]
                before = values if not isinstance(values, dict) else values.get("cached_value")
            except Exception as exc:
                logger.warning("diff re-read failed for %s!%s: %s", path, cell, exc)
        refreshed.append({"cell": cell, "before": before, "after": after})
    return refreshed


async def _plan_rebase(
    session: Session, applied: Operation, client: McpOfficeClient
) -> list[tuple[Operation, dict[str, Any] | None, list[dict[str, Any]] | None]]:
    """Compute every pending proposal's post-apply shape before any DB write.

    Proposals created in one turn share absolute row numbers taken from the
    same file snapshot. Applying one of them shifts the rows below it, so a
    follow-up approved afterwards would otherwise write to — or delete —
    whatever now occupies the old coordinates: an append aimed at row 17 lands
    four rows below the data once four rows above it are gone. Every
    successful apply therefore rebases the still-pending proposals on the same
    sheet by the net row movement of the write that just happened, so the
    batch converges to the intended result whichever order the user approves
    them in. A proposal whose own target row was deleted cannot be relocated;
    the plan marks it ``None`` and the apply pass rejects it with an
    explanation instead of writing to the wrong row.

    Returns ``(operation, new_arguments, diff)`` tuples; ``new_arguments`` is
    None for proposals that must be rejected, and unchanged proposals are left
    out entirely. Network reads happen here; ``_apply_rebase_plan`` persists.
    """
    if applied.tool_name not in ("delete_rows", "insert_rows"):
        return []
    applied_args = applied.arguments or {}
    try:
        start = int(applied_args.get("start_row"))
        count = int(applied_args.get("count", 1) or 1)
    except (TypeError, ValueError):
        return []
    kind = "delete" if applied.tool_name == "delete_rows" else "insert"
    sheet_name = applied_args.get("sheet_name")

    plan: list[tuple[Operation, dict[str, Any] | None, list[dict[str, Any]] | None]] = []
    for other in pending_operations(session, applied.workspace_id):
        if other.id == applied.id or other.rel_path != applied.rel_path:
            continue
        args = other.arguments or {}
        if args.get("sheet_name") != sheet_name:
            continue
        rebased = _rebase_arguments(
            other.tool_name, args, kind=kind, start=start, count=count
        )
        if rebased is None:
            plan.append((other, None, None))
            continue
        new_args, moved = rebased
        if not moved:
            continue
        diff = None
        if other.tool_name == "update_cells":
            diff = await _refresh_diff_before(client, applied.workspace_id, other, new_args)
        plan.append((other, new_args, diff))
    return plan


def _apply_rebase_plan(
    plan: list[tuple[Operation, dict[str, Any] | None, list[dict[str, Any]] | None]],
) -> None:
    """Persist a planned rebase: attribute writes only, no network, no queries."""
    for other, new_args, diff in plan:
        if new_args is None:
            other.status = OperationStatus.rejected
            other.error = (
                "提案的目标行已随先前应用的删除操作一起消失，无法自动重定位，已自动驳回；"
                "请让模型基于最新文件重新提交提案"
            )
            other.resolved_at = datetime.now(timezone.utc)
            continue
        other.arguments = new_args
        other.summary = summarize_operation(other.tool_name, other.rel_path, new_args)
        if diff is not None:
            other.diff = diff


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
