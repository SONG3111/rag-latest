"""Conversation memory compaction: rolling summary + token-budgeted recent window.

Compaction pattern from nageoffer/ragent (Apache-2.0,
``AgentContextCompactionMiddleware`` / ``AgentMemoryPipeline``, code-tree
verified): keep the most recent turns verbatim, compress older material into a
persistent summary, trigger compaction only past a watermark, and degrade to
plain truncation on any failure. The window arithmetic was later re-based onto
what pi-mono documents in ``packages/coding-agent/docs/compaction.md`` and
DeepSeek's harness ships in ``packages/compaction/compaction-basic``: *token*
budgets, not message counts —

* the trigger is token pressure (estimated summary + recent window vs.
  ``memory_context_token_budget``, pi's ``contextTokens > window - reserve``
  with a directly configured budget), ORed with the original message-count
  gate — thin old turns must still fold even when tokens are cheap, or the
  opening context is silently dropped exactly like before the summary existed;
* the verbatim keep boundary walks backward from the newest message within
  ``memory_keep_recent_tokens`` (pi's ``keepRecentTokens``); rows are only ever
  cut at message boundaries, and the newest message is indivisible;
* the ``covered_count`` bookmark plays the role of pi's ``firstKeptEntryId``:
  raw rows are never deleted or rewritten, compaction just moves the window
  start, which is what makes a re-run idempotent.

Shared with the original migration: the summary runs on the cheap
rewrite-model channel and every failure keeps the previous summary — an older
summary beats a broken chat turn; the summary is injected as background
context only. File-freshness facts are never its responsibility: staleness is
decided deterministically by timestamp comparison in the routes layer, which
is also why pi's cumulative <read-files>/<modified-files> ledger is
deliberately not adopted here.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..llm.providers import build_chat_model
from ..models import ConversationSummary, Message, MessageRole
from .token_budget import estimate_messages, estimate_tokens

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


def uncovered_count(session: Session, workspace_id: str) -> int:
    """How many stored messages the current summary does not cover.

    ``covered_count`` counts from the oldest row, so the uncovered tail needs
    the table total. A caller that fetched only the newest N rows uses this to
    drop exactly the rows the summary already carries (pi slices by
    ``firstKeptEntryId``; the bookmark arithmetic here is the same idea).
    """
    total = session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.workspace_id == workspace_id)
    )
    existing = session.get(ConversationSummary, workspace_id)
    covered = existing.covered_count if existing else 0
    return max(0, (total or 0) - covered)


def compact_memory(
    session: Session,
    workspace_id: str,
    settings: Settings | None = None,
    *,
    summarizer=None,
    force: bool = False,
) -> bool:
    """Fold all but the most recent turns into the rolling summary.

    Returns True when a new summary was stored. Normally called after a turn
    completes (background task), so the extra latency never blocks the user;
    ``force=True`` is the synchronous emergency path (context overflow before a
    retry): it skips the trigger check and halves the verbatim tail budget so
    the retried request fits even if the estimate was optimistic. With no
    summarizer the small rewrite model is used; a failed or empty
    summarization keeps the previous state — the next turn simply falls back
    to truncation.
    """
    settings = settings or get_settings()
    rows = list(
        session.scalars(
            select(Message)
            .where(Message.workspace_id == workspace_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    )
    if not rows:
        return False
    existing = session.get(ConversationSummary, workspace_id)
    covered = existing.covered_count if existing else 0
    recent = settings.memory_recent_messages

    keep_budget = settings.memory_keep_recent_tokens
    if force:
        keep_budget = max(keep_budget // 2, 1)
    keep = _keep_boundary(rows, keep_budget, recent)
    to_compress = rows[:-keep]

    # Count gate: batch old thin turns so the summary call is amortized (the
    # original watermark). Token gate: pressure is current, it cannot wait for
    # a batch. Either alone triggers a fold.
    count_gate = len(rows) > max(recent, settings.memory_compact_trigger)
    pressure = _token_pressure(
        existing.summary if existing else None, rows[covered:], recent, settings
    )
    if not (force or count_gate or pressure):
        # Below both gates the plain truncation the history assembly already
        # does is indistinguishable from compaction, so don't pay for a summary.
        return False
    if len(to_compress) <= covered:
        # Nothing new beyond the bookmark (force keeps this check: re-folding
        # covered rows would spend a model call to reproduce the same summary).
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


def _keep_boundary(rows: list[Message], token_budget: int, max_count: int) -> int:
    """How many of the newest rows stay verbatim; the rest folds into the summary.

    Backward walk over token estimates (pi's cut-point walk). The count cap is
    ``memory_recent_messages`` — the routes layer replays at most that many
    rows, so folding less would waste summary coverage on rows the model never
    sees. At least one row is always kept: a window that cannot hold the
    newest message is not something a summary can fix.
    """
    kept = 0
    total = 0
    for row in reversed(rows):
        if kept >= max_count:
            break
        cost = estimate_tokens(row.content or "")
        if kept > 0 and total + cost > token_budget:
            break
        total += cost
        kept += 1
    return kept


def _token_pressure(
    previous: str | None, uncovered: list[Message], recent: int, settings: Settings
) -> bool:
    """Estimated summary + recent window vs. the model-visible token budget."""
    window = uncovered[-recent:]
    used = estimate_tokens(previous or "") + estimate_messages(window)
    return used > settings.memory_context_token_budget


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
