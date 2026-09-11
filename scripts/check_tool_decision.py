"""Does `enable_thinking=False` break the agent's decision to call a tool?

This is a correctness check, not a speed check. If turning thinking off makes the model
answer from its own priors instead of retrieving, the latency win is worthless — it
produces confident wrong answers.

    python scripts/check_tool_decision.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "在用户上传的知识库中做混合检索，回答文档相关的问题",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "检索问题"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_range",
            "description": "读取 Excel 指定区域的值",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "sheet_name": {"type": "string"},
                    "start_cell": {"type": "string"},
                },
                "required": ["path", "sheet_name"],
            },
        },
    },
]

QUESTIONS = [
    "销售表里 A型 的销售额是多少？",
    "制度里规定的报销限额是多少？",
    "帮我查一下张伟报销了多少钱",
]


def main() -> int:
    from app.agent.prompts import SYSTEM_PROMPT
    from app.config import get_settings
    from app.llm.providers import build_chat_model

    settings = get_settings()
    print(f"model = {settings.llm_model}")
    print()

    failures = 0
    for enabled in (True, False):
        model = build_chat_model(
            settings.model_copy(update={"llm_enable_thinking": enabled})
        ).bind_tools(TOOLS)
        print(f"enable_thinking={enabled}")
        for question in QUESTIONS:
            started = time.time()
            response = model.invoke(
                [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=question)]
            )
            elapsed = time.time() - started
            calls = [call["name"] for call in (getattr(response, "tool_calls", None) or [])]
            called = bool(calls)
            if not called:
                failures += 1
            mark = "OK  " if called else "MISS"
            content = str(response.content or "").replace("\n", " ")[:44]
            print(f"  {mark} {question:<28} {elapsed:5.1f}s calls={calls} {content!r}")
        print()

    print("A MISS means the model answered without retrieving — that is a correctness")
    print("regression, not a latency trade-off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
