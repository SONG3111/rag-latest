"""Internal v1 API consumed by the Java business backend (backend-java/).

These endpoints exist only inside the deployment network (docker network or
localhost). They are the seam of the service split: the Java side owns
persistence and every public endpoint; this side owns the MCP subprocess and
everything model- or document-bound. The internal contract is documented in
docs/ (架构迁移); keep it additive-only — the Java side pins to these paths.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import get_settings
from ..llm.providers import build_embeddings
from ..mcp_client import parse_tool_result
from ..retrieval.bm25 import token_counts
from ..retrieval.chunking import chunk_document_groups
from ..retrieval.vector_store import VectorStore, VectorStoreError
from ..services.files import IngestionError, resolve_workspace_path
from ..services.preview import PreviewError, build_preview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


def _get_client(request: Request):
    client = getattr(request.app.state, "mcp_client", None)
    if client is None or not client.started:
        raise HTTPException(status_code=503, detail="MCP document server is not available")
    return client


@router.get("/tools")
async def list_tools(request: Request) -> list[dict[str, Any]]:
    """MCP 工具清单（含审批注解）；Java 的 GET /api/.../tools 透传此结果。"""
    client = _get_client(request)
    return [
        {
            "name": profile.name,
            "description": profile.description,
            "read_only": profile.read_only,
            "destructive": profile.destructive,
            "requires_approval": profile.requires_approval,
            "schema": profile.schema,
        }
        for profile in sorted(client.profiles().values(), key=lambda item: item.name)
    ]


class ToolCallRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


@router.post("/tools/call")
async def call_tool(payload: ToolCallRequest, request: Request) -> dict[str, Any]:
    """按名执行一个 MCP 工具并返回其 ``{"ok": ...}`` 信封。

    写工具经此端点直达文件系统：该端点仅限内网，调用方（Java）负责仅在
    提案审批通过后发起写调用 —— 审批门在业务层，这里不重复实现。
    """
    client = _get_client(request)
    tool = client.tool(payload.tool)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"unknown tool: {payload.tool}")
    return parse_tool_result(await tool.ainvoke(payload.arguments))


@router.get("/workspaces/{workspace_id}/preview")
async def preview(
    workspace_id: str, file: str, location: str, request: Request
) -> dict[str, Any]:
    """引用出处预览：location 解析成读窗口后经只读 MCP 工具取原文。"""
    client = _get_client(request)
    try:
        return await build_preview(client, workspace_id, file, location)
    except PreviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class IndexRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    file_id: str = Field(min_length=1, max_length=64)
    rel_path: str = Field(min_length=1, max_length=500)


def _index_failed(error: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "chunk_count": 0,
        "vector_count": 0,
        "error": error,
        "checksum": "",
        "chunks": [],
    }


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


@router.post("/index")
async def index_file(payload: IndexRequest) -> dict[str, Any]:
    """解析工作区文件为父子分块并写入稠密索引，返回分块行交给 backend-java 落库。

    与旧 index_file 的差别：**本端点不写数据库**——chunk 行由 Java 持久化（app.db 只被
    Java 进程打开）。分块 id 在此生成，Qdrant 的 payload.chunk_id 与之一致。稠密索引是
    尽力而为：嵌入不可用时仍返回分块行，文件依旧可经 BM25 检索。
    """
    settings = get_settings()
    try:
        path = resolve_workspace_path(payload.workspace_id, payload.rel_path)
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not path.exists():
        return _index_failed("file is missing from disk")

    try:
        groups = chunk_document_groups(
            path,
            payload.rel_path,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
        )
    except Exception as exc:
        logger.exception("parsing failed for %s", payload.rel_path)
        return _index_failed(f"parse failed: {exc}")

    rows: list[dict[str, Any]] = []
    child_rows: list[dict[str, Any]] = []
    ordinal = 0
    for group in groups:
        parent_counts = token_counts(group.parent.text)
        parent_id = uuid.uuid4().hex
        rows.append({
            "id": parent_id,
            "parent_id": None,
            "level": "parent",
            "ordinal": ordinal,
            "text": group.parent.text,
            "location": group.parent.location,
            "meta": group.parent.meta,
            "token_counts": parent_counts,
            "token_length": sum(parent_counts.values()),
        })
        ordinal += 1
        for child in group.children:
            child_counts = token_counts(child.text)
            row = {
                "id": uuid.uuid4().hex,
                "parent_id": parent_id,
                "level": "child",
                "ordinal": ordinal,
                "text": child.text,
                "location": child.location,
                "meta": child.meta,
                "token_counts": child_counts,
                "token_length": sum(child_counts.values()),
            }
            ordinal += 1
            rows.append(row)
            child_rows.append(row)

    vectors = None
    vector_error: str | None = None
    if child_rows:
        try:
            embeddings = build_embeddings(settings)
            vectors = embeddings.embed_documents([row["text"] for row in child_rows])
            if len(vectors) != len(child_rows):
                raise VectorStoreError(
                    f"embedding provider returned {len(vectors)} vectors "
                    f"for {len(child_rows)} chunks"
                )
        except Exception as exc:
            vectors = None
            vector_error = str(exc)
            logger.warning(
                "dense indexing failed for %s, keyword search still available: %s",
                payload.rel_path,
                exc,
            )

    store = VectorStore(settings)
    try:  # reindex 覆盖而非追加，一个文件不会重复贡献向量
        store.delete_file(payload.workspace_id, payload.file_id)
    except Exception as exc:
        logger.warning("could not clear old vectors for %s: %s", payload.file_id, exc)

    vector_count = 0
    if vectors is not None:
        try:
            vector_count = store.upsert_vectors(
                payload.workspace_id,
                [row["id"] for row in child_rows],
                vectors,
                [
                    {
                        "file_id": payload.file_id,
                        "rel_path": payload.rel_path,
                        "location": row["location"],
                        "ordinal": row["ordinal"],
                    }
                    for row in child_rows
                ],
            )
        except Exception as exc:
            vector_error = vector_error or str(exc)
            logger.warning(
                "dense index write failed for %s, keyword search still available: %s",
                payload.rel_path,
                exc,
            )

    return {
        "status": "indexed",
        "chunk_count": len(child_rows),
        "vector_count": vector_count,
        "error": (f"dense index unavailable: {vector_error}" if vector_error else None),
        "checksum": _sha256_file(path),
        "chunks": rows,
    }


@router.delete("/collections/{workspace_id}", status_code=204)
async def drop_collection(workspace_id: str) -> None:
    """删除工作区时清掉它的稠密索引集合；失败上抛 502，由调用方降级为告警。"""
    from ..retrieval.vector_store import VectorStore

    try:
        VectorStore().drop_collection(workspace_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"could not drop collection: {exc}") from exc


@router.delete("/collections/{workspace_id}/vectors", status_code=204)
async def delete_file_vectors(workspace_id: str, file_id: str) -> None:
    """删除单文件时按 file_id 过滤清向量（与 python 版 delete_file 行为一致）。"""
    from ..retrieval.vector_store import VectorStore

    try:
        VectorStore().delete_file(workspace_id, file_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"could not delete file vectors: {exc}"
        ) from exc
