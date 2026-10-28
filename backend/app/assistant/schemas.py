"""HTTP models for the assistant API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None


class AssistantConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    channel: str
    external_chat_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class AssistantMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    content: str
    external_event_id: str | None = None
    meta: dict
    created_at: datetime


class AssistantActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    source_message_id: str | None = None
    tool_name: str
    arguments: dict
    preview: dict
    summary: str
    status: str
    approved_by: str | None = None
    approval_channel: str | None = None
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None


class AssistantToolRead(BaseModel):
    name: str
    description: str
    read_only: bool
    destructive: bool
    requires_approval: bool
    schema_: dict = Field(default_factory=dict, alias="schema")

    model_config = ConfigDict(populate_by_name=True)


class AssistantHealthRead(BaseModel):
    enabled: bool
    mail_mcp_started: bool
    mail_auth_ready: bool
    mail_error: str | None = None
    feishu_configured: bool
    feishu_connected: bool
    feishu_error: str | None = None
    pending_actions: int
