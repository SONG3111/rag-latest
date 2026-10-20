"""Read-only LangGraph agent for asking questions about one meeting."""

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
from ..models import Meeting
from .prompts import build_meeting_chat_system_prompt
from .service import (
    format_timestamp,
    read_segment_range,
    search_segments,
)

logger = logging.getLogger(__name__)


class MeetingAgentState(TypedDict):
    messages: list[BaseMessage]
    iterations: int


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
    if tool_name == "search_transcript":
        return f"检索会议转写：{args.get('query', '')}"
    if tool_name == "read_transcript_range":
        start = int(args.get("start_seconds") or 0)
        return f"读取转写：从 {format_timestamp(start * 1000)} 开始"
    return tool_name


class MeetingAgent:
    """A bounded tool loop over one meeting's confirmed transcript."""

    def __init__(
        self,
        session: Session,
        meeting: Meeting,
        *,
        llm: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.meeting = meeting
        self.settings = settings or get_settings()
        if llm is None:
            if self.settings.meeting_summary_model.strip():
                llm = build_chat_model(
                    self.settings,
                    model=self.settings.meeting_summary_model.strip(),
                )
            else:
                llm = build_resilient_chat_model(self.settings)
        self.llm = llm

    def build_tools(self) -> list[BaseTool]:
        meeting_id = self.meeting.id
        session = self.session

        async def search_transcript(query: str, top_k: int = 5) -> str:
            hits = search_segments(
                session,
                meeting_id,
                query,
                top_k=max(1, min(int(top_k), 10)),
            )
            return json.dumps(
                {"ok": True, "data": {"results": hits}},
                ensure_ascii=False,
            )

        async def read_transcript_range(
            start_seconds: int = 0,
            end_seconds: int | None = None,
            max_chars: int = 6000,
        ) -> str:
            lines = read_segment_range(
                session,
                meeting_id,
                start_ms=max(0, int(start_seconds)) * 1000,
                end_ms=(
                    max(0, int(end_seconds)) * 1000
                    if end_seconds is not None
                    else None
                ),
                max_chars=max_chars,
            )
            return json.dumps(
                {"ok": True, "data": {"lines": lines}},
                ensure_ascii=False,
            )

        return [
            StructuredTool.from_function(
                coroutine=search_transcript,
                name="search_transcript",
                description=(
                    "按关键词检索当前会议已确认的转写片段。回答事实前先调用。"
                    "返回文本、起止毫秒和 H:MM:SS 时间戳。"
                ),
            ),
            StructuredTool.from_function(
                coroutine=read_transcript_range,
                name="read_transcript_range",
                description=(
                    "读取当前会议在指定秒数范围内的连续转写，最多返回 max_chars 个字符。"
                    "需要上下文或核对代词指代时使用。"
                ),
            ),
        ]

    def _build_graph(self):
        tools = self.build_tools()
        tool_node = ToolNode(tools, name="meeting_tools", handle_tool_errors=_tool_error)
        llm_with_tools = self.llm.bind_tools(tools)
        max_iterations = max(1, self.settings.meeting_max_iterations)

        async def agent_node(state: MeetingAgentState) -> dict[str, Any]:
            system = SystemMessage(
                content=build_meeting_chat_system_prompt(
                    now=datetime.now(timezone.utc),
                    timezone=self.settings.assistant_timezone,
                    title=self.meeting.title,
                    notes=self.meeting.notes,
                    summary=self.meeting.summary_markdown,
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

        def should_continue(state: MeetingAgentState) -> str:
            if state.get("iterations", 0) >= max_iterations:
                return "stop"
            return (
                "tools"
                if getattr(state["messages"][-1], "tool_calls", None)
                else "stop"
            )

        graph = StateGraph(MeetingAgentState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tool_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent", should_continue, {"tools": "tools", "stop": END}
        )
        graph.add_edge("tools", "agent")
        return graph.compile()

    async def astream(
        self,
        user_message: str,
        history: list[BaseMessage] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        graph = self._build_graph()
        state: MeetingAgentState = {
            "messages": [*(history or []), HumanMessage(content=user_message)],
            "iterations": 0,
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
        except Exception as exc:
            logger.exception("meeting chat failed")
            yield AgentEvent("error", {"message": translate_provider_error(exc)})
            return
        text = "".join(streamed_parts).strip()
        if not text:
            text = "会议中没有找到可回答该问题的内容。"
        yield AgentEvent("done", {"content": text})
