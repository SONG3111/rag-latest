from __future__ import annotations

import json
from types import SimpleNamespace

from mcp_feishu_server import server


def test_tool_envelope(monkeypatch) -> None:
    class FakeMailbox:
        def search(self, request, option):
            return SimpleNamespace(
                data=SimpleNamespace(items=[], page_token=None),
                success=lambda: True,
            )

    runtime = SimpleNamespace(
        client=lambda: SimpleNamespace(
            mail=SimpleNamespace(
                v1=SimpleNamespace(user_mailbox=FakeMailbox())
            )
        ),
        option=lambda: object(),
        mailbox_id=lambda: "me@example.com",
    )
    monkeypatch.setattr(server, "runtime", lambda: runtime)
    payload = json.loads(server.search_emails("hello"))
    assert payload["ok"] is True
    assert payload["data"]["count"] == 0
