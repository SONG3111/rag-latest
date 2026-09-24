"""Workspace path resolution for the runtime-only service.

The upload/registration/indexing half of this module moved to backend-java
(WorkspaceController + IndexingService → ``/v1/index``) during the Java-backend
migration; what remains is the path contract shared by the internal endpoints
and the agent — both must address files under the same sandbox root and refuse
anything that escapes it.
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_settings


class IngestionError(RuntimeError):
    """Raised when a file cannot be accepted or resolved."""


def resolve_workspace_path(workspace_id: str, rel_path: str) -> Path:
    """Resolve a workspace-relative path, refusing anything that escapes the root."""
    settings = get_settings()
    root = settings.workspace_dir(workspace_id).resolve()
    candidate = (root / rel_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise IngestionError(f"path escapes the workspace: {rel_path}")
    return candidate
