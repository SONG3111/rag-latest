"""HTTP and SSE models for the news radar."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NewsCategoryRead(BaseModel):
    id: str
    label: str
    description: str


class NewsCategoriesRead(BaseModel):
    categories: list[NewsCategoryRead]
    defaults: list[str]
    custom_topic_limit: int


class NewsPreferenceRead(BaseModel):
    categories: list[str]
    custom_topics: list[str]


class NewsPreferenceUpdate(BaseModel):
    categories: list[str] = Field(default_factory=list, max_length=10)
    custom_topics: list[str] = Field(default_factory=list, max_length=3)


class NewsSourceRef(BaseModel):
    article_id: str
    source_name: str
    source_domain: str
    title: str
    original_title: str
    url: str
    published_at: datetime
    time_basis: Literal["published", "observed"] = "published"
    language: str


class NewsEventRead(BaseModel):
    id: str
    category: str
    category_label: str
    title: str
    original_title: str
    summary: str
    why_it_matters: str
    importance: float
    confidence: float
    source_count: int
    published_at: datetime
    time_basis: Literal["published", "observed"] = "published"
    sources: list[NewsSourceRef] = Field(default_factory=list)


class NewsDigestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    edition_date: str
    window_start: datetime
    window_end: datetime
    categories: list[str]
    custom_topics: list[str]
    trigger: Literal["scheduled", "manual"]
    status: Literal["pending", "running", "ready", "failed"]
    degraded: bool
    warnings: list[str]
    source_stats: dict
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    events: list[NewsEventRead] = Field(default_factory=list)


class NewsChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class NewsMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    content: str
    external_event_id: str | None = None
    meta: dict
    created_at: datetime


class NewsHealthRead(BaseModel):
    enabled: bool
    timezone: str
    generation_mode: str
    cache_minutes: int
    providers: dict = Field(default_factory=dict)
    last_digest_status: str | None = None
    last_digest_at: datetime | None = None
    last_error: str | None = None
