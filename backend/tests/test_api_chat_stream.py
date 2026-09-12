"""SSE chat-stream tests over the real HTTP surface.

``test_agent.py`` pins the agent's control flow in-process; these tests drive
``POST /chat/stream`` the way the browser does and parse the raw SSE frames,
because the HTTP layer adds its own contract: frame framing, the route-level
``done`` that replaces the graph's, persistence after the stream, and the 404/503
guards. The model is scripted, so no API key or network is involved.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk
from openpyxl import Workbook

pytestmark = pytest.mark.anyio

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ScriptedLLM:
    """Replays a fixed sequence of AI messages over the streaming surface."""

    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]) -> "ScriptedLLM":
        return self

    async def astream(self, messages: list[Any]):
        self.calls.append(list(messages))
        message = self.script.pop(0) if self.script else AIMessage(content="结束")
        yield AIMessageChunk(
            content=message.content,
            tool_calls=list(getattr(message, "tool_calls", None) or []),
        )


class ExplodingLLM(ScriptedLLM):
    """A model whose stream dies mid-turn, simulating a provider failure."""

    async def astream(self, messages: list[Any]):
        self.calls.append(list(messages))
        raise RuntimeError("provider connection reset")
        yield  # pragma: no cover - makes this an async generator


def _call(name: str, args: dict, call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def parse_sse_frames(body: str) -> list[tuple[str, dict]]:
    """Split a raw SSE body into (event, payload) pairs, ignoring comments."""
    frames: list[tuple[str, dict]] = []
    for block in re.split(r"\r?\n\r?\n", body):
        event = None
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data.append(line.split(":", 1)[1].strip())
            # ':' lines are keep-alive comments; sse-starlette sends them between events.
        if event and data:
            frames.append((event, json.loads("\n".join(data))))
    return frames


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
    created = await client.post("/api/workspaces", json={"name": "流式对话"})
    workspace_id = created.json()["id"]
    uploaded = await client.post(
        f"/api/workspaces/{workspace_id}/files",
        files=[("files", ("销售表.xlsx", workbook_bytes(), XLSX_MIME))],
    )
    assert uploaded.status_code == 201
    return workspace_id


async def _stream_turn(
    app, workspace_id: str, message: str
) -> tuple[int, list[tuple[str, dict]]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workspaces/{workspace_id}/chat/stream",
            json={"message": message},
        )
        return response.status_code, parse_sse_frames(response.text)


@pytest.fixture()
def scripted_model(monkeypatch: pytest.MonkeyPatch):
    """Route the agent's lazy ``build_chat_model`` to a scripted replay."""

    def _install(script: list[AIMessage]) -> ScriptedLLM:
        from app.llm import providers

        model = ScriptedLLM(script)
        monkeypatch.setattr(providers, "build_chat_model", lambda settings: model)
        return model

    return _install


async def test_tool_turn_streams_events_and_persists_the_turn(
    app_with_temp_storage, scripted_model
) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            workspace_id = await _workspace_with_workbook(client)
            scripted_model(
                [
                    _call(
                        "read_range",
                        {"path": "销售表.xlsx", "sheet_name": "销售", "start_cell": "A1"},
                    ),
                    AIMessage(content="表格里 A型 的销售额是 1000。"),
                ]
            )

            status, frames = await _stream_turn(
                app_with_temp_storage, workspace_id, "A型 卖了多少？"
            )
            assert status == 200

            events = [name for name, _ in frames]
            assert "tool_call" in events
            assert "tool_result" in events
            assert events.count("done") == 1, "the route must emit exactly one done"

            # Every frame carries its own type for the frontend's dispatcher.
            for name, payload in frames:
                assert payload["type"] == name

            tool_call = next(payload for name, payload in frames if name == "tool_call")
            assert tool_call["tool"] == "read_range"
            # The label is the human-readable progress line shown while working.
            assert "销售表.xlsx" in tool_call["label"]

            done = frames[-1]
            assert done[0] == "done"
            assert done[1]["content"] == "表格里 A型 的销售额是 1000。"

            # The turn survives the stream: user text plus the assembled answer.
            messages = (await client.get(f"/api/workspaces/{workspace_id}/messages")).json()
            assert [item["role"] for item in messages] == ["user", "assistant"]
            assert messages[0]["content"] == "A型 卖了多少？"
            assert messages[1]["content"] == done[1]["content"]
            assert messages[1]["tool_calls"] is None


