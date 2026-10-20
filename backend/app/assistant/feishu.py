"""Feishu bot bridge over the official SDK's WebSocket long connection.

The SDK sample `samples/ws/sample.py` and its `card.action.trigger` callback
shape are the basis for this module (MIT). No public callback URL is required:
one daemon thread owns the long connection while this process is alive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from sqlalchemy.exc import IntegrityError

from ..config import Settings, get_settings
from ..db import SessionLocal
from ..llm.resilience import translate_provider_error
from ..mcp_client import McpGoogleClient
from ..models import AssistantAction, MessageRole
from .agent import AssistantAgent, to_langchain_history
from .service import (
    FEISHU_CHANNEL,
    ActionError,
    append_message,
    approve_action,
    execute_approved_action,
    find_message_by_event,
    get_action,
    get_or_create_conversation,
    recent_messages,
    reject_action,
)

logger = logging.getLogger(__name__)


def _text_from_content(content: str | None) -> str:
    if not content:
        return ""
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("text") or "").strip()


def _truncate(value: str, limit: int = 2400) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def action_card(action: AssistantAction) -> dict[str, Any]:
    preview = action.preview or {}
    kind = preview.get("kind")
    if kind in {"email_draft", "email_send"}:
        verb = "保存回复草稿" if kind == "email_draft" else "发送回复"
        body = (
            f"**动作**：{verb}\n"
            f"**收件人**：{preview.get('to')}\n"
            f"**抄送**：{preview.get('cc') or '无'}\n"
            f"**主题**：{preview.get('subject')}\n\n"
            f"{_truncate(str(preview.get('body') or ''))}"
        )
    elif kind == "email_send_draft":
        body = f"**动作**：发送草稿\n**草稿 ID**：{preview.get('draft_id')}"
    elif kind == "calendar_event":
        attendees = "、".join(preview.get("attendees") or []) or "无"
        invite = "\n**注意**：批准后会向参会人发送邀请。" if preview.get("invitations") else ""
        body = (
            f"**会议**：{preview.get('summary')}\n"
            f"**时间**：{preview.get('start')} – {preview.get('end')}\n"
            f"**时区**：{preview.get('timezone')}\n"
            f"**参会人**：{attendees}{invite}"
        )
    else:
        body = f"**工具**：{action.tool_name}\n{json.dumps(action.arguments, ensure_ascii=False)}"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "待确认操作"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "批准"},
                        "value": {"action_id": action.id, "decision": "approve"},
                    },
                    {
                        "tag": "button",
                        "type": "danger",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "value": {"action_id": action.id, "decision": "reject"},
                    },
                ],
            },
        ],
    }


class FeishuBridge:
    """Own one Feishu WebSocket connection and route events into the agent."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        google_client: McpGoogleClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.google_client = google_client
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._api = None
        self._connected = False
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return bool(self._connected and self._thread and self._thread.is_alive())

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self.settings.feishu_configured:
            logger.info("Feishu bridge disabled: credentials or allowlist missing")
            return
        if self.google_client is None:
            raise RuntimeError("Feishu bridge needs the Google MCP client")
        try:
            import lark_oapi as lark
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("lark-oapi is not installed") from exc

        self.loop = loop
        self._api = (
            lark.Client.builder()
            .app_id(self.settings.feishu_app_id)
            .app_secret(self.settings.feishu_app_secret)
            .build()
        )
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(
                lambda data: self._on_card(data, P2CardActionTriggerResponse)
            )
            .build()
        )
        ws_client = lark.ws.Client(
            self.settings.feishu_app_id,
            self.settings.feishu_app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

        def run() -> None:
            try:
                ws_client.start()
            except Exception as exc:  # pragma: no cover - network runtime
                self.last_error = str(exc)
                self._connected = False
                logger.exception("Feishu long connection stopped")

        self._connected = True
        self._thread = threading.Thread(
            target=run, name="feishu-long-connection", daemon=True
        )
        self._thread.start()
        logger.info("Feishu long connection started")

    def _schedule(self, coro) -> None:
        if self.loop is None or self.loop.is_closed():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _allowed_user(self, open_id: str) -> bool:
        return bool(open_id) and open_id in self.settings.feishu_allowed_users

    def _allowed_chat(self, chat_id: str) -> bool:
        allowed = self.settings.feishu_allowed_chats
        return not allowed or chat_id in allowed

    def _on_message(self, data) -> None:
        try:
            event = data.event
            message = event.message
            sender = event.sender
            open_id = str(getattr(sender.sender_id, "open_id", "") or "")
            chat_id = str(message.chat_id or "")
            if not self._allowed_user(open_id) or not self._allowed_chat(chat_id):
                logger.warning("ignored Feishu message from unauthorised sender/chat")
                return
            if message.message_type != "text":
                self._schedule(self._send_text(chat_id, "目前只支持文字指令。"))
                return
            text = _text_from_content(message.content)
            if not text:
                return
            event_id = str(getattr(data.header, "event_id", "") or "")
            self._schedule(
                self._handle_message(
                    chat_id=chat_id,
                    open_id=open_id,
                    text=text,
                    event_id=event_id,
                )
            )
        except Exception:
            logger.exception("could not parse Feishu message event")

    def _on_card(self, data, response_type):
        try:
            event = data.event
            operator = str(getattr(event.operator, "open_id", "") or "")
            if operator not in self.settings.feishu_approvers:
                return response_type(
                    {"toast": {"type": "error", "content": "你没有审批权限"}}
                )
            value = event.action.value or {}
            action_id = str(value.get("action_id") or "")
            decision = str(value.get("decision") or "")
            if not action_id or decision not in {"approve", "reject"}:
                return response_type(
                    {"toast": {"type": "error", "content": "无效的审批操作"}}
                )
            chat_id = str(getattr(event.context, "open_chat_id", "") or "")
            with SessionLocal() as session:
                action = get_action(session, action_id)
                if decision == "approve":
                    approve_action(
                        session,
                        action_id,
                        approved_by=operator,
                        channel=FEISHU_CHANNEL,
                    )
                    session.commit()
                    self._schedule(
                        self._execute_and_notify(action_id, chat_id or self._chat_for(action))
                    )
                    text = "已批准，正在执行"
                else:
                    reject_action(
                        session,
                        action_id,
                        rejected_by=operator,
                        channel=FEISHU_CHANNEL,
                    )
                    session.commit()
                    text = "已拒绝，未执行任何写入"
            return response_type({"toast": {"type": "success", "content": text}})
        except ActionError as exc:
            return response_type(
                {"toast": {"type": "warning", "content": str(exc)[:80]}}
            )
        except Exception:
            logger.exception("Feishu card callback failed")
            return response_type(
                {"toast": {"type": "error", "content": "审批处理失败，请到网页端重试"}}
            )

    @staticmethod
    def _chat_for(action: AssistantAction) -> str:
        try:
            return action.conversation.external_chat_id  # type: ignore[union-attr]
        except Exception:
            return ""

    async def _handle_message(
        self,
        *,
        chat_id: str,
        open_id: str,
        text: str,
        event_id: str,
    ) -> None:
        if self.google_client is None:
            return
        await self._send_text(chat_id, "收到，正在处理…")
        with SessionLocal() as session:
            conversation = get_or_create_conversation(
                session,
                channel=FEISHU_CHANNEL,
                external_chat_id=chat_id,
                title=f"飞书会话 {chat_id[:12]}",
            )
            if event_id and find_message_by_event(session, conversation.id, event_id):
                return
            previous = recent_messages(
                session, conversation.id, self.settings.assistant_history_messages
            )
            try:
                user_row = append_message(
                    session,
                    conversation.id,
                    MessageRole.user,
                    text,
                    external_event_id=event_id or None,
                )
                session.commit()
            except IntegrityError:
                # Two redeliveries can race past the read-before-write check.
                session.rollback()
                return
            history = to_langchain_history(previous)
            agent = AssistantAgent(
                session,
                conversation,
                self.google_client,
                source_message_id=user_row.id,
            )
            streamed: list[str] = []
            proposals: list[dict] = []
            final = ""
            error = ""
            try:
                async for event in agent.astream(text, history):
                    if event.type == "token":
                        streamed.append(str(event.data.get("text") or ""))
                    elif event.type == "proposal":
                        proposals.append(event.data)
                    elif event.type == "done":
                        final = str(event.data.get("content") or "")
                    elif event.type == "error":
                        error = str(event.data.get("message") or "")
            except Exception as exc:
                error = translate_provider_error(exc)
            answer = final or "".join(streamed).strip()
            if error and not answer:
                answer = error
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
        if answer:
            await self._send_text(chat_id, answer)
        for proposal in proposals:
            action_id = str(proposal.get("id") or "")
            if not action_id:
                continue
            with SessionLocal() as session:
                action = get_action(session, action_id)
                await self._send_card(chat_id, action_card(action))

    async def _execute_and_notify(self, action_id: str, chat_id: str) -> None:
        if self.google_client is None:
            return
        try:
            with SessionLocal() as session:
                action = get_action(session, action_id)
                if action.status.value != "approved":
                    return
                action = await execute_approved_action(
                    session, action_id, self.google_client
                )
                session.commit()
                if action.status.value == "applied":
                    text = f"已执行：{action.summary}"
                else:
                    text = f"执行失败：{action.error or '未知错误'}"
        except Exception as exc:
            text = f"执行失败：{translate_provider_error(exc)}"
        if chat_id:
            await self._send_text(chat_id, text)

    async def _send_text(self, chat_id: str, text: str) -> None:
        await asyncio.to_thread(self._send_message, chat_id, "text", {"text": text})

    async def _send_card(self, chat_id: str, card: dict[str, Any]) -> None:
        await asyncio.to_thread(self._send_message, chat_id, "interactive", card)

    def _send_message(self, chat_id: str, msg_type: str, content: dict[str, Any]) -> None:
        if self._api is None:
            return
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            body = (
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .build()
            )
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(body)
                .build()
            )
            response = self._api.im.v1.message.create(request)
            if not getattr(response, "success", lambda: True)():
                logger.warning(
                    "Feishu send failed: code=%s msg=%s",
                    getattr(response, "code", None),
                    getattr(response, "msg", None),
                )
        except Exception:
            logger.exception("could not send Feishu message")
