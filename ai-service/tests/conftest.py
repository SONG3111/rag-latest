from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture()
def file_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect file storage into a temp tree."""
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


@pytest.fixture(autouse=True)
def _no_small_model_calls(monkeypatch: pytest.MonkeyPatch):
    """Fail fast instead of contacting the small-model channel.

    The intent gate, memory compaction, and followup suggestions share the
    query-rewrite model channel. Left unpatched, a test message that survives
    the rules would build a real ChatOpenAI and — with an API key configured —
    call the provider, which the project rules forbid (AGENTS.md: tests must be
    mock-only). Each consumer's fail-open fallback (full pipeline / no summary /
    no suggestions) makes the raised error harmless, so existing tests keep
    their behavior; the test files that need a scripted small model patch these
    factories themselves.
    """
    import app.agent.intent as intent_module
    import app.services.followups as followups_module
    import app.services.memory as memory_module

    def _disabled(settings=None, **kwargs):
        raise RuntimeError("small-model channel disabled in tests")

    monkeypatch.setattr(intent_module, "build_chat_model", _disabled)
    monkeypatch.setattr(memory_module, "build_chat_model", _disabled)
    monkeypatch.setattr(followups_module, "build_chat_model", _disabled)


@pytest.fixture()
def app_with_temp_storage(tmp_path, monkeypatch):
    """The app with its storage tree and the MCP sandbox in a temp directory.

    There is no database behind the app anymore (app.db is owned exclusively
    by backend-java); the ai-service is stateless, so tests that need a
    workspace file drop it straight onto the temp storage tree — see
    ``seed_workspace_file``.
    """
    from app.config import get_settings
    from app.main import create_app

    settings = get_settings()
    data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "data_dir", data_dir, raising=False)
    settings.ensure_directories()

    app = create_app()
    # The MCP sandbox must point at the same temp tree the app writes into. Patching
    # the parameter builder (rather than the client constructor) keeps the real
    # lifecycle code under test.
    import app.mcp_client as mcp_client

    real_builder = mcp_client.build_server_params

    def builder(settings=None, workspace_root=None):
        return real_builder(settings, workspace_root=settings.workspaces_dir)

    monkeypatch.setattr(mcp_client, "build_server_params", builder)
    yield app


def seed_workspace_file(workspace_id: str, rel_path: str, content: bytes) -> Path:
    """Drop a file into the (temp) storage tree the way Java's upload would.

    The retired public upload endpoint used to create the row and the file in
    one step; with persistence living in backend-java, tests stage files
    directly on disk and, when an index is needed, drive ``POST /v1/index``.
    """
    from app.config import get_settings

    settings = get_settings()
    path = settings.workspace_dir(workspace_id) / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
