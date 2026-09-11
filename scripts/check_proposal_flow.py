"""End-to-end check of the proposal → pending tab flow.

Sends an edit request, then compares three views of the same operation:

1. the `proposal` event on the SSE stream (what the UI receives live),
2. `GET /operations` right after the turn (what the UI reloads),
3. the stored row (what approval would act on).

If the proposal event arrives but the reload does not return a `proposed` row, the
pending tab will look empty even though the write is queued.

    python scripts/check_proposal_flow.py
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
QUESTION = "把销售表里 B型 的销售额改成 2500"


def build_sales_workbook(path: Path) -> None:
    """A sales sheet that actually contains the columns the question asks about.

    Reusing the reimbursement demo here would be a trap: the agent would correctly
    refuse to edit a file whose contents do not match the request, and the resulting
    "no proposal" looks like a bug when it is the system working properly.
    """
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["产品", "区域", "销售额"])
    sheet.append(["A型", "华东", 1000])
    sheet.append(["B型", "华北", 2000])
    sheet.append(["C型", "华南", 3000])
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def main() -> int:

    client = httpx.Client(trust_env=False, timeout=600)
    try:
        workspace_id = client.post(
            f"{BASE}/api/workspaces", json={"name": "提案验证"}
        ).json()["id"]
        corpus = ROOT / "data" / "_proposal_check"
        build_sales_workbook(corpus / "销售表.xlsx")
        uploaded = client.post(
            f"{BASE}/api/workspaces/{workspace_id}/files",
            files=[
                ("files", ("销售表.xlsx", (corpus / "销售表.xlsx").read_bytes(), "application/octet-stream"))
            ],
        ).json()
        print(f"workspace = {workspace_id}")
        print(f"indexed   = {[(i['rel_path'], i['chunk_count']) for i in uploaded]}")
        print(f"question  = {QUESTION}")
        print()

        proposals: list[dict] = []
        tools: list[str] = []
        error: str | None = None
        answer = ""

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
                    if event == "tool_call":
                        tools.append(payload.get("tool"))
                    elif event == "proposal":
                        proposals.append(payload)
                        print(f"  {time.time() - started:5.2f}s  proposal event: "
                              f"id={payload.get('operation_id')}")
                        print(f"           summary = {payload.get('summary')}")
                        print(f"           diff    = {payload.get('diff')}")
                    elif event == "error":
                        error = payload.get("message")
                    elif event == "done":
                        answer = payload.get("content") or ""
                    event = None

        print(f"  tools = {tools}")
        if error:
            print(f"  error = {error}")
        print()
        print("  model answered:")
        for line in answer.splitlines():
            print(f"    {line}")
        print()

        # (2) what the UI reloads right after the stream completes
        all_ops = client.get(f"{BASE}/api/workspaces/{workspace_id}/operations").json()
        pending = [op for op in all_ops if op["status"] == "proposed"]
        print(f"after the turn: total ops = {len(all_ops)}, pending = {len(pending)}")
        for op in all_ops:
            print(f"  [{op['status']}] {op['summary']}")
        print()

        failures = []
        if not proposals:
            failures.append("no proposal event on the stream")
        if not pending:
            failures.append("no proposed operation returned by GET /operations")
        if proposals and pending:
            if proposals[0]["operation_id"] != pending[0]["id"]:
                failures.append("proposal event id does not match the stored operation")

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
