"""Structured errors and credential redaction."""

from __future__ import annotations

import re
from typing import Any

_SECRET = re.compile(
    r"(?i)(Bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"\"(?:access_token|refresh_token|app_secret)\"\s*:\s*\"[^\"]*\")"
)


class FeishuToolError(Exception):
    code = "feishu_tool_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class AuthNotReady(FeishuToolError):
    code = "feishu_auth_not_ready"


class InvalidArgument(FeishuToolError):
    code = "invalid_argument"


class ResourceNotFound(FeishuToolError):
    code = "resource_not_found"


class FeishuApiFailure(FeishuToolError):
    code = "feishu_api_error"


def sanitize_error(value: Any) -> str:
    return _SECRET.sub("[redacted]", str(value))


def error_payload(exc: FeishuToolError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": exc.code,
            "message": sanitize_error(exc.message),
            "detail": sanitize_error(exc.detail) if exc.detail else None,
        },
    }
