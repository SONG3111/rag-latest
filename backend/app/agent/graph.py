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

import contextlib
import json
import logging
import operator
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
from langgraph.errors import NodeCancelledError
from langgraph.graph.message import RemoveMessage, add_messages
from sqlalchemy.orm import Session

from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolInvocationError
from pydantic import ValidationError

from ..config import Settings, get_settings
from ..mcp_client import McpOfficeClient, parse_tool_result
from ..models import Operation, Workspace
from ..retrieval.pipeline import Retriever
from ..services.operations import (
    build_tool_arguments,
    create_operation,
    extract_target_path,
    workspace_relative_path,
)
from .prompts import (
    EMPTY_ANSWER_NUDGE,
    FRESHNESS_NUDGE,
    KNOWLEDGE_TOOL_DESCRIPTION,
    MASKED_TOOLS_NOTICE,
    PROPOSAL_NUDGE,
    SYSTEM_PROMPT,
    history_summary_notice,
    stale_files_notice,
)

logger = logging.getLogger(__name__)

KNOWLEDGE_TOOL_NAME = "search_knowledge_base"

# A value (two or more digits, or a markdown table) inside a statement about documents
# is the signature of an answer that is quoting file content. Two digits rather than
# one keeps "我可以帮你做 1) 读取 2) 修改" from triggering a pointless extra round trip.
VALUE_CLAIM_PATTERN = re.compile(r"\d{2,}|\|")

# The answer announces a submitted proposal. Legitimate after a write tool ran (the
# state then carries proposals); a claim with zero proposals this turn means the model
# replayed the approval phrasing from history instead of calling the tool.
PROPOSAL_CLAIM_PATTERN = re.compile(r"已提交[^。\n]{0,20}提案")

# Guardrail hints (LangChain ToolNode style): a failed tool result is enriched with
# an actionable Chinese correction keyed by the error code, so the model fixes the
# arguments instead of re-reading an English message and guessing.
_TOOL_ERROR_HINTS: dict[str | None, str] = {
    "document_not_found": (
        "工作区里没有这个文件。不要自己缩写或猜测文件名——"
        "先调用 list_files，用返回的 path 原样重试。"
    ),
    "sheet_not_found": (
        "工作表名称不对。error.detail 里列出了所有可用的工作表名，请原样使用其一重试。"
    ),
    "file_locked": (
        "文件正被 Excel/WPS 占用。请先告知用户关闭文件；用户关闭后用相同参数重试即可。"
    ),
    "write_conflict": (
        "文件在你读取之后又被修改过（常见原因：你上一条提案刚被应用）。"
        "请重新 read_range 获取最新内容，再重新提交提案。"
    ),
    "invalid_range": (
        "参数越界：行号/列号从 1 开始、count ≥ 1，且不要超出 read_range 返回的 "
        "sheet_max_row。请核对后重试。"
    ),
    "merge_conflict": (
        "操作区域与已有的合并单元格重叠。请先用 get_doc_structure 查看 merged_ranges，"
        "再 unmerge_cells 取消相关合并后重试；或改用 update_cells 逐格修改。"
    ),
    "sheet_exists": (
        "同名工作表已存在。请换一个名称，或先确认是否要在现有工作表上操作。"
    ),
    "last_sheet": (
        "工作簿至少要保留一个工作表，最后一张表不能删除。"
        "如需清空内容，请用 update_cells 逐格清值。"
    ),
    "calculation_failed": (
        "算式或公式无法计算。工作簿公式必须带 path（和 sheet_name）并用 "
        "Excel 公式语法（如 =SUM(B2:D2)）；纯算式只能包含数字与四则运算。"
        "请修正后重试。"
    ),
    "invalid_proposal": "提案无法创建：请先用读取类工具确认文件路径、工作表与单元格，再重试。",
    "invalid_arguments": "参数不完整或类型不对。请对照该工具的参数说明修正后重试。",
    "unstructured_result": "工具执行出现意外错误。请检查参数后重试；如反复失败，请向用户说明。",
    "corrupt_document": "文件无法解析，可能已损坏。请告知用户重新上传。",
    "tool_failed": "工具执行失败。请检查参数后重试；如反复失败，请向用户说明原因。",
    None: "工具执行失败。请检查参数后重试；如反复失败，请向用户说明原因。",
}

_REPEAT_SUFFIX = (
    "（注意：你已用完全相同的参数连续失败 {n} 次。不要原样重试——"
    "先用读取类工具核实正确参数，或如实告知用户该操作无法完成。）"
)


