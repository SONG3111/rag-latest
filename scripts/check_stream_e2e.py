"""End-to-end streaming check against a brand-new workspace.

A fresh workspace is essential here: in a workspace with existing conversation
history, the model can answer a repeated question straight from context and never
call a tool, which looks like a regression but is just history reuse. Starting clean
removes that ambiguity.

    python scripts/check_stream_e2e.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

BASE = "http://127.0.0.1:8000"
QUESTION = "制度里规定的招待费单笔上限是多少元？"


def main() -> int:
    from make_demo_data import build_document, build_workbook

    client = httpx.Client(trust_env=False, timeout=600)
    try:
        workspace = client.post(
            f"{BASE}/api/workspaces", json={"name": "流式验证"}
        ).json()
        workspace_id = workspace["id"]
        print(f"workspace = {workspace_id} (new)")

        corpus = ROOT / "data" / "_stream_check"
        build_workbook(corpus / "报销明细.xlsx")
        build_document(corpus / "管理制度.docx")
        uploaded = client.post(
            f"{BASE}/api/workspaces/{workspace_id}/files",
            files=[
                ("files", (path.name, path.read_bytes(), "application/octet-stream"))
                for path in sorted(corpus.iterdir())
            ],
        ).json()
        print("indexed   =", [(item["rel_path"], item["chunk_count"]) for item in uploaded])
        print(f"question  = {QUESTION}")
        print()

        order: list[str] = []
        first_token = None
        tokens = 0
        answer = ""
        citations: list[dict] = []

        started = time.time()
        with client.stream(
            "POST",
            f"{BASE}/api/workspaces/{workspace_id}/chat/stream",
            json={"message": QUESTION},
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            event = None
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event:
                    payload = json.loads(line.split(":", 1)[1].strip())
                    order.append(event)
                    if event == "tool_call":
                        print(f"  {time.time() - started:5.2f}s  tool_call  {payload.get('tool')}")
                    elif event == "token":
                        if first_token is None:
                            first_token = time.time() - started
                        tokens += 1
                    elif event == "citations":
                        citations = payload.get("items") or []
                    elif event == "done":
                        answer = payload.get("content") or ""
                    event = None

        elapsed = time.time() - started
        print()
        print(f"first token   : {first_token:.2f}s" if first_token else "first token   : none")
        print(f"token frames  : {tokens}")
        print(f"citations     : {len(citations)}")
        for item in citations[:3]:
            print(f"                  {item.get('score'):.3f}  {item.get('file')}  {item.get('location')}")
        print(f"answer        : {answer[:110]}")
        print(f"total         : {elapsed:.2f}s")
        print()

        failures = []
        if "tool_call" not in order:
            failures.append("no tool_call — the agent answered without retrieving")
        if tokens < 3:
            failures.append(f"only {tokens} token frames — not really streaming")
        if not citations:
            failures.append("no citations")
        if "3000" not in answer and "招待费" not in answer:
            failures.append("answer does not address the question")

        print("RESULT:", "PASS" if not failures else "FAIL")
        for item in failures:
            print(f"  - {item}")

        client.delete(f"{BASE}/api/workspaces/{workspace_id}")
        print(f"\ncleaned up workspace {workspace_id}")
        return 0 if not failures else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
