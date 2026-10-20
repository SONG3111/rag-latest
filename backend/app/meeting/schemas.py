"""HTTP models for the meeting assistant."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MeetingCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    language: Literal["zh", "en", "auto"] = "zh"


class MeetingUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=200_000)
    language: Literal["zh", "en", "auto"] | None = None


class MeetingSegmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    meeting_id: str
    ordinal: int
    start_ms: int
    end_ms: int
    text: str


class MeetingListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    status: str
    summary_status: str
    language: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class MeetingDetail(MeetingListItem):
    notes: str
    summary_markdown: str
    segments: list[MeetingSegmentRead] = Field(default_factory=list)


class MeetingMinutesRead(BaseModel):
    meeting_id: str
    summary_status: str


class MeetingChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class MeetingChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    history: list[MeetingChatMessage] = Field(default_factory=list, max_length=12)


class MeetingHealthRead(BaseModel):
    enabled: bool
    available: bool
    ready: bool
    loading: bool
    backend: str
    model: str
    language: str
    device: str
    compute_type: str
    error: str | None = None
