"""Approval-loop tests driven through the REST endpoints.

``test_operations.py`` pins the service logic in one long-lived session; these
tests exercise the same flow the way the frontend does — one HTTP request per
step, each with its own database session — because that is where the extra
failure modes live: status-to-HTTP mapping (409), cross-workspace isolation, and
the automatic reindex that must follow every applied write.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook, load_workbook

from app.services.operations import create_operation

pytestmark = pytest.mark.anyio

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook_bytes() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["产品", "销售额"])
    sheet.append(["A型", 1000])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


async def _workspace_with_workbook(client: AsyncClient) -> str:
    created = await client.post("/api/workspaces", json={"name": "审批闭环"})
    workspace_id = created.json()["id"]
    uploaded = await client.post(
        f"/api/workspaces/{workspace_id}/files",
        files=[("files", ("销售表.xlsx", workbook_bytes(), XLSX_MIME))],
    )
    assert uploaded.status_code == 201
    return workspace_id


def _seed_proposal(app, workspace_id: str):
    """Record a pending write the way the agent would, then commit for the API."""
    session = app.state.test_session_factory()
    try:
        operation = create_operation(
            session,
            workspace_id,
            "update_cells",
            {
                "path": "销售表.xlsx",
                "sheet_name": "销售",
                "updates": [{"cell": "B2", "value": 1500}],
            },
            diff=[{"cell": "B2", "before": 1000, "after": 1500}],
        )
        session.commit()
        return operation.id
    finally:
        session.close()


def _workbook_on_disk(app, workspace_id: str) -> Path:
    from app.config import get_settings

    return get_settings().workspace_dir(workspace_id) / "销售表.xlsx"


async def test_proposal_apply_revert_cycle_over_http(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            workspace_id = await _workspace_with_workbook(client)
            original = _workbook_on_disk(app_with_temp_storage, workspace_id).read_bytes()
            operation_id = _seed_proposal(app_with_temp_storage, workspace_id)

            # The pending proposal is visible through the API before anything runs.
            pending = await client.get(
                f"/api/workspaces/{workspace_id}/operations",
                params={"status_filter": "proposed"},
            )
            assert pending.status_code == 200
            assert [item["id"] for item in pending.json()] == [operation_id]
            assert pending.json()[0]["status"] == "proposed"
            assert "B2" in pending.json()[0]["summary"]

            applied = await client.post(
                f"/api/workspaces/{workspace_id}/operations/{operation_id}/apply"
            )
            assert applied.status_code == 200
            body = applied.json()
            assert body["status"] == "applied"
            assert body["diff"] == [{"cell": "B2", "before": 1000, "after": 1500}]
            assert Path(body["backup_path"]).exists()

            target = _workbook_on_disk(app_with_temp_storage, workspace_id)
            assert load_workbook(target)["销售"]["B2"].value == 1500

            # Applying twice must be a conflict, not a second write.
            conflict = await client.post(
                f"/api/workspaces/{workspace_id}/operations/{operation_id}/apply"
            )
            assert conflict.status_code == 409

            # Reverting restores the pre-change bytes...
            reverted = await client.post(
                f"/api/workspaces/{workspace_id}/operations/{operation_id}/revert"
            )
            assert reverted.status_code == 200
            assert reverted.json()["status"] == "rejected"
            assert target.read_bytes() == original
            assert load_workbook(target)["销售"]["B2"].value == 1000

            # ...and a reverted operation is closed for good.
            again = await client.post(
                f"/api/workspaces/{workspace_id}/operations/{operation_id}/revert"
            )
            assert again.status_code == 409

            # The write invalidated the index, so the API refreshed it: the file is
            # indexed and its stored checksum matches the reverted bytes.
            files = (await client.get(f"/api/workspaces/{workspace_id}/files")).json()
            assert files[0]["status"] == "indexed"


async def test_reject_leaves_the_file_untouched_over_http(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            workspace_id = await _workspace_with_workbook(client)
            target = _workbook_on_disk(app_with_temp_storage, workspace_id)
            original = target.read_bytes()
            operation_id = _seed_proposal(app_with_temp_storage, workspace_id)

            rejected = await client.post(
                f"/api/workspaces/{workspace_id}/operations/{operation_id}/reject"
            )
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "rejected"
            assert target.read_bytes() == original

            pending = await client.get(
                f"/api/workspaces/{workspace_id}/operations",
                params={"status_filter": "proposed"},
            )
            assert pending.json() == []

            history = await client.get(
                f"/api/workspaces/{workspace_id}/operations"
            )
            statuses = {item["id"]: item["status"] for item in history.json()}
            assert statuses[operation_id] == "rejected"

            again = await client.post(
                f"/api/workspaces/{workspace_id}/operations/{operation_id}/reject"
            )
            assert again.status_code == 409


async def test_operation_urls_are_workspace_scoped(app_with_temp_storage) -> None:
    """An operation belongs to one workspace; another workspace's URL must not see it."""
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            owner_id = await _workspace_with_workbook(client)
            other_id = (await client.post("/api/workspaces", json={"name": "旁观者"})).json()[
                "id"
            ]
            operation_id = _seed_proposal(app_with_temp_storage, owner_id)

            for action in ("apply", "reject", "revert"):
                response = await client.post(
                    f"/api/workspaces/{other_id}/operations/{operation_id}/{action}"
                )
                assert response.status_code == 404

            listing = await client.get(f"/api/workspaces/{other_id}/operations")
            assert listing.json() == []

            missing = await client.post(
                f"/api/workspaces/{owner_id}/operations/no-such-id/apply"
            )
            assert missing.status_code == 404


async def test_operations_without_mcp_refuse_to_apply(app_with_temp_storage) -> None:
    """Without the document server the write path must refuse, not half-run.

    The lifespan context is deliberately not entered, so the app has no MCP client —
    exactly what a broken startup looks like to the API.
    """
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workspace_id = await _workspace_with_workbook(client)
        operation_id = _seed_proposal(app_with_temp_storage, workspace_id)

        response = await client.post(
            f"/api/workspaces/{workspace_id}/operations/{operation_id}/apply"
        )
        assert response.status_code == 503
