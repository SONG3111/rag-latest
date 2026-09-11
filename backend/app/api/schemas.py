"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    file_count: int = 0


class FileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rel_path: str
    kind: str
    size_bytes: int
    status: str
    chunk_count: int
    error: str | None
    indexed_at: datetime | None
    created_at: datetime


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class CitationRead(BaseModel):
    file: str | None = None
    location: str | None = None
    snippet: str | None = None
    score: float | None = None
    score_source: str | None = None
    parent_location: str | None = None


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    content: str
    citations: list | None = None
    tool_calls: list | None = None
    created_at: datetime


class OperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tool_name: str
    rel_path: str
    summary: str
    status: str
    diff: list
    result: dict | None = None
    error: str | None = None
    backup_path: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class ToolRead(BaseModel):
    name: str
    description: str
    read_only: bool
    destructive: bool
    requires_approval: bool
    schema_: dict = Field(default_factory=dict, alias="schema")

    model_config = ConfigDict(populate_by_name=True)


class IndexingResponse(BaseModel):
    file_id: str
    rel_path: str
    chunk_count: int
    vector_count: int
    status: str
    error: str | None = None
