"""End-to-end tests against the HTTP surface, with the real MCP server running."""

from __future__ import annotations

import io

import pytest
from docx import Document
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook


def workbook_bytes() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["产品", "区域", "销售额"])
    sheet.append(["A型", "华东", 1000])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def docx_bytes() -> bytes:
    document = Document()
    document.add_heading("报销制度", level=1)
    document.add_paragraph("第二条 单笔报销金额不得超过 5000 元。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.anyio
async def test_health_reports_mcp_status(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            response = await client.get("/health")
    body = response.json()
    assert response.status_code == 200
    assert body["mcp_started"] is True
    assert body["tools"] >= 12


@pytest.mark.anyio
async def test_workspace_and_file_lifecycle(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            created = await client.post("/api/workspaces", json={"name": "测试工作区"})
            assert created.status_code == 201
            workspace_id = created.json()["id"]

            uploaded = await client.post(
                f"/api/workspaces/{workspace_id}/files",
                files=[
                    ("files", ("销售表.xlsx", workbook_bytes(), "application/vnd.ms-excel")),
                    (
                        "files",
                        (
                            "制度.docx",
                            docx_bytes(),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        ),
                    ),
                ],
            )
            assert uploaded.status_code == 201
            payload = uploaded.json()
            assert {item["rel_path"] for item in payload} == {"销售表.xlsx", "制度.docx"}
            # Chunks are always written even when the dense index is unavailable.
            assert all(item["chunk_count"] > 0 for item in payload)

            files = (await client.get(f"/api/workspaces/{workspace_id}/files")).json()
            assert len(files) == 2

            tools = (await client.get(f"/api/workspaces/{workspace_id}/tools")).json()
            assert len(tools) >= 12
            gated = {tool["name"] for tool in tools if tool["requires_approval"]}
            assert {"update_cells", "replace_text", "delete_rows"} <= gated
            assert "read_range" not in gated

            deleted = await client.delete(f"/api/workspaces/{workspace_id}")
            assert deleted.status_code == 204


@pytest.mark.anyio
async def test_upload_rejects_unsupported_type(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            workspace_id = (
                await client.post("/api/workspaces", json={"name": "类型校验"})
            ).json()["id"]
            response = await client.post(
                f"/api/workspaces/{workspace_id}/files",
                files=[("files", ("note.txt", b"hello", "text/plain"))],
            )
    assert response.status_code == 400
    assert "unsupported" in response.json()["detail"]


@pytest.mark.anyio
async def test_unknown_workspace_returns_404(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            response = await client.get("/api/workspaces/does-not-exist/files")
    assert response.status_code == 404


def test_history_drops_stale_document_answers() -> None:
    """The conversation is replayed verbatim; freshness is handled per turn instead."""
    from app.api.routes import _to_langchain_history
    from app.models import Message, MessageRole

    rows = [
        Message(workspace_id="w", role=MessageRole.user, content="销售表里有什么？"),
        Message(
            workspace_id="w",
            role=MessageRole.assistant,
            content="销售表中 A型 的销售额是 1000。",
        ),
        Message(workspace_id="w", role=MessageRole.user, content="谢谢你"),
        Message(workspace_id="w", role=MessageRole.assistant, content="不客气，随时找我。"),
    ]

    history = _to_langchain_history(rows)
    assert [message.content for message in history] == [
        "销售表里有什么？",
        "销售表中 A型 的销售额是 1000。",
        "谢谢你",
        "不客气，随时找我。",
    ]


def test_files_changed_since_last_turn(temp_session, file_root) -> None:
    """Only a real change to the files invalidates the answers already given."""
    import os
    from datetime import datetime, timezone

    from app.api.routes import _files_changed_since_last_turn
    from app.config import get_settings
    from app.models import DocumentFile, IndexStatus, Message, MessageRole, Workspace

    workspace = Workspace(name="时效性")
    temp_session.add(workspace)
    temp_session.flush()

    directory = get_settings().workspace_dir(workspace.id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "销售表.xlsx"
    path.write_bytes(b"xlsx")
    # Stored timestamps are UTC-naive, and mtime is converted to UTC before comparing.
    stamp = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    os.utime(path, (stamp.timestamp(), stamp.timestamp()))

    temp_session.add(
        DocumentFile(
            workspace_id=workspace.id,
            rel_path="销售表.xlsx",
            kind="excel",
            status=IndexStatus.indexed,
            indexed_at=stamp.replace(tzinfo=None),
            created_at=stamp.replace(tzinfo=None),
        )
    )
    temp_session.flush()

    answered = [
        Message(
            workspace_id=workspace.id,
            role=MessageRole.user,
            content="销售表里有什么？",
            created_at=datetime(2026, 1, 1, 11, 0, 0),
        )
    ]
    assert _files_changed_since_last_turn(temp_session, workspace.id, answered) == []

    # No conversation yet: nothing has been read, so everything counts as changed.
    assert _files_changed_since_last_turn(temp_session, workspace.id, []) != []

    # An edit made outside the app still invalidates the earlier answer.
    later = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    os.utime(path, (later.timestamp(), later.timestamp()))
    assert _files_changed_since_last_turn(temp_session, workspace.id, answered) == [
        ("销售表.xlsx", "2026-01-01 12:00:00")
    ]


def test_recent_messages_returns_the_latest_window(temp_session) -> None:
    """A long conversation must hand the model its most recent turns, not its first."""
    from datetime import datetime

    from app.api.routes import _recent_messages
    from app.models import Message, MessageRole, Workspace

    workspace = Workspace(name="长对话")
    temp_session.add(workspace)
    temp_session.flush()
    for index in range(5):
        temp_session.add(
            Message(
                workspace_id=workspace.id,
                role=MessageRole.user,
                content=f"第 {index} 问",
                created_at=datetime(2026, 1, 1, 10, index, 0),
            )
        )
    temp_session.flush()

    recent = _recent_messages(temp_session, workspace.id, 2)
    assert [message.content for message in recent] == ["第 3 问", "第 4 问"]


def test_compose_answer_survives_an_empty_completion() -> None:
    """An empty model completion must not persist (and render) as an empty bubble."""
    from app.api.routes import EMPTY_ANSWER_FALLBACK, compose_answer

    assert compose_answer(["", "  "], 0) == EMPTY_ANSWER_FALLBACK
    assert compose_answer(["答案 A", "答案 B"], 0) == "答案 A\n\n答案 B"
    # A pending proposal always produces a notice, even with no model text.
    notice = compose_answer([], 2)
    assert "2 条待确认的修改提案" in notice
