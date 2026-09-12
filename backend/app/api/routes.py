"""REST and SSE endpoints."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from ..agent.graph import AgentEvent, WorkspaceAgent
from ..db import get_session, session_scope
from ..models import (
    DocumentFile,
    Message,
    MessageRole,
    Operation,
    OperationStatus,
    Workspace,
)
from ..services.files import (
    IngestionError,
    delete_file,
    files_needing_index,
    index_file,
    save_upload,
)
from ..services.operations import (
    OperationError,
    apply_operation,
    operation_history,
    pending_operations,
    reject_operation,
    revert_operation,
)
from .schemas import (
    ChatRequest,
    FileRead,
    IndexingResponse,
    MessageRead,
    OperationRead,
    ToolRead,
    WorkspaceCreate,
    WorkspaceRead,
)

logger = logging.getLogger(__name__)

# How much conversation the agent sees. Only the most recent turns are loaded; older
# ones are far outside anything a follow-up question can refer to.
HISTORY_MESSAGE_LIMIT = 60

router = APIRouter(prefix="/api")


def _get_workspace(session: Session, workspace_id: str) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return workspace


def _get_client(request: Request):
    client = getattr(request.app.state, "mcp_client", None)
    if client is None or not client.started:
        raise HTTPException(
            status_code=503, detail="MCP document server is not available"
        )
    return client


# --------------------------------------------------------------------------- #
# workspaces
# --------------------------------------------------------------------------- #
@router.get("/workspaces", response_model=list[WorkspaceRead])
def list_workspaces(session: Session = Depends(get_session)) -> list[WorkspaceRead]:
    counts = dict(
        session.execute(
            select(DocumentFile.workspace_id, func.count(DocumentFile.id)).group_by(
                DocumentFile.workspace_id
            )
        ).all()
    )
    workspaces = session.scalars(select(Workspace).order_by(Workspace.created_at.desc()))
    return [
        WorkspaceRead(
            id=workspace.id,
            name=workspace.name,
            description=workspace.description,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
            file_count=counts.get(workspace.id, 0),
        )
        for workspace in workspaces
    ]


@router.post("/workspaces", response_model=WorkspaceRead, status_code=201)
def create_workspace(
    payload: WorkspaceCreate, session: Session = Depends(get_session)
) -> WorkspaceRead:
    workspace = Workspace(name=payload.name.strip(), description=payload.description)
    session.add(workspace)
    session.flush()
    from ..config import get_settings

    get_settings().workspace_dir(workspace.id).mkdir(parents=True, exist_ok=True)
    return WorkspaceRead(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        file_count=0,
    )


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(
    workspace_id: str, session: Session = Depends(get_session)
) -> WorkspaceRead:
    workspace = _get_workspace(session, workspace_id)
    count = session.scalar(
        select(func.count(DocumentFile.id)).where(
            DocumentFile.workspace_id == workspace_id
        )
    )
    return WorkspaceRead(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        file_count=int(count or 0),
    )


@router.delete("/workspaces/{workspace_id}", status_code=204)
def remove_workspace(
    workspace_id: str, session: Session = Depends(get_session)
) -> None:
    import shutil

    from ..config import get_settings
    from ..retrieval.vector_store import VectorStore

    workspace = _get_workspace(session, workspace_id)
    try:
        VectorStore().drop_collection(workspace_id)
    except Exception as exc:
        logger.warning("could not drop vector collection for %s: %s", workspace_id, exc)
    directory = get_settings().workspace_dir(workspace_id)
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    session.delete(workspace)


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
@router.get("/workspaces/{workspace_id}/files", response_model=list[FileRead])
def list_files(
    workspace_id: str, session: Session = Depends(get_session)
) -> list[DocumentFile]:
    _get_workspace(session, workspace_id)
    return list(
        session.scalars(
            select(DocumentFile)
            .where(DocumentFile.workspace_id == workspace_id)
            .order_by(DocumentFile.created_at.asc())
        )
    )


@router.post(
    "/workspaces/{workspace_id}/files",
    response_model=list[IndexingResponse],
    status_code=201,
)
async def upload_files(
    workspace_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
) -> list[IndexingResponse]:
    _get_workspace(session, workspace_id)
    results: list[IndexingResponse] = []

    for upload in files:
        content = await upload.read()
        try:
            record = save_upload(session, _get_workspace(session, workspace_id), upload.filename or "", content)
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = index_file(session, workspace_id, record)
        results.append(
            IndexingResponse(
                file_id=result.file_id,
                rel_path=result.rel_path,
                chunk_count=result.chunk_count,
                vector_count=result.vector_count,
                status=result.status.value,
                error=result.error,
            )
        )
    return results


@router.post(
    "/workspaces/{workspace_id}/files/{file_id}/reindex",
    response_model=IndexingResponse,
)
def reindex_file(
    workspace_id: str, file_id: str, session: Session = Depends(get_session)
) -> IndexingResponse:
    _get_workspace(session, workspace_id)
    record = session.get(DocumentFile, file_id)
    if record is None or record.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="file not found")
    result = index_file(session, workspace_id, record)
    return IndexingResponse(
        file_id=result.file_id,
        rel_path=result.rel_path,
        chunk_count=result.chunk_count,
        vector_count=result.vector_count,
        status=result.status.value,
        error=result.error,
    )


@router.post(
    "/workspaces/{workspace_id}/reindex",
    response_model=list[IndexingResponse],
)
def reindex_workspace(
    workspace_id: str, session: Session = Depends(get_session)
) -> list[IndexingResponse]:
    """Rebuild every document's index in a workspace.

    Needed after a chunking or model change: chunk rows and the dense collection are
    both derived data, so the only safe migration is to rebuild them from the source
    files rather than to patch rows in place.
    """
    _get_workspace(session, workspace_id)
    records = list(
        session.scalars(
            select(DocumentFile)
            .where(DocumentFile.workspace_id == workspace_id)
            .order_by(DocumentFile.created_at.asc())
        )
    )

    results: list[IndexingResponse] = []
    for record in records:
        result = index_file(session, workspace_id, record)
        results.append(
            IndexingResponse(
                file_id=result.file_id,
                rel_path=result.rel_path,
                chunk_count=result.chunk_count,
                vector_count=result.vector_count,
                status=result.status.value,
                error=result.error,
            )
        )
    return results


@router.delete("/workspaces/{workspace_id}/files/{file_id}", status_code=204)
def remove_file(
    workspace_id: str, file_id: str, session: Session = Depends(get_session)
) -> None:
    _get_workspace(session, workspace_id)
    record = session.get(DocumentFile, file_id)
    if record is None or record.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="file not found")
    delete_file(session, workspace_id, record)


@router.get("/workspaces/{workspace_id}/files/{file_id}/download")
def download_file(
    workspace_id: str, file_id: str, session: Session = Depends(get_session)
):
    from fastapi.responses import FileResponse

    from ..services.files import resolve_workspace_path

    _get_workspace(session, workspace_id)
    record = session.get(DocumentFile, file_id)
    if record is None or record.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="file not found")
    path = resolve_workspace_path(workspace_id, record.rel_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="file is missing from disk")
    return FileResponse(path, filename=record.rel_path)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@router.get("/workspaces/{workspace_id}/tools", response_model=list[ToolRead])
def list_tools(workspace_id: str, request: Request) -> list[ToolRead]:
    client = _get_client(request)
    return [
        ToolRead(
            name=profile.name,
            description=profile.description,
            read_only=profile.read_only,
            destructive=profile.destructive,
            requires_approval=profile.requires_approval,
            schema=profile.schema,
        )
        for profile in sorted(client.profiles().values(), key=lambda item: item.name)
    ]


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #
@router.get("/workspaces/{workspace_id}/messages", response_model=list[MessageRead])
def list_messages(
    workspace_id: str, limit: int = 200, session: Session = Depends(get_session)
) -> list[Message]:
    _get_workspace(session, workspace_id)
    # Newest ``limit`` messages, returned oldest-first: the UI shows a conversation,
    # so a long history must not push the recent turns out of the response.
    return _recent_messages(session, workspace_id, limit)


EMPTY_ANSWER_FALLBACK = "模型这次没有返回内容，请再说一次。"


def compose_answer(collected: list[str], proposal_count: int) -> str:
    """Assemble the text persisted for an assistant turn.

    A provider occasionally ends a turn with an empty completion after a tool call.
    Persisting that verbatim leaves an empty bubble in the UI with no way to tell it
    apart from a broken app, so an empty turn gets an explicit placeholder.
    """
    answer = "\n\n".join(text for text in collected if text.strip())
    if proposal_count:
        from ..agent.prompts import PROPOSAL_NOTICE

        answer = (answer + "\n\n" if answer else "") + PROPOSAL_NOTICE.format(
            count=proposal_count
        )
    return answer or EMPTY_ANSWER_FALLBACK


def _recent_messages(session: Session, workspace_id: str, limit: int) -> list[Message]:
    """The newest ``limit`` messages for a workspace, in chronological order.

    ``ORDER BY created_at ASC LIMIT n`` returns the *oldest* n rows, which silently
    pinned long conversations to their opening minutes: the model never saw the latest
    turns, so it kept answering from context that was half an hour out of date.
    """
    rows = list(
        session.scalars(
            select(Message)
            .where(Message.workspace_id == workspace_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    return rows


@router.post("/workspaces/{workspace_id}/chat/stream")
async def chat_stream(
    workspace_id: str,
    payload: ChatRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> EventSourceResponse:
    """Stream one agent turn as Server-Sent Events."""
    workspace = _get_workspace(session, workspace_id)
    client = _get_client(request)

    history_rows = _recent_messages(session, workspace_id, HISTORY_MESSAGE_LIMIT)

    user_message = Message(
        workspace_id=workspace_id,
        role=MessageRole.user,
        content=payload.message,
    )
    session.add(user_message)
    session.flush()

    agent = WorkspaceAgent(session, workspace, client)
    history = _to_langchain_history(history_rows)
    stale_files = _files_changed_since_last_turn(session, workspace_id, history_rows)

    async def event_stream() -> AsyncIterator[dict]:
        collected: list[str] = []
        proposals: list[dict] = []
        citations: list[dict] = []

        try:
            async for event in agent.astream(
                payload.message, history, stale_files=stale_files
            ):
                if event.type == "done":
                    collected.append(str(event.data.get("content", "")))
                    # The authoritative `done` is emitted after the loop: the graph's
                    # own copy is only the last model call, which is empty when the
                    # provider returns nothing after a tool call.
                    continue
                elif event.type == "proposal":
                    proposals.append(event.data)
                elif event.type == "citations":
                    citations = list(event.data.get("items") or [])
                yield {"event": event.type, "data": json.dumps(event.to_payload(), ensure_ascii=False, default=str)}

            answer = compose_answer(collected, len(proposals))
            yield {
                "event": "done",
                "data": json.dumps(
                    {"type": "done", "content": answer}, ensure_ascii=False
                ),
            }

            assistant_message = Message(
                workspace_id=workspace_id,
                role=MessageRole.assistant,
                content=answer,
                citations=citations or None,
                tool_calls=proposals or None,
            )
            session.add(assistant_message)
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.exception("chat stream failed")
            yield {
                "event": "error",
                "data": json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False),
            }

    return EventSourceResponse(event_stream())


def _to_langchain_history(rows: list[Message]) -> list:
    """Rebuild the conversational context passed to the graph.

    Only user and assistant text turns are replayed. Tool messages are not persisted,
    so the history never contains a tool call without its matching tool result, which
    would make the provider reject the request.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    history: list = []
    for row in rows:
        content = (row.content or "").strip()
        if not content:
            continue
        if row.role is MessageRole.user:
            history.append(HumanMessage(content=content))
        elif row.role is MessageRole.assistant:
            history.append(AIMessage(content=content))
    return history[-20:]


