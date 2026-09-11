"""Query rewriting before retrieval.

Two failure modes motivate this, and both are invisible if you only test with
well-formed questions:

1. **Colloquial phrasing.** A user types "报销咋整" while the policy document says
   "报销流程". Embeddings narrow the gap but not reliably, and BM25 cannot bridge it
   at all.
2. **Multi-turn ellipsis.** After asking about 第二条, the user says "那第三条呢".
   Retrieval sees a three-character fragment with no subject and returns noise.

Rewriting is best-effort by design. Every failure path — no API key, timeout, empty
or runaway output — falls back to the original query, because a retrieval run with a
slightly worse query beats a retrieval run that fails outright.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import Settings, get_settings
from ..llm.providers import build_chat_model

logger = logging.getLogger(__name__)

MAX_REWRITE_CHARS = 300

# Structure ported from RAGFlow's ``rag/prompts/full_question_prompt.md``: role, task
# and steps, requirements, worked examples, then the conversation. Two of their rules
# are load-bearing and were kept verbatim in spirit — "if the user's latest question is
# already complete, just return the original" and "output nothing except a refined
# question" — because a rewriter that explains itself corrupts the query it returns.
# The colloquial-to-written mapping in step 3 is ours (Chinese domain wording).
REWRITE_SYSTEM_PROMPT = """## 角色
一个用来补全和规范化检索问题的助手。

## 任务与步骤
1. 生成一句"作为这段对话的下一句也成立"的完整问题，用来在文档知识库里检索。
2. 如果用户的问题依赖上文（"那第三条呢""它的上限是多少"），结合对话历史把指代补全。
3. 如果是口语表达，改写成文档里更可能出现的书面表达（"咋整"→"流程"、"多少钱"→"金额"）。

## 要求与限制
- 如果用户最新的问题本身已经完整，不要改动，原样返回。
- 不要回答问题、不要解释、不要补充文档里没有的信息。
- 保留专有名词、编号、金额、日期等关键信息，不要改动它们。
- 只输出改写后的问题本身：不要引号、不要前缀、不要换行。

## 示例
对话：
USER: 报销制度规定了什么？
ASSISTANT: 单笔报销不得超过 5000 元，超出部分需总经理审批。
USER: 那第三条呢？
输出：员工费用报销管理制度第三条规定了什么？
"""


@dataclass
class RewriteResult:
    """The query actually used for retrieval, plus how it was produced."""

    query: str
    rewritten: bool
    original: str
    error: str | None = None


class QueryRewriter:
    def __init__(self, settings: Settings | None = None, *, llm=None) -> None:
        self.settings = settings or get_settings()
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            # Deliberately a separate, cheaper model: rewriting is a small
            # transformation task and should not consume the conversation model's
            # latency budget or rate limit.
            self._llm = build_chat_model(
                self.settings,
                model=self.settings.query_rewrite_model,
                temperature=0.0,
                timeout=self.settings.query_rewrite_timeout,
            )
        return self._llm

    def _render_history(self, history: list[str]) -> str:
        turns = [line.strip() for line in history if line and line.strip()]
        if not turns:
            return "（无历史对话）"
        limit = self.settings.query_rewrite_history_turns
        return "\n".join(turns[-limit:])

    def rewrite(self, query: str, history: list[str] | None = None) -> RewriteResult:
        """Rewrite a query for retrieval, falling back to it unchanged on failure."""
        original = (query or "").strip()
        if not original:
            return RewriteResult(query=original, rewritten=False, original=original)
        if not self.settings.query_rewrite_enabled:
            return RewriteResult(query=original, rewritten=False, original=original)

        prompt = (
            f"## 对话历史\n{self._render_history(history or [])}\n\n"
            f"## 用户当前问题\n{original}\n\n"
            "## 改写后的检索查询"
        )

        try:
            response = self.llm.invoke(
                [
                    SystemMessage(content=REWRITE_SYSTEM_PROMPT),
                    HumanMessage(content=prompt),
                ]
            )
            candidate = _flatten(response.content).strip()
            # Ollama-style models sometimes prefix an explanation; keep only line one.
            candidate = candidate.splitlines()[0].strip() if candidate else ""
            # Strip quoting after line selection: a wrapped answer can leave the
            # opening quote on line one and the closing quote on line two.
            candidate = candidate.strip("\"'“”‘’ ")
        except Exception as exc:
            logger.warning("query rewrite failed, using the original query: %s", exc)
            return RewriteResult(
                query=original, rewritten=False, original=original, error=str(exc)
            )

        if not candidate or len(candidate) > MAX_REWRITE_CHARS:
            logger.warning("query rewrite produced unusable output; using the original query")
            return RewriteResult(
                query=original,
                rewritten=False,
                original=original,
                error="rewrite output empty or too long",
            )

        return RewriteResult(
            query=candidate,
            rewritten=candidate != original,
            original=original,
        )


def _flatten(content) -> str:
    """LangChain message content may be a string or a list of content blocks."""
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
