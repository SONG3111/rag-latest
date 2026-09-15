"""REST and SSE endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile, status
from langgraph.errors import NodeCancelledError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from ..agent.graph import AgentEvent, WorkspaceAgent
from ..agent.intent import classify_intent
from ..config import get_settings
from ..db import get_session, session_scope
from ..llm.resilience import translate_provider_error
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
from ..services.followups import generate_followups
from ..services.memory import compact_memory, load_summary
from ..services.tracing import TraceCollector, list_runs, persist_traces, run_detail
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
    MessageFeedback,
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
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> EventSourceResponse:
    """Stream one agent turn as Server-Sent Events."""
    workspace = _get_workspace(session, workspace_id)
    client = _get_client(request)
    settings = get_settings()

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
    history_summary = load_summary(session, workspace_id)
    # One run per chat turn: the collector buffers node records in memory and a
    # background task writes them after the response, so tracing is fail-open.
    trace = TraceCollector(workspace_id)

    # Cheap classification before the loop: chitchat is answered directly (no
    # retrieval, no tools), lookup turns get write tools masked, anything
    # uncertain runs the full pipeline. classify_intent never raises.
    intent = await classify_intent(
        payload.message,
        [f"{'用户' if row.role is MessageRole.user else '助手'}: {row.content}"
         for row in history_rows[-4:]],
        settings,
    )

    async def event_stream() -> AsyncIterator[dict]:
        collected: list[str] = []
        streamed: list[str] = []
        proposals: list[dict] = []
        citations: list[dict] = []

        def persist(answer: str) -> None:
            session.add(
                Message(
                    workspace_id=workspace_id,
                    role=MessageRole.assistant,
                    content=answer,
                    citations=citations or None,
                    tool_calls=proposals or None,
                )
            )
            session.commit()

        def partial_answer() -> str:
            """Best-effort text for an interrupted turn.

            Normally ``collected`` holds the graph's authoritative done content.
            When the turn is cut short there is no done event, so the tokens that
            already streamed to the user stand in for it.
            """
            if collected:
                return compose_answer(collected, len(proposals))
            text = "".join(streamed).strip()
            return compose_answer([text] if text else [], len(proposals))

        if intent == "chitchat":
            events = agent.astream_direct(payload.message, history, trace=trace)
        else:
            events = agent.astream(
                payload.message,
                history,
                stale_files=stale_files,
                history_summary=history_summary,
                mask_writes=(intent == "query"),
                trace=trace,
            )

        try:
            # One wall-clock budget for the whole turn (tool loop included). When
            # it fires, whatever has streamed so far is persisted and labelled,
            # instead of the request hanging until the client gives up.
            async with asyncio.timeout(settings.chat_turn_timeout):
                async for event in events:
                    if event.type == "done":
                        collected.append(str(event.data.get("content", "")))
                        # The authoritative `done` is emitted after the loop: the graph's
                        # own copy is only the last model call, which is empty when the
                        # provider returns nothing after a tool call.
                        continue
                    elif event.type == "token":
                        streamed.append(str(event.data.get("text", "")))
                    elif event.type == "proposal":
                        proposals.append(event.data)
                    elif event.type == "citations":
                        citations = list(event.data.get("items") or [])
                    yield {"event": event.type, "data": json.dumps(event.to_payload(), ensure_ascii=False, default=str)}

                answer = compose_answer(collected, len(proposals))
                # Followup suggestions ride the small-model channel; best-effort,
                # emitted before `done` so the stream still ends with `done`.
                followups = await generate_followups(settings, payload.message, answer)
                if followups:
                    yield {
                        "event": "followups",
                        "data": json.dumps(
                            {"type": "followups", "items": followups},
                            ensure_ascii=False,
                        ),
                    }
                # The id is flushed before the done frame so the frontend can
                # attach feedback (点赞/点踩) to the turn without a reload.
                assistant_message = Message(
                    workspace_id=workspace_id,
                    role=MessageRole.assistant,
                    content=answer,
                    citations=citations or None,
                    tool_calls=proposals or None,
                )
                session.add(assistant_message)
                session.flush()
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "type": "done",
                            "content": answer,
                            "run_id": trace.run_id,
                            "message_id": assistant_message.id,
                        },
                        ensure_ascii=False,
                    ),
                }
                session.commit()
                # Rolling memory compaction runs after the response, off the
                # user's latency budget; a failure there must not affect the turn.
                background_tasks.add_task(_compact_memory_task, workspace_id)
        except (asyncio.CancelledError, NodeCancelledError):
            # Client went away (page closed or the frontend's 停止生成 aborted the
            # fetch) and the abort landed inside a graph node: LangGraph surfaces
            # that as NodeCancelledError, a direct abort as CancelledError. The
            # generated part is still worth keeping — the user watched it stream —
            # so persist it, then end quietly instead of leaving a cancelled-task
            # stack trace in the server log for every stop click.
            session.rollback()
            if collected or streamed or proposals:
                try:
                    persist(partial_answer() + "\n\n（已停止生成，以上为已生成的部分。）")
                except Exception:
                    session.rollback()
                    logger.exception("could not persist partial answer after disconnect")
            logger.info("chat stream cancelled by client")
        except TimeoutError:
            session.rollback()
            answer = partial_answer()
            note = "\n\n（本轮生成超时已中断，以上为已生成的部分。）"
            yield {
                "event": "notice",
                "data": json.dumps(
                    {"type": "notice", "message": "本轮生成超时，已中断并保留已生成的部分。"},
                    ensure_ascii=False,
                ),
            }
            yield {
                "event": "done",
                "data": json.dumps(
                    {"type": "done", "content": answer + note, "run_id": trace.run_id},
                    ensure_ascii=False,
                ),
            }
            persist(answer + note)
        except Exception as exc:
            session.rollback()
            logger.exception("chat stream failed")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"type": "error", "message": translate_provider_error(exc)},
                    ensure_ascii=False,
                ),
            }
        finally:
            # Trace rows are written for every outcome — success, timeout, stop,
            # error — because the failed turns are exactly the ones worth reading.
            background_tasks.add_task(_persist_traces_task, trace)

    return EventSourceResponse(event_stream())


def _persist_traces_task(trace: TraceCollector) -> None:
    """Write the run's buffered trace records, on a session of its own."""
    if not len(trace):
        return
    with session_scope() as session:
        try:
            persist_traces(session, trace)
        except Exception:
            session.rollback()
            logger.exception("trace persistence failed for run %s", trace.run_id)


