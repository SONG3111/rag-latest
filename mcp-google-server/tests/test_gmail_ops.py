from __future__ import annotations

import base64
from email import message_from_bytes, policy
from email.headerregistry import Address
from typing import Any

import pytest

from mcp_google_server import gmail_ops
from mcp_google_server.errors import InvalidArgument


def _message(
    *,
    message_id: str = "m1",
    thread_id: str = "t1",
    subject: str = "Quarterly plan",
    sender: str = "Alice <alice@example.com>",
    body: str = "Hello, can we meet Wednesday?",
) -> dict[str, Any]:
    return {
        "id": message_id,
        "threadId": thread_id,
        "snippet": body[:40],
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@example.com"},
                {"name": "Date", "value": "Tue, 20 Oct 2026 10:00:00 +0800"},
                {"name": "Message-ID", "value": "<m1@example.com>"},
                {"name": "References", "value": "<root@example.com>"},
            ],
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {
                        "data": base64.urlsafe_b64encode(
                            b"<p>Hello <b>Alice</b></p><script>bad()</script>"
                        ).decode()
                    },
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
                },
            ],
        },
    }


class _Request:
    def __init__(self, value: Any) -> None:
        self.value = value

    def execute(self) -> Any:
        return self.value


class FakeMessages:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.list_kwargs: dict[str, Any] = {}
        self.sent: dict[str, Any] | None = None

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return _Request({"messages": [{"id": self.message["id"]}]})

    def get(self, **kwargs):
        return _Request(self.message)

    def send(self, **kwargs):
        self.sent = kwargs
        return _Request({"id": "sent-1", "threadId": self.message["threadId"]})


class FakeThreads:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message

    def get(self, **kwargs):
        return _Request({"id": self.message["threadId"], "messages": [self.message]})


class FakeDrafts:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None
        self.sent: dict[str, Any] | None = None

    def create(self, **kwargs):
        self.created = kwargs
        return _Request(
            {"id": "draft-1", "message": {"id": "draft-msg-1", "threadId": "t1"}}
        )

    def send(self, **kwargs):
        self.sent = kwargs
        return _Request({"id": "sent-draft-1", "threadId": "t1"})


class FakeUsers:
    def __init__(self, message: dict[str, Any]) -> None:
        self._messages = FakeMessages(message)
        self._threads = FakeThreads(message)
        self._drafts = FakeDrafts()

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads

    def drafts(self):
        return self._drafts


class FakeGmail:
    def __init__(self, message: dict[str, Any] | None = None) -> None:
        self.users_obj = FakeUsers(message or _message())

    def users(self):
        return self.users_obj


def test_search_emails_summarizes_metadata() -> None:
    service = FakeGmail()
    result = gmail_ops.search_emails(service, "is:unread", 5)
    assert result["count"] == 1
    item = result["messages"][0]
    assert item["id"] == "m1"
    assert item["thread_id"] == "t1"
    assert item["subject"] == "Quarterly plan"
    assert item["unread"] is True
    assert result["untrusted_external_content"] is True
    assert service.users().messages().list_kwargs["q"] == "is:unread"


def test_read_email_prefers_plain_text_and_drops_script() -> None:
    service = FakeGmail()
    result = gmail_ops.read_email(service, "m1")
    assert result["body"] == "Hello, can we meet Wednesday?"
    assert result["body_mime_type"] == "text/plain"
    assert result["truncated"] is False
    assert "<script>" not in result["body"]


def test_save_reply_draft_sets_thread_headers_and_recipient() -> None:
    service = FakeGmail()
    result = gmail_ops.save_reply_draft(
        service,
        thread_id="t1",
        body="Wednesday at 3 works.",
        quote_original=True,
    )
    assert result["draft_id"] == "draft-1"
    created = service.users().drafts().created
    assert created is not None
    raw = created["body"]["message"]["raw"]
    parsed = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    assert parsed["To"] == "Alice <alice@example.com>"
    assert parsed["Subject"] == "Re: Quarterly plan"
    assert parsed["In-Reply-To"] == "<m1@example.com>"
    assert "Wednesday at 3 works." in parsed.get_content()
    assert "wrote:" in parsed.get_content()


def test_send_reply_uses_messages_send() -> None:
    service = FakeGmail()
    result = gmail_ops.send_reply(service, thread_id="t1", body="Confirmed")
    assert result["status"] == "sent"
    sent = service.users().messages().sent
    assert sent is not None
    assert sent["body"]["threadId"] == "t1"


def test_send_draft_uses_draft_id() -> None:
    service = FakeGmail()
    result = gmail_ops.send_draft(service, "draft-1")
    assert result["message_id"] == "sent-draft-1"
    assert service.users().drafts().sent == {"userId": "me", "body": {"id": "draft-1"}}


def test_empty_query_is_rejected() -> None:
    with pytest.raises(InvalidArgument):
        gmail_ops.search_emails(FakeGmail(), "   ")
