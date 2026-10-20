"""Grounded meeting-minutes generation.

Long transcripts are summarized in ordered chunks before the final template
pass. This mirrors Meetily's ``summary/processor.rs`` (MIT): summarize chunks,
combine the summaries, then fill the requested report template. Keeping the
same structure avoids truncating a two-hour meeting at an arbitrary character
limit while preserving timestamp evidence.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import Settings, get_settings
from ..db import session_scope
from ..llm.providers import build_chat_model, build_resilient_chat_model
from ..llm.resilience import translate_provider_error
from ..models import Meeting, MeetingSegment
from ..services.token_budget import estimate_tokens
from .prompts import (
    build_chunk_summary_prompt,
    build_combine_prompt,
    build_minutes_system_prompt,
    build_minutes_user_prompt,
)
from .service import (
    MeetingError,
    complete_summary,
    fail_summary,
    format_timestamp,
    get_meeting,
    transcript_segments,
)

logger = logging.getLogger(__name__)

REQUIRED_SECTIONS = (
    "## 会议摘要",
    "## 关键结论与决策",
    "## 行动项",
    "## 风险与待确认",
    "## 讨论要点",
)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


async def collect_llm_text(llm: Any, messages: list[Any]) -> str:
    """Collect a streaming response into one string.

    ``ResilientChatModel`` exposes streaming as the common interface. Tests use
    a scripted object with the same method, which is why this helper lives here
    instead of relying on provider-specific ``invoke`` implementations.
    """
    parts: list[str] = []
    async for chunk in llm.astream(messages):
        parts.append(_message_text(getattr(chunk, "content", chunk)))
    return "".join(parts).strip()


def _clean_markdown(raw: str) -> str:
    text = raw.strip()
    for prefix in ("```markdown\n", "```\n"):
        if text.startswith(prefix) and text.endswith("```"):
            return text[len(prefix) : -3].strip()
    return text


def _chunk_lines(
    segments: list[MeetingSegment],
    *,
    chunk_token_budget: int,
) -> list[str]:
    if not segments:
        return []
    lines = [
        f"[{format_timestamp(row.start_ms)}] {row.text}" for row in segments
    ]
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for line in lines:
        line_tokens = max(1, estimate_tokens(line))
        if current and current_tokens + line_tokens > chunk_token_budget:
            chunks.append("\n".join(current))
            # One overlapping line preserves the sentence that straddles the
            # boundary without duplicating a large block of context.
            current = [current[-1], line]
            current_tokens = estimate_tokens(current[0]) + line_tokens
        else:
            current.append(line)
            current_tokens += line_tokens
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _source_text_for_final_pass(
    meeting: Meeting,
    segments: list[MeetingSegment],
    *,
    llm: Any,
    settings: Settings,
) -> str:
    full = "\n".join(
        f"[{format_timestamp(row.start_ms)}] {row.text}" for row in segments
    )
    if not full or estimate_tokens(full) <= settings.meeting_transcript_token_budget:
        return full

    chunk_budget = max(
        1200,
        min(8000, settings.meeting_transcript_token_budget // 2),
    )
    chunks = _chunk_lines(segments, chunk_token_budget=chunk_budget)
    summaries: list[str] = []
    for index, chunk in enumerate(chunks):
        summary = await collect_llm_text(
            llm,
            [
                SystemMessage(
                    content=(
                        "你是会议事实提取器。只提取输入中转写明确出现的信息，"
                        "保留时间戳，不推测，不补充外部知识。"
                    )
                ),
                HumanMessage(
                    content=build_chunk_summary_prompt(
                        chunk=chunk, title=meeting.title
                    )
                ),
            ],
        )
        if not summary:
            raise MeetingError(
                f"转写第 {index + 1}/{len(chunks)} 段没有生成有效摘要，请重试。",
                status_code=502,
            )
        summaries.append(summary)
    if len(summaries) == 1:
        return summaries[0]
    combined = await collect_llm_text(
        llm,
        [
            SystemMessage(
                content=(
                    "你是会议摘要合并器。合并时保留全部关键事实和时间戳，"
                    "不新增输入中没有的信息。"
                )
            ),
            HumanMessage(
                content=build_combine_prompt(
                    chunk_summaries="\n\n---\n\n".join(summaries),
                    title=meeting.title,
                )
            ),
        ],
    )
    if not combined:
        raise MeetingError("分段摘要合并没有返回内容，请重试。", status_code=502)
    return combined


def _missing_sections(markdown: str) -> list[str]:
    return [heading for heading in REQUIRED_SECTIONS if heading not in markdown]


async def generate_minutes_markdown(
    meeting: Meeting,
    segments: list[MeetingSegment],
    *,
    llm: Any,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    source = await _source_text_for_final_pass(
        meeting, segments, llm=llm, settings=settings
    )
    system = build_minutes_system_prompt(
        now=now or datetime.now(timezone.utc),
        timezone=settings.assistant_timezone,
    )
    user = build_minutes_user_prompt(
        title=meeting.title,
        started_at=(
            meeting.started_at.isoformat() if meeting.started_at else "未记录"
        ),
        duration_seconds=meeting.duration_seconds,
        notes=meeting.notes,
        transcript=source,
    )
    markdown = _clean_markdown(
        await collect_llm_text(
            llm,
            [SystemMessage(content=system), HumanMessage(content=user)],
        )
    )
    if not markdown:
        raise MeetingError("模型没有返回纪要内容，请重试。", status_code=502)

    missing = _missing_sections(markdown)
    if missing:
        # One bounded repair pass. It only asks for structure; factual grounding
        # still comes from the original source, never from the draft alone.
        markdown = _clean_markdown(
            await collect_llm_text(
                llm,
                [
                    SystemMessage(content=system),
                    HumanMessage(
                        content=(
                            f"{user}\n\n<draft>\n{markdown}\n</draft>\n\n"
                            "请修正草稿的 Markdown 结构，保留事实与时间戳，"
                            f"必须包含这些标题：{'、'.join(missing)}。"
                        )
                    ),
                ],
            )
        )
    if not markdown:
        raise MeetingError("模型没有返回有效纪要，请重试。", status_code=502)
    return markdown


def build_minutes_llm(settings: Settings) -> Any:
    if settings.meeting_summary_model.strip():
        return build_chat_model(
            settings, model=settings.meeting_summary_model.strip()
        )
    return build_resilient_chat_model(settings)


async def run_minutes_job(meeting_id: str) -> None:
    """Background job: generate and persist minutes without holding a request."""
    settings = get_settings()
    try:
        with session_scope() as session:
            meeting = get_meeting(session, meeting_id)
            segments = transcript_segments(session, meeting_id)
            meeting_snapshot = Meeting(
                id=meeting.id,
                title=meeting.title,
                language=meeting.language,
                notes=meeting.notes,
                started_at=meeting.started_at,
                ended_at=meeting.ended_at,
                duration_seconds=meeting.duration_seconds,
                summary_markdown=meeting.summary_markdown,
            )
            segment_snapshots = [
                MeetingSegment(
                    id=item.id,
                    meeting_id=item.meeting_id,
                    ordinal=item.ordinal,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    text=item.text,
                )
                for item in segments
            ]
        llm = build_minutes_llm(settings)
        async with asyncio.timeout(settings.meeting_summary_timeout):
            markdown = await generate_minutes_markdown(
                meeting_snapshot,
                segment_snapshots,
                llm=llm,
                settings=settings,
            )
        with session_scope() as session:
            complete_summary(session, meeting_id, markdown)
    except Exception as exc:
        logger.exception("meeting minutes generation failed: %s", meeting_id)
        message = (
            str(exc)
            if isinstance(exc, MeetingError)
            else translate_provider_error(exc)
        )
        try:
            with session_scope() as session:
                fail_summary(session, meeting_id, message)
        except Exception:
            logger.exception("could not persist meeting summary failure")
