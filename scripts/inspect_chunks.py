"""Read-only: report each workspace's chunk composition.

Distinguishes parent/child chunks from flat pre-hierarchy rows, which is what decides
whether a workspace needs a rebuild to get precise citations.

    python scripts/inspect_chunks.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    from app.config import get_settings
    from app.models import Chunk, DocumentFile, Workspace

    settings = get_settings()
    engine = create_engine(
        settings.effective_database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )

    with Session(engine) as session:
        workspaces = list(
            session.scalars(select(Workspace).order_by(Workspace.created_at.asc()))
        )
        chunks = list(session.scalars(select(Chunk)))
        files = list(session.scalars(select(DocumentFile)))

    by_workspace: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_workspace.setdefault(chunk.workspace_id, []).append(chunk)
    files_by_workspace: dict[str, list[DocumentFile]] = {}
    for record in files:
        files_by_workspace.setdefault(record.workspace_id, []).append(record)

    print(f"{'workspace':<24} {'parent':>7} {'child':>7} {'legacy':>7} {'files':>6}  verdict")
    print("-" * 78)

    needs_rebuild: list[str] = []
    for workspace in workspaces:
        rows = by_workspace.get(workspace.id, [])
        parents = sum(1 for row in rows if row.level == "parent")
        children = sum(1 for row in rows if row.level == "child")
        legacy = len(rows) - parents - children
        file_count = len(files_by_workspace.get(workspace.id, []))

        if not rows and file_count == 0:
            verdict = "空工作区（可删除）"
        elif parents == 0 and children > 0:
            verdict = "需要重建索引"
            needs_rebuild.append(workspace.id)
        elif parents == 0 and legacy > 0:
            verdict = "需要重建索引"
            needs_rebuild.append(workspace.id)
        elif file_count == 0:
            verdict = "无文件"
        else:
            verdict = "ok"

        print(
            f"{workspace.name[:22]:<24} {parents:>7} {children:>7} {legacy:>7} "
            f"{file_count:>6}  {verdict}"
        )

    print()
    if needs_rebuild:
        print("需要重建的工作区 id：")
        for workspace_id in needs_rebuild:
            print(f"  {workspace_id}")
    else:
        print("所有工作区都已是父子分块。")

    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
