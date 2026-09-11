"""File backups taken immediately before an approved write.

Every mutation is preceded by a copy of the original bytes. Rejecting a proposal
never touches the file at all, but a backup also makes an approved write reversible,
which matters because the agent's diff is only as good as the model that produced it.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings


def backup_path_for(workspace_id: str, source: Path) -> Path:
    """Deterministic destination for a backup, without performing the copy.

    Computed separately from :func:`backup_file` so an operation row can record
    where its backup will live before the copy happens.
    """
    settings = get_settings()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return settings.backups_dir / workspace_id / f"{stamp}__{source.name}"


def backup_file(workspace_id: str, source: Path) -> Path:
    """Copy ``source`` into the backup tree and return the new path."""
    settings = get_settings()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    target_dir = settings.backups_dir / workspace_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{stamp}__{source.name}"
    shutil.copy2(source, target)
    return target


def restore_file(workspace_id: str, backup_path: str | Path, destination: Path) -> Path:
    """Copy a backup back over the destination."""
    source = Path(backup_path)
    if not source.exists():
        raise FileNotFoundError(f"backup not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def prune_backups(workspace_id: str, keep: int = 50) -> int:
    """Keep only the most recent ``keep`` backups for a workspace."""
    settings = get_settings()
    directory = settings.backups_dir / workspace_id
    if not directory.exists():
        return 0
    backups = sorted(
        (path for path in directory.iterdir() if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in backups[keep:]:
        path.unlink(missing_ok=True)
        removed += 1
    return removed
