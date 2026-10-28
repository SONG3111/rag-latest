"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .assistant.feishu import FeishuBridge
from .assistant.routes import router as assistant_router
from .assistant.service import recover_interrupted_actions
from .config import get_settings
from .db import init_db, session_scope
from .meeting.asr import WhisperLiveKitTranscriber
from .meeting.routes import router as meeting_router
from .meeting.service import recover_interrupted_meetings
from .mcp_client import McpFeishuClient, McpOfficeClient, McpStartupError
from .news.routes import router as news_router

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
    if settings.assistant_enabled:
        with session_scope() as session:
            recovered = recover_interrupted_actions(session)
        if recovered:
            logger.warning(
                "marked %d interrupted assistant action(s) as failed", recovered
            )
    if settings.meeting_enabled:
        with session_scope() as session:
            interrupted, summaries = recover_interrupted_meetings(session)
        if interrupted or summaries:
            logger.warning(
                "meeting recovery: %d interrupted recording(s), %d summary job(s)",
                interrupted,
                summaries,
            )
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

    assistant_client = McpFeishuClient(settings)
    if settings.assistant_enabled:
        try:
            await assistant_client.start()
        except McpStartupError as exc:
            logger.error("Feishu Mail MCP server unavailable: %s", exc)
            app.state.assistant_mcp_error = str(exc)
        else:
            app.state.assistant_mcp_error = None
    else:
        app.state.assistant_mcp_error = "assistant disabled"
    app.state.assistant_mcp_client = assistant_client

    # The ASR engine is lazy: constructing this adapter does not import or load
    # Whisper until the first recording starts.
    app.state.meeting_transcriber = WhisperLiveKitTranscriber(settings)

    bridge = FeishuBridge(settings, mail_client=assistant_client)
    app.state.feishu_bridge = bridge
    if (
        settings.assistant_enabled
        and assistant_client.started
        and settings.feishu_mail_configured
    ):
        try:
            bridge.start(asyncio.get_running_loop())
        except Exception as exc:
            app.state.feishu_error = str(exc)
            logger.error("Feishu bridge failed to start: %s", exc)
        else:
            app.state.feishu_error = bridge.last_error
    else:
        app.state.feishu_error = (
            "assistant disabled"
            if not settings.assistant_enabled
            else ("Feishu Mail MCP is unavailable" if not assistant_client.started else None)
        )

    try:
        yield
    finally:
        if assistant_client.started:
            await assistant_client.stop()
        # Close in reverse order of startup: stdio_client uses anyio cancel
        # scopes, which must unwind LIFO within the lifespan task.
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
    app.include_router(assistant_router)
    app.include_router(meeting_router)
    app.include_router(news_router)

    @app.get("/health")
    def health() -> dict:
        client = getattr(app.state, "mcp_client", None)
        assistant = getattr(app.state, "assistant_mcp_client", None)
        bridge = getattr(app.state, "feishu_bridge", None)
        meeting = getattr(app.state, "meeting_transcriber", None)
        settings = get_settings()
        return {
            "status": "ok",
            "mcp_started": bool(client and client.started),
            "mcp_error": getattr(app.state, "mcp_error", None),
            "tools": len(client.profiles()) if client and client.started else 0,
            "assistant": {
                "enabled": settings.assistant_enabled,
                "mail_mcp_started": bool(assistant and assistant.started),
                "mail_auth_ready": settings.feishu_mail_configured,
                "mail_error": getattr(app.state, "assistant_mcp_error", None),
                "feishu_configured": settings.feishu_configured,
                "feishu_connected": bool(bridge and bridge.connected),
                "feishu_error": getattr(app.state, "feishu_error", None),
            },
            "meeting": (
                meeting.health()
                if meeting is not None
                else {"enabled": settings.meeting_enabled, "ready": False}
            ),
            "news": {
                "enabled": settings.news_enabled,
                "timezone": settings.news_timezone,
                "generation_mode": "on_demand",
                "cache_minutes": settings.news_cache_minutes,
            },
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
