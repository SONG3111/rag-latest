"""The LangGraph agent and its streaming protocol.

Shape of the graph: a single agent node decides, a tools node executes, and a
conditional edge decides whether to loop. Tool-calling loops are the part of agent
design that most often goes wrong in practice, so two guardrails are explicit:
the loop is bounded by ``agent_max_iterations``, and any destructive tool is
*intercepted* rather than executed.

The approval gate deliberately does not use LangGraph's ``interrupt`` primitive. An
interrupt requires a checkpointer and a resumable thread, which would make the write
path depend on graph state that must survive a server restart. Instead the agent
stops, hands the proposal back to the caller, and the write is replayed later by an
explicit REST call. That keeps approval durable in the database, auditable, and
reversible without any graph-level state.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, AsyncIterator, Literal, Sequence, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import RemoveMessage, add_messages
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..mcp_client import McpOfficeClient, parse_tool_result
from ..models import Operation, Workspace
from ..retrieval.pipeline import Retriever
from ..services.operations import (
    OperationError,
    build_tool_arguments,
    create_operation,
    workspace_relative_path,
)
from .prompts import (
    EMPTY_ANSWER_NUDGE,
    FRESHNESS_NUDGE,
    KNOWLEDGE_TOOL_DESCRIPTION,
    SYSTEM_PROMPT,
    stale_files_notice,
)

logger = logging.getLogger(__name__)

KNOWLEDGE_TOOL_NAME = "search_knowledge_base"

# A value (two or more digits, or a markdown table) inside a statement about documents
# is the signature of an answer that is quoting file content. Two digits rather than
# one keeps "我可以帮你做 1) 读取 2) 修改" from triggering a pointless extra round trip.
VALUE_CLAIM_PATTERN = re.compile(r"\d{2,}|\|")

# Statements about the workspace's documents, as opposed to small talk. Used to decide
# whether an ungrounded answer is worth sending back for a fresh read.
DOCUMENT_HINT_PATTERN = re.compile(
    r"\.(?:xlsx|xlsm|docx)|表格|工作表|单元格|文档|文件|条款|段落|附件|数据|"
    r"表(?![示现达情明白态演扬决])|[0-9]+\s*行"
)


class ToolProposalError(RuntimeError):
    """Raised when a proposed write cannot be turned into an approval request."""


class AgentState(TypedDict):
    messages: Annotated[Sequence[AnyMessage], add_messages]
    iterations: int
    citations: list[dict]
    proposals: list[dict]
    tool_calls_made: int
    nudges: int


@dataclass
class AgentEvent:
    """One frame of the SSE stream consumed by the frontend."""

    type: Literal[
        "token",
        "tool_call",
        "tool_result",
        "proposal",
        "citations",
        "done",
        "error",
    ]
    data: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


def _summarize_tool_call(name: str, args: dict[str, Any]) -> str:
    """Human-readable label shown while the agent is working."""
    if name == KNOWLEDGE_TOOL_NAME:
        return f"正在检索知识库：{args.get('query', '')}"
    if name == "list_files":
        return "正在查看工作区文件"
    if name == "get_doc_structure":
        return f"正在查看文档结构：{args.get('path', '')}"
    if name == "read_range":
        return f"正在读取 {args.get('path', '')} 的 {args.get('start_cell', 'A1')}"
    if name == "read_paragraphs":
        return f"正在读取 {args.get('path', '')} 的段落"
    if name == "read_table":
        return f"正在读取 {args.get('path', '')} 的表格"
    if name == "find_text":
        return f"正在查找「{args.get('query', args.get('text', ''))}」"
    if name in {"update_cells", "set_formula", "insert_rows", "delete_rows", "format_range"}:
        return f"准备修改表格：{args.get('path', '')}"
    if name in {"replace_text", "update_table_cell"}:
        return f"准备修改文档：{args.get('path', '')}"
    return f"正在调用 {name}"


class WorkspaceAgent:
    """Wraps the compiled graph plus everything it needs per conversation."""

    def __init__(
        self,
        session: Session,
        workspace: Workspace,
        client: McpOfficeClient,
        settings: Settings | None = None,
        *,
        llm=None,
    ) -> None:
        self.session = session
        self.workspace = workspace
        self.client = client
        self.settings = settings or get_settings()
        self._llm = llm
        # Recent conversation turns, used to resolve elliptical follow-up questions
        # ("那第三条呢") during query rewriting.
        self._history: list[str] = []
        # Files that changed since the previous turn, as ``(rel_path, changed_at)``.
        # Empty means the conversation's own earlier answers are still safe to reuse.
        self._stale_files: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ #
    # tools
    # ------------------------------------------------------------------ #
    @property
    def llm(self):
        if self._llm is None:
            from ..llm.providers import build_chat_model

            self._llm = build_chat_model(self.settings)
        return self._llm

    def _knowledge_tool(self) -> BaseTool:
        """Wrap retrieval as a tool so the agent decides when to search."""
        session = self.session
        workspace_id = self.workspace.id
        settings = self.settings
        agent = self

        def _search(query: str, top_k: int = 5) -> str:
            retriever = Retriever(session, workspace_id, settings)
            hits = retriever.search(query, top_k=top_k, history=agent._history)
            run = retriever.last_run
            meta = {
                "original_query": run.original_query,
                "effective_query": run.effective_query,
                "rewritten": run.rewritten,
                "relevance_threshold": run.threshold_applied,
            }
            if not hits:
                return json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "results": [],
                            "retrieval": meta,
                            "note": (
                                "知识库中没有找到相关内容。"
                                "请直接告诉用户没有找到，不要用常识或推测补充答案。"
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
            payload = {
                "results": [hit.to_reference() for hit in hits],
                "retrieval": meta,
                "note": (
                    "score 是交叉编码器算出的相关性分数（0-1）。"
                    "分数普遍偏低说明知识库可能没有答案，此时应如实说明而不是猜测。"
                ),
            }
            return json.dumps({"ok": True, "data": payload}, ensure_ascii=False)

        return StructuredTool.from_function(
            func=_search,
            name=KNOWLEDGE_TOOL_NAME,
            description=KNOWLEDGE_TOOL_DESCRIPTION,
        )

    def build_tools(self) -> list[BaseTool]:
        return [self._knowledge_tool(), *self.client.tools()]

    def _scope_listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Keep only the active workspace's files in a ``list_files`` result.

        The MCP sandbox root is the shared ``workspaces`` directory, so the raw
        listing covers every workspace — and also files that were deleted from the
        UI but whose bytes are still on disk. Handing that list to the model invites
        it to read a stale or unrelated file and then answer about the wrong data,
        so paths are filtered down to the current workspace and rewritten to be
        workspace-relative, matching what the other tools expect back.
        """
        if not payload.get("ok"):
            return payload
        data = payload.get("data")
        if not isinstance(data, dict):
            return payload
        files = data.get("files")
        if not isinstance(files, list):
            return payload

        # Intersect with the user's own file list. The model should see exactly what
        # the workspace shows in the UI, not whatever bytes happen to sit on disk.
        from sqlalchemy import select

        from ..models import DocumentFile

        tracked = {
            row.rel_path
            for row in self.session.scalars(
                select(DocumentFile).where(
                    DocumentFile.workspace_id == self.workspace.id
                )
            )
        }

        prefix = f"{self.workspace.id}/"
        scoped: list[dict[str, Any]] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix) :]
            if relative not in tracked:
                continue
            scoped.append({**item, "path": relative})
        return {**payload, "data": {**data, "files": scoped}}

    # ------------------------------------------------------------------ #
    # graph
    # ------------------------------------------------------------------ #
    def _build_graph(self):
        tools = self.build_tools()
        tool_map = {tool.name: tool for tool in tools}
        llm_with_tools = self.llm.bind_tools(tools)
        max_iterations = self.settings.agent_max_iterations

        async def agent_node(state: AgentState) -> dict[str, Any]:
            # Streamed rather than awaited whole. The node still returns a complete
            # AIMessage, but LangGraph's "messages" stream mode exposes the tokens of
            # each internal call as it arrives, which is what lets the UI type the
            # answer out instead of freezing until the model finishes.
            system_prompts = [SystemMessage(content=SYSTEM_PROMPT)]
            if self._stale_files:
                system_prompts.append(
                    SystemMessage(content=stale_files_notice(self._stale_files))
                )
            chunks: list[Any] = []
            async for chunk in llm_with_tools.astream(
                [*system_prompts, *state["messages"]]
            ):
                chunks.append(chunk)
            if chunks:
                response = chunks[0]
                for chunk in chunks[1:]:
                    response = response + chunk
            else:  # pragma: no cover - a stream always yields at least one chunk
                response = AIMessage(content="")
            return {
                "messages": [response],
                "iterations": state.get("iterations", 0) + 1,
            }

        async def tools_node(state: AgentState) -> dict[str, Any]:
            # Find the most recent AI message that requested tools. Walking backwards
            # rather than taking the tail keeps this correct regardless of how the
            # message reducer orders the accumulated state.
            pending = next(
                (
                    message
                    for message in reversed(list(state["messages"]))
                    if isinstance(message, AIMessage) and getattr(message, "tool_calls", None)
                ),
                None,
            )
            if pending is None:
                return {"messages": []}

            outputs: list[BaseMessage] = []
            proposals: list[dict] = []
            citations: list[dict] = list(state.get("citations") or [])

            for call in getattr(pending, "tool_calls", []) or []:
                name = call.get("name", "")
                args = call.get("args") or {}
                tool = tool_map.get(name)

                if tool is None:
                    outputs.append(
                        ToolMessage(
                            content=json.dumps(
                                {"ok": False, "error": {"code": "unknown_tool", "message": name}},
                                ensure_ascii=False,
                            ),
                            tool_call_id=call.get("id", ""),
                        )
                    )
                    continue

                # Gate: a write becomes a proposal instead of an invocation. Only
                # MCP-provided tools are governed here; locally defined tools such as
                # the knowledge-base search are read-only by construction.
                if self.client.is_governed_tool(name) and self.client.requires_approval(name):
                    try:
                        proposal, operation = await self._propose(name, args)
                    except (OperationError, ToolProposalError) as exc:
                        logger.warning("cannot propose %s: %s", name, exc)
                        outputs.append(
                            ToolMessage(
                                content=json.dumps(
                                    {
                                        "ok": False,
                                        "error": {
                                            "code": "invalid_proposal",
                                            "message": str(exc),
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                                tool_call_id=call.get("id", ""),
                            )
                        )
                        continue
                    proposals.append(proposal)
                    outputs.append(
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "ok": True,
                                    "data": {
                                        "status": "pending_user_approval",
                                        "operation_id": operation.id,
                                        "summary": operation.summary,
                                    },
                                    "note": "该修改已提交为待确认提案，尚未写入文件。",
                                },
                                ensure_ascii=False,
                            ),
                            tool_call_id=call.get("id", ""),
                        )
                    )
                    continue

                try:
                    # MCP tools are async-only: StructuredTool raises on sync invocation.
                    # Paths are rewritten because the sandbox root is the shared
                    # workspaces directory, not the individual workspace.
                    raw = await tool.ainvoke(
                        build_tool_arguments(self.workspace.id, args)
                        if self.client.is_governed_tool(name)
                        else args
                    )
                except Exception as exc:
                    logger.warning("tool %s failed: %s", name, exc)
                    outputs.append(
                        ToolMessage(
                            content=json.dumps(
                                {"ok": False, "error": {"code": "tool_failed", "message": str(exc)}},
                                ensure_ascii=False,
                            ),
                            tool_call_id=call.get("id", ""),
                        )
                    )
                    continue

                payload = parse_tool_result(raw)
                if name == "list_files":
                    payload = self._scope_listing(payload)
                if name == KNOWLEDGE_TOOL_NAME and payload.get("ok"):
                    # Reuse the same citation shape the REST layer emits, so the UI
                    # and the persisted message never disagree about field names.
                    from ..retrieval.pipeline import RetrievedChunk

                    for item in (payload.get("data") or {}).get("results", []):
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

                outputs.append(
                    ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False, default=str),
                        tool_call_id=call.get("id", ""),
                    )
                )

            return {
                "messages": outputs,
                "proposals": proposals,
                "citations": citations,
                "tool_calls_made": state.get("tool_calls_made", 0) + len(pending.tool_calls or []),
            }

        def should_continue(state: AgentState) -> str:
            if state.get("iterations", 0) >= max_iterations:
                logger.warning("agent hit the iteration ceiling (%d)", max_iterations)
                return "stop"
            last = state["messages"][-1]
            if getattr(last, "tool_calls", None):
                return "tools"
            # An empty visible answer: reasoning-only finishes land here, because the
            # provider's reasoning channel is dropped before it reaches us.
            if (
                state.get("nudges", 0) == 0
                and isinstance(last, AIMessage)
                and not _message_text(last.content).strip()
            ):
                return "nudge"
            # The answer claims to know file contents, but nothing was read this turn:
            # it is being replayed from history, which may be out of date. Ask for one
            # fresh read. Exactly once, so a stubborn model cannot loop here.
            if (
                state.get("tool_calls_made", 0) == 0
                and state.get("nudges", 0) == 0
                and self._stale_files
                and isinstance(last, AIMessage)
                and VALUE_CLAIM_PATTERN.search(_message_text(last.content))
                and DOCUMENT_HINT_PATTERN.search(_message_text(last.content))
            ):
                return "nudge"
            return "stop"

        async def nudge_node(state: AgentState) -> dict[str, Any]:
            draft = state["messages"][-1]
            draft_text = _message_text(getattr(draft, "content", "")).strip()
            reason = "stale" if draft_text else "empty"
            logger.info("nudging the model (%s)", reason)
            # Drop the draft so the replacement answer is not a continuation of it —
            # otherwise the model restates the stale content and the user sees both.
            messages: list[AnyMessage] = []
            if isinstance(draft, AIMessage) and getattr(draft, "id", None):
                messages.append(RemoveMessage(id=draft.id))
            messages.append(
                HumanMessage(
                    content=FRESHNESS_NUDGE if reason == "stale" else EMPTY_ANSWER_NUDGE
                )
            )
            return {
                "messages": messages,
                "nudges": state.get("nudges", 0) + 1,
            }

        graph = StateGraph(AgentState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tools_node)
        graph.add_node("nudge", nudge_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent",
            should_continue,
            {"tools": "tools", "nudge": "nudge", "stop": END},
        )
        graph.add_edge("tools", "agent")
        graph.add_edge("nudge", "agent")
        return graph.compile()

    async def _propose(self, name: str, args: dict[str, Any]) -> tuple[dict, Operation]:
        """Record a pending write and derive its diff from a fresh read."""
        enriched = self._enrich_arguments(name, args)
        diff = await self._read_before_values(name, enriched)
        try:
            operation = create_operation(
                self.session, self.workspace.id, name, enriched, diff=diff
            )
        except OperationError as exc:
            # A write call without a resolvable target cannot become a proposal, and
            # fabricating one would be worse than telling the model to try again.
            raise ToolProposalError(str(exc)) from exc
        self.session.flush()
        return (
            {
                "operation_id": operation.id,
                "tool": name,
                "summary": operation.summary,
                "path": operation.rel_path,
                "diff": diff,
            },
            operation,
        )

    def _enrich_arguments(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Attach the current file digest to write arguments.

        The digest is read here rather than asked of the model: it is bookkeeping the
        model has no way to know, and the whole point of the field is to detect a
        concurrent edit between proposal and approval.
        """
        enriched = dict(args)
        path = args.get("path") or args.get("filepath")
        if not path or name not in _EXCEL_WRITE_TOOLS:
            return enriched

        digest = self._file_digest(path)
        if digest:
            enriched.setdefault("expected_digest", digest)
        return enriched

    def _file_digest(self, rel_path: str) -> str | None:
        import hashlib

        from ..services.files import resolve_workspace_path

        try:
            target = resolve_workspace_path(self.workspace.id, rel_path)
        except Exception:
            return None
        if not target.exists():
            return None
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()

    async def _read_before_values(self, name: str, args: dict[str, Any]) -> list[dict]:
        """Read the cells a proposal intends to change, so the UI can show a diff."""
        if name != "update_cells":
            return []
        raw_path = args.get("path")
        sheet = args.get("sheet_name")
        updates = args.get("updates") or []
        if not raw_path or not sheet or not updates:
            return []

        tool = self.client.tool("read_range")
        if tool is None:
            return []

        # The sandbox root is the shared workspaces directory, so every path handed
        # to a tool must be prefixed with the workspace id.
        path = workspace_relative_path(self.workspace.id, raw_path)

        diff: list[dict] = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            coordinate = str(update.get("cell", "")).strip()
            if not coordinate:
                continue
            try:
                payload = parse_tool_result(
                    await tool.ainvoke(
                        {"path": path, "sheet_name": sheet, "start_cell": coordinate}
                    )
                )
                values = ((payload.get("data") or {}).get("values") or [[None]])[0][0]
            except Exception as exc:
                logger.warning("diff pre-read failed for %s!%s: %s", path, coordinate, exc)
                values = None
            diff.append(
                {
                    "cell": coordinate,
                    "before": values if not isinstance(values, dict) else values.get("cached_value"),
                    "after": update.get("value"),
                }
            )
        return diff

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    async def astream(
        self,
        user_message: str,
        history: list[BaseMessage] | None = None,
        *,
        stale_files: list[tuple[str, str]] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn, yielding events as they happen."""
        # Snapshot the conversation for query rewriting. The knowledge tool is a plain
        # callable with no access to the graph state, so the context is handed to it
        # here rather than threaded through the tool signature.
        self._history = _render_history(history or [])
        self._stale_files = list(stale_files or [])

        graph = self._build_graph()
        messages: list[AnyMessage] = [*(history or []), HumanMessage(content=user_message)]
        state: AgentState = {
            "messages": messages,
            "iterations": 0,
            "citations": [],
            "proposals": [],
            "tool_calls_made": 0,
            "nudges": 0,
        }

        final_text = ""
        streamed_message_id: str | None = None
        # Tokens are only surfaced for the agent node, and only for its final answer.
        # The tool-selection call also produces (empty) content chunks per iteration,
        # so the message id is tracked to avoid interleaving separate calls.
        streamed_parts: list[str] = []

        try:
            async for mode, chunk in graph.astream(
                state, stream_mode=["updates", "messages"]
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
                        # A new model call started; earlier text belonged to a
                        # different turn of the loop, so the buffer restarts.
                        streamed_message_id = message_id
                        streamed_parts = []
                    streamed_parts.append(text)
                    yield AgentEvent("token", {"text": text})
                    continue

                for node, update in chunk.items():
                    if not isinstance(update, dict):
                        continue

                    if node == "agent":
                        produced = update.get("messages") or []
                        for message in produced:
                            if not isinstance(message, AIMessage):
                                continue
                            for call in getattr(message, "tool_calls", []) or []:
                                yield AgentEvent(
                                    "tool_call",
                                    {
                                        "tool": call.get("name"),
                                        "args": call.get("args") or {},
                                        "label": _summarize_tool_call(
                                            call.get("name", ""), call.get("args") or {}
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
                        citations = update.get("citations") or []
                        if citations:
                            yield AgentEvent("citations", {"items": citations})
        except Exception as exc:
            logger.exception("agent run failed")
            yield AgentEvent("error", {"message": str(exc)})
            return

        # The streamed tokens are the authoritative text when the graph produced any;
        # `final_text` is the fallback for providers that do not emit chunks.
        streamed_text = "".join(streamed_parts).strip()
        if streamed_text:
            final_text = streamed_text

        yield AgentEvent("done", {"content": final_text})


_EXCEL_WRITE_TOOLS = {
    "update_cells",
    "set_formula",
    "insert_rows",
    "delete_rows",
    "format_range",
}


def _message_text(content: Any) -> str:
    """Flatten LangChain message content, which may be a string or content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


def _render_history(messages: list[BaseMessage], limit: int = 8) -> list[str]:
    """Render recent turns as ``角色: 内容`` lines for the rewriting prompt."""
    rendered: list[str] = []
    for message in messages[-limit:]:
        text = _message_text(message.content).strip()
        if not text:
            continue
        if isinstance(message, HumanMessage):
            rendered.append(f"用户: {text}")
        elif isinstance(message, AIMessage):
            rendered.append(f"助手: {text}")
    return rendered
