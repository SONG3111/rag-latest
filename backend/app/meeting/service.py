"""Meeting persistence and lifecycle helpers.

The recording lifecycle is intentionally separate from summary generation:
``status`` tracks audio capture, while ``summary_status`` tracks the LLM job.
That makes an interrupted recording still useful, and lets a failed summary be
retried without pretending the meeting itself failed.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Meeting,
    MeetingSegment,
    MeetingStatus,
    MeetingSummaryStatus,
)
from ..retrieval.bm25 import B, K1, tokenize
from ..services.token_budget import estimate_tokens
from .text import is_probable_hallucination, normalize_transcript_text


class MeetingError(RuntimeError):
    """A meeting operation failed with an HTTP-mappable status."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SegmentInput:
    start_ms: int
    end_ms: int
    text: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(ms: int) -> str:
    """Format a meeting offset as ``H:MM:SS``."""
    total = max(0, int(ms // 1000))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def create_meeting(session: Session, *, title: str, language: str = "zh") -> Meeting:
    row = Meeting(
        title=title.strip()[:200],
        language=(language or "zh").strip()[:10],
        status=MeetingStatus.draft,
        summary_status=MeetingSummaryStatus.none,
    )
    session.add(row)
    session.flush()
    return row


def list_meetings(session: Session, limit: int = 100) -> list[Meeting]:
    return list(
        session.scalars(
            select(Meeting)
            .order_by(Meeting.created_at.desc())
            .limit(max(1, min(limit, 500)))
        )
    )


def get_meeting(session: Session, meeting_id: str) -> Meeting:
    row = session.get(Meeting, meeting_id)
    if row is None:
        raise MeetingError("meeting not found", status_code=404)
    return row


def update_meeting(
    session: Session,
    meeting_id: str,
    *,
    title: str | None = None,
    notes: str | None = None,
    language: str | None = None,
) -> Meeting:
    row = get_meeting(session, meeting_id)
    if title is not None:
        cleaned = title.strip()
        if not cleaned:
            raise MeetingError("meeting title must not be empty", status_code=422)
        row.title = cleaned[:200]
    if notes is not None:
        row.notes = notes
    if language is not None:
        row.language = language.strip()[:10] or "zh"
    row.updated_at = _now()
    session.flush()
    return row


def delete_meeting(session: Session, meeting_id: str) -> None:
    row = get_meeting(session, meeting_id)
    if row.status is MeetingStatus.recording:
        raise MeetingError("cannot delete a meeting while it is recording")
    session.delete(row)
    session.flush()


def begin_recording(session: Session, meeting_id: str) -> Meeting:
    """Claim the single active recording slot.

    A meeting has one recording session in v1. Interrupted/recorded meetings may
    still be summarized, but resuming into the same timeline is intentionally not
    supported because it creates ambiguous timestamps and segment ordering.
    """
    active = session.scalar(
        select(Meeting.id).where(Meeting.status == MeetingStatus.recording)
    )
    if active and active != meeting_id:
        raise MeetingError("another meeting is already recording")

    now = _now()
    result = session.execute(
        update(Meeting)
        .where(
            Meeting.id == meeting_id,
            Meeting.status == MeetingStatus.draft,
        )
        .values(
            status=MeetingStatus.recording,
            started_at=now,
            ended_at=None,
            duration_seconds=0,
            error=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        session.expire_all()
        row = get_meeting(session, meeting_id)
        raise MeetingError(f"meeting is {row.status.value}; it cannot start recording")
    session.expire_all()
    return get_meeting(session, meeting_id)


def finish_recording(
    session: Session,
    meeting_id: str,
    *,
    interrupted: bool = False,
    error: str | None = None,
) -> Meeting:
    now = _now()
    statement = (
        update(Meeting)
        .where(
            Meeting.id == meeting_id,
            Meeting.status == MeetingStatus.recording,
        )
        .values(
            status=(
                MeetingStatus.interrupted if interrupted else MeetingStatus.recorded
            ),
            ended_at=now,
            error=error,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    session.execute(statement)
    session.expire_all()
    row = get_meeting(session, meeting_id)
    if row.started_at is not None and row.ended_at is not None:
        row.duration_seconds = max(
            0, int((row.ended_at - row.started_at).total_seconds())
        )
    session.flush()
    return row


def fail_recording(session: Session, meeting_id: str, error: str) -> Meeting:
    row = get_meeting(session, meeting_id)
    row.status = MeetingStatus.failed
    row.error = error[:2000]
    row.ended_at = _now()
    if row.started_at is not None:
        row.duration_seconds = max(
            0, int((row.ended_at - row.started_at).total_seconds())
        )
    row.updated_at = _now()
    session.flush()
    return row


def upsert_segments(
    session: Session,
    meeting_id: str,
    incoming: list[SegmentInput],
) -> list[MeetingSegment]:
    """Upsert confirmed lines using the utterance start as stable identity.

    WhisperLiveKit's full-mode snapshots can grow the end timestamp and text of
    the same utterance. Treating ``(start, end)`` as identity creates one
    database row per revision, so the key is intentionally ``start_ms``.
    """
    meeting = get_meeting(session, meeting_id)
    settings = get_settings()
    existing = {
        row.start_ms: row
        for row in session.scalars(
            select(MeetingSegment).where(MeetingSegment.meeting_id == meeting_id)
        )
    }
    for item in incoming:
        text = normalize_transcript_text(
            item.text,
            language=meeting.language,
            simplify=settings.meeting_simplify_chinese,
        )
        if not text or item.end_ms < item.start_ms:
            continue
        if is_probable_hallucination(
            text, start_ms=item.start_ms, end_ms=item.end_ms
        ):
            continue
        counts = dict(Counter(tokenize(text)))
        length = estimate_tokens(text)
        row = existing.get(item.start_ms)
        if row is None:
            row = MeetingSegment(
                meeting_id=meeting_id,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                text=text,
                token_counts=counts,
                token_length=length,
            )
            session.add(row)
            existing[item.start_ms] = row
        else:
            # Ignore stale snapshots that arrive after a longer revision.
            if item.end_ms < row.end_ms:
                continue
            if item.end_ms == row.end_ms and length < row.token_length:
                continue
            row.text = text
            row.end_ms = item.end_ms
            row.token_counts = counts
            row.token_length = length
    session.flush()

    rows = list(
        session.scalars(
            select(MeetingSegment)
            .where(MeetingSegment.meeting_id == meeting_id)
            .order_by(MeetingSegment.start_ms, MeetingSegment.end_ms)
        )
    )
    for ordinal, row in enumerate(rows):
        row.ordinal = ordinal
    meeting.updated_at = _now()
    session.flush()
    return rows


def transcript_segments(session: Session, meeting_id: str) -> list[MeetingSegment]:
    return list(
        session.scalars(
            select(MeetingSegment)
            .where(MeetingSegment.meeting_id == meeting_id)
            .order_by(MeetingSegment.ordinal)
        )
    )


def transcript_text(session: Session, meeting_id: str) -> str:
    return "\n".join(
        f"[{format_timestamp(row.start_ms)}] {row.text}"
        for row in transcript_segments(session, meeting_id)
    )


def begin_summary(session: Session, meeting_id: str) -> Meeting:
    row = get_meeting(session, meeting_id)
    if row.status is MeetingStatus.recording:
        raise MeetingError("stop the recording before generating minutes")
    if not row.notes.strip() and not transcript_segments(session, meeting_id):
        raise MeetingError(
            "meeting has no transcript or notes to summarize", status_code=422
        )
    now = _now()
    result = session.execute(
        update(Meeting)
        .where(
            Meeting.id == meeting_id,
            Meeting.summary_status.in_(
                [MeetingSummaryStatus.none, MeetingSummaryStatus.failed]
            ),
        )
        .values(
            summary_status=MeetingSummaryStatus.generating,
            error=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        session.expire_all()
        row = get_meeting(session, meeting_id)
        raise MeetingError(f"summary is {row.summary_status.value}; it cannot start")
    session.expire_all()
    return get_meeting(session, meeting_id)


def complete_summary(session: Session, meeting_id: str, markdown: str) -> Meeting:
    row = get_meeting(session, meeting_id)
    row.summary_markdown = markdown.strip()
    row.summary_status = MeetingSummaryStatus.ready
    row.error = None
    row.updated_at = _now()
    session.flush()
    return row


def fail_summary(session: Session, meeting_id: str, error: str) -> Meeting:
    row = get_meeting(session, meeting_id)
    row.summary_status = MeetingSummaryStatus.failed
    row.error = error[:2000]
    row.updated_at = _now()
    session.flush()
    return row


def recover_interrupted_meetings(session: Session) -> tuple[int, int]:
    """Fail closed after a restart; keep every confirmed transcript line."""
    now = _now()
    recording = session.execute(
        update(Meeting)
        .where(Meeting.status == MeetingStatus.recording)
        .values(
            status=MeetingStatus.interrupted,
            error="录音被中断；已保留停止前的转写，可继续生成纪要。",
            ended_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    summarizing = session.execute(
        update(Meeting)
        .where(Meeting.summary_status == MeetingSummaryStatus.generating)
        .values(
            summary_status=MeetingSummaryStatus.failed,
            error="纪要生成被中断，请重试；转写内容仍然保留。",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    session.flush()
    return int(recording.rowcount or 0), int(summarizing.rowcount or 0)


def search_segments(
    session: Session,
    meeting_id: str,
    query: str,
    *,
    top_k: int = 5,
) -> list[dict]:
    """Small in-memory BM25 over one meeting.

    The existing knowledge-base BM25 class is bound to document chunks. Meeting
    lines use the same tokenizer and scoring constants, but a separate table and
    much smaller corpus, so scoring them locally keeps retrieval independent of
    the workspace/Qdrant lifecycle.
    """
    rows = transcript_segments(session, meeting_id)
    query_terms = set(tokenize(query))
    if not rows or not query_terms:
        return []

    counts = []
    lengths: list[int] = []
    postings: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        row_counts = row.token_counts or {}
        counts.append(row_counts)
        lengths.append(max(1, row.token_length or sum(row_counts.values())))
        for term in row_counts:
            postings[term].append(index)

    total = len(rows)
    avg_length = sum(lengths) / total if total else 0.0
    scores: dict[int, float] = defaultdict(float)
    for term in query_terms:
        document_frequency = len(postings.get(term, ()))
        if document_frequency == 0:
            continue
        idf = math.log(
            1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        for index in postings[term]:
            frequency = counts[index].get(term, 0)
            if not frequency:
                continue
            denominator = frequency + K1 * (
                1 - B + B * (lengths[index] / (avg_length or 1.0))
            )
            scores[index] += idf * (frequency * (K1 + 1)) / denominator

    ranked = sorted(scores.items(), key=lambda item: -item[1])[: max(1, top_k)]
    return [
        {
            "start_ms": rows[index].start_ms,
            "end_ms": rows[index].end_ms,
            "timestamp": format_timestamp(rows[index].start_ms),
            "text": rows[index].text,
            "score": round(score, 4),
        }
        for index, score in ranked
    ]


def read_segment_range(
    session: Session,
    meeting_id: str,
    *,
    start_ms: int = 0,
    end_ms: int | None = None,
    max_chars: int = 6000,
) -> list[dict]:
    bounded_max = max(200, min(max_chars, 12000))
    rows = [
        row
        for row in transcript_segments(session, meeting_id)
        if row.end_ms >= start_ms and (end_ms is None or row.start_ms <= end_ms)
    ]
    result: list[dict] = []
    used = 0
    for row in rows:
        text = row.text
        remaining = bounded_max - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
        result.append(
            {
                "start_ms": row.start_ms,
                "end_ms": row.end_ms,
                "timestamp": format_timestamp(row.start_ms),
                "text": text,
            }
        )
        used += len(text)
    return result


def meeting_has_audio(session: Session, meeting_id: str) -> bool:
    return bool(
        session.scalar(
            select(func.count(MeetingSegment.id)).where(
                MeetingSegment.meeting_id == meeting_id
            )
        )
    )


def build_markdown_export(session: Session, meeting_id: str) -> str:
    row = get_meeting(session, meeting_id)
    segments = transcript_segments(session, meeting_id)
    lines = [
        f"# {row.title or '会议纪要'}",
        "",
        f"- 时间：{row.started_at.isoformat() if row.started_at else '未记录'}",
        f"- 时长：{row.duration_seconds // 60} 分 {row.duration_seconds % 60} 秒",
        f"- 语言：{row.language}",
        "",
    ]
    if row.notes.strip():
        lines.extend(["## 会议笔记", "", row.notes.strip(), ""])
    lines.extend(
        [
            "## 会议纪要",
            "",
            row.summary_markdown.strip() or "纪要尚未生成。",
            "",
            "## 完整转写",
            "",
        ]
    )
    if segments:
        lines.extend(
            f"- [{format_timestamp(item.start_ms)}] {item.text}" for item in segments
        )
    else:
        lines.append("没有可用的转写内容。")
    lines.append("")
    return "\n".join(lines)
