from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture()
def temp_session(tmp_path: Path):
    """A throwaway database, independent of the application's configured one."""
    from app.db import Base
    from app import models  # noqa: F401  (register mappers before create_all)

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def file_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect file storage (workspaces + backups) into a temp tree."""
    from app.config import get_settings

    root = tmp_path / "data"
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", root, raising=False)
    root.mkdir(parents=True, exist_ok=True)
    settings.ensure_directories()
    return root


@pytest.fixture()
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A sandbox root for tests that exercise the MCP server directly."""
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(root))
    return root


@pytest.fixture()
def anyio_backend() -> str:
    """Run anyio-marked tests on asyncio only; trio is not a dependency here."""
    return "asyncio"
