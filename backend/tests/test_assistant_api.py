from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.tools import StructuredTool

from app.assistant.service import create_action, get_or_create_conversation
from app.assistant.agent import AssistantAgent
from app.mcp_client import ToolProfile, parse_tool_result

pytestmark = pytest.mark.anyio


class ScriptedLLM:
    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)

    def bind_tools(self, tools: list[Any]):
        self.bound_tools = tools
        return self

    async def astream(self, messages: list[Any]):
        message = self.script.pop(0) if self.script else AIMessage(content="结束")
        yield AIMessageChunk(
            content=message.content,
            tool_calls=list(getattr(message, "tool_calls", None) or []),
        )


class FakeMCP:
    started = True

    def __init__(self) -> None:
        self.write_calls = 0

        async def search_emails(query: str, max_results: int = 10) -> str:
            return json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "id": "m1",
                                "thread_id": "t1",
                                "subject": "Plan",
                                "from": "Alice <alice@example.com>",
                            }
                        ]
                    },
                }
            )

        async def save_reply_draft(
            thread_id: str, body: str, to: str | None = None
        ) -> str:
            self.write_calls += 1
            return json.dumps(
                {
                    "ok": True,
                    "data": {"draft_id": "draft-1", "thread_id": thread_id},
                }
            )

        self._tools = [
            StructuredTool.from_function(
                coroutine=search_emails, name="search_emails", description="search"
            ),
            StructuredTool.from_function(
                coroutine=save_reply_draft,
                name="save_reply_draft",
                description="save draft",
            ),
        ]
        self._profiles = {
            "search_emails": ToolProfile("search_emails", "search", True, False, {}),
            "save_reply_draft": ToolProfile(
                "save_reply_draft", "save draft", False, False, {}
            ),
        }

    def tools(self):
        return list(self._tools)

    def tool(self, name: str):
        return next((tool for tool in self._tools if tool.name == name), None)

    def profiles(self):
        return dict(self._profiles)

    def requires_approval(self, name: str) -> bool:
        profile = self._profiles.get(name)
        return True if profile is None else not profile.read_only


async def test_assistant_action_http_approval_is_idempotent(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    client = FakeMCP()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            app.state.google_mcp_client = client
            session = app.state.test_session_factory()
            try:
                conversation = get_or_create_conversation(
                    session,
                    channel="web",
                    external_chat_id="test-web",
                    title="test",
                )
                action = create_action(
                    session,
                    conversation_id=conversation.id,
                    tool_name="save_reply_draft",
                    arguments={
                        "thread_id": "t1",
                        "to": "alice@example.com",
                        "body": "Thanks",
                    },
                )
                session.commit()
                action_id = action.id
            finally:
                session.close()

            applied = await http.post(f"/api/assistant/actions/{action_id}/apply")
            assert applied.status_code == 200
            assert applied.json()["status"] == "applied"
            assert client.write_calls == 1

            duplicate = await http.post(f"/api/assistant/actions/{action_id}/apply")
            assert duplicate.status_code == 409
            assert client.write_calls == 1

            history = await http.get("/api/assistant/actions")
            assert history.status_code == 200
            assert history.json()[0]["result"]["draft_id"] == "draft-1"


async def test_assistant_chat_stream_creates_pending_action(
    app_with_temp_storage, monkeypatch
) -> None:
    app = app_with_temp_storage
    fake = FakeMCP()
    scripted = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_reply_draft",
                        "args": {
                            "thread_id": "t1",
                            "to": "alice@example.com",
                            "body": "Thanks",
                        },
                        "id": "c1",
                    }
                ],
            ),
            AIMessage(content="已生成回复草稿提案。"),
        ]
    )
    monkeypatch.setattr(
        "app.assistant.agent.build_resilient_chat_model",
        lambda settings: scripted,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            app.state.google_mcp_client = fake
            response = await http.post(
                "/api/assistant/chat/stream",
                json={"message": "帮我回复这封邮件"},
            )
            assert response.status_code == 200
            assert "event: proposal" in response.text
            assert "event: done" in response.text
            assert fake.write_calls == 0

            actions = await http.get("/api/assistant/actions")
            assert actions.status_code == 200
            assert actions.json()[0]["status"] == "proposed"

            messages = await http.get("/api/assistant/messages")
            assert messages.status_code == 200
            roles = [item["role"] for item in messages.json()]
            assert roles == ["user", "assistant"]


async def test_assistant_health_keeps_document_fields(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        async with app.router.lifespan_context(app):
            response = await http.get("/health")
            assert response.status_code == 200
            body = response.json()
            assert "mcp_started" in body
            assert "assistant" in body
            assert "google_auth_ready" in body["assistant"]
            assistant_health = await http.get("/api/assistant/health")
            assert assistant_health.status_code == 200
            assert assistant_health.json()["pending_actions"] == 0


async def test_google_mcp_exposes_expected_approval_annotations(
    app_with_temp_storage,
) -> None:
    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        profiles = app.state.google_mcp_client.profiles()
        assert {
            "search_emails",
            "read_email",
            "get_email_thread",
            "list_calendar_events",
            "find_free_slots",
            "save_reply_draft",
            "send_reply",
            "send_draft",
            "create_calendar_event",
        } <= set(profiles)
        assert profiles["search_emails"].read_only is True
        assert profiles["save_reply_draft"].requires_approval is True
        assert profiles["create_calendar_event"].requires_approval is True


async def test_real_mcp_tools_can_be_wrapped_for_the_agent(
    app_with_temp_storage,
) -> None:
    app = app_with_temp_storage
    async with app.router.lifespan_context(app):
        session = app.state.test_session_factory()
        try:
            conversation = get_or_create_conversation(
                session, channel="web", external_chat_id="tool-wrap"
            )
            agent = AssistantAgent(
                session,
                conversation,
                app.state.google_mcp_client,
                llm=ScriptedLLM([]),
            )
            tools = agent.build_tools()
            assert {tool.name for tool in tools} == {
                "search_emails",
                "read_email",
                "get_email_thread",
                "list_calendar_events",
                "find_free_slots",
                "save_reply_draft",
                "send_reply",
                "send_draft",
                "create_calendar_event",
            }
            raw = await app.state.google_mcp_client.tool("search_emails").ainvoke(
                {"query": "is:unread"}
            )
            payload = parse_tool_result(raw)
            assert payload["ok"] is False
            assert payload["error"]["code"] == "google_auth_not_ready"
        finally:
            session.close()
