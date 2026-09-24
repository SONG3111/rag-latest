"""Run every automated test suite and print one summary.

Suites:
  ai-service — pytest under ``ai-service/``: unit tests plus HTTP-level e2e that
              spawns the real MCP document server (no API key needed; the LLM
              is never called).
  backend-java — Maven test suite of the Java business backend via the pinned
              wrapper jar (``java -cp``, no shell).
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
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
BACKEND_JAVA_DIR = PROJECT_ROOT / "backend-java"
WRAPPER_JAR = BACKEND_JAVA_DIR / ".mvn" / "wrapper" / "maven-wrapper.jar"


def run_backend_pytest() -> int:
    """Unit + HTTP e2e suites; the configured pytest.ini selects tests/."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=PROJECT_ROOT / "ai-service",
        shell=False,
    )
    return completed.returncode


def run_java_tests() -> int:
    """Java business backend. Runs the pinned Maven through its wrapper jar
    (pure ``java -cp``, list-argv, no shell); needs a JDK 17+.

    ``-Dmaven.multiModuleProjectDirectory`` 平台属性平时由 mvnw 脚本设置；绕过
    脚本直跑 wrapper jar 时必须显式带上，否则 Maven 3.9 启动器直接报错退出。
    """
    if not WRAPPER_JAR.exists():
        print("backend-java/.mvn/wrapper/maven-wrapper.jar missing; cannot run Maven")
        return 1
    completed = subprocess.run(
        [
            "java",
            f"-Dmaven.multiModuleProjectDirectory={BACKEND_JAVA_DIR}",
            "-cp",
            str(WRAPPER_JAR),
            "org.apache.maven.wrapper.MavenWrapperMain",
            "test",
        ],
        cwd=BACKEND_JAVA_DIR,
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
    ("ai-service", run_backend_pytest),
    ("backend-java", run_java_tests),
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
