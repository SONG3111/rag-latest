"""One-time local OAuth setup for Gmail and Google Calendar."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import SCOPES, client_secrets_path, token_path
from .errors import AuthNotReady, sanitize_error

logger = logging.getLogger(__name__)


def load_credentials(path: Path | None = None) -> Credentials:
    """Load and, when needed, refresh the stored user credential."""
    target = path or token_path()
    if not target.exists():
        raise AuthNotReady(
            "Google OAuth is not ready. Run `python -m mcp_google_server.auth` first.",
            detail=f"missing token: {target}",
        )
    try:
        credentials = Credentials.from_authorized_user_file(str(target), SCOPES)
        if credentials.valid:
            return credentials
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            _save(credentials, target)
            return credentials
    except Exception as exc:
        raise AuthNotReady(
            "Stored Google credential could not be loaded or refreshed.",
            detail=sanitize_error(exc),
        ) from exc
    raise AuthNotReady(
        "Stored Google credential is invalid. Re-run the local OAuth command.",
        detail=str(target),
    )


def _save(credentials: Credentials, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        # Windows ACLs do not map cleanly onto POSIX modes; the data directory
        # is still local and git-ignored, so this is best effort.
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    secrets_file = client_secrets_path()
    target = token_path()
    if not secrets_file.exists():
        raise SystemExit(f"client secrets file not found: {secrets_file}")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), SCOPES)
    credentials = flow.run_local_server(
        port=int(os.getenv("GOOGLE_OAUTH_PORT", "8765")),
        authorization_prompt_message="请在浏览器中完成 Google 授权…",
        success_message="授权完成，可以关闭此页面。",
        open_browser=True,
    )
    _save(credentials, target)
    print(f"Google credential saved to {target}")


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
