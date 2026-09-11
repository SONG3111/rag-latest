"""The sandbox is the security boundary of the whole server, so it gets the
most adversarial tests in the suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_office_server import sandbox
from mcp_office_server.errors import (
    DocumentNotFound,
    SandboxViolation,
    UnsupportedDocument,
)


def test_missing_root_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    with pytest.raises(SandboxViolation):
        sandbox.configured_roots()
    with pytest.raises(SandboxViolation):
        sandbox.resolve_path("anything.xlsx")


def test_nonexistent_root_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "does-not-exist"))
    with pytest.raises(SandboxViolation):
        sandbox.configured_roots()


@pytest.mark.parametrize(
    "candidate",
    [
        "../outside.xlsx",
        "a/../../outside.xlsx",
        "sub/../../../etc/passwd",
    ],
)
def test_traversal_is_rejected(workspace: Path, candidate: str) -> None:
    with pytest.raises(SandboxViolation):
        sandbox.resolve_path(candidate, must_exist=False)


def test_absolute_path_is_rejected(workspace: Path) -> None:
    target = workspace / "销售表.xlsx"
    target.touch()
    with pytest.raises(SandboxViolation):
        sandbox.resolve_path(str(target))


def test_nul_byte_is_rejected(workspace: Path) -> None:
    with pytest.raises(SandboxViolation):
        sandbox.resolve_path("a\x00b.xlsx")


def test_empty_path_is_rejected(workspace: Path) -> None:
    with pytest.raises(SandboxViolation):
        sandbox.resolve_path("   ")


def test_missing_file_reports_not_found(workspace: Path) -> None:
    with pytest.raises(DocumentNotFound):
        sandbox.resolve_path("不存在.xlsx")


def test_sibling_directory_is_not_inside_root(workspace: Path) -> None:
    """A prefix-sharing sibling must not be mistaken for the workspace itself."""
    root = sandbox.configured_roots()[0]
    sibling = root.parent / f"{root.name}-other" / "file.xlsx"
    sibling.parent.mkdir()
    sibling.touch()
    assert not sandbox._is_within(root, sibling.resolve())


def test_unsupported_extension_is_rejected(workspace: Path) -> None:
    (workspace / "note.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(UnsupportedDocument):
        sandbox.resolve_document("note.txt")


def test_multiple_roots_are_supported(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = tmp_path / "second"
    second.mkdir()
    (second / "b.xlsx").touch()
    monkeypatch.setenv("WORKSPACE_ROOT", f"{workspace}{__import__('os').pathsep}{second}")
    resolved = sandbox.resolve_path("b.xlsx")
    assert resolved.parent == second.resolve()


def test_listing_skips_office_lock_files(workspace: Path) -> None:
    (workspace / "~$temp.xlsx").touch()
    (workspace / "real.xlsx").touch()
    (workspace / "notes.txt").write_text("x", encoding="utf-8")
    names = {item["path"] for item in sandbox.list_documents()}
    assert names == {"real.xlsx"}


def test_symlink_escape_is_rejected(workspace: Path, tmp_path: Path) -> None:
    """A symlink pointing outside the root resolves outside and must be refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.xlsx"
    target.touch()
    link = workspace / "link.xlsx"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires elevated privileges on this platform")
    with pytest.raises(SandboxViolation):
        sandbox.resolve_path("link.xlsx")
