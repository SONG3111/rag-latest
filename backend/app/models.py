"""ORM models for workspaces, indexed chunks, chat history, and write operations."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
# All model timestamps use the compatibility type. The local alias keeps the
# column declarations readable while making clear this is not SQLAlchemy's
# plain DateTime (which cannot read the legacy epoch-millisecond rows).
from .db_types import EpochMillisDateTime as DateTime


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IndexStatus(str, enum.Enum):
    pending = "pending"
    indexing = "indexing"
    indexed = "indexed"
    failed = "failed"


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class OperationStatus(str, enum.Enum):
    proposed = "proposed"
    applied = "applied"
    rejected = "rejected"
    failed = "failed"


class AssistantActionStatus(str, enum.Enum):
    proposed = "proposed"
    approved = "approved"
    executing = "executing"
    applied = "applied"
    rejected = "rejected"
    failed = "failed"
    expired = "expired"


class MeetingStatus(str, enum.Enum):
    draft = "draft"
    recording = "recording"
    recorded = "recorded"
    interrupted = "interrupted"
    failed = "failed"


class MeetingSummaryStatus(str, enum.Enum):
    none = "none"
    generating = "generating"
    ready = "ready"
    failed = "failed"


class NewsDigestStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    ready = "ready"
    failed = "failed"


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    files: Mapped[list["DocumentFile"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    operations: Mapped[list["Operation"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class DocumentFile(Base):
    __tablename__ = "document_files"
    __table_args__ = (UniqueConstraint("workspace_id", "rel_path", name="uq_file_per_ws"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    rel_path: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[IndexStatus] = mapped_column(
        Enum(IndexStatus), default=IndexStatus.pending, nullable=False
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="files")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class Chunk(Base):
    """A retrievable text unit, optionally in a parent/child relationship.

    Chunks form two levels. *Children* are the precise units that get embedded and
    keyword-indexed — one spreadsheet row, one paragraph — so a hit points at the
    exact place that answers the question. *Parents* are the surrounding block (a
    run of rows, a heading section) and are never embedded; they exist to give the
    model enough context to interpret a child that was retrieved on its own.

    Dense vectors live in Qdrant; this table is the source of truth for text and
    carries the token counts used by the in-process BM25 scorer, which keeps the
    keyword leg of hybrid retrieval dependency-free.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunk_workspace_file", "workspace_id", "file_id"),
        Index("ix_chunk_level_workspace", "workspace_id", "level"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    file_id: Mapped[str] = mapped_column(
        ForeignKey("document_files.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Null for parents and for flat (pre-hierarchy) chunks; set on children.
    parent_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    # "child" | "parent"; defaults to child so old rows behave as retrievable units.
    level: Mapped[str] = mapped_column(String(10), default="child", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    token_counts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    token_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    file: Mapped[DocumentFile] = relationship(back_populates="chunks")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    citations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # "up" | "down" | null. The value of thumbs is the data loop: every down
    # vote is a candidate for the evaluation set, so it must live on the row
    # the user actually saw.
    feedback: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, index=True, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="messages")


class ConversationSummary(Base):
    """Rolling summary of a conversation's older turns, one row per workspace.

    Long conversations used to be truncated to the most recent N messages, which
    silently dropped the opening context ("那个文件最初是干嘛的"). The summary is
    regenerated over all but the most recent turns whenever compaction runs, so it
    carries the early context forward at a bounded token cost. It deliberately
    never carries file-freshness facts — staleness is decided deterministically by
    timestamp comparison in the routes layer, not by what a summary happens to keep.
    """

    __tablename__ = "conversation_summaries"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # How many of the oldest messages the current summary covers; the compaction
    # trigger compares this against the message count to decide if there is new
    # material to fold in.
    covered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )


class RunTrace(Base):
    """One node of one chat turn's execution trace.

    A run is one user turn; its nodes follow the RAGent trace shape (rewrite /
    dense / bm25 / fuse / rerank / select for the retrieval pipeline, agent /
    tools / nudge / direct for the agent loop) with per-node duration, bounded
    input/output summaries, and the error when the node failed. Written in a
    background task after the turn, so tracing can never break the turn itself.
    """

    __tablename__ = "run_traces"
    __table_args__ = (Index("ix_run_trace_run", "workspace_id", "run_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    node: Mapped[str] = mapped_column(String(50), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )


class Operation(Base):
    """A proposed document mutation awaiting human approval.

    The MCP server stays stateless: the agent computes a diff by reading first, and
    this row records the exact tool call so the host can replay it only after the
    user approves.
    """

    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    rel_path: Mapped[str] = mapped_column(String(500), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    diff: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus), default=OperationStatus.proposed, nullable=False
    )
    backup_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, index=True, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="operations")


class AssistantConversation(Base):
    """One conversation with the mail/calendar assistant.

    The web UI owns a single ``web-default`` row; every Feishu chat gets its own
    row keyed by the chat id. Keeping the channel identity explicit prevents a
    forwarded Feishu event from being replayed into another user's thread.
    """

    __tablename__ = "assistant_conversations"
    __table_args__ = (
        UniqueConstraint(
            "channel", "external_chat_id", name="uq_assistant_conversation_channel"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    external_chat_id: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    messages: Mapped[list["AssistantMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    actions: Mapped[list["AssistantAction"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class AssistantMessage(Base):
    """A persisted turn in an assistant conversation.

    ``external_event_id`` is the Feishu event id. The unique constraint is the
    replay guard: Feishu can redeliver an event after a reconnect, and a second
    agent turn would otherwise duplicate both the answer and any proposal.
    """

    __tablename__ = "assistant_messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "external_event_id", name="uq_assistant_external_event"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, index=True, nullable=False
    )

    conversation: Mapped[AssistantConversation] = relationship(back_populates="messages")


class AssistantAction(Base):
    """A Feishu Mail/Calendar mutation waiting for human approval.

    The row stores the exact MCP tool call. A successful approval claims the row
    with an atomic status transition before invoking the tool, so two browser
    clicks or a replayed Feishu card cannot send the same email twice.
    """

    __tablename__ = "assistant_actions"
    __table_args__ = (
        Index("ix_assistant_action_status", "status", "created_at"),
        UniqueConstraint("idempotency_key", name="uq_assistant_action_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    source_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    preview: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[AssistantActionStatus] = mapped_column(
        Enum(AssistantActionStatus),
        default=AssistantActionStatus.proposed,
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), default=_uuid, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approval_channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    conversation: Mapped[AssistantConversation] = relationship(back_populates="actions")


class McpToolCache(Base):
    """Snapshot of tools advertised by the MCP server, with their annotations.

    The agent reads approval requirements from here rather than hardcoding a list,
    so enabling a new tool upstream does not require touching approval logic.
    """

    __tablename__ = "mcp_tools"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    destructive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schema_: Mapped[dict] = mapped_column("schema_json", JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )


class Meeting(Base):
    """One locally transcribed meeting and its generated minutes.

    Audio is deliberately absent from this model. The browser streams PCM to the
    live ASR adapter, and only confirmed transcript lines, notes, and the final
    Markdown minutes are persisted. That keeps privacy and disk usage bounded
    while still allowing a meeting to survive a page reload or process restart.
    """

    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[MeetingStatus] = mapped_column(
        Enum(MeetingStatus), default=MeetingStatus.draft, nullable=False
    )
    summary_status: Mapped[MeetingSummaryStatus] = mapped_column(
        Enum(MeetingSummaryStatus),
        default=MeetingSummaryStatus.none,
        nullable=False,
    )
    language: Mapped[str] = mapped_column(String(10), default="zh", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, index=True, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    segments: Mapped[list["MeetingSegment"]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        order_by="MeetingSegment.ordinal",
    )


class MeetingSegment(Base):
    """A confirmed transcript line with a stable position in meeting audio time.

    ``(meeting_id, start_ms, end_ms)`` is the idempotency key. WhisperLiveKit
    sends the full committed line list on every update; upserting by this key
    prevents a repeated snapshot from duplicating rows while still allowing a
    line's text to be corrected before the meeting ends.
    """

    __tablename__ = "meeting_segments"
    __table_args__ = (
        UniqueConstraint(
            "meeting_id", "start_ms", "end_ms", name="uq_meeting_segment_span"
        ),
        Index(
            "uq_meeting_segment_start",
            "meeting_id",
            "start_ms",
            unique=True,
        ),
        Index("ix_meeting_segment_order", "meeting_id", "ordinal"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    token_counts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    token_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    meeting: Mapped[Meeting] = relationship(back_populates="segments")


class NewsPreference(Base):
    """The single local user's last news-category selection.

    Keeping this server-side (rather than only in browser storage) is what lets a
    news question generate the same categories the user last chose.
    """

    __tablename__ = "news_preferences"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: "default")
    categories: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    custom_topics: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )


class NewsDigest(Base):
    """One immutable news snapshot revision.

    Each question-triggered collection creates a revision rather than replacing an
    older snapshot, so the history page can distinguish what the user saw and when.
    """

    __tablename__ = "news_digests"
    __table_args__ = (
        Index("ix_news_digest_date_status", "edition_date", "status"),
        UniqueConstraint("generation_key", name="uq_news_digest_generation_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    edition_date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    categories: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    custom_topics: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    status: Mapped[NewsDigestStatus] = mapped_column(
        Enum(NewsDigestStatus), default=NewsDigestStatus.pending, nullable=False
    )
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    source_stats: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    generation_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, index=True, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    events: Mapped[list["NewsEvent"]] = relationship(
        back_populates="digest", cascade="all, delete-orphan"
    )
    articles: Mapped[list["NewsArticle"]] = relationship(
        back_populates="digest", cascade="all, delete-orphan"
    )


class NewsArticle(Base):
    """Normalized feed item retained for evidence and follow-up search."""

    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint(
            "digest_id", "category", "canonical_url", name="uq_news_digest_category_url"
        ),
        Index("ix_news_article_category_time", "category", "published_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    digest_id: Mapped[str] = mapped_column(
        ForeignKey("news_digests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    original_title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    source_domain: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    source_tier: Mapped[str] = mapped_column(String(20), default="media", nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="zh", nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # "published" means the feed supplied a publication timestamp; "observed"
    # means this is a hot-list snapshot and the time is when we fetched it.
    time_basis: Mapped[str] = mapped_column(
        String(20), default="published", nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content_status: Mapped[str] = mapped_column(String(20), default="feed", nullable=False)

    digest: Mapped[NewsDigest] = relationship(back_populates="articles")


class NewsEvent(Base):
    """A cross-source event cluster shown as one card in a digest."""

    __tablename__ = "news_events"
    __table_args__ = (
        Index("ix_news_event_digest_rank", "digest_id", "rank"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    digest_id: Mapped[str] = mapped_column(
        ForeignKey("news_digests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    original_title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    why_it_matters: Mapped[str] = mapped_column(Text, default="", nullable=False)
    importance: Mapped[float] = mapped_column(default=0.0, nullable=False)
    confidence: Mapped[float] = mapped_column(default=1.0, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    article_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    digest: Mapped[NewsDigest] = relationship(back_populates="events")
