"""Lightweight intent gate ahead of the agent loop.

Pattern from nageoffer/ragent (Apache-2.0): an intent tree that splits chit-chat
from knowledge lookup from business calls, plus the fail-open principle of its
``AgentSkillMaskingMiddleware`` — a classifier problem must never block the
pipeline it guards.

Three classes and their routing:

* ``chitchat`` — greetings, thanks, identity, small talk. Answered directly by
  the chat model with no tools and no graph: the measured lesson was that
  ungated small talk entering the tool loop burned six tool calls and over two
  minutes for an answer the model already knew.
* ``query`` — lookup/read intent. Full agent loop, but write tools are masked
  so the visible tool list stays small ("tool count vs. selection accuracy").
* ``write`` — any modification intent. Full tool set. This is also the
  deliberate default on uncertainty: a masked write would strand the user's
  actual request, while an unmasked query only costs a longer tool list.

Classification is rules-first (free and deterministic for the clear cases) with
one cheap small-model call (the query-rewrite channel) for the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import Settings, get_settings
from ..llm.providers import build_chat_model

logger = logging.getLogger(__name__)

Intent = Literal["chitchat", "query", "write"]

# Full-match greetings and small talk. Anchored on purpose: "你好，帮我查一下
# 报销上限" must NOT classify as chitchat no matter how much it looks like one —
# a missed lookup is the expensive mistake here, a missed greeting is not.
_CHITCHAT_RE = re.compile(
    r"^(你好|您好|您好呀|你好呀|hi|hello|hey|嗨|哈喽|在吗|在么|在不在"
    r"|谢谢|多谢|感谢|辛苦了|麻烦了|辛苦啦"
    r"|晚安|早安|早上好|中午好|下午好|晚上好"
    r"|你是谁|你叫什么名字|你叫啥|你能做什么|你能干什么|你会什么|介绍一下你自己)"
    r"[!！?？.。,，~～\s]*$",
    re.IGNORECASE,
)
# Length cap for the rule above; a "greeting" this long has content worth the loop.
_CHITCHAT_MAX_CHARS = 20

# Modification verbs force the full tool set without any model call. Being
# over-inclusive here is free (an unmasked query costs nothing), being
# under-inclusive strands a real write behind masked tools, so err wide.
_WRITE_HINT_RE = re.compile(
    r"(修改|改成|改为|更新|插入|删除|新增|添加|替换|合并|取消合并|拆分|"
    r"写入|填写|填入|清空|重命名|复制|移动|隐藏|冻结|加粗|标红|标黄|高亮|"
    r"设置格式|套用格式|生成.{0,6}提案|提交.{0,6}提案|改成|帮我改|"
    r"增一行|加一行|删一行|增一列|加一列|删一列)"
)

INTENT_SYSTEM_PROMPT = """你是意图分类器。根据"用户最新消息"和对话历史，判断它属于哪一类：

- chat：寒暄、问候、感谢、闲聊，或与工作区文档无关的通用请求（比如写首诗、讲个笑话）。
- query：想查询、阅读、核对、了解工作区文档的内容。
- write：想修改、新增、删除工作区文档的内容。

只输出一个 JSON 对象，不要解释：{"intent": "chat"} 或 {"intent": "query"} 或 {"intent": "write"}。
不确定时输出 {"intent": "query"}。"""


def classify_by_rules(message: str) -> Intent | None:
    """Deterministic classification for the clear cases; None means "ask the model"."""
    text = (message or "").strip()
    if not text:
        return "chitchat"
    if _WRITE_HINT_RE.search(text):
        return "write"
    if len(text) <= _CHITCHAT_MAX_CHARS and _CHITCHAT_RE.match(text):
        return "chitchat"
    return None


async def classify_intent(
    message: str,
    history: list[str] | None = None,
    settings: Settings | None = None,
    *,
    llm=None,
) -> Intent:
    """Classify one user message; never raises.

    Order: rules → one cheap model call → conservative fallback. The fallback is
    ``write`` (the unmasked full pipeline) because a wrong "query" both risks a
    missed write and — worse — a wrong "chitchat" would skip retrieval entirely.
    """
    settings = settings or get_settings()
    if not settings.intent_gate_enabled:
        return "write"

    by_rules = classify_by_rules(message)
    if by_rules is not None:
        return by_rules

    rendered_history = "\n".join((history or [])[-4:]) or "（无）"
    prompt = f"## 对话历史\n{rendered_history}\n\n## 用户最新消息\n{message.strip()}\n"
    try:
        if llm is None:
            # Same cheap channel as query rewriting; built per call because the
            # gate runs once per turn and the constructor is cached upstream.
            llm = build_chat_model(
                settings,
                model=settings.query_rewrite_model,
                temperature=0.0,
                timeout=settings.query_rewrite_timeout,
            )
        response = await asyncio.wait_for(
            llm.ainvoke(
                [SystemMessage(content=INTENT_SYSTEM_PROMPT), HumanMessage(content=prompt)]
            ),
            timeout=settings.query_rewrite_timeout,
        )
        intent = _parse_intent(getattr(response, "content", None))
    except Exception as exc:
        logger.info("intent classification failed, falling back to full pipeline: %s", exc)
        return "write"

    if intent is None:
        return "write"
    logger.info("intent gate: %s -> %s", message[:40], intent)
    return intent


def _parse_intent(content) -> Intent | None:
    """Extract the intent from the model output; unusable output → None."""
    if isinstance(content, list):
        content = "".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    text = str(content or "")
    # The model was told to emit pure JSON, but tolerate prose that merely
    # mentions the label inside a JSON-ish fragment.
    match = re.search(r'"intent"\s*:\s*"(chat|query|write)"', text)
    if not match:
        try:
            data = json.loads(text)
            label = str(data.get("intent", ""))
        except (ValueError, AttributeError):
            return None
        match = re.fullmatch(r"(chat|query|write)", label)
        if not match:
            return None
    return {"chat": "chitchat", "query": "query", "write": "write"}[match.group(1)]