def _files_changed_since_last_turn(
    session: Session, workspace_id: str, rows: list[Message]
) -> list[tuple[str, str]]:
    """Which workspace files changed after the last stored message, and when.

    Earlier answers are only unsafe to reuse once the files behind them changed; the
    caller turns this into a notice for the current turn. With no conversation yet every
    file counts as changed, so the first answer is always grounded in a fresh read.
    """
    records = list(
        session.scalars(
            select(DocumentFile).where(DocumentFile.workspace_id == workspace_id)
        )
    )
    if not records:
        return []

    from ..services.files import resolve_workspace_path

    cutoff = max((_as_naive(row.created_at) for row in rows), default=None)
    changed: list[tuple[str, str]] = []
    for record in records:
        moments = [
            _as_naive(record.indexed_at),
            _as_naive(record.created_at),
        ]
        # An edit made outside the app still invalidates earlier answers.
        path = resolve_workspace_path(workspace_id, record.rel_path)
        if path.exists():
            # Stored timestamps are UTC-naive; a local-time mtime would look newer than
            # everything and mark every file as changed on every turn.
            moments.append(
                datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(
                    tzinfo=None
                )
            )
        latest = max((moment for moment in moments if moment), default=None)
        if latest is None:
            continue
        if cutoff is None or latest > cutoff:
            changed.append((record.rel_path, latest.strftime("%Y-%m-%d %H:%M:%S")))
    return changed


