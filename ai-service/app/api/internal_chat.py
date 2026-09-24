"""POST /v1/chat/stream：backend-java 专用的无状态聊天编排端点。

契约要点（设计见 docs/ 架构迁移与 MIGRATION-PROGRESS.md §三）：

* Java 预组装请求：全量升序消息（封顶 500 条）+ 滚动摘要书签 + stale files +
  tracked 文件清单。**历史裁剪留在本端**——压缩本来就需要全量消息，一次传输
  避免两套裁剪逻辑（与已批准计划的偏差之一）。
* SSE 帧分两类：可转发帧（token/thinking/notice/tool_call/tool_result/citations/
  followups/proposal）原样给前端；最后一帧恒为内部 ``persist``，携带本轮的
  权威内容、提案、trace 节点与新摘要，由 Java 落库后合成对外的 done。
* proposal 帧带全 ``arguments``、``operation_id`` 为 null——Java 拦截帧时生成
  id、建 Operation 行、替换后转发。
* 本端不写任何数据库：检索语料经 httpx 回读 Java 的 /internal 端点（每次
  search 一次），提案只进 runtime 内存。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import NodeCancelledError
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..agent.graph import AgentRuntime, WorkspaceAgent
from ..agent.intent import classify_intent
from ..config import Settings, get_settings
from ..llm.resilience import translate_provider_error
from ..retrieval.pipeline import ChunkRecord
from ..services.followups import generate_followups
from ..services.memory import compute_compaction
from ..services.token_budget import estimate_tokens
from ..services.tracing import TraceCollector
from .routes import compose_answer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


class ChatMessageIn(BaseModel):
    """一条历史消息；role 为 "user" | "assistant"（与 messages 表的取值一致）。"""

    role: str
    content: str = ""
    tool_calls: list[dict] | None = None


class StaleFileIn(BaseModel):
    rel_path: str
    changed_at: str = ""


class InternalChatRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    # Java 生成并持久化的本轮 run id（run_traces / done 帧共用）。
    run_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=8000)
    # 单轮墙钟预算（秒）；缺省回退到本服务自己的 chat_turn_timeout。
    timeout_seconds: float | None = Field(default=None, gt=0)
    # 全量升序消息（Java 侧封顶 500 条）。
    messages: list[ChatMessageIn] = Field(default_factory=list)
    # 超过封顶时的消息总数（covered 算术用）；缺省按 len(messages) 处理。
    total_messages: int | None = Field(default=None, ge=0)
    summary: str | None = None
    covered_count: int = Field(default=0, ge=0)
    stale_files: list[StaleFileIn] = Field(default_factory=list)
    # 工作区登记在册的文件清单（scope_listing 过滤用）。
    files: list[str] = Field(default_factory=list)


@dataclass
class _TurnRow:
    """压缩用的合成行：请求消息与本轮 user/assistant 行共用一个鸭子类型。"""

    role: str
    content: str
    tool_calls: list | None = None


def _get_client(request: Request):
    client = getattr(request.app.state, "mcp_client", None)
    if client is None or not client.started:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="MCP document server is not available")
    return client


def _build_corpus_loader(workspace_id: str, settings: Settings):
    """一次 search 一次回读：语料由 backend-java 的内部端点提供。"""

    def load():
        response = httpx.get(
            f"{settings.java_backend_base_url.rstrip('/')}"
            f"/internal/workspaces/{workspace_id}/retrieval-corpus",
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        files = {str(k): str(v) for k, v in (payload.get("files") or {}).items()}
        chunks = [
            ChunkRecord(
                id=str(item["id"]),
                file_id=str(item["file_id"]),
                parent_id=item.get("parent_id"),
                level=str(item.get("level") or "child"),
                text=str(item.get("text") or ""),
                location=str(item.get("location") or ""),
                meta=dict(item.get("meta") or {}),
                token_counts=dict(item.get("token_counts") or {}),
                token_length=int(item.get("token_length") or 0),
                ordinal=int(item.get("ordinal") or 0),
            )
            for item in payload.get("chunks") or []
        ]
        return files, chunks

    return load


def _to_langchain_history(rows: list[ChatMessageIn], settings: Settings, summary: str | None = None) -> list:
    """请求消息 → 模型可见的历史窗口（移植 routes._to_langchain_history）。

    两道封顶不变：条数封顶 memory_recent_messages，再按 token 预算从最新向前
    走；摘要已有的开销从预算里先扣。与 routes 版本的唯一差别是 role 比较用
    字符串（请求消息不走 MessageRole 枚举）。
    """
    history: list = []
    for row in rows:
        content = (row.content or "").strip()
        if not content:
            continue
        if row.role == "user":
            history.append(HumanMessage(content=content))
        elif row.role == "assistant":
            history.append(AIMessage(content=content))
    history = history[-settings.memory_recent_messages :]

    budget = settings.memory_keep_recent_tokens
    if summary:
        budget = min(budget, settings.memory_context_token_budget - estimate_tokens(summary))
    kept: list = []
    total = 0
    for message in reversed(history):
        cost = estimate_tokens(str(message.content))
        if kept and total + cost > budget:
            break
        total += cost
        kept.append(message)
    kept.reverse()
    return kept


@router.post("/chat/stream")
async def chat_stream(payload: InternalChatRequest, request: Request) -> EventSourceResponse:
    """编排一轮无状态聊天：全部编排逻辑移植自 routes.chat_stream，落库职责除外。"""
    client = _get_client(request)
    settings = get_settings()

    messages = list(payload.messages)
    total = payload.total_messages if payload.total_messages is not None else len(messages)
    # Drop the rows the rolling summary already carries (bookmark counts from
    # the oldest row, so the tail needs the table total — Java 提供的 total)。
    uncovered = max(0, total - payload.covered_count)
    history_rows = messages[max(0, len(messages) - uncovered) :]

    runtime = AgentRuntime(
        workspace_id=payload.workspace_id,
        tracked_files=set(payload.files),
        corpus_loader=_build_corpus_loader(payload.workspace_id, settings),
        memory_rows=[*messages, _TurnRow(role="user", content=payload.message)],
        memory_summary=(payload.summary or "").strip() or None,
        memory_covered=payload.covered_count,
    )
    agent = WorkspaceAgent(client=client, runtime=runtime)
    history_summary = runtime.memory_summary
    history = _to_langchain_history(history_rows, settings, history_summary)
    stale_files = [(item.rel_path, item.changed_at) for item in payload.stale_files]
    trace = TraceCollector(payload.workspace_id, run_id=payload.run_id)

    intent = await classify_intent(
        payload.message,
        [
            f"{'用户' if m.role == 'user' else '助手'}: {m.content}"
            for m in history_rows[-4:]
        ],
        settings,
    )

    async def event_stream() -> AsyncIterator[dict]:
        collected: list[str] = []
        streamed: list[str] = []
        proposals: list[dict] = []
        citations: list[dict] = []

        def partial_answer() -> str:
            if collected:
                return compose_answer(collected, len(proposals))
            text = "".join(streamed).strip()
            return compose_answer([text] if text else [], len(proposals))

        if intent == "chitchat":
            events = agent.astream_direct(payload.message, history, trace=trace)
        else:
            events = agent.astream(
                payload.message,
                history,
                stale_files=stale_files,
                history_summary=history_summary,
                mask_writes=(intent == "query"),
                trace=trace,
            )

        status = "ok"
        answer = ""
        try:
            async with asyncio.timeout(payload.timeout_seconds or settings.chat_turn_timeout):
                async for event in events:
                    if event.type == "done":
                        # done 不转发：persist 帧带权威内容，Java 合成 done。
                        collected.append(str(event.data.get("content", "")))
                        continue
                    elif event.type == "token":
                        streamed.append(str(event.data.get("text", "")))
                    elif event.type == "proposal":
                        proposals.append(event.data)
                    elif event.type == "citations":
                        citations = list(event.data.get("items") or [])
                    yield {
                        "event": event.type,
                        "data": json.dumps(
                            event.to_payload(), ensure_ascii=False, default=str
                        ),
                    }

                answer = compose_answer(collected, len(proposals))
                followups = await generate_followups(settings, payload.message, answer)
                if followups:
                    yield {
                        "event": "followups",
                        "data": json.dumps(
                            {"type": "followups", "items": followups},
                            ensure_ascii=False,
                        ),
                    }
        except (asyncio.CancelledError, NodeCancelledError):
            # Java 侧（唯一客户端）断开：帧发不出去，直接停。部分答案的持久化
            # 由 Java 用它已转发的 token 快照完成（见 ChatController）。
            logger.info("internal chat stream cancelled by caller")
            raise
        except TimeoutError:
            status = "timeout"
            answer = partial_answer() + "\n\n（本轮生成超时已中断，以上为已生成的部分。）"
            yield {
                "event": "notice",
                "data": json.dumps(
                    {"type": "notice", "message": "本轮生成超时，已中断并保留已生成的部分。"},
                    ensure_ascii=False,
                ),
            }
        except Exception as exc:
            status = "error"
            answer = partial_answer()
            logger.exception("internal chat stream failed")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"type": "error", "message": translate_provider_error(exc)},
                    ensure_ascii=False,
                ),
            }

        # 滚动摘要：请求消息 + 本轮 user/assistant 合成行上跑一次常规压缩；
        # 溢出强制压缩若已回写 runtime，作为基线参与（与旧版两次共享同一书签
        # 的语义一致），其结果也必须带回给 Java，否则书签移动会丢。
        base_summary = runtime.forced_summary if runtime.forced_summary is not None else history_summary
        base_covered = runtime.forced_covered if runtime.forced_covered is not None else payload.covered_count
        summary_frame = None
        compaction_rows = [
            *messages,
            _TurnRow(role="user", content=payload.message),
            _TurnRow(role="assistant", content=answer, tool_calls=proposals or None),
        ]
        try:
            result = compute_compaction(
                compaction_rows, base_summary, base_covered, settings
            )
        except Exception as exc:
            logger.warning("post-turn compaction failed: %s", exc)
            result = None
        if result is not None:
            summary_frame = {"text": result[0], "covered_count": result[1]}
        elif runtime.forced_summary is not None:
            summary_frame = {
                "text": runtime.forced_summary,
                "covered_count": runtime.forced_covered or 0,
            }

        yield {
            "event": "persist",
            "data": json.dumps(
                {
                    "type": "persist",
                    "status": status,
                    "content": answer,
                    "citations": citations,
                    "proposals": proposals,
                    "trace_nodes": trace.records,
                    "summary": summary_frame,
                },
                ensure_ascii=False,
                default=str,
            ),
        }

    return EventSourceResponse(event_stream())
