"""FastAPI application entry point.

Runtime-only since the Java-backend split (M4): backend-java owns persistence
and every public endpoint; this process serves the internal ``/v1`` contract
(tools / preview / index / chat-stream) and ``/health``.
"""

from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.internal import router as internal_router
from .api.internal_chat import router as internal_chat_router
from .config import get_settings
from .llm.providers import build_embeddings
from .logging_setup import close_file_logging, configure_file_logging
from .mcp_client import McpOfficeClient, McpStartupError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 内部契约的请求头名，与 Java 侧 InternalToken.HEADER 必须逐字一致。
INTERNAL_TOKEN_HEADER = "X-Internal-Token"


class InternalTokenMiddleware:
    """把 /v1 内部契约关进共享密钥后面。

    为什么需要它：这些端点没有会话可言（服务间调用），在无状态拆分之后它们
    承载着索引、检索语料、工具调用与整轮对话——任何能访问 8001 的进程都能
    读遍所有工作区。改造前 Java 的 /internal 端点在对外 8000 上完全裸奔，
    那是真正被利用的那个洞；这里是同一道边界的内侧。

    未配置令牌时不拦截（本机裸跑与测试保持"零配置可跑"），但打一条醒目警告：
    生产部署（compose）必须设置，两侧取值见 .env.example。

    **刻意写成纯 ASGI 中间件而不是 `@app.middleware("http")`**：后者在 Starlette
    里是 ``BaseHTTPMiddleware``，它会包一层响应对象，破坏流式响应的取消/超时语义
    ——本项目 /v1/chat/stream 的"超时落库部分答案""客户端断流落库"全靠这套语义，
    换上 BaseHTTPMiddleware 后这些用例会静默不再触发（notices 为空）。纯 ASGI
    中间件只是原样转发 ``send``，对流无任何影响。
    """

    def __init__(self, app, expected: str) -> None:
        self.app = app
        self.expected = expected or ""

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] == "http"
            and self.expected
            and scope.get("path", "").startswith("/v1")
        ):
            headers = dict(scope.get("headers") or [])
            presented = headers.get(INTERNAL_TOKEN_HEADER.lower().encode(), b"").decode(
                "latin-1"
            )
            # 常量时间比较：避免按字节早退泄露令牌前缀。
            if not secrets.compare_digest(presented, self.expected):
                response = JSONResponse(
                    {"detail": "internal token required"}, status_code=401
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _warm_embeddings() -> None:
    """后台预载嵌入模型：把首个文件 5-15s 的冷启动挪到服务启动期。

    失败只告警——嵌入仍是尽力而为（BM25 兜底），首个索引请求会再试一次。
    """
    try:
        build_embeddings(get_settings())
        logger.info("embedding model warmed up")
    except Exception:
        logger.warning("embedding warmup failed; will retry on first index", exc_info=True)


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

    threading.Thread(target=_warm_embeddings, name="embedding-warmup", daemon=True).start()

    try:
        yield
    finally:
        await client.stop()
        from .retrieval.vector_store import close_client

        close_client()
        logger.info("shutdown complete")
        # 放在最后一条日志之后：卸载文件 handler 并释放句柄。
        close_file_logging()


def create_app() -> FastAPI:
    settings = get_settings()
    # 文件日志随应用创建装配（data_dir/logs/ai-service.log，轮转）；
    # 放在 create_app 而不是 lifespan，测试的 ASGITransport 不跑 lifespan。
    configure_file_logging(settings)
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
    if not settings.internal_token:
        logger.warning(
            "%s 未设置：/v1 内部契约不受保护，仅可用于本机开发；"
            "部署前请在 .env 中设置两侧一致的令牌（见 .env.example）",
            "INTERNAL_TOKEN",
        )
    app.add_middleware(InternalTokenMiddleware, expected=settings.internal_token)
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
