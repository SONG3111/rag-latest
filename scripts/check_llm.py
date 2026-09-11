"""Verify the configured chat model actually does what this project needs.

Two capabilities are load-bearing and neither is guaranteed by a model being
reachable:

1. **Tool calling** — the whole agent stops working without it.
2. **Query rewriting** — multi-turn and colloquial questions are only resolvable
   if the rewrite step produces a usable query.

A model can answer chat completions perfectly and still fail both, so this checks
behaviour rather than connectivity.

    python scripts/check_llm.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


REWRITE_CASES = [
    ("那第三条呢", ["用户: 报销制度第二条讲的是什么", "助手: 第二条是关于报销限额的规定。"]),
    ("它的上限是多少", ["用户: 招待费怎么规定的", "助手: 招待费属于报销范围。"]),
    ("报销咋整", []),
    ("谁还没给批啊", []),
    ("请客吃饭能报几个钱", []),
    ("招待费单笔上限是多少", []),
]


def main() -> int:
    from langchain_core.messages import HumanMessage

    from app.config import get_settings
    from app.llm.providers import build_chat_model
    from app.retrieval.rewriter import QueryRewriter

    settings = get_settings()
    if not settings.dashscope_api_key:
        print("DASHSCOPE_API_KEY is not set")
        return 1

    print(f"LLM             : {settings.llm_model}")
    print(f"rewrite model   : {settings.query_rewrite_model}")
    print(f"rewrite enabled : {settings.query_rewrite_enabled}")
    print()

    failures = 0

    # ------------------------------------------------------------------ #
    # chat + tool calling
    # ------------------------------------------------------------------ #
    try:
        llm = build_chat_model(settings)
        bound = llm.bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "list_files",
                        "description": "列出工作区中的文件",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                        },
                    },
                }
            ]
        )
        started = time.time()
        response = bound.invoke([HumanMessage(content="工作区里有哪些文件？调用工具看看")])
        elapsed = time.time() - started
        calls = getattr(response, "tool_calls", None) or []
        if calls:
            print(f"tool calling    : OK   called {calls[0]['name']}  ({elapsed:.1f}s)")
        else:
            print(f"tool calling    : FAIL no tool call emitted  ({elapsed:.1f}s)")
            print(f"                  content: {str(response.content)[:120]}")
            failures += 1
    except Exception as exc:
        print(f"tool calling    : FAIL {type(exc).__name__}: {exc}")
        failures += 1

    # ------------------------------------------------------------------ #
    # rewriting
    # ------------------------------------------------------------------ #
    print()
    print("query rewriting")
    print("-" * 64)
    rewriter = QueryRewriter(settings)
    rewritten_count = 0
    try:
        for query, history in REWRITE_CASES:
            started = time.time()
            result = rewriter.rewrite(query, history)
            elapsed = time.time() - started
            if result.error:
                print(f"  FAIL {query!r}: {result.error[:80]}")
                failures += 1
                continue
            if result.rewritten:
                rewritten_count += 1
            flag = "改" if result.rewritten else "同"
            print(f"  [{flag}] {query!r} -> {result.query!r}  ({elapsed:.1f}s)")
    except Exception as exc:
        print(f"  FAIL {type(exc).__name__}: {exc}")
        failures += 1

    print()
    print(f"改写触发: {rewritten_count}/{len(REWRITE_CASES)}")
    print()
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures} problem(s))")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