def _as_naive(moment: datetime | None) -> datetime | None:
    """SQLite stores naive timestamps; compare them on one footing."""
    if moment is None:
        return None
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
@router.get(
    "/workspaces/{workspace_id}/operations", response_model=list[OperationRead]
)
def list_operations(
    workspace_id: str,
    status_filter: str | None = None,
    session: Session = Depends(get_session),
) -> list[Operation]:
    _get_workspace(session, workspace_id)
    if status_filter == "proposed":
        return pending_operations(session, workspace_id)
    return operation_history(session, workspace_id)


@router.post(
    "/workspaces/{workspace_id}/operations/{operation_id}/apply",
    response_model=OperationRead,
)
async def apply(
    workspace_id: str,
    operation_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Operation:
    _get_workspace(session, workspace_id)
    client = _get_client(request)
    operation = _load_operation(session, workspace_id, operation_id)
    try:
        await apply_operation(session, operation, client)
    except OperationError as exc:
        # Persist the failed state before the dependency's rollback wipes it,
        # so the operation history shows what actually happened.
        session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    # The file changed on disk, so its chunks are now stale. Re-embedding runs
    # in the background: it costs seconds of CPU on the local model, and making
    # the confirmation click wait for it delayed the "已应用" feedback badly.
    background_tasks.add_task(_reindex_after_write_task, workspace_id, operation.rel_path)
    return operation


@router.post(
    "/workspaces/{workspace_id}/operations/{operation_id}/reject",
    response_model=OperationRead,
)
def reject(
    workspace_id: str, operation_id: str, session: Session = Depends(get_session)
) -> Operation:
    _get_workspace(session, workspace_id)
    operation = _load_operation(session, workspace_id, operation_id)
    try:
        reject_operation(session, operation)
    except OperationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return operation


@router.post(
    "/workspaces/{workspace_id}/operations/{operation_id}/revert",
    response_model=OperationRead,
)
def revert(
    workspace_id: str,
    operation_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Operation:
    _get_workspace(session, workspace_id)
    operation = _load_operation(session, workspace_id, operation_id)
    try:
        revert_operation(session, operation)
    except OperationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(_reindex_after_write_task, workspace_id, operation.rel_path)
    return operation


def _reindex_after_write_task(workspace_id: str, rel_path: str) -> None:
    """Reindex in the request's background, on a session of its own."""
    with session_scope() as session:
        _reindex_after_write(session, workspace_id, rel_path)


def _load_operation(session: Session, workspace_id: str, operation_id: str) -> Operation:
    operation = session.get(Operation, operation_id)
    if operation is None or operation.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="operation not found")
    return operation


def _reindex_after_write(session: Session, workspace_id: str, rel_path: str) -> None:
    """Refresh the knowledge base for a file the agent just modified."""
    record = session.scalar(
        select(DocumentFile).where(
            DocumentFile.workspace_id == workspace_id,
            DocumentFile.rel_path == rel_path,
        )
    )
    if record is None:
        return
    try:
        index_file(session, workspace_id, record)
    except Exception as exc:
        logger.warning("reindex after write failed for %s: %s", rel_path, exc)
