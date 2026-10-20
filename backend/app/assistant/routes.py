"""REST and SSE endpoints for the mail/calendar assistant."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from ..config import get_settings
from ..db import get_session
from ..llm.resilience import translate_provider_error
from ..mcp_client import McpGoogleClient
from ..models import (
    AssistantAction,
    AssistantActionStatus,
    AssistantConversation,
    MessageRole,
)
from .agent import AssistantAgent, to_langchain_history
from .schemas import (
    AssistantActionRead,
    AssistantChatRequest,
    AssistantHealthRead,
    AssistantMessageRead,
    AssistantToolRead,
)
from .service import (
    WEB_CHANNEL,
    ActionError,
    append_message,
    apply_action,
    expire_stale_actions,
    list_actions,
    reject_action,
    recent_messages,
    web_conversation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assistant")


def _client(request: Request) -> McpGoogleClient:
    client = getattr(request.app.state, "google_mcp_client", None)
    if client is None or not client.started:
        raise HTTPException(
            status_code=503, detail="Google MCP server is not available"
        )
    return client


@router.post("/chat/stream")
async def chat_stream(
    payload: AssistantChatRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> EventSourceResponse:
    client = _client(request)
    settings = get_settings()
    if payload.conversation_id:
        conversation = session.get(AssistantConversation, payload.conversation_id)
        if conversation is None or conversation.channel != WEB_CHANNEL:
            raise HTTPException(status_code=404, detail="assistant conversation not found")
    else:
        conversation = web_conversation(session)
    assert conversation is not None

    previous = recent_messages(
        session, conversation.id, settings.assistant_history_messages
    )
    user_row = append_message(
        session, conversation.id, MessageRole.user, payload.message
    )
    history = to_langchain_history(previous)
    agent = AssistantAgent(
        session,
        conversation,
        client,
        source_message_id=user_row.id,
    )

    async def events() -> AsyncIterator[dict]:
        collected: list[str] = []
        streamed: list[str] = []
        proposals: list[dict] = []

        def save_partial(note: str = "") -> str:
            answer = (collected[-1] if collected else "".join(streamed)).strip()
            if note:
                answer = (answer + note).strip()
            if not answer and proposals:
                answer = f"已生成 {len(proposals)} 条待确认操作。"
            append_message(
                session,
                conversation.id,
                MessageRole.assistant,
                answer,
                meta={"action_ids": [item.get("id") for item in proposals]},
            )
            session.commit()
            return answer

        try:
            async with asyncio.timeout(settings.chat_turn_timeout):
                async for event in agent.astream(payload.message, history):
                    if event.type == "done":
                        collected.append(str(event.data.get("content") or ""))
                        continue
                    if event.type == "token":
                        streamed.append(str(event.data.get("text") or ""))
                    elif event.type == "proposal":
                        proposals.append(event.data)
                    yield {
                        "event": event.type,
                        "data": json.dumps(
                            event.to_payload(), ensure_ascii=False, default=str
                        ),
                    }
                answer = (collected[-1] if collected else "".join(streamed)).strip()
                if not answer and proposals:
                    answer = f"已生成 {len(proposals)} 条待确认操作。"
                assistant_row = append_message(
                    session,
                    conversation.id,
                    MessageRole.assistant,
                    answer,
                    meta={"action_ids": [item.get("id") for item in proposals]},
                )
                session.commit()
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "type": "done",
                            "content": answer,
                            "message_id": assistant_row.id,
                            "conversation_id": conversation.id,
                        },
                        ensure_ascii=False,
                    ),
                }
        except asyncio.CancelledError:
            answer = save_partial("\n\n（本轮已中断，以上为已生成的部分。）")
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "type": "done",
                        "content": answer,
                        "conversation_id": conversation.id,
                    },
                    ensure_ascii=False,
                ),
            }
        except TimeoutError:
            answer = save_partial("\n\n（本轮生成超时，以上为已生成的部分。）")
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "type": "done",
                        "content": answer,
                        "conversation_id": conversation.id,
                    },
                    ensure_ascii=False,
                ),
            }
        except Exception as exc:
            logger.exception("assistant stream failed")
            save_partial("\n\n（本轮处理失败，请稍后重试。）")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"type": "error", "message": translate_provider_error(exc)},
                    ensure_ascii=False,
                ),
            }

    return EventSourceResponse(events())


@router.get("/messages", response_model=list[AssistantMessageRead])
def messages(
    conversation_id: str | None = None,
    session: Session = Depends(get_session),
) -> list[AssistantMessageRead]:
    if conversation_id:
        conversation = session.get(AssistantConversation, conversation_id)
        if conversation is None or conversation.channel != WEB_CHANNEL:
            raise HTTPException(status_code=404, detail="assistant conversation not found")
        rows = recent_messages(session, conversation_id, 200)
    else:
        conversation = web_conversation(session, create=False)
        rows = recent_messages(session, conversation.id, 200) if conversation else []
    return [
        AssistantMessageRead.model_validate(row, from_attributes=True) for row in rows
    ]


@router.get("/actions", response_model=list[AssistantActionRead])
def actions(
    conversation_id: str | None = None,
    status_filter: str | None = None,
    session: Session = Depends(get_session),
) -> list[AssistantActionRead]:
    if status_filter:
        try:
            AssistantActionStatus(status_filter)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid action status") from exc
    rows = list_actions(
        session, conversation_id=conversation_id, status_filter=status_filter
    )
    return [AssistantActionRead.model_validate(row, from_attributes=True) for row in rows]


@router.post("/actions/{action_id}/apply", response_model=AssistantActionRead)
async def apply(
    action_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> AssistantAction:
    client = _client(request)
    try:
        return await apply_action(
            session,
            action_id,
            client,
            approved_by="web-user",
            channel=WEB_CHANNEL,
        )
    except ActionError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/actions/{action_id}/reject", response_model=AssistantActionRead)
def reject(
    action_id: str,
    session: Session = Depends(get_session),
) -> AssistantAction:
    try:
        return reject_action(
            session,
            action_id,
            rejected_by="web-user",
            channel=WEB_CHANNEL,
        )
    except ActionError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tools", response_model=list[AssistantToolRead])
def tools(request: Request) -> list[AssistantToolRead]:
    settings = get_settings()
    client = getattr(request.app.state, "google_mcp_client", None)
    if not settings.assistant_enabled or client is None or not client.started:
        return []
    return [
        AssistantToolRead(
            name=profile.name,
            description=profile.description,
            read_only=profile.read_only,
            destructive=profile.destructive,
            requires_approval=profile.requires_approval,
            schema=profile.schema,
        )
        for profile in client.profiles().values()
    ]


@router.get("/health", response_model=AssistantHealthRead)
async def health(
    request: Request,
    session: Session = Depends(get_session),
) -> AssistantHealthRead:
    settings = get_settings()
    client = getattr(request.app.state, "google_mcp_client", None)
    bridge = getattr(request.app.state, "feishu_bridge", None)
    google_error = getattr(request.app.state, "google_mcp_error", None)
    if google_error is None and not settings.effective_google_token_path.exists():
        google_error = "Google OAuth is not ready"
    expire_stale_actions(session)
    pending = session.scalar(
        select(func.count(AssistantAction.id)).where(
            AssistantAction.status == AssistantActionStatus.proposed
        )
    )
    return AssistantHealthRead(
        enabled=settings.assistant_enabled,
        google_mcp_started=bool(client and client.started),
        google_auth_ready=settings.effective_google_token_path.exists(),
        google_error=google_error,
        feishu_configured=settings.feishu_configured,
        feishu_connected=bool(bridge and bridge.connected),
        feishu_error=(
            getattr(request.app.state, "feishu_error", None)
            or getattr(bridge, "last_error", None)
        ),
        pending_actions=int(pending or 0),
    )
