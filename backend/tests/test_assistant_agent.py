"""Assistant control-flow tests with a scripted model and fake MCP tools.

No real LLM, Gmail, or Calendar call is made. The tests pin the most important
property: a write tool can only create a pending action, never execute during
the agent turn.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.tools import StructuredTool
from sqlalchemy import select

from app.assistant.agent import AssistantAgent
from app.assistant.service import (
    ActionError,
    apply_action,
    create_action,
    get_or_create_conversation,
    recover_interrupted_actions,
    reject_action,
)
from app.mcp_client import ToolProfile, parse_tool_result
from app.models import AssistantAction, AssistantActionStatus, MessageRole


class ScriptedLLM:
    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":
        self.bound_tools = tools
        return self

    async def astream(self, messages: list[Any]):
        self.calls.append(list(messages))
        message = self.script.pop(0) if self.script else AIMessage(content="结束")
        yield AIMessageChunk(
            content=message.content,
            tool_calls=list(getattr(message, "tool_calls", None) or []),
        )


class FakeMCP:
    started = True

    def __init__(self) -> None:
        self.read_calls = 0
        self.write_calls = 0

        async def search_emails(query: str, max_results: int = 10) -> str:
            self.read_calls += 1
            return json.dumps(
                {
                    "ok": True,
                    "data": {
                        "messages": [
                            {
                                "id": "m1",
                                "thread_id": "t1",
                                "subject": "plan",
                                "from": "Alice <alice@example.com>",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )

        async def save_reply_draft(
            thread_id: str, body: str, to: str | None = None
        ) -> str:
            self.write_calls += 1
            return json.dumps(
                {
                    "ok": True,
                    "data": {
                        "draft_id": "draft-1",
                        "thread_id": thread_id,
                        "status": "draft",
                    },
                },
                ensure_ascii=False,
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
            "search_emails": ToolProfile(
                name="search_emails",
                description="search",
                read_only=True,
                destructive=False,
                schema={},
            ),
            "save_reply_draft": ToolProfile(
                name="save_reply_draft",
                description="save draft",
                read_only=False,
                destructive=False,
                schema={},
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


def _call(name: str, args: dict[str, Any], call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


async def _run(agent: AssistantAgent, message: str) -> list:
    events = []
    async for event in agent.astream(message):
        events.append(event)
    return events


@pytest.fixture()
def assistant_env(temp_session):
    conversation = get_or_create_conversation(
        temp_session,
        channel="web",
        external_chat_id="test",
        title="test",
    )
    temp_session.commit()
    return conversation


@pytest.mark.anyio
async def test_read_tool_executes_but_write_only_proposes(
    temp_session, assistant_env
) -> None:
    client = FakeMCP()
    llm = ScriptedLLM(
        [
            _call("search_emails", {"query": "is:unread"}),
            _call(
                "save_reply_draft",
                {
                    "thread_id": "t1",
                    "to": "alice@example.com",
                    "body": "Thanks, Wednesday works.",
                },
                "c2",
            ),
            AIMessage(content="已准备回复草稿，等待你确认。"),
        ]
    )
    agent = AssistantAgent(temp_session, assistant_env, client, llm=llm)
    events = await _run(agent, "回复这封邮件")

    assert client.read_calls == 1
    assert client.write_calls == 0
    assert [event.type for event in events].count("proposal") == 1
    action = temp_session.scalar(select(AssistantAction))
    assert action is not None
    assert action.status is AssistantActionStatus.proposed
    assert action.preview["kind"] == "email_draft"


@pytest.mark.anyio
async def test_approval_executes_exactly_once(temp_session, assistant_env) -> None:
    client = FakeMCP()
    action = create_action(
        temp_session,
        conversation_id=assistant_env.id,
        tool_name="save_reply_draft",
        arguments={"thread_id": "t1", "to": "alice@example.com", "body": "Thanks"},
    )
    temp_session.commit()

    applied = await apply_action(
        temp_session,
        action.id,
        client,
        approved_by="tester",
        channel="web",
    )
    assert applied.status is AssistantActionStatus.applied
    assert applied.result["draft_id"] == "draft-1"
    assert client.write_calls == 1

    with pytest.raises(ActionError):
        await apply_action(
            temp_session,
            action.id,
            client,
            approved_by="tester",
            channel="web",
        )
    assert client.write_calls == 1


@pytest.mark.anyio
async def test_proposal_without_explicit_recipient_is_rejected(
    temp_session, assistant_env
) -> None:
    client = FakeMCP()
    llm = ScriptedLLM(
        [
            _call(
                "save_reply_draft",
                {"thread_id": "t1", "body": "Thanks"},
            ),
            AIMessage(content="缺少明确收件人，无法生成可审批的草稿。"),
        ]
    )
    agent = AssistantAgent(temp_session, assistant_env, client, llm=llm)
    events = await _run(agent, "回复这封邮件")
    assert client.write_calls == 0
    assert "proposal" not in [event.type for event in events]
    action = temp_session.scalar(select(AssistantAction))
    assert action is None


def test_reject_never_calls_the_tool(temp_session, assistant_env) -> None:
    client = FakeMCP()
    action = create_action(
        temp_session,
        conversation_id=assistant_env.id,
        tool_name="save_reply_draft",
        arguments={"thread_id": "t1", "to": "alice@example.com", "body": "Thanks"},
    )
    temp_session.commit()
    rejected = reject_action(
        temp_session, action.id, rejected_by="tester", channel="web"
    )
    assert rejected.status is AssistantActionStatus.rejected
    assert client.write_calls == 0


@pytest.mark.anyio
async def test_expired_action_cannot_be_applied(temp_session, assistant_env) -> None:
    client = FakeMCP()
    action = create_action(
        temp_session,
        conversation_id=assistant_env.id,
        tool_name="save_reply_draft",
        arguments={"thread_id": "t1", "to": "alice@example.com", "body": "Thanks"},
        ttl_hours=1,
    )
    action.expires_at = action.created_at
    temp_session.commit()

    with pytest.raises(ActionError):
        await apply_action(
            temp_session,
            action.id,
            client,
            approved_by="tester",
            channel="web",
        )
    temp_session.expire_all()
    refreshed = temp_session.get(AssistantAction, action.id)
    assert refreshed is not None
    assert refreshed.status is AssistantActionStatus.expired
    assert client.write_calls == 0


def test_tool_result_envelope_is_parseable() -> None:
    payload = parse_tool_result('{"ok": true, "data": {"x": 1}}')
    assert payload["data"]["x"] == 1


def test_restart_marks_in_flight_actions_failed(temp_session, assistant_env) -> None:
    action = create_action(
        temp_session,
        conversation_id=assistant_env.id,
        tool_name="save_reply_draft",
        arguments={"thread_id": "t1", "body": "Thanks"},
    )
    action.status = AssistantActionStatus.approved
    temp_session.commit()
    assert recover_interrupted_actions(temp_session) == 1
    temp_session.expire_all()
    refreshed = temp_session.get(AssistantAction, action.id)
    assert refreshed is not None
    assert refreshed.status is AssistantActionStatus.failed
    assert "不确定" in (refreshed.error or "")
