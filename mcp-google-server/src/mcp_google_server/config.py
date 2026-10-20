"""Environment and OAuth scope configuration."""

from __future__ import annotations

import os
from pathlib import Path

GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"

SCOPES = [
    GMAIL_READ_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_SEND_SCOPE,
    CALENDAR_READ_SCOPE,
    CALENDAR_EVENTS_SCOPE,
]

DEFAULT_TIMEZONE = "Asia/Shanghai"


def token_path() -> Path:
    value = os.getenv("GOOGLE_TOKEN_PATH", "").strip()
    if not value:
        raise RuntimeError("GOOGLE_TOKEN_PATH is not configured")
    return Path(value).expanduser()


def client_secrets_path() -> Path:
    value = os.getenv("GOOGLE_CLIENT_SECRETS_PATH", "").strip()
    if not value:
        raise RuntimeError("GOOGLE_CLIENT_SECRETS_PATH is not configured")
    return Path(value).expanduser()
