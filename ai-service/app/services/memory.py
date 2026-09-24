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

Since the Java-backend split this module is pure: rows arrive as duck-typed
objects (the stateless chat request's message models qualify), and the caller
decides where the returned ``(summary, covered_count)`` lands — in the split
deployment that is the internal persist frame handed back to backend-java,
which owns the ``conversation_summaries`` table. The summary runs on the cheap
rewrite-model channel and every failure keeps the previous summary — an older
summary beats a broken chat turn. File-freshness facts are never its
responsibility: staleness is decided deterministically by timestamp comparison
on the Java side.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import Settings, get_settings
from ..llm.providers import build_chat_model
from .token_budget import estimate_messages, estimate_tokens

logger = logging.getLogger(__name__)

# A single message longer than this is truncated before entering the summarizer
# prompt: the summary needs facts, not full verbatim copies, and a handful of
# long tool-heavy answers would otherwise blow the small model's context.
_PER_MESSAGE_CHAR_LIMIT = 500

# Structured summary template — the section set is the Chinese convergence of
# pi-mono's default compaction sections (Goal / Key Decisions / Progress / Next
# Steps / Critical Context, docs/compaction.md) and dsh compaction-basic's
# checklist (Primary Request / Files and Code / Errors and Fixes / Pending Jobs
# / Next Step). Fixed sections survive iterative re-summarization far better
# than free prose: the merge instruction targets paragraphs, so an old summary
# is folded into its place instead of copied verbatim (both upstream projects
# merge, never append). Deliberately absent: pi's cumulative
# <read-files>/<modified-files> ledger — file-freshness facts never enter the
# summary here; staleness is decided by timestamp comparison on the Java side.
COMPACT_SYSTEM_PROMPT = """## 角色
对话记忆压缩助手。

## 任务与步骤
1. 把对话历史压缩成一份结构化摘要，作为后续对话理解上下文的背景。
2. 已有旧摘要时，把它的内容**合并进下面对应的段落**（改写整合，不是原样粘贴）。

## 输出格式（Markdown，固定以下段落，顺序不变）
### 用户目标与意图
### 关键事实与决定
（文件名、工作表、单元格、数值、日期、已确认的结论；重复内容合并）
### 错误与修复
### 未决事项与下一步
### 关键背景
（寒暄之外、理解后续对话必需的语境）

## 要求与限制
- 某段没有内容就只写「（无）」。
- 不要回答问题，不要补充对话里没有的信息，不要编造。
- 只输出摘要本身，不超过 {max_chars} 字，不要额外说明。"""


def shrink_history(messages: list, token_budget: int) -> list:
    """Head-reduce an in-memory LangChain history to the newest turns that fit.

    Same backward walk as the keep boundary, over messages instead of rows.
    Used by the overflow retry: the provider has already confirmed the request
    was too large, so this turn shrinks without waiting on another summarizer
    round-trip (dsh's "one maximal balanced head reduction"); the durable half
    — folding fat older turns into the summary so later turns start slim — is
    the caller's ``compute_compaction(..., force=True)``.
    """
    from .token_budget import estimate_message

    kept: list = []
    total = 0
    for message in reversed(messages):
        cost = estimate_message(message)
        if kept and total + cost > token_budget:
            break
        total += cost
        kept.append(message)
    kept.reverse()
    return kept


def compute_compaction(
    rows: list,
    existing_summary: str | None,
    covered: int,
    settings: Settings | None = None,
    *,
    summarizer=None,
    force: bool = False,
) -> tuple[str, int] | None:
    """Decide whether to fold, run the summarizer, return the new bookmark.

    ``rows`` are duck-typed (``.role``/``.content``/``.tool_calls`` — the
    stateless chat request's message models qualify) and are treated read-only:
    the caller decides where the returned ``(summary, covered_count)`` lands.
    Returns ``None`` when no fold happened (below both gates, nothing new
    beyond the bookmark, or a failed summarization) — callers keep the
    previous state in that case.
    """
    settings = settings or get_settings()
    if not rows:
        return None
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
    pressure = _token_pressure(existing_summary, rows[covered:], recent, settings)
    if not (force or count_gate or pressure):
        # Below both gates the plain truncation the history assembly already
        # does is indistinguishable from compaction, so don't pay for a summary.
        return None
    if len(to_compress) <= covered:
        # Nothing new beyond the bookmark (force keeps this check: re-folding
        # covered rows would spend a model call to reproduce the same summary).
        return None

    prompt = _build_prompt(existing_summary, to_compress)
    try:
        summary = (
            summarizer(prompt) if summarizer is not None else _summarize(settings, prompt)
        )
    except Exception as exc:
        # A broken summarizer must never surface into the caller's turn; the
        # fallback is simply "no new summary".
        logger.warning("memory summarization failed: %s", exc)
        summary = None
    if not summary:
        logger.info("memory compaction produced no summary; keeping truncation fallback")
        return None
    return summary, len(to_compress)


def _keep_boundary(rows: list, token_budget: int, max_count: int) -> int:
    """How many of the newest rows stay verbatim; the rest folds into the summary.

    Backward walk over token estimates (pi's cut-point walk). The count cap is
    ``memory_recent_messages`` — the history assembly replays at most that many
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
    previous: str | None, uncovered: list, recent: int, settings: Settings
) -> bool:
    """Estimated summary + recent window vs. the model-visible token budget."""
    window = uncovered[-recent:]
    used = estimate_tokens(previous or "") + estimate_messages(window)
    return used > settings.memory_context_token_budget


def _build_prompt(previous: str | None, rows: list) -> str:
    lines: list[str] = []
    for row in rows:
        role = "用户" if row.role == "user" else "助手"
        content = (row.content or "").strip().replace("\n", " ")
        if not content:
            continue
        lines.append(f"{role}: {content[:_PER_MESSAGE_CHAR_LIMIT]}")
        # Write proposals live on the assistant row as structured dicts; their
        # tool/path/summary triple is exactly the "Files and Code" material a
        # summary needs, and it survives even when the prose answer was terse.
        for proposal in row.tool_calls or []:
            if not isinstance(proposal, dict):
                continue
            tool = proposal.get("tool") or proposal.get("name") or "未知工具"
            path = proposal.get("path") or ""
            brief = str(proposal.get("summary") or "")[:200]
            location = f"（{path}）" if path else ""
            lines.append(f"修改提案: {tool}{location} {brief}".strip())
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
        system_prompt = COMPACT_SYSTEM_PROMPT.format(
            max_chars=settings.memory_summary_max_chars
        )
        response = llm.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
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
