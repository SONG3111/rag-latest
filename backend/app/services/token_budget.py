"""CJK-aware token estimation for memory budgets.

A heuristic, deliberately not a tokenizer: budgets only need a conservative
upper bound, and a real tokenizer would drag model-specific dependencies into
every call site. Two figures:

* CJK characters (han, kana, CJK punctuation, fullwidth forms) cost **1 token
  each**. That is conservative-high — qwen-family tokenizers merge most common
  two-character words — which is the safe direction for a budget.
* Everything else uses ~1 token per 4 characters.

The CJK split exists because of a documented failure elsewhere: DeepSeek's
harness (deepseek-ai/deepseek-harness, ``packages/compaction-basic/README.md``)
prices text at ~4 chars/token flat and notes it "underprices CJK and JSON
Schema text" — a Chinese conversation then blows past a budget computed with
it. This corpus is Chinese-first, so CJK is priced separately.

The trigger/keep formulas these estimates feed follow pi-mono
(badlogic/pi-mono, ``packages/coding-agent/docs/compaction.md``): compaction
fires on estimated context tokens against a configured budget, never on
message counts — message count and token cost have no stable relationship once
tool-heavy answers vary in size.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# ~1 token per 4 non-CJK characters (the figure compaction-basic uses flat).
_OTHER_CHARS_PER_TOKEN = 4

# Inclusive code-point ranges priced as one token per character: CJK
# punctuation, kana (rare here but cheap to include), the two han blocks, the
# compatibility ideographs, and fullwidth forms.
_CJK_RANGES = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _text_of(content: Any) -> str:
    """Flatten message content (str or a list of text blocks) to plain text."""
    if content is None:
        return ""
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
    return str(content)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for one piece of text."""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / _OTHER_CHARS_PER_TOKEN)


def estimate_messages(messages: Iterable[Any]) -> int:
    """Total estimate over objects exposing ``.content`` (DB rows or LangChain messages)."""
    return sum(estimate_tokens(_text_of(getattr(message, "content", message))) for message in messages)
