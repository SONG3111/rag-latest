"""Ask the running server a question and print the streamed turn.

Assumes the backend is already running (``scripts/check_server.py`` or uvicorn).
Useful for a quick live check without going through the browser.

    python scripts/ask.py "制度里规定的招待费上限是多少？"
    python scripts/ask.py --workspace <id> "张伟报销了多少钱？"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"

# Same reason as the other check scripts: a Windows system proxy will otherwise
# swallow loopback requests and return a confusing 502.
CLIENT = httpx.Client(trust_env=False, timeout=900.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument(
        "--workspace", default=None, help="workspace id; defaults to the first one"
    )
    args = parser.parse_args()

    try:
        workspaces = CLIENT.get(f"{BASE}/api/workspaces").json()
    except Exception as exc:
        print(f"cannot reach the backend at {BASE}: {exc}")
        print("start it with: scripts/check_server.py 或 uvicorn app.main:app")
        return 1

    if not workspaces:
        print("no workspace exists; upload a file first")
        return 1

    workspace = args.workspace or workspaces[0]["id"]
    name = next((w["name"] for w in workspaces if w["id"] == workspace), workspace)
    print(f"workspace: {name} ({workspace})")
    print(f"question : {args.question}")
    print()

    tools: list[str] = []
    citations: list[dict] = []
    answer = ""
    error: str | None = None
    started = time.time()
    first_token_at: float | None = None
    token_count = 0
    streamed = ""
    event_counts: dict[str, int] = {}

    with CLIENT.stream(
        "POST",
        f"{BASE}/api/workspaces/{workspace}/chat/stream",
        json={"message": args.question},
        headers={"Accept": "text/event-stream"},
    ) as response:
        response.raise_for_status()
        event = None
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                payload = json.loads(line.split(":", 1)[1].strip())
                event_counts[event] = event_counts.get(event, 0) + 1
                if event == "token":
                    if first_token_at is None:
                        first_token_at = time.time() - started
                    token_count += 1
                    streamed += payload.get("text", "")
                elif event == "tool_call":
                    tools.append(payload.get("tool", ""))
                    print(f"  → {payload.get('label', payload.get('tool'))}")
                elif event == "citations":
                    citations = payload.get("items", [])
                elif event == "proposal":
                    print(f"  ⚠ 待确认修改：{payload.get('summary')}")
                elif event == "done":
                    answer = payload.get("content", "")
                elif event == "error":
                    error = payload.get("message", "")

    elapsed = time.time() - started
    if first_token_at is not None:
        print(f"  ⚡ 首个 token 于 {first_token_at:.2f}s 到达（共 {token_count} 个分块）")
    print(f"  事件统计: {event_counts}")
    print()
    if error:
        print(f"错误: {error}")
        return 1
    print(answer)
    if streamed and streamed.strip() != answer.strip():
        print()
        print("注意：流式文本与 done 文本不一致")
        print(f"  streamed={streamed[:80]!r}")
        print(f"  done={answer[:80]!r}")
    print()
    if citations:
        print("引用来源：")
        for item in citations:
            score = item.get("score")
            score_text = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
            print(f"  {score_text}  {item.get('file')}  {item.get('location')}")
    else:
        print("引用来源：无")
    print()
    print(f"工具调用: {tools}")
    print(f"耗时    : {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
