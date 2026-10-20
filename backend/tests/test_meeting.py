"""Meeting assistant tests. No real ASR or LLM calls are made."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk

from app.config import get_settings
from app.meeting.agent import MeetingAgent
from app.meeting.asr import (
    ASRLine,
    ASRUpdate,
    build_asr_config,
    faster_whisper_transcribe_kwargs,
)
from app.meeting.minutes import generate_minutes_markdown
from app.meeting.service import (
    SegmentInput,
    begin_recording,
    begin_summary,
    build_markdown_export,
    create_meeting,
    fail_recording,
    finish_recording,
    recover_interrupted_meetings,
    transcript_segments,
    upsert_segments,
)
from app.models import MeetingStatus, MeetingSummaryStatus
from app.meeting.text import (
    is_probable_hallucination,
    normalize_transcript_text,
)


class ScriptedLLM:
    def __init__(self, script: list[AIMessage]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]):
        self.bound_tools = tools
        return self

    async def astream(self, messages: list[Any]):
        self.calls.append(list(messages))
        message = self.script.pop(0) if self.script else AIMessage(content="结束")
        yield AIMessageChunk(
            content=message.content,
            tool_calls=list(getattr(message, "tool_calls", None) or []),
        )


class DynamicMinutesLLM:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def astream(self, messages: list[Any]):
        self.calls.append(list(messages))
        prompt = str(messages[-1].content)
        if "<transcript_chunk>" in prompt:
            content = "分段事实摘要 [0:00:01]"
        elif "<chunk_summaries>" in prompt:
            content = "合并后的事实摘要"
        elif "<meeting>" in prompt:
            content = MINUTES_MARKDOWN
        else:
            content = MINUTES_MARKDOWN
        yield AIMessageChunk(content=content)


class FakeASRSession:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[ASRUpdate | None] = asyncio.Queue()
        self.audio_bytes = 0

    async def push_audio(self, data: bytes) -> None:
        self.audio_bytes += len(data)
        await self.queue.put(
            ASRUpdate(
                lines=[],
                partial="正在识别这句话",
                compute_lag_seconds=3.2,
            )
        )

    async def stop(self) -> None:
        await self.queue.put(
            ASRUpdate(
                lines=[
                    ASRLine(start_ms=0, end_ms=1800, text="我们今天讨论发布计划。"),
                    ASRLine(start_ms=1800, end_ms=3600, text="张三负责周五上线。"),
                ],
                partial="",
            )
        )
        await self.queue.put(None)

    async def updates(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def cleanup(self) -> None:
        return None


class FakeTranscriber:
    @property
    def ready(self) -> bool:
        return True

    async def open_session(self, *, language: str) -> FakeASRSession:
        self.language = language
        return FakeASRSession()

    def health(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "available": True,
            "ready": True,
            "loading": False,
            "backend": "fake",
            "model": "fake",
            "language": "zh",
            "device": "cpu",
            "compute_type": "int8",
            "error": None,
        }


MINUTES_MARKDOWN = """## 会议摘要
团队讨论发布计划。

## 关键结论与决策
- 计划周五上线。[0:00:00]

## 行动项
| 负责人 | 事项 | 截止时间 | 依据 |
| --- | --- | --- | --- |
| 张三 | 负责上线 | 周五 | [0:00:01] |

## 风险与待确认
未提及

