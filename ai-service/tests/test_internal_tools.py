"""Internal /v1 contract tests: the seam the Java business backend consumes.

The real MCP subprocess lifecycle runs against a temp sandbox (same pattern as
the public API tests); no LLM is ever contacted, per AGENTS.md.
"""

from __future__ import annotations

import io

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

from conftest import seed_workspace_file

pytestmark = pytest.mark.anyio


def workbook_bytes() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["产品", "销售额"])
    sheet.append(["A型", 100])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
async def seeded(app_with_temp_storage):
    """A workspace holding one staged xlsx, plus a live HTTP client."""
    app = app_with_temp_storage
    workspace_id = "ws-internal"
    seed_workspace_file(workspace_id, "销售表.xlsx", workbook_bytes())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client, workspace_id, "销售表.xlsx"


async def test_tools_list_profiles_with_approval_flags(seeded) -> None:
    client, _, _ = seeded
    response = await client.get("/v1/tools")
    assert response.status_code == 200
    tools = {item["name"]: item for item in response.json()}
    names = [item["name"] for item in response.json()]
    assert names == sorted(names)
    assert tools["read_range"]["read_only"] is True
    assert tools["read_range"]["requires_approval"] is False
    assert tools["update_cells"]["requires_approval"] is True
    assert tools["update_cells"]["destructive"] is True
    assert tools["read_range"]["schema"]


async def test_tools_call_round_trips_read_range(seeded) -> None:
    client, workspace_id, rel_path = seeded
    response = await client.post(
        "/v1/tools/call",
        json={
            "tool": "read_range",
            "arguments": {
                "path": f"{workspace_id}/{rel_path}",
                "sheet_name": "销售",
                "start_cell": "A1",
                "end_cell": "B1",
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["values"] == [["产品", "销售额"]]


async def test_tools_call_unknown_tool_is_404(seeded) -> None:
    client, _, _ = seeded
    response = await client.post(
        "/v1/tools/call", json={"tool": "no_such_tool", "arguments": {}}
    )
    assert response.status_code == 404


async def test_tools_call_rejects_empty_tool_name(seeded) -> None:
    client, _, _ = seeded
    response = await client.post("/v1/tools/call", json={"tool": "", "arguments": {}})
    assert response.status_code in (400, 422)


async def test_preview_returns_the_cited_window(seeded) -> None:
    client, workspace_id, rel_path = seeded
    response = await client.get(
        f"/v1/workspaces/{workspace_id}/preview",
        params={"file": rel_path, "location": "销售!第1行"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "excel"
    assert payload["file"] == rel_path
    # 预览窗口固定拉到 N 列，行尾用 None 补齐；只断言前两个有效单元格。
    assert payload["rows"][0][:2] == ["产品", "销售额"]


async def test_preview_bad_location_is_400(seeded) -> None:
    client, workspace_id, rel_path = seeded
    response = await client.get(
        f"/v1/workspaces/{workspace_id}/preview",
        params={"file": rel_path, "location": "不存在的格式"},
    )
    assert response.status_code == 400


async def test_drop_collection_reports_success(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, workspace_id, _ = seeded
    import app.retrieval.vector_store as vector_store

    calls: list[str] = []
    monkeypatch.setattr(
        vector_store.VectorStore, "drop_collection", lambda self, ws: calls.append(ws)
    )
    response = await client.delete(f"/v1/collections/{workspace_id}")
    assert response.status_code == 204
    assert calls == [workspace_id]


async def test_drop_collection_failure_is_502(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, workspace_id, _ = seeded
    import app.retrieval.vector_store as vector_store

    def boom(self, ws):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(vector_store.VectorStore, "drop_collection", boom)
    response = await client.delete(f"/v1/collections/{workspace_id}")
    assert response.status_code == 502


async def test_delete_file_vectors_filters_by_file_id(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, workspace_id, _ = seeded
    import app.retrieval.vector_store as vector_store

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        vector_store.VectorStore,
        "delete_file",
        lambda self, ws, file_id: calls.append((ws, file_id)),
    )
    response = await client.delete(
        f"/v1/collections/{workspace_id}/vectors", params={"file_id": "f123"}
    )
    assert response.status_code == 204
    assert calls == [(workspace_id, "f123")]
