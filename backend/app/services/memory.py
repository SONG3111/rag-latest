"""Conversation memory compaction: rolling summary + recent original turns.

Pattern from nageoffer/ragent (Apache-2.0, ``AgentContextCompactionMiddleware`` /
``AgentMemoryPipeline``, code-tree verified): keep the most recent turns verbatim,
compress older material into a persistent summary, trigger compaction only past a
watermark, and degrade to plain truncation on any failure. The single-machine
version differs deliberately:

* the trigger is a message-count watermark, not a token watermark — counting rows
  is free and good enough at this scale;
* the summary runs on the cheap rewrite-model channel and every failure keeps the
  previous summary (or none) — an older summary beats a broken chat turn;
* the summary is injected as background context only. File-freshness facts are
  never its responsibility: staleness is decided deterministically by timestamp
  comparison in the routes layer.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..llm.providers import build_chat_model
from ..models import ConversationSummary, Message, MessageRole

logger = logging.getLogger(__name__)

# A single message longer than this is truncated before entering the summarizer
# prompt: the summary needs facts, not full verbatim copies, and a handful of
# long tool-heavy answers would otherwise blow the small model's context.
_PER_MESSAGE_CHAR_LIMIT = 500

COMPACT_SYSTEM_PROMPT = """## 角色
对话记忆压缩助手。

## 任务与步骤
1. 把对话历史压缩成一段简洁摘要，作为后续对话理解上下文的背景。
2. 已有旧摘要时，把它当作其中一部分内容一并整合，而不是另起炉灶。

## 要求与限制
- 保留用户的目标、已确认的关键事实：文件名、工作表、单元格、数值、日期、结论。
- 合并重复内容，丢弃寒暄与客套。
- 不要回答问题，不要补充对话里没有的信息，不要编造。
- 只输出摘要本身，不超过 300 字，不要标题和前缀。
"""


def load_summary(session: Session, workspace_id: str) -> str | None:
    """The workspace's stored conversation summary, or None."""
    row = session.get(ConversationSummary, workspace_id)
    if row is None or not (row.summary or "").strip():
        return None
    return row.summary.strip()


def compact_memory(
    session: Session,
    workspace_id: str,
    settings: Settings | None = None,
    *,
    summarizer=None,
) -> bool:
    """Fold all but the most recent turns into the rolling summary.

    Returns True when a new summary was stored. Called after a turn completes
    (background task), so the extra latency never blocks the user. With no
    summarizer the small rewrite model is used; a failed or empty summarization
    keeps the previous state — the next turn simply falls back to truncation.
    """
    settings = settings or get_settings()
    rows = list(
        session.scalars(
            select(Message)
            .where(Message.workspace_id == workspace_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    )
    recent = settings.memory_recent_messages
    if len(rows) <= max(recent, settings.memory_compact_trigger):
        # Below the watermark the plain truncation the history assembly already
        # does is indistinguishable from compaction, so don't pay for a summary.
        return False

    to_compress = rows[:-recent]
    existing = session.get(ConversationSummary, workspace_id)
    covered = existing.covered_count if existing else 0
    if len(to_compress) <= covered:
        return False

    prompt = _build_prompt(existing.summary if existing else None, to_compress)
    try:
        summary = (
            summarizer(prompt) if summarizer is not None else _summarize(settings, prompt)
        )
    except Exception as exc:
        # A broken summarizer must never surface into the background task that
        # calls compaction; the fallback is simply "no new summary".
        logger.warning("memory summarization failed: %s", exc)
        summary = None
    if not summary:
        logger.info("memory compaction produced no summary; keeping truncation fallback")
        return False

    if existing is None:
        existing = ConversationSummary(workspace_id=workspace_id)
        session.add(existing)
    existing.summary = summary
    existing.covered_count = len(to_compress)
    session.flush()
    logger.info(
        "compacted %d older messages into the conversation summary of %s",
        len(to_compress),
        workspace_id,
    )
    return True


def _build_prompt(previous: str | None, rows: list[Message]) -> str:
    lines: list[str] = []
    for row in rows:
        role = "用户" if row.role is MessageRole.user else "助手"
        content = (row.content or "").strip().replace("\n", " ")
        if not content:
            continue
        lines.append(f"{role}: {content[:_PER_MESSAGE_CHAR_LIMIT]}")
    parts = []
    if previous and previous.strip():
        parts.append(f"## 旧摘要\n{previous.strip()}\n")
    parts.append("## 对话历史\n" + "\n".join(lines))
    parts.append("\n## 整合后的摘要")
    return "\n".join(parts)


def _summarize(settings: Settings, prompt: str) -> str | None:
    """One cheap model call; any failure returns None (caller keeps the old state)."""
    try:
        # Same channel as query rewriting: a small transformation task must not
        # consume the conversation model's latency budget or rate limit.
        llm = build_chat_model(
            settings,
            model=settings.query_rewrite_model,
            temperature=0.0,
            timeout=settings.query_rewrite_timeout,
        )
        response = llm.invoke(
            [SystemMessage(content=COMPACT_SYSTEM_PROMPT), HumanMessage(content=prompt)]
        )
        text = _flatten(response.content).strip()
        return text or None
    except Exception as exc:
        logger.warning("memory summarization failed: %s", exc)
        return None


def _flatten(content) -> str:
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
