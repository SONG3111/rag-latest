"""Read-only LangGraph agent for grounded news follow-up."""

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
from ..models import AssistantMessage, MessageRole, NewsPreference
from .categories import category_id, category_label
from .prompts import build_news_chat_system_prompt
from .service import (
    event_detail,
    read_article_content,
    search_cached_news,
    search_live_news,
)

logger = logging.getLogger(__name__)


class NewsAgentState(TypedDict):
    messages: list[BaseMessage]
    iterations: int


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
            "error": {"code": "tool_call_failed", "message": str(exc)},
        },
        ensure_ascii=False,
    )


def _label(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "search_cached_news":
        return f"检索新闻缓存：{args.get('query', '')}"
    if tool_name == "search_live_news":
        return f"定向搜索新闻：{args.get('query', '')}"
    if tool_name == "get_news_event":
        return "读取新闻事件来源"
    if tool_name == "read_news_article":
        return "读取新闻正文"
    return tool_name


def _category_filter(
    value: str | list[str] | None,
    selected: list[str],
) -> list[str]:
    """Normalize the several shapes models use for an optional string array."""

    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                values = [str(item) for item in parsed] if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                values = [raw]
        else:
            values = raw.replace("，", ",").split(",")
    else:
        values = [str(item) for item in value]
    aliases = {category_label(item): item for item in selected}
    return list(
        dict.fromkeys(
            aliases.get(str(item).strip(), category_id(str(item).strip()))
            for item in values
            if str(item).strip()
        )
    )


class NewsAgent:
    """Bounded tool loop over cached news and a targeted live-search fallback."""

    def __init__(
        self,
        session: Session,
        *,
        selected_categories: list[str] | None = None,
        llm: Any | None = None,
        settings: Settings | None = None,
        live_fetcher: Any | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        if llm is None:
            if self.settings.news_model.strip():
                llm = build_chat_model(
                    self.settings, model=self.settings.news_model.strip()
                )
            else:
                llm = build_resilient_chat_model(self.settings)
        self.llm = llm
        preference = session.get(NewsPreference, "default")
        preferred = list(preference.categories if preference else [])
        topics = list(preference.custom_topics if preference else [])
        self.selected_categories = (
            selected_categories
            if selected_categories is not None
            else [*preferred, *(f"custom:{topic}" for topic in topics)]
        )
        self.live_fetcher = live_fetcher

    def build_tools(self) -> list[BaseTool]:
        async def search_cached(
            query: str,
            categories: str | list[str] | None = None,
            date_from: str | None = None,
            date_to: str | None = None,
            limit: int = 8,
        ) -> str:
            rows = search_cached_news(
                self.session,
                query=query,
                categories=_category_filter(categories, self.selected_categories)
                or self.selected_categories,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
            data: dict[str, Any] = {"results": rows}
            if not rows:
                data["next_action"] = (
                    "缓存没有命中。请改写关键词并调用 search_live_news 做国内源定向补搜；"
                    "仍为空时明确说明没有检索到相关报道。"
                )
            return json.dumps(
                {"ok": True, "data": data},
                ensure_ascii=False,
                default=str,
            )

        async def search_live(query: str, limit: int = 8) -> str:
            rows = await search_live_news(
                query,
                categories=self.selected_categories,
                fetcher=self.live_fetcher,
                settings=self.settings,
                limit=limit,
            )
            data: dict[str, Any] = {"results": rows}
            if not rows:
                data["next_action"] = (
                    "国内源仍未命中。可以扩大日期范围或用更短的主体关键词再试一次；"
                    "不要回答“没有收到问题”，也不要用常识补造事实。"
                )
            return json.dumps(
                {"ok": True, "data": data},
                ensure_ascii=False,
                default=str,
            )

        async def event_sources(event_id: str) -> str:
            row = event_detail(self.session, event_id)
            return json.dumps(
                (
                    {"ok": True, "data": row}
                    if row is not None
                    else {
                        "ok": False,
                        "error": {
                            "code": "event_not_found",
                            "message": "新闻事件不存在",
                        },
                    }
                ),
                ensure_ascii=False,
                default=str,
            )

        async def article(article_id: str) -> str:
            row = await read_article_content(
                self.session, article_id, settings=self.settings
            )
            return json.dumps(
                (
                    {"ok": True, "data": row}
                    if row is not None
                    else {
                        "ok": False,
                        "error": {
                            "code": "article_not_found",
                            "message": "新闻文章不存在",
                        },
                    }
                ),
                ensure_ascii=False,
                default=str,
            )

        return [
            StructuredTool.from_function(
                coroutine=search_cached,
                name="search_cached_news",
                description=(
                    "检索最近 30 天已缓存的新闻。回答多数新闻问题先调用此工具；"
                    "可按分类和 YYYY-MM-DD 日期范围筛选。"
                ),
            ),
            StructuredTool.from_function(
                coroutine=search_live,
                name="search_live_news",
                description=(
                    "缓存结果不足或用户明确要最新消息时，按关键词定向搜索最近 7 天新闻。"
                ),
            ),
            StructuredTool.from_function(
                coroutine=event_sources,
                name="get_news_event",
                description="读取一个新闻事件及其全部候选来源、链接和发布时间。",
            ),
            StructuredTool.from_function(
                coroutine=article,
                name="read_news_article",
                description=(
                    "按 article_id 读取新闻正文；正文抽取失败时返回已保存的订阅摘要和警告。"
                ),
            ),
        ]

    def _build_graph(self):
        tools = self.build_tools()
        tool_node = ToolNode(tools, name="news_tools", handle_tool_errors=_tool_error)
        llm_with_tools = self.llm.bind_tools(tools)
        max_iterations = max(1, self.settings.news_max_iterations)

        async def agent_node(state: NewsAgentState) -> dict[str, Any]:
            system = SystemMessage(
                content=build_news_chat_system_prompt(
                    now=datetime.now(timezone.utc),
                    timezone=self.settings.news_timezone,
                    selected_categories=[
                        category_label(item) for item in self.selected_categories
                    ],
                )
            )
            chunks: list[Any] = []
            async for chunk in llm_with_tools.astream([system, *state["messages"]]):
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

        def should_continue(state: NewsAgentState) -> str:
            if state.get("iterations", 0) >= max_iterations:
                return "stop"
            return (
                "tools"
                if getattr(state["messages"][-1], "tool_calls", None)
                else "stop"
            )

        graph = StateGraph(NewsAgentState)
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
        state: NewsAgentState = {
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
            logger.exception("news agent failed")
            yield AgentEvent("error", {"message": translate_provider_error(exc)})
            return
        text = "".join(streamed_parts).strip()
        if not text:
            text = "本轮没有生成可见回复，请重试。"
        yield AgentEvent("done", {"content": text})
