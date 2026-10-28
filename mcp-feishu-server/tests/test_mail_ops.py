from __future__ import annotations

from types import SimpleNamespace

import pytest
from lark_oapi.api.mail.v1 import (
    Draft,
    MailAddress,
    MailSearchItem,
    MailSearchMeta,
    Message,
    Thread,
)

from mcp_feishu_server import mail_ops
from mcp_feishu_server.errors import InvalidArgument


def _response(data):
    return SimpleNamespace(data=data, success=lambda: True, code=0, msg="ok")


class FakeUserMailbox:
    def __init__(self, items) -> None:
        self.items = items
        self.request = None

    def search(self, request, option):
        self.request = request
        return _response(SimpleNamespace(items=self.items, page_token=None))


class FakeThread:
    def __init__(self, thread) -> None:
        self.thread = thread

    def get(self, request, option):
        return _response(SimpleNamespace(thread=self.thread))


class FakeMessage:
    def __init__(self, message) -> None:
        self.message = message

    def get(self, request, option):
        return _response(SimpleNamespace(message=self.message))

    def send(self, request, option):
        return _response(
            SimpleNamespace(message_id="sent-1", thread_id="thread-1")
        )


class FakeDraft:
    def __init__(self, draft) -> None:
        self.draft = draft
        self.created = None

    def create(self, request, option):
        self.created = request
        return _response(SimpleNamespace(draft=self.draft, reference=None))

    def send(self, request, option):
        self.sent = request
        return _response(
            SimpleNamespace(message_id="sent-draft-1", thread_id="thread-1")
        )


class FakeMailV1:
    def __init__(self, message, thread) -> None:
        self.user_mailbox = FakeUserMailbox(
            [
                MailSearchItem.builder()
                .id("m1")
                .display_info("Preview")
                .meta_data(
                    MailSearchMeta.builder()
                    .title("Project plan")
                    .thread_id("thread-1")
                    .message_biz_id("m1")
                    .create_time("2026-10-26T10:00:00+08:00")
                    .from_(
                        MailAddress.builder()
                        .mail_address("alice@example.com")
                        .name("Alice")
                        .build()
                    )
                    .build()
                )
                .build()
            ]
        )
        self.user_mailbox_message = FakeMessage(message)
        self.user_mailbox_thread = FakeThread(thread)
        self.user_mailbox_draft = FakeDraft(
            Draft.builder().id("draft-1").build()
        )


class FakeRuntime:
    def __init__(self) -> None:
        self.message = (
            Message.builder()
            .message_id("m1")
            .smtp_message_id("<m1@example.com>")
            .thread_id("thread-1")
            .subject("Project plan")
            .head_from(
                MailAddress.builder()
                .mail_address("alice@example.com")
                .name("Alice")
                .build()
            )
            .to(
                [
                    MailAddress.builder()
                    .mail_address("me@example.com")
                    .build()
                ]
            )
            .body_plain_text("Can we meet Friday?")
            .body_preview("Can we meet Friday?")
            .internal_date(1761444000000)
            .references("<root@example.com>")
            .build()
        )
        thread = Thread.builder().id("thread-1").messages([self.message]).build()
        self._v1 = FakeMailV1(self.message, thread)

    def client(self):
        return SimpleNamespace(mail=SimpleNamespace(v1=self._v1))

    def option(self):
        return object()

    def mailbox_id(self):
        return "me@example.com"


def test_search_maps_feishu_results() -> None:
    runtime = FakeRuntime()
    result = mail_ops.search_emails(runtime, "project", 5)
    assert result["count"] == 1
    assert result["messages"][0]["subject"] == "Project plan"
    assert result["messages"][0]["thread_id"] == "thread-1"
    request = runtime._v1.user_mailbox.request
    assert request.request_body.query == "project"


def test_read_and_thread_include_body() -> None:
    runtime = FakeRuntime()
    message = mail_ops.read_email(runtime, "m1")
    assert message["body"] == "Can we meet Friday?"
    thread = mail_ops.get_email_thread(runtime, "thread-1")
    assert thread["messages"][0]["id"] == "m1"
    assert thread["untrusted_external_content"] is True


def test_save_reply_draft_uses_raw_thread_headers() -> None:
    runtime = FakeRuntime()
    result = mail_ops.save_reply_draft(
        runtime,
        thread_id="thread-1",
        body="Friday works.",
        to="alice@example.com",
    )
    assert result["draft_id"] == "draft-1"
    request = runtime._v1.user_mailbox_draft.created
    assert request.request_body.raw


def test_send_reply_and_draft() -> None:
    runtime = FakeRuntime()
    sent = mail_ops.send_reply(
        runtime,
        thread_id="thread-1",
        body="Friday works.",
        to="alice@example.com",
    )
    assert sent["message_id"] == "sent-1"
    draft = mail_ops.send_draft(runtime, "draft-1")
    assert draft["message_id"] == "sent-draft-1"


def test_empty_id_is_rejected() -> None:
    with pytest.raises(InvalidArgument):
        mail_ops.read_email(FakeRuntime(), " ")
