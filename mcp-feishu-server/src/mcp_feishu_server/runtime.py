"""Lark client and request option construction."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import lark_oapi as lark
from lark_oapi.core.model import RequestOption

from . import config
from .auth import access_token
from .errors import AuthNotReady, FeishuApiFailure, sanitize_error


class FeishuRuntime:
    def __init__(self) -> None:
        self._client = None
        self._primary_calendar_id: str | None = config.calendar_id() or None

    def client(self):
        if self._client is None:
            if not config.app_id() or not config.app_secret():
                raise AuthNotReady("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
            self._client = (
                lark.Client.builder()
                .app_id(config.app_id())
                .app_secret(config.app_secret())
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        return self._client

    def option(self) -> RequestOption:
        return (
            RequestOption.builder()
            .user_access_token(access_token())
            .build()
        )

    def mailbox_id(self) -> str:
        value = config.user_mailbox_id()
        if not value:
            raise AuthNotReady("FEISHU_MAIL_USER_MAILBOX_ID is not configured")
        return value

    def calendar(self) -> str:
        if self._primary_calendar_id:
            return self._primary_calendar_id
        from lark_oapi.api.calendar.v4 import PrimaryCalendarRequest

        response = self.client().calendar.v4.calendar.primary(
            PrimaryCalendarRequest.builder().user_id_type("open_id").build(),
            self.option(),
        )
        check_response(response, "resolve primary calendar")
        calendars = (response.data.calendars or []) if response.data else []
        for item in calendars:
            calendar = getattr(item, "calendar", None)
            if calendar and calendar.calendar_id:
                self._primary_calendar_id = calendar.calendar_id
                return self._primary_calendar_id
        raise FeishuApiFailure("Feishu returned no primary calendar")


def check_response(response: Any, action: str) -> None:
    success = getattr(response, "success", None)
    ok = success() if callable(success) else bool(success)
    if ok:
        return
    code = getattr(response, "code", None)
    msg = getattr(response, "msg", None)
    if code == 99991663 or code == 99991668:
        raise AuthNotReady(
            "Feishu user token was rejected or lacks mail/calendar permission",
            detail=sanitize_error(msg or code),
        )
    raise FeishuApiFailure(
        f"Feishu API failed while trying to {action}.",
        detail=sanitize_error(msg or f"code={code}"),
    )


@lru_cache(maxsize=1)
def runtime() -> FeishuRuntime:
    return FeishuRuntime()
