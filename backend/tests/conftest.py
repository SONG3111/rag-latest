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


@pytest.fixture()
def app_with_temp_storage(tmp_path, monkeypatch):
    """The full app with storage, database, and the MCP sandbox in a temp tree.

    The real MCP subprocess lifecycle runs; only the sandbox root is pointed at the
    same temp tree the app writes into. The engine behind the session override is
    exposed as ``app.state.test_session_factory`` so tests can seed rows (e.g. a
    pending operation) the way the agent would.
    """
    from app.config import get_settings
    from app.db import Base, get_session
    from app.main import create_app
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", data_dir, raising=False)
    settings.ensure_directories()

    engine = create_engine(
        f"sqlite:///{(data_dir / 'api.db').as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    import app.models  # noqa: F401  (register mappers)

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def override_session():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    # The MCP sandbox must point at the same temp tree the app writes into. Patching
    # the parameter builder (rather than the client constructor) keeps the real
    # lifecycle code under test.
    import app.mcp_client as mcp_client

    real_builder = mcp_client.build_server_params

    def builder(settings=None, workspace_root=None):
        return real_builder(settings, workspace_root=settings.workspaces_dir)

    monkeypatch.setattr(mcp_client, "build_server_params", builder)
    app.state.test_session_factory = factory
    yield app
    engine.dispose()
