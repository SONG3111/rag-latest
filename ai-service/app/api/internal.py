"""Internal v1 API consumed by the Java business backend (backend-java/).

These endpoints exist only inside the deployment network (docker network or
localhost). They are the seam of the service split: the Java side owns
persistence and every public endpoint; this side owns the MCP subprocess and
everything model- or document-bound. The internal contract is documented in
docs/ (架构迁移); keep it additive-only — the Java side pins to these paths.
"""

from __future__ import annotations

import anyio
import hashlib
import logging
import uuid
from pathlib import Path
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

# 索引重活（解析/分块/嵌入/向量写）的串行闸门。
#
# 两个来源：(1) CPU 上并发 encode 同一个 SentenceTransformer 只会互相争抢
# intra-op 线程池——Onyx #8396 实测无上限并发让 CPU 嵌入慢约 200 倍，
# sentence-transformers #857 官方结论 "encode already distributes work across
# threads"；(2) 共享模型对象并非官方声明线程安全。
# limiter=1 保证任一时刻只有一段索引在跑；重活经 to_thread 挪出事件循环，
# 长索引期间 /health、chat、preview 不再被阻塞（asyncio 线程池默认 40 线程足够）。
INDEX_CAPACITY = anyio.CapacityLimiter(1)


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


class IndexFile(BaseModel):
    file_id: str = Field(min_length=1, max_length=64)
    rel_path: str = Field(min_length=1, max_length=500)


class IndexRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    file_id: str = Field(min_length=1, max_length=64)
    rel_path: str = Field(min_length=1, max_length=500)


class IndexBatchRequest(BaseModel):
    """批量索引：跨文件合并嵌入（长度排序池更大、padding 浪费更少）。

    批量 API 先例：RAGFlow 的 document_ids 列表式 ingest 触发
    （docs/references/http_api_reference.md）。
    """

    workspace_id: str = Field(min_length=1, max_length=64)
    files: list[IndexFile] = Field(min_length=1, max_length=50)


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


def _parse_chunks(
    settings, workspace_id: str, rel_path: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Path] | str:
    """解析 + 分块 + 组行（含 jieba token_counts）。

    失败返回错误字符串（文件缺失/解析失败）；路径非法抛 ``IngestionError``，
    由单文件端点保持 400 语义、批量端点按文件降级为 failed。
    """
    path = resolve_workspace_path(workspace_id, rel_path)

    if not path.exists():
        return "file is missing from disk"

    try:
        groups = chunk_document_groups(
            path,
            rel_path,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
        )
    except Exception as exc:
        logger.exception("parsing failed for %s", rel_path)
        return f"parse failed: {exc}"

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
    return rows, child_rows, path


def _embed_texts(settings, texts: list[str]) -> tuple[list[list[float]] | None, str | None]:
    """全量嵌入一批文本；失败返回 (None, error)——稠密索引是尽力而为，BM25 兜底。"""
    if not texts:
        return [], None
    try:
        embeddings = build_embeddings(settings)
        vectors = embeddings.embed_documents(texts)
        if len(vectors) != len(texts):
            raise VectorStoreError(
                f"embedding provider returned {len(vectors)} vectors "
                f"for {len(texts)} chunks"
            )
        return vectors, None
    except Exception as exc:
        logger.warning("dense indexing failed, keyword search still available: %s", exc)
        return None, str(exc)


def _write_vectors(
    settings, workspace_id: str, file: IndexFile, child_rows, vectors
) -> tuple[int, str | None]:
    """reindex 覆盖而非追加（一个文件不会重复贡献向量）；返回 (vector_count, error)。"""
    store = VectorStore(settings)
    try:
        store.delete_file(workspace_id, file.file_id)
    except Exception as exc:
        logger.warning("could not clear old vectors for %s: %s", file.file_id, exc)

    if vectors is None:
        return 0, None
    try:
        count = store.upsert_vectors(
            workspace_id,
            [row["id"] for row in child_rows],
            vectors,
            [
                {
                    "file_id": file.file_id,
                    "rel_path": file.rel_path,
                    "location": row["location"],
                    "ordinal": row["ordinal"],
                }
                for row in child_rows
            ],
        )
        return count, None
    except Exception as exc:
        logger.warning(
            "dense index write failed for %s, keyword search still available: %s",
            file.rel_path,
            exc,
        )
        return 0, str(exc)


