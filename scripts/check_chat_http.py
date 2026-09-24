"""End-to-end conversation check over real HTTP against a live backend.

The ASGI-based test exercises the same code paths but not the same transport. This
one talks to the running two-process stack over the network (backend-java on 8000,
ai-service on 8001 — start both first, see MIGRATION-PROGRESS.md §五) and reads the
SSE stream exactly as the browser does — which is the only way to catch a framing
or serialization bug that the in-process test would hide.

    python scripts/check_chat_http.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"

# trust_env=False is required, not cosmetic: this machine has a Windows system proxy
# configured, and an HTTP client that honours it routes loopback traffic through it.
# The symptom is a confusing 502 from the proxy that looks like a server crash.
CLIENT = httpx.Client(trust_env=False, timeout=900.0)


def wait_for_health(timeout: float = 60.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = CLIENT.get(f"{BASE}/health", timeout=5)
            if response.status_code == 200:
                return response.json()
        except Exception:
            time.sleep(1)
    return None


def post_json(path: str, payload: dict, timeout: float = 600.0) -> dict:
    response = CLIENT.post(f"{BASE}{path}", json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def upload(path: str, files: list[Path], timeout: float = 600.0) -> list[dict]:
    """Upload via httpx so multipart encoding matches what a browser sends."""
    handles = [
        ("files", (file_path.name, file_path.read_bytes(), "application/octet-stream"))
        for file_path in files
    ]
    response = CLIENT.post(f"{BASE}{path}", files=handles, timeout=timeout)
    if response.status_code >= 400:
        print(f"  upload failed: {response.status_code} {response.text[:400]}")
        response.raise_for_status()
    return response.json()


def stream_chat(workspace_id: str, message: str) -> dict:
    """Consume the SSE stream and return the assembled turn."""
    answer = ""
    citations: list[dict] = []
    tools: list[str] = []
    event = None

    with CLIENT.stream(
        "POST",
        f"{BASE}/api/workspaces/{workspace_id}/chat/stream",
        json={"message": message},
        headers={"Accept": "text/event-stream"},
        timeout=900,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            line = line.rstrip("\n")
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                payload = json.loads(line.split(":", 1)[1].strip())
                if event == "tool_call":
                    tools.append(payload.get("tool", ""))
                elif event == "citations":
                    citations = payload.get("items", [])
                elif event == "done":
                    answer = payload.get("content", "")
                elif event == "error":
                    answer = f"[error] {payload.get('message')}"
    return {"answer": answer, "citations": citations, "tools": tools}


def main() -> int:
    health = wait_for_health()
    if health is None:
        print("backend did not answer /health within 60s; is the stack running?")
        print("  (ai-service on 8001 first, then backend-java on 8000)")
        return 1
    print(f"health: mcp_started={health.get('mcp_started')} tools={health.get('tools')}")

    sys.path.insert(0, str(ROOT / "scripts"))
    from make_demo_data import build_document, build_workbook

    corpus = ROOT / "data" / "_http_corpus"
    build_workbook(corpus / "2026年第一季度报销明细.xlsx")
    build_document(corpus / "员工费用报销管理制度.docx")

    workspace = post_json("/api/workspaces", {"name": "HTTP 端到端"})
    workspace_id = workspace["id"]
    indexed = upload(
        f"/api/workspaces/{workspace_id}/files", sorted(corpus.iterdir())
    )
    print(
        "indexed:",
        ", ".join(f"{i['rel_path']}({i['chunk_count']}子块)" for i in indexed),
    )
    print()

    failures = 0
    questions = [
        ("制度里规定的招待费单笔上限是多少？", "answerable", "3000", True),
        ("张伟报销了多少钱？", "answerable", "3200", True),
        ("公司班车几点发车？", "negative", "", False),
    ]

    for question, kind, expected, want_citations in questions:
        print(f"Q: {question}")
        started = time.time()
        result = stream_chat(workspace_id, question)
        elapsed = time.time() - started

        print(f"  tools  : {result['tools']}")
        print(f"  cites  : {len(result['citations'])}")
        for item in result["citations"][:3]:
            score = item.get("score")
            print(
                f"           {score if score is None else f'{score:.4f}'}  "
                f"{item.get('file')}  {item.get('location')}"
            )
        print(f"  answer : {result['answer'][:180]}")
        print(f"  time   : {elapsed:.1f}s")

        if expected and expected not in result["answer"]:
            print(f"  !! expected {expected!r} in the answer")
            failures += 1
        if want_citations and not result["citations"]:
            print("  !! expected citations")
            failures += 1
        if not want_citations:
            if result["citations"]:
                print(f"  !! expected no citations, got {len(result['citations'])}")
                failures += 1
            markers = ("没有找到", "未找到", "没有相关", "无法找到", "没有提到")
            if not any(marker in result["answer"] for marker in markers):
                print("  !! expected a refusal")
                failures += 1
        print()

    CLIENT.delete(f"{BASE}/api/workspaces/{workspace_id}")
    print(f"cleaned up workspace {workspace_id}")
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures} problem(s))")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
