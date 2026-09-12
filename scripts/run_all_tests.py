"""Run every automated test suite and print one summary.

Suites:
  backend   — pytest under ``backend/``: unit tests plus HTTP-level e2e that
              spawns the real MCP document server (no API key needed; the LLM
              is never called).
  mcp       — pytest under ``mcp-office-server/``: the Excel/Word tool
              contract and the sandbox path checks.
  frontend  — node checks under ``frontend/``: SSE frame parsing always,
              the reactivity check when ``frontend/node_modules`` exists.

The live frontend check (``sse-live-check.mjs``) needs a running backend plus
``--experimental-strip-types``; run it by hand against a dev server.

Usage:
    python scripts/run_all_tests.py [--with-frontend] [--stop-on-failure]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"


def run_backend_pytest() -> int:
    """Unit + HTTP e2e suites; the configured pytest.ini selects tests/."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=PROJECT_ROOT / "backend",
        shell=False,
    )
    return completed.returncode


def run_mcp_server_pytest() -> int:
    """Excel/Word tool contract and sandbox path checks."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests"],
        cwd=PROJECT_ROOT / "mcp-office-server",
        shell=False,
    )
    return completed.returncode


def run_frontend_check(script_name: str) -> int:
    """One offline node check; only fixed, reviewed script names reach here."""
    completed = subprocess.run(
        ["node", script_name],
        cwd=FRONTEND_DIR,
        shell=False,
    )
    return completed.returncode


SUITES = [
    ("backend", run_backend_pytest),
    ("mcp-server", run_mcp_server_pytest),
]


def frontend_suite_entries() -> list[tuple[str, callable]]:
    """The node checks that can run without a live server, if node is usable."""
    entries: list[tuple[str, callable]] = [
        ("frontend:sse-framing", lambda: run_frontend_check("sse-framing-check.mjs")),
    ]
    if (FRONTEND_DIR / "node_modules" / "@vue" / "reactivity").exists():
        entries.append(
            ("frontend:reactivity", lambda: run_frontend_check("reactivity-check.cjs"))
        )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-frontend",
        action="store_true",
        help="also run the offline frontend checks (requires node)",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="skip the remaining suites after the first failure",
    )
    args = parser.parse_args()

    suites = list(SUITES)
    if args.with_frontend:
        if shutil.which("node") is None:
            print("node is not on PATH; skipping frontend checks")
        else:
            suites.extend(frontend_suite_entries())

    results: list[tuple[str, str, float]] = []
    for name, runner in suites:
        # Flush before each suite: child processes write straight through, so an
        # unflushed parent buffer makes piped output appear out of order.
        print(f"\n{'=' * 64}\n▶ {name}\n{'=' * 64}", flush=True)
        started = time.perf_counter()
        try:
            code = runner()
            outcome = "PASS" if code == 0 else "FAIL"
        except OSError as exc:
            print(f"suite '{name}' could not start: {exc}")
            outcome = "FAIL"
        elapsed = time.perf_counter() - started
        results.append((name, outcome, elapsed))

        if outcome == "FAIL" and args.stop_on_failure:
            break

    print(f"\n{'=' * 64}\n■ 汇总\n{'=' * 64}", flush=True)
    failed = False
    for name, outcome, elapsed in results:
        mark = {"PASS": "✅", "FAIL": "❌"}[outcome]
        if outcome == "FAIL":
            failed = True
        print(f"{mark} {name:<22} {outcome}   ({elapsed:.1f}s)", flush=True)
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
