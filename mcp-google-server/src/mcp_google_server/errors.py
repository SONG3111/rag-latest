"""Structured errors for the Google tool surface.

The agent consumes a JSON envelope rather than transport-level exceptions so a
missing token or a Google API rejection stays actionable. Error text is
sanitized before it leaves the process: OAuth artifacts and authorization
headers must never be copied into chat history or logs.
"""

from __future__ import annotations

import re
from typing import Any

_TOKENISH = re.compile(
    r"(?i)("
    r"ya29\.[A-Za-z0-9._-]+|"
    r"1//[A-Za-z0-9._-]{8,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"\"(?:access_token|refresh_token|client_secret)\"\s*:\s*\"[^\"]*\""
    r")"
)


class GoogleToolError(Exception):
    code = "google_tool_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class AuthNotReady(GoogleToolError):
    code = "google_auth_not_ready"


class InvalidArgument(GoogleToolError):
    code = "invalid_argument"


class ResourceNotFound(GoogleToolError):
    code = "resource_not_found"


class GoogleApiFailure(GoogleToolError):
    code = "google_api_error"


def sanitize_error(value: Any) -> str:
    """Remove common OAuth token shapes from an upstream error."""
    return _TOKENISH.sub("[redacted]", str(value))


def error_payload(exc: GoogleToolError) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": exc.code,
            "message": sanitize_error(exc.message),
            "detail": sanitize_error(exc.detail) if exc.detail else None,
        },
    }
