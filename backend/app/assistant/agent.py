"""LangGraph tool loop for the mail/calendar assistant."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, TypedDict

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from sqlalchemy.orm import Session

from ..agent.graph import AgentEvent
from ..config import Settings, get_settings
from ..llm.providers import build_chat_model, build_resilient_chat_model
from ..llm.resilience import translate_provider_error
from ..mcp_client import McpFeishuClient, parse_tool_result
from ..models import AssistantConversation, AssistantMessage, MessageRole
from .prompts import build_system_prompt
from .service import create_action

logger = logging.getLogger(__name__)


class AssistantState(TypedDict):
    messages: list[BaseMessage]
    iterations: int
    proposals: list[dict]


def to_langchain_history(rows: list[AssistantMessage]) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for row in rows:
        if row.role is MessageRole.user:
            messages.append(HumanMessage(content=row.content))
        elif row.role is MessageRole.assistant:
            messages.append(AIMessage(content=row.content))
    return messages


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


def _tool_error(exc: Exception) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": "tool_call_failed",
                "message": str(exc),
            },
        },
        ensure_ascii=False,
    )


def _label(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "search_emails":
        return f"搜索邮件：{args.get('query', '')}"
    if tool_name == "read_email":
        return f"读取邮件：{args.get('message_id', '')}"
    if tool_name == "get_email_thread":
        return f"读取邮件线程：{args.get('thread_id', '')}"
    if tool_name == "list_calendar_events":
        return "查询日历日程"
    if tool_name == "find_free_slots":
        return "查询共同空闲时间"
    if tool_name == "save_reply_draft":
        return "准备回复草稿"
    if tool_name == "send_reply":
        return "提交发送回复提案"
    if tool_name == "send_draft":
        return "提交发送草稿提案"
    if tool_name == "create_calendar_event":
        return f"提交会议提案：{args.get('summary', '')}"
    return tool_name


class AssistantAgent:
    """A single tool-calling graph with an annotation-driven approval gate."""

    def __init__(
        self,
        session: Session,
        conversation: AssistantConversation,
        client: McpFeishuClient,
        *,
        llm: Any | None = None,
        settings: Settings | None = None,
        source_message_id: str | None = None,
    ) -> None:
        self.session = session
        self.conversation = conversation
        self.client = client
        self.settings = settings or get_settings()
        if llm is None:
            if self.settings.assistant_model.strip():
                llm = build_chat_model(
                    self.settings, model=self.settings.assistant_model.strip()
                )
            else:
                llm = build_resilient_chat_model(self.settings)
        self.llm = llm
        self.source_message_id = source_message_id
        self._gated: set[str] = set()

    def _gated_tool(self, tool: BaseTool) -> BaseTool:
        agent = self

        async def propose(**kwargs: Any) -> str:
            if tool.name in {"save_reply_draft", "send_reply"} and not str(
                kwargs.get("to") or ""
            ).strip():
                raise ValueError(
                    "to is required for an email proposal; copy the latest sender's "
                    "full address from get_email_thread so the approval card can show it"
                )
            action = create_action(
                agent.session,
                conversation_id=agent.conversation.id,
                tool_name=tool.name,
                arguments=kwargs,
                source_message_id=agent.source_message_id,
                ttl_hours=agent.settings.assistant_action_ttl_hours,
            )
            return json.dumps(
                {
                    "ok": True,
                    "data": {
                        "status": "pending_user_approval",
                        "action_id": action.id,
                        "tool": action.tool_name,
                        "summary": action.summary,
                        "preview": action.preview,
                        "expires_at": action.expires_at.isoformat(),
                    },
                    "note": "已生成待确认提案，尚未执行任何飞书邮箱/日历写入。",
                },
                ensure_ascii=False,
                default=str,
            )

        return StructuredTool.from_function(
            coroutine=propose,
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
        )

    def build_tools(self) -> list[BaseTool]:
        tools: list[BaseTool] = []
        for tool in self.client.tools():
            if self.client.requires_approval(tool.name):
                tools.append(self._gated_tool(tool))
                self._gated.add(tool.name)
            else:
                tools.append(tool)
        return tools

    def _build_graph(self):
        tools = self.build_tools()
        tool_node = ToolNode(
            tools, name="assistant_tools", handle_tool_errors=_tool_error
        )
        llm_with_tools = self.llm.bind_tools(tools)
        max_iterations = max(1, self.settings.assistant_max_iterations)

        async def agent_node(state: AssistantState) -> dict[str, Any]:
            system = SystemMessage(
                content=build_system_prompt(
                    now=datetime.now(timezone.utc),
                    timezone=self.settings.assistant_timezone,
                )
            )
            chunks: list[Any] = []
            async for chunk in llm_with_tools.astream(
                [system, *state["messages"]]
            ):
                chunks.append(chunk)
            if chunks:
                response = chunks[0]
                for chunk in chunks[1:]:
                    response = response + chunk
            else:
                response = AIMessageChunk(content="")
            return {
                "messages": [response],
                "iterations": state.get("iterations", 0) + 1,
            }

        async def tools_node(state: AssistantState) -> dict[str, Any]:
            update = await tool_node.ainvoke(state)
            proposals = list(state.get("proposals") or [])
            messages = list(update.get("messages") or [])
            pending_calls = next(
                (
                    message
                    for message in reversed(state["messages"])
                    if isinstance(message, AIMessage)
                    and getattr(message, "tool_calls", None)
                ),
                None,
            )
            for message in messages:
                if not isinstance(message, ToolMessage):
                    continue
                payload = parse_tool_result(message.content)
                data = payload.get("data") or {}
                if data.get("status") != "pending_user_approval":
                    continue
                args: dict[str, Any] = {}
                for call in getattr(pending_calls, "tool_calls", None) or []:
                    if call.get("id") == message.tool_call_id:
                        args = call.get("args") or {}
                        break
                proposals.append(
                    {
                        "id": data.get("action_id"),
                        "tool_name": data.get("tool"),
                        "summary": data.get("summary"),
                        "preview": data.get("preview") or {},
                        "status": "proposed",
                        "arguments": args,
                        "result": None,
                        "error": None,
                        "expires_at": data.get("expires_at"),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            return {"messages": messages, "proposals": proposals}

        def should_continue(state: AssistantState) -> str:
            if state.get("iterations", 0) >= max_iterations:
                logger.warning("assistant hit its iteration ceiling (%d)", max_iterations)
                return "stop"
            last = state["messages"][-1]
            return "tools" if getattr(last, "tool_calls", None) else "stop"

        graph = StateGraph(AssistantState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tools_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent", should_continue, {"tools": "tools", "stop": END}
        )
        graph.add_edge("tools", "agent")
        return graph.compile()

    async def astream(
        self, user_message: str, history: list[BaseMessage] | None = None
    ) -> AsyncIterator[AgentEvent]:
        graph = self._build_graph()
        state: AssistantState = {
            "messages": [*(history or []), HumanMessage(content=user_message)],
            "iterations": 0,
            "proposals": [],
        }
        streamed_parts: list[str] = []
        streamed_message_id: str | None = None
        try:
            async for mode, chunk in graph.astream(
                state,
                stream_mode=["updates", "messages"],
                config={"recursion_limit": self.settings.agent_recursion_limit},
            ):
                if mode == "messages":
                    message_chunk, metadata = chunk
                    if (metadata or {}).get("langgraph_node") != "agent":
                        continue
                    text = _message_text(getattr(message_chunk, "content", None))
                    if not text:
                        continue
                    message_id = getattr(message_chunk, "id", None)
                    if message_id != streamed_message_id:
                        streamed_message_id = message_id
                        streamed_parts = []
                    streamed_parts.append(text)
                    yield AgentEvent("token", {"text": text})
                    continue

                for node, update in chunk.items():
                    if not isinstance(update, dict):
                        continue
                    if node == "agent":
                        for message in update.get("messages") or []:
                            if not isinstance(message, AIMessage):
                                continue
                            for call in getattr(message, "tool_calls", None) or []:
                                yield AgentEvent(
                                    "tool_call",
                                    {
                                        "tool": call.get("name"),
                                        "args": call.get("args") or {},
                                        "label": _label(
                                            str(call.get("name") or ""),
                                            call.get("args") or {},
                                        ),
                                    },
                                )
                    elif node == "tools":
                        for message in update.get("messages") or []:
                            if isinstance(message, ToolMessage):
                                yield AgentEvent(
                                    "tool_result",
                                    {
                                        "tool_call_id": message.tool_call_id,
                                        "content": _message_text(message.content),
                                    },
                                )
                        for proposal in update.get("proposals") or []:
                            yield AgentEvent("proposal", proposal)
        except Exception as exc:
            logger.exception("assistant run failed")
            yield AgentEvent("error", {"message": translate_provider_error(exc)})
            return
        text = "".join(streamed_parts).strip()
        if not text:
            text = "本轮没有生成可见回复，请重试。"
        yield AgentEvent("done", {"content": text})
