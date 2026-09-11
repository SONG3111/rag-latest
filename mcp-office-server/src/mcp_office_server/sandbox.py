"""Workspace sandbox: every filesystem path used by a tool must resolve inside a root.

Design notes
------------
The upstream projects this package borrows its implementation layer from had two
different strategies: one accepted only absolute paths (stdio mode), the other
accepted only paths relative to a single environment-configured directory. Neither
fits a multi-workspace application, so we generalize both:

* ``WORKSPACE_ROOT`` may hold a single path or several ``os.pathsep``-separated paths.
* Callers pass paths relative to one of those roots.
* Absolute paths are rejected outright, as are traversal attempts and symlink
  escapes, which are caught by resolving the real path before the containment check.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import DocumentNotFound, SandboxViolation, UnsupportedDocument

EXCEL_SUFFIXES = frozenset({".xlsx", ".xlsm"})
WORD_SUFFIXES = frozenset({".docx"})
DOCUMENT_SUFFIXES = EXCEL_SUFFIXES | WORD_SUFFIXES

_WORKSPACE_ROOT_ENV = "WORKSPACE_ROOT"


def configured_roots() -> list[Path]:
    """Return the configured sandbox roots.

    Raises:
        SandboxViolation: when no root has been configured, because running tools
            without a sandbox is never the intended behaviour.
    """
    raw = os.environ.get(_WORKSPACE_ROOT_ENV, "").strip()
    if not raw:
        raise SandboxViolation(
            f"{_WORKSPACE_ROOT_ENV} is not configured; refusing to operate on the "
            "filesystem without a sandbox root"
        )

    roots: list[Path] = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            resolved = Path(chunk).expanduser().resolve(strict=True)
        except OSError as exc:
            raise SandboxViolation(f"workspace root does not exist: {chunk}") from exc
        if not resolved.is_dir():
            raise SandboxViolation(f"workspace root is not a directory: {resolved}")
        roots.append(resolved)

    if not roots:
        raise SandboxViolation(f"{_WORKSPACE_ROOT_ENV} contains no usable path")
    return roots


def _is_within(base: Path, candidate: Path) -> bool:
    """True when ``candidate`` is ``base`` or lives underneath it."""
    if candidate == base:
        return True
    return base in candidate.parents


def resolve_path(relative_path: str, *, must_exist: bool = True) -> Path:
    """Resolve a workspace-relative path, guaranteeing it stays inside a root.

    Args:
        relative_path: Path relative to a configured workspace root.
        must_exist: When true, the resolved file must already exist.

    Returns:
        The fully resolved absolute path.

    Raises:
        SandboxViolation: on absolute paths, NUL bytes, traversal, or root escape.
        DocumentNotFound: when the file is missing and ``must_exist`` is set.
    """
    if not relative_path or not relative_path.strip():
        raise SandboxViolation("path must not be empty")
    if "\x00" in relative_path:
        raise SandboxViolation("path must not contain NUL bytes")
    if os.path.isabs(relative_path) or Path(relative_path).is_absolute():
        raise SandboxViolation(
            f"absolute paths are not allowed inside the workspace sandbox: {relative_path}"
        )

    roots = configured_roots()
    last_missing: Path | None = None

    for root in roots:
        candidate = (root / relative_path).resolve()
        if not _is_within(root, candidate):
            continue
        if must_exist and not candidate.exists():
            last_missing = candidate
            continue
        return candidate

    if last_missing is not None:
        raise DocumentNotFound(f"no such file in workspace: {relative_path}")

    raise SandboxViolation(
        f"path escapes the workspace sandbox: {relative_path} "
        f"(allowed roots: {', '.join(str(r) for r in roots)})"
    )


def resolve_document(path: str, *, must_exist: bool = True) -> Path:
    """Resolve a path and assert it is a supported Office document."""
    resolved = resolve_path(path, must_exist=must_exist)
    suffix = resolved.suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        raise UnsupportedDocument(
            f"unsupported document type '{suffix or '<none>'}'; "
            f"supported: {', '.join(sorted(DOCUMENT_SUFFIXES))}"
        )
    return resolved


def document_kind(path: str | Path) -> str:
    """Classify a document as ``excel`` or ``word``."""
    suffix = Path(path).suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return "excel"
    if suffix in WORD_SUFFIXES:
        return "word"
    raise UnsupportedDocument(f"unsupported document type '{suffix or '<none>'}'")


def relative_to_root(path: Path) -> str:
    """Render an absolute path back into the workspace-relative form shown to the agent."""
    for root in configured_roots():
        if _is_within(root, path):
            return str(path.relative_to(root)).replace(os.sep, "/")
    return str(path).replace(os.sep, "/")


def list_documents(root: Path | None = None) -> list[dict]:
    """List every supported document under the workspace, sorted by relative path."""
    roots = [root] if root is not None else configured_roots()
    found: list[dict] = []
    for base in roots:
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in DOCUMENT_SUFFIXES:
                continue
            if path.name.startswith("~$"):
                continue
            stat = path.stat()
            found.append(
                {
                    "path": relative_to_root(path),
                    "kind": document_kind(path),
                    "size_bytes": stat.st_size,
                    "modified_at": int(stat.st_mtime),
                }
            )
    return found