@router.get("/workspaces/{workspace_id}/traces")
def list_trace_runs(
    workspace_id: str, limit: int = 20, session: Session = Depends(get_session)
) -> list[dict]:
    """Recent chat runs with node counts and total time, newest first."""
    _get_workspace(session, workspace_id)
    return list_runs(session, workspace_id, limit)


@router.get("/workspaces/{workspace_id}/traces/{run_id}")
def get_trace_run(
    workspace_id: str, run_id: str, session: Session = Depends(get_session)
) -> dict:
    """One run's full node chain — the "why did this turn answer wrongly" view."""
    _get_workspace(session, workspace_id)
    detail = run_detail(session, workspace_id, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="trace run not found")
    return {"run_id": run_id, "nodes": detail}


# --------------------------------------------------------------------------- #
# citation preview
# --------------------------------------------------------------------------- #
@router.get("/workspaces/{workspace_id}/preview")
async def preview_citation(
    workspace_id: str,
    file: str,
    location: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Read the original text window behind one citation location.

    The cited chunk's ``location`` is parsed back into a read window (a few rows
    or paragraphs of context) and fetched through the read-only MCP tools, so the
    user sees the source as it is on disk right now — not a cached snippet.
    """
    _get_workspace(session, workspace_id)
    client = _get_client(request)
    from ..services.preview import PreviewError, build_preview

    try:
        return await build_preview(client, workspace_id, file, location)
    except PreviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/workspaces/{workspace_id}/messages/{message_id}/feedback",
    response_model=MessageRead,
)
def set_message_feedback(
    workspace_id: str,
    message_id: str,
    payload: MessageFeedback,
    session: Session = Depends(get_session),
) -> Message:
    """Attach thumbs feedback to one assistant message ("none" clears it).

    The value is the data loop from docs/05 §4.5: every down vote is a candidate
    for the evaluation set, so it is stored on the exact row the user saw.
    """
    _get_workspace(session, workspace_id)
    message = session.get(Message, message_id)
    if message is None or message.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="message not found")
    message.feedback = None if payload.feedback == "none" else payload.feedback
    session.commit()
    return message


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


def _compact_memory_task(workspace_id: str) -> None:
    """Fold older conversation turns into the rolling summary, off-request."""
    with session_scope() as session:
        compact_memory(session, workspace_id, get_settings())


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
