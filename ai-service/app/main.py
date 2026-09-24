"""FastAPI application entry point.

Runtime-only since the Java-backend split (M4): backend-java owns persistence
and every public endpoint; this process serves the internal ``/v1`` contract
(tools / preview / index / chat-stream) and ``/health``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.internal import router as internal_router
from .api.internal_chat import router as internal_chat_router
from .config import get_settings
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
        title="工作区文档 Agent（AI 运行时）",
        description=(
            "面向本地工作区的文档 Agent 运行时：agent 编排、检索、嵌入与 MCP 子进程。"
            "对外端点由 backend-java 提供，本服务仅暴露内部 /v1 契约。"
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


app = create_app()
