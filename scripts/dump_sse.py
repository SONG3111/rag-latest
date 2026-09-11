"""Read-only: print the raw SSE event sequence for one question.

Prints every frame in arrival order, so the order and shape of `token` / `tool_call` /
`citations` / `done` frames can be inspected directly rather than inferred from a
client-side summary.

    python scripts/dump_sse.py "制度里规定的单笔报销上限是多少元？"
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()

    with httpx.Client(trust_env=False, timeout=600) as client:
        workspaces = client.get(f"{BASE}/api/workspaces").json()
        workspace = args.workspace or workspaces[0]["id"]
        print(f"workspace = {workspace}")
        print(f"question  = {args.question}")
        print()
        print(f"{'t(s)':>7}  {'event':<12} payload")
        print("-" * 100)

        started = time.time()
        with client.stream(
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
                    elapsed = time.time() - started
                    if event == "token":
                        body = repr(payload.get("text"))
                    elif event == "done":
                        body = repr(str(payload.get("content"))[:60])
                    elif event == "citations":
                        body = f"{len(payload.get('items') or [])} item(s)"
                    else:
                        body = json.dumps(payload, ensure_ascii=False)[:90]
                    print(f"{elapsed:7.2f}  {event:<12} {body}")
                    event = None

    print()
    print("Raw sequence above. A tool-using turn should show tool_call before "
          "tool_result, then token frames, then done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
