"""Environment configuration for the Feishu MCP subprocess."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    # The auth CLI runs from the repository root. Loading .env here means users
    # do not have to export app credentials and scopes in a separate shell.
    load_dotenv(Path.cwd() / ".env", override=False)
except ImportError:  # pragma: no cover - package dependency guard
    pass

DEFAULT_DOMAIN = "https://open.feishu.cn"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def app_id() -> str:
    return os.getenv("FEISHU_APP_ID", "").strip()


def app_secret() -> str:
    return os.getenv("FEISHU_APP_SECRET", "").strip()


def user_mailbox_id() -> str:
    return os.getenv("FEISHU_MAIL_USER_MAILBOX_ID", "").strip()


def calendar_id() -> str:
    return os.getenv("FEISHU_CALENDAR_ID", "").strip()


def user_token_path() -> Path:
    value = os.getenv("FEISHU_USER_TOKEN_PATH", "").strip()
    if not value:
        raise RuntimeError("FEISHU_USER_TOKEN_PATH is not configured")
    return Path(value).expanduser()


def domain() -> str:
    return (os.getenv("FEISHU_OPEN_BASE_URL", DEFAULT_DOMAIN).strip() or DEFAULT_DOMAIN).rstrip("/")


def oauth_scopes() -> list[str]:
    return [
        item
        for item in os.getenv("FEISHU_OAUTH_SCOPES", "").replace(",", " ").split()
        if item
    ]