async def test_token_only_turn_streams_text_without_tool_events(
    app_with_temp_storage, scripted_model
) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            workspace_id = await _workspace_with_workbook(client)
            scripted_model([AIMessage(content="你好！想查数据还是改文档？")])

            status, frames = await _stream_turn(
                app_with_temp_storage, workspace_id, "你好"
            )
            assert status == 200

            events = [name for name, _ in frames]
            assert "tool_call" not in events and "proposal" not in events
            streamed = "".join(
                payload["text"] for name, payload in frames if name == "token"
            )
            done = next(payload for name, payload in frames if name == "done")
            assert streamed.strip() == done["content"] == "你好！想查数据还是改文档？"


async def test_write_turn_becomes_a_pending_proposal_not_a_write(
    app_with_temp_storage, scripted_model
) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            workspace_id = await _workspace_with_workbook(client)
            from app.config import get_settings

            target = get_settings().workspace_dir(workspace_id) / "销售表.xlsx"
            original = target.read_bytes()

            scripted_model(
                [
                    _call(
                        "update_cells",
                        {
                            "path": "销售表.xlsx",
                            "sheet_name": "销售",
                            "updates": [{"cell": "B2", "value": 1500}],
                        },
                    ),
                    AIMessage(content="已生成修改提案，等待你确认。"),
                ]
            )

            status, frames = await _stream_turn(
                app_with_temp_storage, workspace_id, "把 B2 改成 1500"
            )
            assert status == 200

            proposal = next(payload for name, payload in frames if name == "proposal")
            assert proposal["tool"] == "update_cells"
            assert proposal["diff"] == [{"cell": "B2", "before": 1000, "after": 1500}]

            done = next(payload for name, payload in frames if name == "done")
            assert "1 条待确认的修改提案" in done["content"]

            # The gate held: nothing reached the file during the chat turn.
            assert target.read_bytes() == original

            # The proposal is durable and shows up as pending work.
            messages = (await client.get(f"/api/workspaces/{workspace_id}/messages")).json()
            assistant = messages[-1]
            assert assistant["tool_calls"] and assistant["tool_calls"][0]["operation_id"]

            pending = await client.get(
                f"/api/workspaces/{workspace_id}/operations",
                params={"status_filter": "proposed"},
            )
            assert [item["id"] for item in pending.json()] == [proposal["operation_id"]]


async def test_provider_failure_yields_an_error_frame_and_a_placeholder_turn(
    app_with_temp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm import providers

    monkeypatch.setattr(
        providers, "build_chat_model", lambda settings: ExplodingLLM([])
    )

    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            workspace_id = await _workspace_with_workbook(client)

            status, frames = await _stream_turn(
                app_with_temp_storage, workspace_id, "随便说点什么"
            )
            assert status == 200

            events = [name for name, _ in frames]
            assert "error" in events, "a provider crash must surface as an SSE error"
            # The stream still terminates cleanly with the empty-answer placeholder.
            assert events[-1] == "done"
            assert frames[-1][1]["content"].strip() != ""

            messages = (await client.get(f"/api/workspaces/{workspace_id}/messages")).json()
            assert [item["role"] for item in messages] == ["user", "assistant"]


async def test_chat_stream_guards(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            unknown = await client.post(
                "/api/workspaces/no-such-workspace/chat/stream",
                json={"message": "在吗"},
            )
            assert unknown.status_code == 404

            blank = await client.post(
                "/api/workspaces/anything/chat/stream", json={"message": "  "}
            )
            assert blank.status_code in {404, 422}, "a blank message never reaches the agent"

    # Without the MCP server the chat endpoint refuses up front with 503.
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/workspaces", json={"name": "无MCP"})
        workspace_id = created.json()["id"]
        response = await client.post(
            f"/api/workspaces/{workspace_id}/chat/stream",
            json={"message": "在吗"},
        )
        assert response.status_code == 503
