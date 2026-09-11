"""Verify the model client ignores proxy environment variables.

Points the process at a dead proxy and confirms the model still connects. Without the
fix this is exactly the failure users hit: `WinError 10061` against a proxy that is
no longer even running, while every other tool on the machine works fine.

    python scripts/check_proxy_bypass.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Port 9 (discard) is almost never listening, so a client that honours these will
# fail fast instead of silently succeeding.
DEAD_PROXY = "http://127.0.0.1:9"


def main() -> int:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        os.environ[name] = DEAD_PROXY
    for name in ("NO_PROXY", "no_proxy"):
        os.environ.pop(name, None)

    from langchain_core.messages import HumanMessage

    from app.config import get_settings
    from app.llm.providers import build_chat_model, build_embeddings

    settings = get_settings()
    if not settings.dashscope_api_key:
        print("DASHSCOPE_API_KEY is not set")
        return 1

    print(f"pretending the proxy is {DEAD_PROXY}")
    print()

    failures = 0

    # --- chat + tool calling ---
    try:
        started = time.time()
        model = build_chat_model(settings)
        response = model.bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "ping",
                        "description": "ping",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        ).invoke([HumanMessage(content="调用 ping 工具")])
        calls = getattr(response, "tool_calls", None) or []
        print(f"chat + tools : OK   called={[c['name'] for c in calls]}  ({time.time() - started:.1f}s)")
    except Exception as exc:
        print(f"chat + tools : FAIL {type(exc).__name__}: {str(exc)[:160]}")
        failures += 1

    # --- embeddings (local, but the factory must not be dragged to the proxy either) ---
    try:
        started = time.time()
        vectors = build_embeddings(settings).embed_documents(["测试"])
        print(f"embeddings   : OK   dim={len(vectors[0])}  ({time.time() - started:.1f}s)")
    except Exception as exc:
        print(f"embeddings   : FAIL {type(exc).__name__}: {str(exc)[:160]}")
        failures += 1

    print()
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures} problem(s))")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
