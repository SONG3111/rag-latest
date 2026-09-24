"""Regression tests for the CJK-aware token estimator.

Pure functions only — no model, no database. What is pinned: the per-class
pricing (CJK 1 token/char, other ~1/4 chars), the conservative direction
(over-estimate, never under), and the message-content flattening.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.token_budget import estimate_messages, estimate_tokens


def test_empty_text_is_free():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_ascii_text_is_priced_four_chars_per_token():
    # 8 ASCII chars -> 2 tokens; 7 -> ceil(7/4) = 2 (rounds up, never down).
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("abcdefg") == 2


def test_cjk_text_is_priced_one_token_per_char():
    # 7 han characters -> exactly 7. The conservative direction: qwen tokenizers
    # usually merge common words, so reality is cheaper than the estimate.
    assert estimate_tokens("报销上限五千元") == 7


def test_cjk_punctuation_and_fullwidth_forms_count_as_cjk():
    assert estimate_tokens("，；！）") == 4
    assert estimate_tokens("ＡＢＣ") == 3


def test_mixed_text_breakdown():
    text = "金额total上限"
    # "金额" (2) + "total" (ceil(5/4)=2) + "上限" (2) = 6.
    assert estimate_tokens(text) == 6

    mixed = "报销 limit 上限"
    # "报销上限" (4 han) + " limit " (7 others -> ceil(7/4)=2) = 6.
    assert estimate_tokens(mixed) == 6


def test_estimate_messages_flattens_content_and_blocks():
    rows = [
        SimpleNamespace(content="第一条消息"),
        SimpleNamespace(content="plain ascii text"),  # 16 chars -> 4
    ]
    assert estimate_messages(rows) == 5 + 4

    # LangChain-style list-of-blocks content is flattened, not stringified.
    blocked = SimpleNamespace(content=[{"type": "text", "text": "你好"}, "world"])
    # "你好world" = 2 han + "world" (ceil(5/4)=2) = 4.
    assert estimate_messages([blocked]) == 4