def _index_one_sync(settings, payload: IndexRequest) -> dict[str, Any]:
    """单文件索引的同步核心（跑在 executor 线程上）。"""
    parsed = _parse_chunks(settings, payload.workspace_id, payload.rel_path)
    if isinstance(parsed, str):
        return _index_failed(parsed)
    rows, child_rows, path = parsed

    vectors, embed_error = _embed_texts(settings, [row["text"] for row in child_rows])
    vector_count, upsert_error = _write_vectors(
        settings, payload.workspace_id, IndexFile(file_id=payload.file_id, rel_path=payload.rel_path),
        child_rows, vectors,
    )
    vector_error = embed_error or upsert_error
    return {
        "status": "indexed",
        "chunk_count": len(child_rows),
        "vector_count": vector_count,
        "error": (f"dense index unavailable: {vector_error}" if vector_error else None),
        "checksum": _sha256_file(path),
        "chunks": rows,
    }


def _index_batch_sync(settings, payload: IndexBatchRequest) -> list[dict[str, Any]]:
    """批量索引的同步核心：逐文件解析（单文件失败不拖垮整批）→ 全部 child 一次嵌入
    → 逐文件写向量。返回与单文件响应同构的结果数组，顺序与请求 files 一致。"""
    parsed: list[Any] = []
    for file in payload.files:
        try:
            parsed.append(_parse_chunks(settings, payload.workspace_id, file.rel_path))
        except IngestionError as exc:
            parsed.append(f"invalid path: {exc}")

    # 跨文件合并嵌入：一次 embed_documents 让长度排序池覆盖全部文件，
    # 减小 per-call 的排序/tokenizer 边界开销与 padding 浪费。
    all_texts: list[str] = []
    for entry in parsed:
        if not isinstance(entry, str):
            all_texts.extend(row["text"] for row in entry[1])
    vectors, embed_error = _embed_texts(settings, all_texts)

    results: list[dict[str, Any]] = []
    offset = 0
    for file, entry in zip(payload.files, parsed):
        if isinstance(entry, str):
            results.append(_index_failed(entry))
            continue
        rows, child_rows, path = entry
        file_vectors = None
        if vectors is not None:
            file_vectors = vectors[offset:offset + len(child_rows)]
        offset += len(child_rows)
        vector_count, upsert_error = _write_vectors(
            settings, payload.workspace_id, file, child_rows, file_vectors)
        vector_error = embed_error or upsert_error
        results.append({
            "status": "indexed",
            "chunk_count": len(child_rows),
            "vector_count": vector_count,
            "error": (f"dense index unavailable: {vector_error}" if vector_error else None),
            "checksum": _sha256_file(path),
            "chunks": rows,
        })
    return results


@router.post("/index")
async def index_file(payload: IndexRequest) -> dict[str, Any]:
    """解析工作区文件为父子分块并写入稠密索引，返回分块行交给 backend-java 落库。

    与旧 index_file 的差别：**本端点不写数据库**——chunk 行由 Java 持久化（app.db 只被
    Java 进程打开）。分块 id 在此生成，Qdrant 的 payload.chunk_id 与之一致。稠密索引是
    尽力而为：嵌入不可用时仍返回分块行，文件依旧可经 BM25 检索。

    重活经 ``anyio.to_thread`` 挪出事件循环并受 ``INDEX_CAPACITY``（=1）串行化：
    长索引不再阻塞 /health、chat、preview（事件循环停摆的实测对照见
    docs/bugs/），CPU 上并发 encode 只会更慢（Onyx #8396）。
    """
    settings = get_settings()
    try:
        return await anyio.to_thread.run_sync(
            lambda: _index_one_sync(settings, payload), limiter=INDEX_CAPACITY
        )
    except IngestionError as exc:
        # 路径非法维持原 400 语义（与拆分前一致）；批量端点则按文件降级 failed。
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/index-batch")
async def index_batch(payload: IndexBatchRequest) -> list[dict[str, Any]]:
    """批量索引：一次请求处理多文件（跨文件合并嵌入），结果数组与请求同序。

    供 Java 侧 RabbitMQ 攒批消费调用；单文件的解析失败只影响该文件
    （对应结果 status=failed），不拖垮整批。
    """
    settings = get_settings()
    return await anyio.to_thread.run_sync(
        lambda: _index_batch_sync(settings, payload), limiter=INDEX_CAPACITY
    )


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
