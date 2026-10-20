"""The read-only data-analysis sub-agent.

Shape
-----
A small tool-calling graph of its own (``agent`` → ``tools`` → ``agent``), invoked
by the workspace agent **as a tool** — the *supervisor (tool-calling)* arrangement
from LangGraph's multi-agent guide
(https://langchain-ai.github.io/langgraph/concepts/multi_agent/), which is also how
``langchain-ai/open_deep_research`` hands work to its research sub-agents. It is a
separate graph rather than a second prompt on the main one because the analysis loop
needs its own tool set, its own iteration budget, and an isolated context: the main
agent's transcript is about a *conversation*, not about the statistics being computed.

Read-only by construction
-------------------------
``build_tools`` keeps only the MCP tools the server advertises as ``readOnlyHint``
(a missing annotation would fail closed, since ``requires_approval`` is
``not read_only``). A write tool is therefore not merely discouraged by the prompt —
it is absent from the model's tool list and cannot be called at all. Nothing this
sub-agent does can change a user's file.

Why it returns a report instead of streaming
--------------------------------------------
Its intermediate steps are not surfaced as SSE events. The parent sees exactly one
``tool_call`` and one ``tool_result`` (the report plus the steps behind it), which
keeps the transport unchanged: no new event types, no stream merging.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Sequence, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..mcp_client import McpOfficeClient, parse_tool_result
from ..models import Workspace
from ..services.operations import build_tool_arguments
from .analysis_prompts import ANALYSIS_SYSTEM_PROMPT
from .graph import (
    KNOWLEDGE_TOOL_NAME,
    _failure_signature,
    _format_tool_error,
    _message_text,
    _with_error_hint,
    build_knowledge_tool,
)

logger = logging.getLogger(__name__)

MAX_STEP_ARG_CHARS = 60


@dataclass
class AnalysisResult:
    """What the sub-agent hands back to the tool wrapper in the main graph."""

    report: str
    # Same shape the REST layer emits (file / location / snippet / score), so the
    # parent can forward them into the citation panel without translation.
    citations: list[dict] = field(default_factory=list)
    # One entry per executed tool call: what data the report is grounded in.
    steps: list[dict] = field(default_factory=list)


class AnalysisState(TypedDict):
    messages: Annotated[Sequence[AnyMessage], add_messages]
    iterations: int
    citations: list[dict]
    steps: list[dict]
    last_failure: str | None
    failure_repeats: int


class DataAnalysisAgent:
    """Read-only analysis graph, built per delegation."""

    def __init__(
        self,
        session: Session,
        workspace: Workspace,
        client: McpOfficeClient,
        settings: Settings | None = None,
        *,
        llm=None,
        history: list[str] | None = None,
        trace=None,
    ) -> None:
        self.session = session
        self.workspace = workspace
        self.client = client
        self.settings = settings or get_settings()
        self._llm = llm
        # Same context the retrieval tool needs from the main agent: recent turns
        # (to resolve "那第二条呢" style follow-ups) and the run tracer.
        self._history = list(history or [])
        self._trace = trace

    # ------------------------------------------------------------------ #
    # context handed to build_knowledge_tool (duck-typed)
    # ------------------------------------------------------------------ #
    @property
    def history(self) -> list[str]:
        return self._history

    @property
    def trace(self):
        return self._trace

    @property
    def llm(self):
        if self._llm is None:
            from ..llm.providers import build_resilient_chat_model

            settings = self.settings
            override = (settings.analysis_model or "").strip()
            if override:
                # Analysis can be pointed at a stronger model without changing what
                # the conversational agent uses; the fallback chain still applies.
                settings = settings.model_copy(update={"llm_model": override})
            self._llm = build_resilient_chat_model(settings)
        return self._llm

    def _open_span(self, node: str, input_summary: dict[str, Any] | None = None):
        if self._trace is None:
            return contextlib.nullcontext({"output": {}, "error": None})
        return self._trace.span(node, input_summary)

    # ------------------------------------------------------------------ #
    # tools
    # ------------------------------------------------------------------ #
    def build_tools(self) -> list[BaseTool]:
        """Read-only tools plus retrieval — write tools are structurally excluded."""
        profiles = self.client.profiles()
        read_only = [
            tool
            for tool in self.client.tools()
            if profiles.get(tool.name) is not None and profiles[tool.name].read_only
        ]
        return [
            build_knowledge_tool(self.session, self.workspace.id, self.settings, self),
            *read_only,
        ]

    async def _prefix_tool_paths(self, request: ToolCallRequest, execute):
        """Prefix workspace-relative paths — the sandbox root is shared."""
        call = dict(request.tool_call)
        call["args"] = build_tool_arguments(self.workspace.id, call.get("args") or {})
        return await execute(request.override(tool_call=call))

    # ------------------------------------------------------------------ #
    # graph
    # ------------------------------------------------------------------ #
    def _build_graph(self):
        tools = self.build_tools()
        self._tool_node = ToolNode(
            tools,
            name="analysis_tools",
            handle_tool_errors=_format_tool_error,
            awrap_tool_call=self._prefix_tool_paths,
        )
        llm_with_tools = self.llm.bind_tools(tools)
        max_iterations = self.settings.analysis_max_iterations

        async def agent_node(state: AnalysisState) -> dict[str, Any]:
            messages = [SystemMessage(content=ANALYSIS_SYSTEM_PROMPT), *state["messages"]]
            chunks: list[Any] = []
            with self._open_span(
                "analysis_agent", {"history_messages": len(state["messages"])}
            ) as span:
                async for chunk in llm_with_tools.astream(messages):
                    chunks.append(chunk)
            if chunks:
                response = chunks[0]
                for chunk in chunks[1:]:
                    response = response + chunk
            else:  # pragma: no cover - a stream always yields at least one chunk
                response = AIMessage(content="")
            span["output"] = {
                "text_chars": len(_message_text(getattr(response, "content", None))),
                "tool_calls": [
                    call.get("name")
                    for call in getattr(response, "tool_calls", []) or []
                ],
            }
            return {
                "messages": [response],
                "iterations": state.get("iterations", 0) + 1,
            }

        async def tools_node(state: AnalysisState) -> dict[str, Any]:
            pending = next(
                (
                    message
                    for message in reversed(list(state["messages"]))
                    if isinstance(message, AIMessage) and getattr(message, "tool_calls", None)
                ),
                None,
            )
            with self._open_span(
                "analysis_tools",
                {
                    "calls": [
                        call.get("name")
                        for call in getattr(pending, "tool_calls", []) or []
                    ]
                },
            ) as span:
                update = await self._tool_node.ainvoke(state)
                span["output"] = {"results": len(update.get("messages") or [])}

            messages = list(update.get("messages") or [])
            last_failure = state.get("last_failure")
            failure_repeats = state.get("failure_repeats", 0)
            citations = list(state.get("citations") or [])
            steps = list(state.get("steps") or [])

            for position, message in enumerate(messages):
                if not isinstance(message, ToolMessage):
                    continue
                name = message.name or ""
                call_args: dict[str, Any] = {}
                for call in getattr(pending, "tool_calls", None) or []:
                    if call.get("id") == message.tool_call_id:
                        call_args = call.get("args") or {}
                        break

                payload = parse_tool_result(message.content)
                if payload.get("ok") is False:
                    # Identical args failing again means the model is spinning, not
                    # adapting; the escalating hint is what breaks that loop.
                    signature = _failure_signature(name, call_args)
                    failure_repeats = (
                        failure_repeats + 1 if last_failure == signature else 1
                    )
                    last_failure = signature
                    payload = _with_error_hint(payload, failure_repeats=failure_repeats)
                else:
                    last_failure, failure_repeats = None, 0

                data = payload.get("data") or {}
                if name == KNOWLEDGE_TOOL_NAME and payload.get("ok"):
                    from ..retrieval.pipeline import RetrievedChunk

                    for item in (data or {}).get("results", []):
                        citations.append(
                            RetrievedChunk(
                                chunk_id="",
                                text=item.get("text") or "",
                                rel_path=item.get("file") or "",
                                location=item.get("location") or "",
                                score=float(item.get("score") or 0.0),
                                score_source=str(item.get("score_source") or ""),
                            ).to_citation()
                        )

                steps.append({"tool": name, "args": _summarize_args(call_args)})
                messages[position] = ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                    tool_call_id=message.tool_call_id,
                    name=name,
                )

            return {
                "messages": messages,
                "citations": citations,
                "steps": steps,
                "last_failure": last_failure,
                "failure_repeats": failure_repeats,
            }

        def should_continue(state: AnalysisState) -> str:
            if state.get("iterations", 0) >= max_iterations:
                logger.warning("analysis sub-agent hit its iteration ceiling (%d)", max_iterations)
                return "stop"
            last = state["messages"][-1]
            if getattr(last, "tool_calls", None):
                return "tools"
            return "stop"

        graph = StateGraph(AnalysisState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tools_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent", should_continue, {"tools": "tools", "stop": END}
        )
        graph.add_edge("tools", "agent")
        return graph.compile()

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    async def arun(self, request: str) -> AnalysisResult:
        """Run the analysis loop once and return its report plus provenance."""
        user_message = (request or "").strip()
        graph = self._build_graph()
        state: AnalysisState = {
            "messages": [HumanMessage(content=user_message)],
            "iterations": 0,
            "citations": [],
            "steps": [],
            "last_failure": None,
            "failure_repeats": 0,
        }
        with self._open_span("analysis", {"request_chars": len(user_message)}) as span:
            final = await graph.ainvoke(
                state, config={"recursion_limit": self.settings.agent_recursion_limit}
            )
        messages = list(final.get("messages") or [])
        span["output"] = {"messages": len(messages)}

        report = ""
        for message in reversed(messages):
            if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
                text = _message_text(message.content).strip()
                if text:
                    report = text
                    break

        return AnalysisResult(
            report=report,
            citations=list(final.get("citations") or []),
            steps=list(final.get("steps") or []),
        )


def _summarize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Shorten arguments for the provenance list the parent agent is shown."""
    summary: dict[str, Any] = {}
    for key, value in (args or {}).items():
        text = str(value)
        summary[key] = text if len(text) <= MAX_STEP_ARG_CHARS else text[:MAX_STEP_ARG_CHARS] + "…"
    return summary
