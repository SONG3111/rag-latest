"""Internal v1 API consumed by the Java business backend (backend-java/).

These endpoints exist only inside the deployment network (docker network or
localhost). They are the seam of the service split: the Java side owns
persistence and every public endpoint; this side owns the MCP subprocess and
everything model- or document-bound. The internal contract is documented in
docs/ (架构迁移); keep it additive-only — the Java side pins to these paths.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..mcp_client import parse_tool_result
from ..services.preview import PreviewError, build_preview

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
