"""Start the API on a scratch port, verify it comes up, then shut it down.

Useful as a pre-demo sanity check: it proves the MCP subprocess starts, the database
initialises, and the HTTP surface answers.

    python scripts/check_server.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8077


def main() -> int:
    python = ROOT / ".venv312" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)

    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        cwd=ROOT / "backend",
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "backend")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    health: dict | None = None
    try:
        deadline = time.time() + 45
        while time.time() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                print("server exited early:\n", output[-2000:])
                return 1
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/health", timeout=3
                ) as response:
                    health = json.loads(response.read().decode("utf-8"))
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(1)

        if health is None:
            print("server did not become healthy within 45s")
            return 1

        print("health:", json.dumps(health, ensure_ascii=False))

        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/docs", timeout=5) as response:
            print("docs:", response.status)

        ok = health.get("mcp_started") and health.get("tools", 0) >= 12
        print()
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
