"""Google API service construction.

Implementation modules accept an injected service object. That keeps every
operation testable with a fake client and prevents OAuth concerns from leaking
into the mailbox/calendar semantics.
"""

from __future__ import annotations

from functools import lru_cache

from googleapiclient.discovery import build

from .auth import load_credentials
from .errors import AuthNotReady


@lru_cache(maxsize=4)
def _cached_service(name: str, version: str, token_file: str):
    credentials = load_credentials()
    return build(name, version, credentials=credentials, cache_discovery=False)


def gmail_service():
    from .config import token_path

    try:
        path = token_path()
    except RuntimeError as exc:
        raise AuthNotReady("GOOGLE_TOKEN_PATH is not configured") from exc
    return _cached_service("gmail", "v1", str(path.resolve()))


def calendar_service():
    from .config import token_path

    try:
        path = token_path()
    except RuntimeError as exc:
        raise AuthNotReady("GOOGLE_TOKEN_PATH is not configured") from exc
    return _cached_service("calendar", "v3", str(path.resolve()))
