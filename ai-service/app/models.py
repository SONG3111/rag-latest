"""ORM models for workspaces, indexed chunks, chat history, and write operations."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
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