def _with_error_hint(payload: dict[str, Any], *, failure_repeats: int = 0) -> dict[str, Any]:
    """Attach a Chinese correction hint to a failed tool result."""
    error = payload.get("error")
    if not isinstance(error, dict):
        return payload
    hint = _TOOL_ERROR_HINTS.get(error.get("code"), _TOOL_ERROR_HINTS[None])
    if failure_repeats >= 2:
        hint = f"{hint} {_REPEAT_SUFFIX.format(n=failure_repeats)}"
    error["hint"] = hint
    return payload


def _failure_signature(name: str, call_args: dict[str, Any]) -> str:
    try:
        return f"{name}|{json.dumps(call_args, ensure_ascii=False, sort_keys=True, default=str)[:300]}"
    except (TypeError, ValueError):
        return f"{name}|{call_args!r}"


def _format_tool_error(exc: Exception) -> str:
    """LangGraph ToolNode error handler (handle_tool_errors=callable).

    The returned string becomes the ToolMessage content verbatim, so failures are
    formatted as the same JSON envelope successful tools use — with a Chinese
    message the model can act on instead of a raw traceback.
    """
    if isinstance(exc, ToolInvocationError):
        errors = exc.filtered_errors or []
        detail = "; ".join(
            f"{'.'.join(str(loc) for loc in e.get('loc', ()))}: {e.get('msg', '')}"
            for e in errors
        ) or str(exc.source)
        payload = {
            "ok": False,
            "error": {
                "code": "invalid_arguments",
                "message": f"参数不符合工具「{exc.tool_name}」的要求: {detail}；请修正后重试",
            },
        }
    elif isinstance(exc, ToolProposalError):
        payload = {"ok": False, "error": {"code": "invalid_proposal", "message": str(exc)}}
    else:
        payload = {
            "ok": False,
            "error": {"code": "tool_failed", "message": f"工具执行失败: {exc}；请检查参数后重试"},
        }
    return json.dumps(payload, ensure_ascii=False, default=str)

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
    last_failure: str | None
    failure_repeats: int
    # Fallback-chain switches ("已切换备用模型") raised by the resilient model
    # wrapper. add_messages-style accumulation because the agent node runs once
    # per tool-loop iteration and each may switch models.
    notices: Annotated[list[str], operator.add]


