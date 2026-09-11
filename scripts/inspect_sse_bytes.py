"""Read-only: inspect the raw bytes of the SSE stream.

The frontend parses frames by splitting on a blank line. Whether the server
terminates lines with LF or CRLF therefore decides whether the parser works at all —
and a mismatch produces a confusing symptom set: events appear to be dropped, and
text shows up only when the stream ends.

    python scripts/inspect_sse_bytes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(trust_env=False, timeout=600) as client:
        workspaces = client.get(f"{BASE}/api/workspaces").json()
        if not workspaces:
            print("no workspace available")
            return 1
        workspace_id = workspaces[0]["id"]

        samples: list[bytes] = []
        with client.stream(
            "POST",
            f"{BASE}/api/workspaces/{workspace_id}/chat/stream",
            json={"message": "报销制度规定了什么？"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            print(f"content-type = {response.headers.get('content-type')}")
            for chunk in response.iter_bytes():
                if chunk:
                    samples.append(chunk)
                if len(samples) >= 6:
                    break

    print()
    print("first raw chunks (repr, truncated):")
    for index, chunk in enumerate(samples):
        print(f"  [{index}] {chunk[:120]!r}")

    joined = b"".join(samples)
    print()
    print(f"contains CRLFCRLF (\\r\\n\\r\\n) : {b'\\r\\n\\r\\n' in joined}")
    print(f"contains LFLF     (\\n\\n)     : {b'\\n\\n' in joined}")
    print()
    if b"\r\n\r\n" in joined and b"\n\n" not in joined:
        print("=> frames are CRLF-terminated: a parser splitting on '\\n\\n' will not")
        print("   split them and will only flush the trailing buffer when the stream ends")
        return 0
    if b"\n\n" in joined:
        print("=> frames are LF-terminated; an '\\n\\n' split is correct")
        return 0
    print("=> no frame terminator seen in the sampled chunks")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
