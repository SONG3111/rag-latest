"""Followup question suggestions, appended after a turn completes.

Docs/05 §4.4: at the end of a turn, one cheap call on the query-rewrite model
channel predicts what the user will most likely ask next; the route emits an
SSE ``followups`` event and the frontend renders the questions as clickable
chips. Cost is one small-model call per turn; like everything else on this
channel it is best-effort — any failure simply yields no suggestions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import Settings, get_settings
from ..llm.providers import build_chat_model

logger = logging.getLogger(__name__)

_MAX_ITEMS = 3
_MAX_CHARS = 80

FOLLOWUPS_SYSTEM_PROMPT = """## 角色
对话后续问题预测助手。

## 任务
根据"用户的问题"和"助手的回答"，预测用户接下来最可能追问的 2~3 个问题。

## 要求与限制
- 问题必须是这段回答能够自然延伸出来的（追问细节、要求举例、请 Agent 执行相关修改等）。
- 与工作区文档无关的闲聊不要生成后续问题。
- 每个问题不超过 40 个字，用用户的口吻。
- 只输出一个 JSON 字符串数组，例如 ["问题一","问题二"]，不要解释、不要编号。"""


async def generate_followups(
    settings: Settings | None,
    question: str,
    answer: str,
) -> list[str]:
    """Predict followup questions for a finished turn; [] on any failure."""
    settings = settings or get_settings()
    if not settings.followups_enabled:
        return []
    try:
        llm = build_chat_model(
            settings,
            model=settings.query_rewrite_model,
            temperature=0.3,
            timeout=settings.query_rewrite_timeout,
        )
        response = await asyncio.wait_for(
            llm.ainvoke(
                [
                    SystemMessage(content=FOLLOWUPS_SYSTEM_PROMPT),
                    HumanMessage(
                        content=f"## 用户的问题\n{question}\n\n## 助手的回答\n{answer[:2000]}"
                    ),
                ]
            ),
            timeout=settings.query_rewrite_timeout,
        )
        return _parse_followups(getattr(response, "content", None))
    except Exception as exc:
        logger.info("followup generation skipped: %s", exc)
        return []


def _parse_followups(content) -> list[str]:
    """Extract the suggestion array from the model output; unusable output → []."""
    if isinstance(content, list):
        content = "".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    text = str(content or "")
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    if not isinstance(data, list):
        return []

    suggestions: list[str] = []
    for item in data:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned or len(cleaned) > _MAX_CHARS or cleaned in suggestions:
            continue
        suggestions.append(cleaned)
        if len(suggestions) >= _MAX_ITEMS:
            break
    return suggestions
