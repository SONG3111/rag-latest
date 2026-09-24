"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.internal import router as internal_router
from .api.internal_chat import router as internal_chat_router
from .api.routes import router
from .config import get_settings
from .db import init_db
from .mcp_client import McpOfficeClient, McpStartupError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_directories()
    init_db()
    logger.info("database ready at %s", settings.effective_database_url)
    _warn_about_legacy_chunks()

    client = McpOfficeClient(settings)
    try:
        await client.start()
    except McpStartupError as exc:
        # The API still starts so the UI can explain what is wrong, but every
        # document endpoint will refuse to run until the server is reachable.
        logger.error("MCP document server unavailable: %s", exc)
        app.state.mcp_error = str(exc)
    else:
        app.state.mcp_error = None
    app.state.mcp_client = client

    try:
        yield
    finally:
        await client.stop()
        from .retrieval.vector_store import close_client

        close_client()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="工作区文档 Agent",
        description=(
            "面向本地工作区的文档 Agent：对话式修改 Excel/Word，"
            "并通过知识库检索回答文档相关问题。"
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    # 内部契约：仅 backend-java（内网）调用，不对前端暴露。
    app.include_router(internal_router)
    app.include_router(internal_chat_router)

    @app.get("/health")
    def health() -> dict:
        client = getattr(app.state, "mcp_client", None)
        return {
            "status": "ok",
            "mcp_started": bool(client and client.started),
            "mcp_error": getattr(app.state, "mcp_error", None),
            "tools": len(client.profiles()) if client and client.started else 0,
        }

    return app


def _warn_about_legacy_chunks() -> None:
    """Point operators at the rebuild endpoint when chunks predate the hierarchy."""
    from sqlalchemy.exc import SQLAlchemyError

    from .db import session_scope
    from .services.files import legacy_chunk_workspaces

    try:
        with session_scope() as session:
            workspace_ids = legacy_chunk_workspaces(session)
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        logger.warning("could not inspect chunk format: %s", exc)
        return

    if workspace_ids:
        logger.warning(
            "%d workspace(s) still hold flat, pre-hierarchy chunks and cite imprecisely. "
            "Rebuild them with POST /api/workspaces/{id}/reindex. Affected: %s",
            len(workspace_ids),
            ", ".join(workspace_ids),
        )


app = create_app()
