"""Feishu OAuth Device Flow and local token storage.

Device Flow follows the official `lark-oapi` channel auth client. It avoids a
public OAuth callback URL while still producing the user access token required
by Mail send/search and user Calendar APIs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from lark_oapi.channel.auth.device_flow import DeviceFlowClient

from .config import app_id, app_secret, domain, oauth_scopes, user_token_path
from .errors import AuthNotReady, sanitize_error

logger = logging.getLogger(__name__)


def _write_token(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_token(path: Path | None = None) -> dict[str, Any]:
    target = path or user_token_path()
    if not target.exists():
        raise AuthNotReady(
            "Feishu user authorization is not ready. "
            "Run `python -m mcp_feishu_server.auth`."
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuthNotReady("Stored Feishu user token is unreadable.") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise AuthNotReady("Stored Feishu user token is invalid.")
    return payload


def refresh_token_sync(token: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    refresh = str(token.get("refresh_token") or "")
    if not refresh:
        raise AuthNotReady("Feishu token has no refresh_token; authorize again.")
    response = httpx.post(
        domain() + "/open-apis/authen/v2/oauth/token",
        json={
            "client_id": app_id(),
            "client_secret": app_secret(),
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        timeout=30,
    )
    try:
        data = response.json()
    except Exception as exc:
        raise AuthNotReady(
            f"Feishu token refresh returned HTTP {response.status_code}"
        ) from exc
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if response.is_error or (isinstance(payload.get("code"), int) and payload["code"] != 0):
        raise AuthNotReady(
            f"Feishu token refresh failed: {payload.get('msg') or response.status_code}"
        )
    access = payload.get("access_token")
    if not access:
        raise AuthNotReady("Feishu token refresh returned no access_token.")
    now = time.time()
    updated = {
        **token,
        "access_token": access,
        "refresh_token": payload.get("refresh_token") or refresh,
        "expires_at": now + int(payload.get("expires_in") or 0),
        "refresh_expires_at": now + int(payload.get("refresh_token_expires_in") or 0),
        "scopes": str(payload.get("scope") or "").split(),
        "raw": payload,
    }
    _write_token(path or user_token_path(), updated)
    return updated


def access_token(path: Path | None = None) -> str:
    target = path or user_token_path()
    token = load_token(target)
    expires_at = float(token.get("expires_at") or 0)
    if expires_at and expires_at - time.time() <= 300:
        token = refresh_token_sync(token, target)
    return str(token["access_token"])


async def authorize() -> None:
    if not app_id() or not app_secret():
        raise SystemExit("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
    client = DeviceFlowClient(app_id(), app_secret(), domain=domain())
    try:
        started = await client.start(oauth_scopes())
        print("请在飞书中完成授权：")
        print(started.verification_uri_complete)
        print(f"用户码：{started.user_code}")
        uat = await client.poll(
            started.device_code,
            interval=started.interval,
            timeout_seconds=started.expires_in,
        )
    finally:
        await client.close()
    payload = {
        "access_token": uat.access_token,
        "refresh_token": uat.refresh_token,
        "expires_at": uat.expires_at,
        "refresh_expires_at": uat.refresh_expires_at,
        "scopes": uat.scopes,
        "open_id": uat.open_id,
        "raw": uat.raw,
    }
    _write_token(user_token_path(), payload)
    print(f"授权完成，令牌已保存到 {user_token_path()}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(authorize())
    except Exception as exc:
        raise SystemExit(sanitize_error(exc)) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