## 讨论要点
- 发布节奏与负责人已明确。[0:00:01]
"""


def _seed_meeting(session):
    meeting = create_meeting(session, title="发布评审", language="zh")
    begin_recording(session, meeting.id)
    upsert_segments(
        session,
        meeting.id,
        [
            SegmentInput(0, 1800, "我们今天讨论发布计划。"),
            SegmentInput(1800, 3600, "张三负责周五上线。"),
        ],
    )
    finish_recording(session, meeting.id)
    session.commit()
    return meeting


def test_meeting_lifecycle_and_segment_idempotency(temp_session) -> None:
    meeting = create_meeting(temp_session, title="周会", language="zh")
    begin_recording(temp_session, meeting.id)
    upsert_segments(
        temp_session,
        meeting.id,
        [SegmentInput(0, 1000, "第一句"), SegmentInput(1000, 2000, "第二句")],
    )
    upsert_segments(
        temp_session,
        meeting.id,
        [SegmentInput(0, 1000, "第一句（修正）"), SegmentInput(1000, 2000, "第二句")],
    )
    finish_recording(temp_session, meeting.id)
    temp_session.commit()

    rows = transcript_segments(temp_session, meeting.id)
    assert [row.text for row in rows] == ["第一句（修正）", "第二句"]
    assert [row.ordinal for row in rows] == [0, 1]
    temp_session.expire_all()
    assert temp_session.get(type(meeting), meeting.id).status is MeetingStatus.recorded


def test_progressive_snapshots_update_one_transcript_row(temp_session) -> None:
    meeting = create_meeting(temp_session, title="累积更新", language="zh")
    begin_recording(temp_session, meeting.id)
    rows = []
    for snapshot in (
        SegmentInput(1000, 2000, "這是"),
        SegmentInput(1000, 3000, "這是繁體"),
        SegmentInput(1000, 4000, "這是繁體中文"),
    ):
        rows = upsert_segments(temp_session, meeting.id, [snapshot])
    temp_session.commit()

    assert len(rows) == 1
    assert rows[0].start_ms == 1000
    assert rows[0].end_ms == 4000
    assert rows[0].text == "这是繁体中文"


def test_chinese_normalization_and_hallucination_filter() -> None:
    assert (
        normalize_transcript_text(
            "這是繁體中文，API 2026",
            language="zh",
            simplify=True,
        )
        == "这是繁体中文，API 2026"
    )
    assert (
        normalize_transcript_text(
            "放著人躺著",
            language="zh",
            simplify=True,
        )
        == "放着人躺着"
    )
    assert (
        normalize_transcript_text(
            "讨论结束请使用简体中文普通话会议，",
            language="zh",
            simplify=True,
        )
        == "讨论结束"
    )
    assert (
        normalize_transcript_text(
            "這是繁體中文",
            language="en",
            simplify=True,
        )
        == "這是繁體中文"
    )
    assert is_probable_hallucination(
        "字幕by索兰娅", start_ms=0, end_ms=2000
    )
    assert not is_probable_hallucination(
        "这段字幕by索兰娅是会议中正常提到的内容，需要保留下来。",
        start_ms=0,
        end_ms=10_000,
    )


def test_recovery_marks_recording_and_summary_interrupted(temp_session) -> None:
    meeting = create_meeting(temp_session, title="中断", language="zh")
    begin_recording(temp_session, meeting.id)
    upsert_segments(temp_session, meeting.id, [SegmentInput(0, 1000, "保留这句")])
    meeting.summary_status = MeetingSummaryStatus.generating
    temp_session.commit()

    interrupted, summaries = recover_interrupted_meetings(temp_session)
    temp_session.commit()
    temp_session.expire_all()
    row = temp_session.get(type(meeting), meeting.id)
    assert (interrupted, summaries) == (1, 1)
    assert row.status is MeetingStatus.interrupted
    assert row.summary_status is MeetingSummaryStatus.failed
    assert len(transcript_segments(temp_session, meeting.id)) == 1


@pytest.mark.anyio
async def test_minutes_are_grounded_and_long_transcript_is_chunked(
    temp_session, monkeypatch
) -> None:
    meeting = create_meeting(temp_session, title="长会议", language="zh")
    begin_recording(temp_session, meeting.id)
    lines = [
        SegmentInput(
            index * 1000,
            (index + 1) * 1000,
            f"第 {index} 段讨论产品发布、风险、负责人和后续安排。" * 4,
        )
        for index in range(80)
    ]
    upsert_segments(temp_session, meeting.id, lines)
    finish_recording(temp_session, meeting.id)
    meeting = temp_session.get(type(meeting), meeting.id)
    meeting.duration_seconds = 200
    temp_session.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "meeting_transcript_token_budget", 500)
    llm = DynamicMinutesLLM()
    markdown = await generate_minutes_markdown(
        meeting,
        transcript_segments(temp_session, meeting.id),
        llm=llm,
        settings=settings,
    )

    assert "## 行动项" in markdown
    assert len(llm.calls) >= 3
    assert "只使用输入中出现的事实" in str(llm.calls[-1][0].content)
    assert "3 分 20 秒" in str(llm.calls[-1][1].content)


def test_export_contains_notes_summary_and_timestamps(temp_session) -> None:
    meeting = _seed_meeting(temp_session)
    meeting.notes = "重点：上线风险"
    begin_summary(temp_session, meeting.id)
    meeting.summary_markdown = MINUTES_MARKDOWN
    meeting.summary_status = MeetingSummaryStatus.ready
    temp_session.commit()

    markdown = build_markdown_export(temp_session, meeting.id)
    assert "重点：上线风险" in markdown
    assert "## 行动项" in markdown
    assert "[0:00:01] 张三负责周五上线。" in markdown


@pytest.mark.anyio
async def test_meeting_agent_calls_transcript_tool(temp_session) -> None:
    meeting = _seed_meeting(temp_session)
    llm = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_transcript",
                        "args": {"query": "谁负责上线"},
                        "id": "c1",
                    }
                ],
            ),
            AIMessage(content="张三负责周五上线。[0:00:01]"),
        ]
    )
    agent = MeetingAgent(temp_session, meeting, llm=llm)
    events = [event async for event in agent.astream("谁负责上线？")]
    types = [event.type for event in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert types[-1] == "done"
    assert "[0:00:01]" in events[-1].data["content"]


def test_meeting_rest_crud_and_websocket(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    with TestClient(app) as client:
        app.state.meeting_transcriber = FakeTranscriber()
        created = client.post(
            "/api/meetings", json={"title": "接口测试", "language": "zh"}
        )
        assert created.status_code == 201
        meeting_id = created.json()["id"]

        listed = client.get("/api/meetings")
        assert listed.status_code == 200
        assert listed.json()[0]["title"] == "接口测试"

        with client.websocket_connect(
            f"/api/meetings/{meeting_id}/audio"
        ) as websocket:
            loading = websocket.receive_json()
            assert loading["state"] == "loading_model"
            started = websocket.receive_json()
            assert started["state"] == "recording"
            websocket.send_bytes(b"\x00\x00" * 1600)
            metrics = websocket.receive_json()
            assert metrics["type"] == "metrics"
            assert metrics["compute_lag_seconds"] == 3.2
            partial = websocket.receive_json()
            assert partial["type"] == "partial"
            websocket.send_bytes(b"")
            final_seen = False
            while True:
                event = websocket.receive_json()
                if event["type"] == "transcript":
                    final_seen = True
                if event["type"] == "state" and event["state"] == "recorded":
                    break
            assert final_seen

        detail = client.get(f"/api/meetings/{meeting_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["status"] == "recorded"
        assert len(body["segments"]) == 2
        assert body["segments"][0]["text"] == "我们今天讨论发布计划。"

        patched = client.patch(
            f"/api/meetings/{meeting_id}", json={"notes": "人工笔记"}
        )
        assert patched.status_code == 200
        assert patched.json()["notes"] == "人工笔记"

        exported = client.get(f"/api/meetings/{meeting_id}/export.md")
        assert exported.status_code == 200
        assert "人工笔记" in exported.text

        deleted = client.delete(f"/api/meetings/{meeting_id}")
        assert deleted.status_code == 204


def test_meeting_health_does_not_load_model(app_with_temp_storage) -> None:
    with TestClient(app_with_temp_storage) as client:
        response = client.get("/api/meetings/health")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["available"] is True
        assert body["ready"] is False
        assert body["loading"] is False
        assert body["model"] == "small"


def test_asr_config_filters_fields_from_installed_version(tmp_path) -> None:
    settings = get_settings()
    config = build_asr_config(
        settings,
        cache_dir=str(tmp_path),
        model_dir=None,
    )
    assert config.model_size == settings.meeting_asr_model
    assert config.pcm_input is True
    assert config.backend == settings.meeting_asr_backend
    assert config.asr_coalesce_min_s == settings.meeting_asr_coalesce_seconds
    assert (
        config.buffer_trimming_sec
        == settings.meeting_asr_buffer_trimming_seconds
    )

    kwargs = faster_whisper_transcribe_kwargs(
        settings,
        language="zh",
        init_prompt="上一句",
    )
    assert kwargs["beam_size"] == 2
    assert kwargs["condition_on_previous_text"] is True
    assert kwargs["no_speech_threshold"] == 0.6
    assert kwargs["initial_prompt"] == "上一句"


def test_meeting_conflict_and_empty_summary(app_with_temp_storage) -> None:
    app = app_with_temp_storage
    with TestClient(app) as client:
        app.state.meeting_transcriber = FakeTranscriber()
        first = client.post("/api/meetings", json={"title": "第一场"}).json()
        second = client.post("/api/meetings", json={"title": "第二场"}).json()

        empty = client.post(f"/api/meetings/{first['id']}/minutes")
        assert empty.status_code == 422

        with client.websocket_connect(
            f"/api/meetings/{first['id']}/audio"
        ) as websocket:
            assert websocket.receive_json()["state"] == "loading_model"
            assert websocket.receive_json()["state"] == "recording"
            with client.websocket_connect(
                f"/api/meetings/{second['id']}/audio"
            ) as other:
                error = other.receive_json()
                assert error["code"] == "meeting_conflict"
            websocket.send_bytes(b"")
            while True:
                event = websocket.receive_json()
                if event["type"] == "state" and event["state"] in {
                    "recorded",
                    "failed",
                }:
                    break


def test_minutes_endpoint_claims_job_once(
    app_with_temp_storage, monkeypatch
) -> None:
    app = app_with_temp_storage

    async def noop(_meeting_id: str) -> None:
        return None

    monkeypatch.setattr("app.meeting.routes.run_minutes_job", noop)
    with TestClient(app) as client:
        session = app.state.test_session_factory()
        try:
            meeting = create_meeting(session, title="纪要接口", language="zh")
            begin_recording(session, meeting.id)
            upsert_segments(
                session,
                meeting.id,
                [SegmentInput(0, 1000, "这是一条可总结的转写。")],
            )
            finish_recording(session, meeting.id)
            session.commit()
            meeting_id = meeting.id
        finally:
            session.close()

        first = client.post(f"/api/meetings/{meeting_id}/minutes")
        assert first.status_code == 202
        assert first.json()["summary_status"] == "generating"

        second = client.post(f"/api/meetings/{meeting_id}/minutes")
        assert second.status_code == 409


def test_meeting_chat_stream_is_transcript_grounded(
    app_with_temp_storage, monkeypatch
) -> None:
    app = app_with_temp_storage
    scripted = ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_transcript",
                        "args": {"query": "谁负责上线"},
                        "id": "c1",
                    }
                ],
            ),
            AIMessage(content="张三负责周五上线。[0:00:01]"),
        ]
    )
    monkeypatch.setattr(
        "app.meeting.agent.build_resilient_chat_model",
        lambda settings: scripted,
    )

    with TestClient(app) as client:
        session = app.state.test_session_factory()
        try:
            meeting = _seed_meeting(session)
            meeting_id = meeting.id
        finally:
            session.close()

        response = client.post(
            f"/api/meetings/{meeting_id}/chat/stream",
            json={"message": "谁负责上线？", "history": []},
        )
        assert response.status_code == 200
        assert "event: tool_call" in response.text
        assert "event: done" in response.text
        assert "[0:00:01]" in response.text