@dataclass
class AgentEvent:
    """One frame of the SSE stream consumed by the frontend."""

    type: Literal[
        "token",
        # Reasoning-channel fragments; the frontend folds them into a collapsed
        # "thinking" section. Never persisted with the answer.
        "thinking",
        # User-facing status notices (model fallback switches, timeout notes).
        "notice",
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
    if name in {
        "update_cells", "set_formula", "insert_rows", "delete_rows", "format_range",
        "insert_columns", "delete_columns", "copy_range", "delete_range",
        "merge_cells", "unmerge_cells", "find_replace", "manage_sheets",
    }:
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
        # Rolling summary of turns older than the recent window (memory compaction).
        self._history_summary: str | None = None
        # Write tools are masked out on lookup-intent turns (intent gate).
        self._mask_writes: bool = False
        # Optional run tracer (app.services.tracing.TraceCollector); None disables
        # all instrumentation, which keeps the graph usable without observability.
        self._trace = None

    # ------------------------------------------------------------------ #
    # tools
    # ------------------------------------------------------------------ #
    @property
    def llm(self):
        if self._llm is None:
            # Resilient wrapper: primary + fallback chain with an in-process
            # circuit breaker (see app.llm.resilience). Degrades to a single
            # candidate when no fallback models are configured.
            from ..llm.providers import build_resilient_chat_model

            self._llm = build_resilient_chat_model(self.settings)
        return self._llm

    def _open_span(self, node: str, input_summary: dict[str, Any] | None = None):
        """Trace span when a collector is installed, else a no-op record dict."""
        if self._trace is None:
            return contextlib.nullcontext({"output": {}, "error": None})
        return self._trace.span(node, input_summary)

    def _knowledge_tool(self) -> BaseTool:
        """Wrap retrieval as a tool so the agent decides when to search."""
        session = self.session
        workspace_id = self.workspace.id
        settings = self.settings
        agent = self

        def _search(query: str, top_k: int = 5) -> str:
            retriever = Retriever(session, workspace_id, settings)
            hits = retriever.search(
                query, top_k=top_k, history=agent._history, trace=agent._trace
            )
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
        tools: list[BaseTool] = [self._knowledge_tool()]
        self._gated_write_names: set[str] = set()
        for tool in self.client.tools():
            if self.client.requires_approval(tool.name):
                # A write tool that never executes: the framework ToolNode runs
                # whatever it is handed, so the approval gate is packed into a
                # proxy tool that records a proposal and returns the
                # pending-approval payload without ever touching the file.
                if self._mask_writes:
                    # Lookup-intent turn: every write tool is approval-gated, so
                    # skipping the proxy here is the whole masking step — the
                    # model only ever sees read-only tools plus retrieval.
                    continue
                tools.append(self._gated_write_tool(tool))
                self._gated_write_names.add(tool.name)
            else:
                tools.append(tool)
        return tools

    async def _prefix_tool_paths(self, request: ToolCallRequest, execute):
        """Prefix model paths with the workspace id before a tool runs.

        The MCP sandbox root is the shared workspaces directory, so every path
        the model emits is workspace-relative and must be prefixed on its way to
        a tool — except for gated write tools, which never touch the filesystem
        and whose proposals store workspace-relative paths.
        """
        call = dict(request.tool_call)
        if call.get("name") not in self._gated_write_names:
            call["args"] = build_tool_arguments(self.workspace.id, call.get("args") or {})
        return await execute(request.override(tool_call=call))

    def _gated_write_tool(self, tool: BaseTool) -> BaseTool:
        agent = self
        name = tool.name

        async def propose(**kwargs: Any) -> str:
            enriched = agent._enrich_arguments(name, kwargs)
            # Validate the target before recording anything: models occasionally
            # shorten or mix up file names, and an unvalidated path would become
            # a proposal that can never be applied. A structured rejection here
            # lets the model retry with the exact path from its earlier reads.
            from ..services.files import resolve_workspace_path

            rel_path = extract_target_path(enriched)
            try:
                target = resolve_workspace_path(agent.workspace.id, rel_path)
            except Exception as exc:
                raise ToolProposalError(f"工作区里找不到文件「{rel_path}」: {exc}") from exc
            if not target.exists():
                raise ToolProposalError(
                    f"工作区里不存在文件「{rel_path}」；"
                    "请改用本轮 list_files / read_range 返回的确切路径重新提交提案"
                )
            diff = await agent._read_before_values(name, enriched)
            operation = create_operation(
                agent.session, agent.workspace.id, name, enriched, diff=diff
            )
            agent.session.flush()
            return json.dumps(
                {
                    "ok": True,
                    "data": {
                        "status": "pending_user_approval",
                        "operation_id": operation.id,
                        "tool": name,
                        "path": operation.rel_path,
                        "summary": operation.summary,
                        "diff": diff,
                    },
                    "note": "该修改已提交为待确认提案，尚未写入文件。",
                },
                ensure_ascii=False,
            )

        return StructuredTool.from_function(
            coroutine=propose,
            name=name,
            description=tool.description,
            args_schema=tool.args_schema,
        )

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
        # The framework node validates args against each tool's schema, runs the
        # calls, catches unknown tool names and failures, and formats errors via
        # our handler (handle_tool_errors=callable).
        self._tool_node = ToolNode(
            tools,
            name="tools",
            handle_tool_errors=_format_tool_error,
            awrap_tool_call=self._prefix_tool_paths,
        )
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
            if self._history_summary:
                system_prompts.append(
                    SystemMessage(content=history_summary_notice(self._history_summary))
                )
            if self._mask_writes:
                system_prompts.append(SystemMessage(content=MASKED_TOOLS_NOTICE))
            chunks: list[Any] = []
            with self._open_span(
                "agent",
                {"history_messages": len(state["messages"]), "masked_writes": self._mask_writes},
            ) as span:
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
            span["output"] = {
                "text_chars": len(_message_text(getattr(response, "content", None))),
                "tool_calls": [call.get("name") for call in getattr(response, "tool_calls", []) or []],
            }
            update: dict[str, Any] = {
                "messages": [response],
                "iterations": state.get("iterations", 0) + 1,
            }
            # Fallback switches are reported once per completed model call: the
            # bound wrapper accumulates them, this node forwards and clears them.
            notices = getattr(llm_with_tools, "notices", None)
            if notices:
                update["notices"] = list(notices)
                notices.clear()
            return update

        async def tools_node(state: AgentState) -> dict[str, Any]:
            # The framework ToolNode validates arguments against each tool's
            # schema, executes the calls, and routes unknown tool names,
            # validation errors and failures through _format_tool_error.
            # Everything project-specific — workspace scoping, citations,
            # proposal events, repeat-failure escalation — is post-processing
            # on the messages it returns.
            last_ai = next(
                (
                    message
                    for message in reversed(list(state["messages"]))
                    if isinstance(message, AIMessage) and getattr(message, "tool_calls", None)
                ),
                None,
            )
            with self._open_span(
                "tools",
                {"calls": [call.get("name") for call in getattr(last_ai, "tool_calls", []) or []]},
            ) as span:
                update = await self._tool_node.ainvoke(state)
                span["output"] = {"results": len(update.get("messages") or [])}
            messages = list(update.get("messages") or [])

            pending = next(
                (
                    message
                    for message in reversed(list(state["messages"]))
                    if isinstance(message, AIMessage) and getattr(message, "tool_calls", None)
                ),
                None,
            )

            last_failure = state.get("last_failure")
            failure_repeats = state.get("failure_repeats", 0)
            proposals: list[dict] = []
            citations: list[dict] = list(state.get("citations") or [])

            for message in messages:
                if not isinstance(message, ToolMessage):
                    continue
                name = message.name or ""
                payload = parse_tool_result(message.content)

                if payload.get("ok") is False:
                    # Correlate the failure with its call args: identical args
                    # failing again means the model is spinning, not adapting.
                    call_args: dict[str, Any] = {}
                    for call in getattr(pending, "tool_calls", None) or []:
                        if call.get("id") == message.tool_call_id:
                            call_args = call.get("args") or {}
                            break
                    signature = _failure_signature(name, call_args)
                    failure_repeats = failure_repeats + 1 if last_failure == signature else 1
                    last_failure = signature
                    payload = _with_error_hint(payload, failure_repeats=failure_repeats)
                else:
                    last_failure, failure_repeats = None, 0

                if name == "list_files" and payload.get("ok"):
                    payload = self._scope_listing(payload)

                data = payload.get("data") or {}
                if name == KNOWLEDGE_TOOL_NAME and payload.get("ok"):
                    # Reuse the same citation shape the REST layer emits, so the UI
                    # and the persisted message never disagree about field names.
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

                if isinstance(data, dict) and data.get("status") == "pending_user_approval":
                    proposals.append({
                        "operation_id": data.get("operation_id"),
                        "tool": name,
                        "summary": data.get("summary"),
                        "path": data.get("path"),
                        "diff": data.get("diff") or [],
                    })

                # Replace the message so hints and scoping are what the client sees.
                messages[messages.index(message)] = ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                    tool_call_id=message.tool_call_id,
                    name=name,
                )

            return {
                "messages": messages,
                "proposals": proposals,
                "citations": citations,
                "tool_calls_made": state.get("tool_calls_made", 0) + len(messages),
                "last_failure": last_failure,
                "failure_repeats": failure_repeats,
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
            # The answer claims a proposal was submitted, but no write tool ran
            # this turn: the model replayed the approval phrasing from history,
            # and the user's pending panel would stay empty. One nudge makes it
            # actually read the file and call the write tool.
            if (
                state.get("nudges", 0) == 0
                and isinstance(last, AIMessage)
                and not state.get("proposals")
                and PROPOSAL_CLAIM_PATTERN.search(_message_text(last.content))
            ):
                return "nudge"
            return "stop"

        async def nudge_node(state: AgentState) -> dict[str, Any]:
            draft = state["messages"][-1]
            draft_text = _message_text(getattr(draft, "content", "")).strip()
            if PROPOSAL_CLAIM_PATTERN.search(draft_text) and not state.get("proposals"):
                reason = "proposal"
            elif draft_text:
                reason = "stale"
            else:
                reason = "empty"
            logger.info("nudging the model (%s)", reason)
            with self._open_span("nudge", {"reason": reason}):
                # Drop the draft so the replacement answer is not a continuation of it —
                # otherwise the model restates the stale content and the user sees both.
                messages: list[AnyMessage] = []
                if isinstance(draft, AIMessage) and getattr(draft, "id", None):
                    messages.append(RemoveMessage(id=draft.id))
                nudge = {
                    "proposal": PROPOSAL_NUDGE,
                    "stale": FRESHNESS_NUDGE,
                    "empty": EMPTY_ANSWER_NUDGE,
                }[reason]
                messages.append(HumanMessage(content=nudge))
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

    def _enrich_arguments(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Attach the current file digest to write arguments.

        The digest is read here rather than asked of the model: it is bookkeeping
        the model has no way to know, and the whole point of the field is to
        detect a concurrent edit between proposal and approval. The model's own
        value — if it emits one — is always overwritten: models regenerate long
        hex strings token by token and hallucinate them, and a fabricated digest
        would poison the proposal into an unapplyable state.
        """
        enriched = dict(args)
        path = args.get("path") or args.get("filepath")
        if not path or name not in _EXCEL_WRITE_TOOLS:
            return enriched

        digest = self._file_digest(path)
        if digest:
            enriched["expected_digest"] = digest
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
    async def astream_direct(
        self,
        user_message: str,
        history: list[BaseMessage] | None = None,
        *,
        trace=None,
    ) -> AsyncIterator[AgentEvent]:
        """Answer without the agent loop: no tools, no retrieval, no graph.

        Used for chit-chat classified by the intent gate — the measured lesson was
        that greetings entering the tool loop burned minutes of tool calls for an
        answer the model already knew. Streams straight from the (resilient) chat
        model; tool-loop features like citations and proposals do not apply.
        """
        self._trace = trace
        messages: list[AnyMessage] = [
            SystemMessage(content=SYSTEM_PROMPT),
            *(history or []),
            HumanMessage(content=user_message),
        ]
        streamed: list[str] = []
        try:
            with self._open_span("direct", {"prompt_chars": len(user_message)}) as span:
                async for chunk in self.llm.astream(messages):
                    text = _message_text(getattr(chunk, "content", None))
                    if not text:
                        continue
                    streamed.append(text)
                    yield AgentEvent("token", {"text": text})
                span["output"] = {"text_chars": len("".join(streamed))}
        except Exception as exc:
            logger.exception("direct answer failed")
            from ..llm.resilience import translate_provider_error

            yield AgentEvent("error", {"message": translate_provider_error(exc)})
            return
        yield AgentEvent("done", {"content": "".join(streamed).strip()})

    def _compact_for_overflow(self) -> bool:
        """Durable half of the overflow recovery: force a compaction fold.

        The provider confirmed the request was too large, so the normal trigger
        is bypassed and the keep budget halves (dsh compaction-basic: overflow
        "may force a useful balanced reduction even below the normal
        threshold"). Moving the bookmark means the *next* turn's history starts
        slim; this turn shrinks in memory instead. Runs on the cheap rewrite
        channel and fails open — a failed fold still leaves the in-memory head
        reduction for the retry.
        """
        from ..services.memory import compact_memory

        try:
            compacted = compact_memory(
                self.session, self.workspace.id, self.settings, force=True
            )
            self.session.commit()
            return compacted
        except Exception as exc:
            logger.warning("emergency compaction before overflow retry failed: %s", exc)
            self.session.rollback()
            return False

    async def astream(
        self,
        user_message: str,
        history: list[BaseMessage] | None = None,
        *,
        stale_files: list[tuple[str, str]] | None = None,
        history_summary: str | None = None,
        mask_writes: bool = False,
        trace=None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn, yielding events as they happen."""
        # Snapshot the conversation for query rewriting. The knowledge tool is a plain
        # callable with no access to the graph state, so the context is handed to it
        # here rather than threaded through the tool signature.
        self._history = _render_history(history or [])
        self._stale_files = list(stale_files or [])
        self._history_summary = (history_summary or "").strip() or None
        self._mask_writes = mask_writes
        self._trace = trace

        graph = self._build_graph()
        messages: list[AnyMessage] = [*(history or []), HumanMessage(content=user_message)]
        state: AgentState = {
            "messages": messages,
            "iterations": 0,
            "citations": [],
            "proposals": [],
            "tool_calls_made": 0,
            "nudges": 0,
            "last_failure": None,
            "failure_repeats": 0,
            "notices": [],
        }

        final_text = ""
        streamed_message_id: str | None = None
        # Tokens are only surfaced for the agent node, and only for its final answer.
        # The tool-selection call also produces (empty) content chunks per iteration,
        # so the message id is tracked to avoid interleaving separate calls.
        streamed_parts: list[str] = []

        # At most one context-overflow retry (dsh compaction-basic's overflow
        # recovery): a provider-confirmed overflow before the first packet
        # authorizes a forced compaction plus one head-reduced retry. Tokens
        # already streamed belong to the user — past that point an error
        # surfaces instead of a retry, same as any mid-stream failure.
        attempt = 0
        while True:
            try:
                async for mode, chunk in graph.astream(
                    state, stream_mode=["updates", "messages"]
                ):
                    if mode == "messages":
                        message_chunk, metadata = chunk
                        if (metadata or {}).get("langgraph_node") != "agent":
                            continue
                        # Reasoning fragments stream to their own SSE event so the UI
                        # can show what the model is deliberating; they must never
                        # count as answer text (an answer that lives only in the
                        # thinking channel still triggers the empty-answer nudge).
                        reasoning = _reasoning_text(message_chunk)
                        if reasoning:
                            yield AgentEvent("thinking", {"text": reasoning})
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
                            for notice in update.get("notices") or []:
                                yield AgentEvent("notice", {"message": notice})
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
            except NodeCancelledError:
                # A cancelled node means the turn was aborted (client disconnect or
                # the turn-level timeout); the SSE layer persists the partial answer,
                # so the cancellation must not become an error frame here.
                raise
            except Exception as exc:
                from ..llm.resilience import is_context_overflow_error

                if (
                    attempt == 0
                    and not streamed_parts
                    and is_context_overflow_error(exc)
                ):
                    attempt += 1
                    yield AgentEvent(
                        "notice",
                        {"message": "上下文超过模型限制，正在压缩对话历史后重试。"},
                    )
                    from ..services.memory import shrink_history

                    # Durable half: fold fat older turns into the summary so the
                    # next turn's bookmarked history starts slim. This turn's
                    # shrink is the in-memory head reduction below.
                    self._compact_for_overflow()
                    history = shrink_history(
                        history, max(self.settings.memory_keep_recent_tokens // 2, 1)
                    )
                    messages = [*history, HumanMessage(content=user_message)]
                    state = {
                        "messages": messages,
                        "iterations": 0,
                        "citations": [],
                        "proposals": [],
                        "tool_calls_made": 0,
                        "nudges": 0,
                        "last_failure": None,
                        "failure_repeats": 0,
                        "notices": [],
                    }
                    graph = self._build_graph()
                    streamed_message_id = None
                    streamed_parts = []
                    continue
                logger.exception("agent run failed")
                # Provider errors are translated to one actionable sentence here, so
                # the SSE error frame never carries a raw vendor traceback.
                from ..llm.resilience import translate_provider_error

                yield AgentEvent("error", {"message": translate_provider_error(exc)})
                return
            break

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
    "insert_columns",
    "delete_columns",
    "copy_range",
    "delete_range",
    "merge_cells",
    "unmerge_cells",
    "find_replace",
    "manage_sheets",
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


# Content-block type names that carry reasoning across the providers/langchain
# versions this deployment may run: DashScope compatible-mode puts reasoning in
# ``additional_kwargs["reasoning_content"]``, while OpenAI-shaped providers surface
# block types like "thinking" (Anthropic-style) or "reasoning".
_REASONING_BLOCK_TYPES = {"reasoning", "reasoning_content", "thinking"}
_REASONING_BLOCK_FIELDS = ("thinking", "reasoning_content", "text")


def _reasoning_text(message: Any) -> str:
    """Extract reasoning-channel text from a streamed chunk, or "" if none.

    Deliberately does not consult ``message.content`` strings: only the dedicated
    reasoning fields qualify, so ordinary text can never leak into the thinking UI.
    """
    fragments: list[str] = []
    additional = getattr(message, "additional_kwargs", None) or {}
    # Some providers wrap reasoning blocks inside kwargs (e.g. thinking_blocks)
    # rather than a bare reasoning_content string; catch both shapes.
    for value in additional.values():
        if isinstance(value, dict) and value.get("type") in _REASONING_BLOCK_TYPES:
            fragments.append(_reasoning_fragment_text(value))
    reasoning_content = additional.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        fragments.append(reasoning_content)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in _REASONING_BLOCK_TYPES:
                fragments.append(_reasoning_fragment_text(block))
    return "".join(fragments)


def _reasoning_fragment_text(block: dict[str, Any]) -> str:
    for field_name in _REASONING_BLOCK_FIELDS:
        value = block.get(field_name)
        if isinstance(value, str) and value:
            return value
    return ""


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
