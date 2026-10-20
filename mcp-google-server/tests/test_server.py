from __future__ import annotations

import json

from mcp_google_server import server


class _Request:
    def execute(self):
        return {"id": "event-1", "summary": "Demo"}


class _Events:
    def insert(self, **kwargs):
        return _Request()


class _Calendar:
    def events(self):
        return _Events()


def test_write_tool_returns_envelope(monkeypatch) -> None:
    monkeypatch.setattr(server, "calendar_service", lambda: _Calendar())
    payload = json.loads(
        server.create_calendar_event(
            summary="Demo",
            start="2026-10-21T15:00:00+08:00",
            end="2026-10-21T16:00:00+08:00",
        )
    )
    assert payload["ok"] is True
    assert payload["data"]["id"] == "event-1"


def test_missing_token_becomes_structured_error(monkeypatch) -> None:
    def boom():
        raise RuntimeError("GOOGLE_TOKEN_PATH is not configured")

    monkeypatch.setattr(server, "gmail_service", boom)
    payload = json.loads(server.search_emails("is:unread"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unexpected_failure"
    assert "GOOGLE_TOKEN_PATH" in payload["error"]["message"]
