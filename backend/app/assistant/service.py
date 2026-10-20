"""Conversation persistence and the durable Gmail/Calendar approval loop.

The pending -> approved -> executing transition is a compare-and-set update.
`Anil-matcha/open-agent-email` (Apache-2.0) uses the same shape for its review
items; it is what makes duplicate Web clicks and replayed Feishu card callbacks
safe when two requests race.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import session_scope
from ..mcp_client import McpGoogleClient, parse_tool_result
from ..models import (
    AssistantAction,
    AssistantActionStatus,
    AssistantConversation,
    AssistantMessage,
    MessageRole,
)

WEB_CHANNEL = "web"
FEISHU_CHANNEL = "feishu"
WEB_CHAT_ID = "web-default"

_TOKENISH = re.compile(
    r"(?i)(ya29\.[A-Za-z0-9._-]+|1//[A-Za-z0-9._-]{8,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]+)"
)


class ActionError(RuntimeError):
    """Raised when an action cannot be claimed or executed."""


def _now() -> datetime:
    # SQLAlchemy's DateTime column is timezone-naive in SQLite. The assistant
    # stores UTC wall time consistently so status updates and expiry comparisons
    # never mix aware and naive values.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_error(exc: Any) -> str:
    return _TOKENISH.sub("[redacted]", str(exc))


def get_or_create_conversation(
    session: Session,
    *,
    channel: str,
    external_chat_id: str,
    title: str = "",
) -> AssistantConversation:
    row = session.scalar(
        select(AssistantConversation).where(
            AssistantConversation.channel == channel,
            AssistantConversation.external_chat_id == external_chat_id,
        )
    )
    if row is not None:
        return row
    row = AssistantConversation(
        channel=channel,
        external_chat_id=external_chat_id,
        title=title,
    )
    session.add(row)
    session.flush()
    return row


def web_conversation(session: Session, *, create: bool = True) -> AssistantConversation | None:
    row = session.scalar(
        select(AssistantConversation).where(
            AssistantConversation.channel == WEB_CHANNEL,
            AssistantConversation.external_chat_id == WEB_CHAT_ID,
        )
    )
    if row is None and create:
        row = get_or_create_conversation(
            session,
            channel=WEB_CHANNEL,
            external_chat_id=WEB_CHAT_ID,
            title="网页邮件助手",
        )
    return row


def append_message(
    session: Session,
    conversation_id: str,
    role: MessageRole,
    content: str,
    *,
    external_event_id: str | None = None,
    meta: dict | None = None,
) -> AssistantMessage:
    row = AssistantMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        external_event_id=external_event_id,
        meta=meta or {},
    )
    session.add(row)
    session.flush()
    return row


def recent_messages(
    session: Session, conversation_id: str, limit: int = 30
) -> list[AssistantMessage]:
    rows = list(
        session.scalars(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conversation_id)
            .order_by(AssistantMessage.created_at.desc())
            .limit(max(1, limit))
        )
    )
    return list(reversed(rows))


def find_message_by_event(
    session: Session, conversation_id: str, external_event_id: str
) -> AssistantMessage | None:
    return session.scalar(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation_id,
            AssistantMessage.external_event_id == external_event_id,
        )
    )


def _preview(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "save_reply_draft":
        return {
            "kind": "email_draft",
            "thread_id": args.get("thread_id"),
            "to": args.get("to") or "线程最新发件人",
            "cc": args.get("cc") or "",
            "subject": args.get("subject") or "Re: 原邮件主题",
            "body": args.get("body") or "",
            "quote_original": bool(args.get("quote_original")),
        }
    if tool_name == "send_reply":
        return {
            "kind": "email_send",
            "thread_id": args.get("thread_id"),
            "to": args.get("to") or "线程最新发件人",
            "cc": args.get("cc") or "",
            "subject": args.get("subject") or "Re: 原邮件主题",
            "body": args.get("body") or "",
            "quote_original": bool(args.get("quote_original")),
        }
    if tool_name == "send_draft":
        return {
            "kind": "email_send_draft",
            "draft_id": args.get("draft_id"),
        }
    if tool_name == "create_calendar_event":
        attendees = list(args.get("attendees") or [])
        return {
            "kind": "calendar_event",
            "summary": args.get("summary") or "",
            "start": args.get("start") or "",
            "end": args.get("end") or "",
            "timezone": args.get("timezone") or "Asia/Shanghai",
            "attendees": attendees,
            "description": args.get("description") or "",
            "location": args.get("location") or "",
            "invitations": bool(attendees),
        }
    return {"kind": "tool", "tool": tool_name, "arguments": args}


def _summary(tool_name: str, preview: dict[str, Any]) -> str:
    if tool_name == "save_reply_draft":
        return f"保存邮件回复草稿给 {preview.get('to')}"
    if tool_name == "send_reply":
        return f"发送邮件回复给 {preview.get('to')}"
    if tool_name == "send_draft":
        return f"发送 Gmail 草稿 {preview.get('draft_id')}"
    if tool_name == "create_calendar_event":
        suffix = "，并邀请参会人" if preview.get("invitations") else ""
        return (
            f"创建会议「{preview.get('summary')}」："
            f"{preview.get('start')} – {preview.get('end')}{suffix}"
        )
    return f"执行 {tool_name}"


def create_action(
    session: Session,
    *,
    conversation_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    source_message_id: str | None = None,
    ttl_hours: float = 24.0,
) -> AssistantAction:
    preview = _preview(tool_name, arguments)
    row = AssistantAction(
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        tool_name=tool_name,
        arguments=arguments,
        preview=preview,
        summary=_summary(tool_name, preview),
        status=AssistantActionStatus.proposed,
        idempotency_key=uuid.uuid4().hex,
        expires_at=_now() + timedelta(hours=max(1.0, ttl_hours)),
    )
    session.add(row)
    session.flush()
    return row


def expire_stale_actions(session: Session, conversation_id: str | None = None) -> int:
    now = _now()
    statement = update(AssistantAction).where(
        AssistantAction.status == AssistantActionStatus.proposed,
        AssistantAction.expires_at <= now,
    )
    if conversation_id:
        statement = statement.where(AssistantAction.conversation_id == conversation_id)
    result = session.execute(
        statement.values(
            status=AssistantActionStatus.expired,
            resolved_at=now,
            error="approval expired",
        ).execution_options(synchronize_session=False)
    )
    session.flush()
    return int(result.rowcount or 0)


def list_actions(
    session: Session,
    *,
    conversation_id: str | None = None,
    status_filter: str | None = None,
) -> list[AssistantAction]:
    expire_stale_actions(session, conversation_id)
    statement = select(AssistantAction).order_by(AssistantAction.created_at.desc())
    if conversation_id:
        statement = statement.where(AssistantAction.conversation_id == conversation_id)
    if status_filter:
        statement = statement.where(AssistantAction.status == AssistantActionStatus(status_filter))
    return list(session.scalars(statement))


def _load_action(session: Session, action_id: str) -> AssistantAction:
    action = session.get(AssistantAction, action_id)
    if action is None:
        raise ActionError("assistant action not found")
    return action


def approve_action(
    session: Session,
    action_id: str,
    *,
    approved_by: str,
    channel: str,
) -> AssistantAction:
    expire_stale_actions(session)
    now = _now()
    result = session.execute(
        update(AssistantAction)
        .where(
            AssistantAction.id == action_id,
            AssistantAction.status == AssistantActionStatus.proposed,
            AssistantAction.expires_at > now,
        )
        .values(
            status=AssistantActionStatus.approved,
            approved_by=approved_by,
            approval_channel=channel,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        session.expire_all()
        action = _load_action(session, action_id)
        raise ActionError(f"action is {action.status.value}; it cannot be approved")
    session.expire_all()
    return _load_action(session, action_id)


def reject_action(
    session: Session,
    action_id: str,
    *,
    rejected_by: str,
    channel: str,
) -> AssistantAction:
    now = _now()
    result = session.execute(
        update(AssistantAction)
        .where(
            AssistantAction.id == action_id,
            AssistantAction.status == AssistantActionStatus.proposed,
        )
        .values(
            status=AssistantActionStatus.rejected,
            approved_by=rejected_by,
            approval_channel=channel,
            resolved_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        session.expire_all()
        action = _load_action(session, action_id)
        raise ActionError(f"action is {action.status.value}; it cannot be rejected")
    session.expire_all()
    return _load_action(session, action_id)


async def execute_approved_action(
    session: Session,
    action_id: str,
    client: McpGoogleClient,
) -> AssistantAction:
    now = _now()
    claimed = session.execute(
        update(AssistantAction)
        .where(
            AssistantAction.id == action_id,
            AssistantAction.status == AssistantActionStatus.approved,
        )
        .values(status=AssistantActionStatus.executing)
        .execution_options(synchronize_session=False)
    )
    if int(claimed.rowcount or 0) != 1:
        session.expire_all()
        action = _load_action(session, action_id)
        raise ActionError(f"action is {action.status.value}; it cannot be executed")
    session.flush()
    session.expire_all()
    action = _load_action(session, action_id)
    tool = client.tool(action.tool_name)
    if tool is None:
        action.status = AssistantActionStatus.failed
        action.error = f"tool {action.tool_name} is not available"
        action.resolved_at = now
        session.flush()
        return action
    try:
        raw = await tool.ainvoke(action.arguments)
        payload = parse_tool_result(raw)
    except Exception as exc:
        action.status = AssistantActionStatus.failed
        action.error = _safe_error(exc)
        action.resolved_at = _now()
        session.flush()
        return action
    if not payload.get("ok"):
        error = payload.get("error") or {}
        action.status = AssistantActionStatus.failed
        action.error = _safe_error(error.get("message") or error or "tool failed")
        action.resolved_at = _now()
        session.flush()
        return action
    action.status = AssistantActionStatus.applied
    action.result = payload.get("data") or {}
    action.error = None
    action.resolved_at = _now()
    session.flush()
    return action


async def apply_action(
    session: Session,
    action_id: str,
    client: McpGoogleClient,
    *,
    approved_by: str,
    channel: str,
) -> AssistantAction:
    approve_action(session, action_id, approved_by=approved_by, channel=channel)
    return await execute_approved_action(session, action_id, client)


async def execute_action_in_background(action_id: str, client: McpGoogleClient) -> None:
    """Execute an action claimed by the Feishu callback in its own session."""
    with session_scope() as session:
        await execute_approved_action(session, action_id, client)


def get_action(session: Session, action_id: str) -> AssistantAction:
    return _load_action(session, action_id)


def recover_interrupted_actions(session: Session) -> int:
    """Fail closed after a restart in the middle of an external write.

    We cannot prove whether Gmail/Calendar accepted the call before the process
    died, so retrying automatically could send twice. Mark it failed and require
    the user to inspect the mailbox/calendar before creating a new proposal.
    """
    now = _now()
    result = session.execute(
        update(AssistantAction)
        .where(
            AssistantAction.status.in_(
                [AssistantActionStatus.approved, AssistantActionStatus.executing]
            )
        )
        .values(
            status=AssistantActionStatus.failed,
            error=(
                "执行过程被中断，外部结果不确定；请先检查 Gmail/Calendar，"
                "确认后再决定是否重新提交。"
            ),
            resolved_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    session.flush()
    return int(result.rowcount or 0)
