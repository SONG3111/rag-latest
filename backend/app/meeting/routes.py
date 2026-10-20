"""REST, SSE, and WebSocket endpoints for the meeting assistant."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from ..config import get_settings
from ..db import SessionLocal, get_session
from ..llm.resilience import translate_provider_error
from ..models import MeetingStatus
from .agent import MeetingAgent
from .asr import MeetingASRUnavailable, MeetingASRSession, MeetingTranscriber
from .minutes import run_minutes_job
from .schemas import (
    MeetingChatRequest,
    MeetingCreate,
    MeetingDetail,
    MeetingHealthRead,
    MeetingListItem,
    MeetingMinutesRead,
    MeetingUpdate,
)
from .service import (
    MeetingError,
    SegmentInput,
    begin_recording,
    begin_summary,
    build_markdown_export,
    create_meeting,
    delete_meeting,
    fail_recording,
    finish_recording,
    get_meeting,
    list_meetings,
    transcript_segments,
    update_meeting,
    upsert_segments,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meetings")


def _transcriber(request: Request) -> MeetingTranscriber | None:
    return getattr(request.app.state, "meeting_transcriber", None)


def _raise(exc: MeetingError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/health", response_model=MeetingHealthRead)
def health(request: Request) -> MeetingHealthRead:
    settings = get_settings()
    transcriber = _transcriber(request)
    if transcriber is None:
        return MeetingHealthRead(
            enabled=settings.meeting_enabled,
            available=False,
            ready=False,
            loading=False,
            backend=settings.meeting_asr_backend,
            model=settings.meeting_asr_model,
            language=settings.meeting_asr_language,
            device=settings.meeting_asr_device,
            compute_type=settings.meeting_asr_compute_type,
            error="meeting transcriber is not initialized",
        )
    return MeetingHealthRead(**transcriber.health())


@router.post("", response_model=MeetingDetail, status_code=201)
def create(
    payload: MeetingCreate,
    session: Session = Depends(get_session),
) -> MeetingDetail:
    settings = get_settings()
    default_title = datetime.now(
        ZoneInfo(settings.assistant_timezone)
    ).strftime("会议 %Y-%m-%d %H:%M")
    row = create_meeting(
        session,
        title=payload.title.strip() or default_title,
        language=payload.language,
    )
    session.commit()
    return MeetingDetail.model_validate(row)


@router.get("", response_model=list[MeetingListItem])
def list_all(session: Session = Depends(get_session)) -> list[MeetingListItem]:
    return [
        MeetingListItem.model_validate(row) for row in list_meetings(session)
    ]


@router.get("/{meeting_id}", response_model=MeetingDetail)
def detail(
    meeting_id: str,
    session: Session = Depends(get_session),
) -> MeetingDetail:
    try:
        row = get_meeting(session, meeting_id)
    except MeetingError as exc:
        _raise(exc)
    return MeetingDetail.model_validate(row)


@router.patch("/{meeting_id}", response_model=MeetingDetail)
def patch(
    meeting_id: str,
    payload: MeetingUpdate,
    session: Session = Depends(get_session),
) -> MeetingDetail:
    try:
        row = update_meeting(
            session,
            meeting_id,
            title=payload.title,
            notes=payload.notes,
            language=payload.language,
        )
    except MeetingError as exc:
        _raise(exc)
    session.commit()
    return MeetingDetail.model_validate(row)


@router.delete("/{meeting_id}", status_code=204)
def remove(
    meeting_id: str,
    session: Session = Depends(get_session),
) -> Response:
    try:
        delete_meeting(session, meeting_id)
    except MeetingError as exc:
        _raise(exc)
    session.commit()
    return Response(status_code=204)


@router.post("/{meeting_id}/minutes", response_model=MeetingMinutesRead, status_code=202)
def generate_minutes(
    meeting_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> MeetingMinutesRead:
    try:
        row = begin_summary(session, meeting_id)
    except MeetingError as exc:
        _raise(exc)
    session.commit()
    background_tasks.add_task(run_minutes_job, meeting_id)
    return MeetingMinutesRead(
        meeting_id=meeting_id,
        summary_status=row.summary_status.value,
    )


@router.get("/{meeting_id}/export.md")
def export_markdown(
    meeting_id: str,
    session: Session = Depends(get_session),
) -> Response:
    try:
        markdown = build_markdown_export(session, meeting_id)
    except MeetingError as exc:
        _raise(exc)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="meeting-{meeting_id}.md"'
            )
        },
    )


@router.post("/{meeting_id}/chat/stream")
async def chat_stream(
    meeting_id: str,
    payload: MeetingChatRequest,
    session: Session = Depends(get_session),
) -> EventSourceResponse:
    try:
        meeting = get_meeting(session, meeting_id)
    except MeetingError as exc:
        _raise(exc)
    if meeting.status is MeetingStatus.recording:
        raise HTTPException(
            status_code=409, detail="stop the recording before asking questions"
        )
    if not transcript_segments(session, meeting_id):
        raise HTTPException(
            status_code=422, detail="meeting has no transcript to search"
        )

    history = [
        (
            HumanMessage(content=item.content)
            if item.role == "user"
            else AIMessage(content=item.content)
        )
        for item in payload.history[-12:]
    ]
    agent = MeetingAgent(session, meeting)

    async def events():
        try:
            async for event in agent.astream(payload.message, history):
                yield {
                    "event": event.type,
                    "data": json.dumps(
                        event.to_payload(), ensure_ascii=False, default=str
                    ),
                }
        except Exception as exc:
            logger.exception("meeting chat stream failed")
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "type": "error",
                        "message": translate_provider_error(exc),
                    },
                    ensure_ascii=False,
                ),
            }

    return EventSourceResponse(events())


async def _forward_updates(
    websocket: WebSocket,
    session: Session,
    meeting_id: str,
    asr_session: MeetingASRSession,
    done: asyncio.Event,
    state: dict[str, str | None],
) -> None:
    try:
        async for update in asr_session.updates():
            if update.lines or update.compute_lag_seconds > 0:
                await websocket.send_json(
                    {
                        "type": "metrics",
                        "compute_lag_seconds": update.compute_lag_seconds,
                    }
                )
            if update.lines:
                rows = upsert_segments(
                    session,
                    meeting_id,
                    [
                        SegmentInput(
                            start_ms=item.start_ms,
                            end_ms=item.end_ms,
                            text=item.text,
                        )
                        for item in update.lines
                    ],
                )
                session.commit()
                await websocket.send_json(
                    {
                        "type": "transcript",
                        "segments": [
                            {
                                "id": row.id,
                                "start_ms": row.start_ms,
                                "end_ms": row.end_ms,
                                "text": row.text,
                            }
                            for row in rows
                        ],
                        "partial": update.partial,
                    }
                )
            elif update.partial:
                await websocket.send_json(
                    {"type": "partial", "text": update.partial}
                )
            if update.error:
                state["error"] = update.error
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "asr_failed",
                        "message": update.error,
                    }
                )
                await websocket.close(code=1011, reason="ASR failed")
                break
    except WebSocketDisconnect:
        state["disconnected"] = "1"
    except Exception as exc:
        logger.exception("meeting ASR forwarding failed")
        state["error"] = f"转写推送失败：{exc}"
    finally:
        done.set()


@router.websocket("/{meeting_id}/audio")
async def audio_stream(websocket: WebSocket, meeting_id: str) -> None:
    await websocket.accept()
    settings = get_settings()
    session_factory = getattr(
        websocket.app.state, "test_session_factory", SessionLocal
    )
    session = session_factory()
    asr_session: MeetingASRSession | None = None
    sender: asyncio.Task | None = None
    done = asyncio.Event()
    state: dict[str, str | None] = {"error": None, "disconnected": None}
    stop_requested = False

    try:
        transcriber = getattr(
            websocket.app.state, "meeting_transcriber", None
        )
        if transcriber is None:
            raise MeetingASRUnavailable("meeting transcriber is not initialized")
        try:
            meeting = begin_recording(session, meeting_id)
            session.commit()
        except MeetingError as exc:
            await websocket.send_json(
                {"type": "error", "code": "meeting_conflict", "message": str(exc)}
            )
            await websocket.close(code=4409, reason=str(exc))
            return

        await websocket.send_json(
            {
                "type": "state",
                "state": "loading_model",
                "model": settings.meeting_asr_model,
            }
        )
        try:
            asr_session = await transcriber.open_session(language=meeting.language)
        except MeetingASRUnavailable as exc:
            fail_recording(session, meeting_id, str(exc))
            session.commit()
            await websocket.send_json(
                {"type": "error", "code": "asr_unavailable", "message": str(exc)}
            )
            await websocket.close(code=1013, reason="ASR unavailable")
            return

        await websocket.send_json(
            {
                "type": "state",
                "state": "recording",
                "language": meeting.language,
                "model": settings.meeting_asr_model,
            }
        )
        sender = asyncio.create_task(
            _forward_updates(
                websocket,
                session,
                meeting_id,
                asr_session,
                done,
                state,
            )
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1, settings.meeting_max_minutes) * 60
        while True:
            if loop.time() >= deadline:
                state["error"] = (
                    f"会议已达到 {settings.meeting_max_minutes} 分钟的录音上限。"
                )
                stop_requested = True
                await asr_session.stop()
                break
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                state["disconnected"] = "1"
                break
            data = message.get("bytes")
            if data is None:
                continue
            if not data:
                stop_requested = True
                await asr_session.stop()
                break
            if len(data) > 2 * 1024 * 1024:
                state["error"] = "音频分片过大。"
                stop_requested = True
                await asr_session.stop()
                break
            await asr_session.push_audio(data)

        if stop_requested:
            try:
                await asyncio.wait_for(done.wait(), timeout=30)
            except TimeoutError:
                state["error"] = "等待本地转写结束时超时。"

        if state["error"]:
            fail_recording(session, meeting_id, state["error"] or "转写失败")
        elif state["disconnected"]:
            finish_recording(session, meeting_id, interrupted=True)
        else:
            finish_recording(session, meeting_id)
        session.commit()

        if state["disconnected"] is None:
            try:
                await websocket.send_json(
                    {
                        "type": "state",
                        "state": (
                            "failed"
                            if state["error"]
                            else (
                                "interrupted"
                                if state["disconnected"]
                                else "recorded"
                            )
                        ),
                        "error": state["error"],
                    }
                )
            except Exception:
                pass
    except WebSocketDisconnect:
        try:
            finish_recording(session, meeting_id, interrupted=True)
            session.commit()
        except Exception:
            session.rollback()
    except Exception as exc:
        logger.exception("meeting audio websocket failed")
        try:
            fail_recording(session, meeting_id, str(exc))
            session.commit()
        except Exception:
            session.rollback()
        try:
            await websocket.send_json(
                {"type": "error", "code": "transport_failed", "message": str(exc)}
            )
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if sender is not None and not sender.done():
            sender.cancel()
        if sender is not None:
            await asyncio.gather(sender, return_exceptions=True)
        if asr_session is not None:
            try:
                await asr_session.cleanup()
            except Exception:
                logger.exception("meeting ASR cleanup failed")
        session.close()
