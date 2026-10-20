from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy.orm import sessionmaker

from app.assistant import feishu
from app.assistant.feishu import FeishuBridge, action_card, _text_from_content
from app.assistant.service import create_action, get_or_create_conversation
from app.config import get_settings
from app.models import AssistantAction, AssistantActionStatus


def _response(payload):
    return payload


def test_text_content_and_card_preview(temp_session) -> None:
    assert _text_from_content(json.dumps({"text": "查一下邮件"})) == "查一下邮件"
    assert _text_from_content("not json") == ""
    conversation = get_or_create_conversation(
        temp_session, channel="feishu", external_chat_id="chat-1"
    )
    action = create_action(
        temp_session,
        conversation_id=conversation.id,
        tool_name="create_calendar_event",
        arguments={
            "summary": "项目会",
            "start": "2026-10-21T15:00:00+08:00",
            "end": "2026-10-21T15:30:00+08:00",
            "timezone": "Asia/Shanghai",
            "attendees": ["bob@example.com"],
        },
    )
    temp_session.flush()
    card = action_card(action)
    text = json.dumps(card, ensure_ascii=False)
    assert "待确认操作" in text
    assert "批准" in text
    assert "发送邀请" in text


def test_unauthorised_card_operator_is_rejected(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "feishu_approver_open_ids", "ou-owner", raising=False)
    bridge = FeishuBridge(settings)
    data = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou-other"),
            action=SimpleNamespace(value={"action_id": "a1", "decision": "approve"}),
            context=SimpleNamespace(open_chat_id="chat-1"),
        )
    )
    result = bridge._on_card(data, _response)
    assert result["toast"]["type"] == "error"
    assert "权限" in result["toast"]["content"]


def test_authorised_card_claims_action_without_blocking(temp_session, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "feishu_approver_open_ids", "ou-owner", raising=False)
    factory = sessionmaker(
        bind=temp_session.bind, expire_on_commit=False, future=True
    )
    monkeypatch.setattr(feishu, "SessionLocal", factory)
    conversation = get_or_create_conversation(
        temp_session, channel="feishu", external_chat_id="chat-1"
    )
    action = create_action(
        temp_session,
        conversation_id=conversation.id,
        tool_name="save_reply_draft",
        arguments={"thread_id": "t1", "to": "alice@example.com", "body": "Thanks"},
    )
    temp_session.commit()

    bridge = FeishuBridge(settings)
    monkeypatch.setattr(bridge, "_schedule", lambda coro: coro.close())
    data = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou-owner"),
            action=SimpleNamespace(
                value={"action_id": action.id, "decision": "approve"}
            ),
            context=SimpleNamespace(open_chat_id="chat-1"),
        )
    )
    result = bridge._on_card(data, _response)
    assert result["toast"]["type"] == "success"
    temp_session.expire_all()
    refreshed = temp_session.get(AssistantAction, action.id)
    assert refreshed is not None
    assert refreshed.status is AssistantActionStatus.approved
