"""Feishu Calendar v4 operations used by the MCP tool layer."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lark_oapi.api.calendar.v4 import (
    CalendarEvent,
    CalendarEventAttendee,
    CreateCalendarEventRequest,
    EventLocation,
    ListCalendarEventRequest,
    TimeInfo,
)

from .config import DEFAULT_TIMEZONE
from .errors import FeishuApiFailure, InvalidArgument, sanitize_error
from .runtime import FeishuRuntime, check_response


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise InvalidArgument(f"unknown timezone: {name}") from exc


def _parse_dt(value: str | datetime, zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise InvalidArgument("datetime value must not be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidArgument(
                f"invalid ISO datetime: {text}; use e.g. 2026-10-26T15:00:00+08:00"
            ) from exc
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def _clock(value: str, field: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except Exception as exc:
        raise InvalidArgument(f"{field} must use HH:MM, got {value!r}") from exc


def _event_start(event: Any, zone: ZoneInfo) -> datetime | None:
    info = getattr(event, "start_time", None)
    for value in (
        getattr(info, "date_time", None),
        getattr(info, "timestamp", None),
        getattr(info, "date", None),
    ):
        if value:
            if str(value).isdigit() and len(str(value)) >= 10:
                return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone(zone)
            try:
                return _parse_dt(str(value), zone)
            except InvalidArgument:
                continue
    return None


def _event_end(event: Any, zone: ZoneInfo) -> datetime | None:
    info = getattr(event, "end_time", None)
    for value in (
        getattr(info, "date_time", None),
        getattr(info, "timestamp", None),
        getattr(info, "date", None),
    ):
        if value:
            if str(value).isdigit() and len(str(value)) >= 10:
                return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone(zone)
            try:
                return _parse_dt(str(value), zone)
            except InvalidArgument:
                continue
    return None


def _event_summary(event: Any, zone: ZoneInfo) -> dict[str, Any]:
    start = _event_start(event, zone)
    end = _event_end(event, zone)
    attendees = [
        {
            "id": getattr(item, "attendee_id", None),
            "display_name": getattr(item, "display_name", None),
            "email": getattr(item, "third_party_email", None),
            "rsvp_status": getattr(item, "rsvp_status", None),
        }
        for item in (getattr(event, "attendees", None) or [])
    ]
    location = getattr(event, "location", None)
    return {
        "id": getattr(event, "event_id", None),
        "summary": getattr(event, "summary", "") or "",
        "description": getattr(event, "description", "") or "",
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
        "timezone": str(zone),
        "status": getattr(event, "status", None),
        "app_link": getattr(event, "app_link", None),
        "location": getattr(location, "name", None) or "",
        "attendees": attendees,
    }


def list_calendar_events(
    runtime: FeishuRuntime,
    *,
    time_min: str,
    time_max: str,
    max_results: int = 50,
) -> dict[str, Any]:
    zone = _zone(DEFAULT_TIMEZONE)
    start = _parse_dt(time_min, zone)
    end = _parse_dt(time_max, zone)
    if end <= start:
        raise InvalidArgument("time_max must be after time_min")
    request = (
        ListCalendarEventRequest.builder()
        .calendar_id(runtime.calendar())
        .start_time(start.isoformat())
        .end_time(end.isoformat())
        .page_size(max(1, min(int(max_results), 100)))
        .user_id_type("open_id")
        .build()
    )
    try:
        response = runtime.client().calendar.v4.calendar_event.list(
            request, runtime.option()
        )
        check_response(response, "list Feishu calendar events")
    except Exception as exc:
        if isinstance(exc, FeishuApiFailure):
            raise
        raise FeishuApiFailure(
            "Feishu Calendar list failed.", detail=sanitize_error(exc)
        ) from exc
    data = response.data
    items = list(getattr(data, "items", None) or []) if data else []
    return {
        "time_min": start.isoformat(),
        "time_max": end.isoformat(),
        "timezone": str(zone),
        "events": [_event_summary(item, zone) for item in items],
        "count": len(items),
    }


def find_free_slots(
    runtime: FeishuRuntime,
    *,
    time_min: str,
    time_max: str,
    duration_minutes: int = 30,
    attendees: list[str] | None = None,
    workday_start: str = "09:00",
    workday_end: str = "18:00",
    timezone: str = DEFAULT_TIMEZONE,
    max_slots: int = 10,
) -> dict[str, Any]:
    zone = _zone(timezone)
    start = _parse_dt(time_min, zone)
    end = _parse_dt(time_max, zone)
    if end <= start:
        raise InvalidArgument("time_max must be after time_min")
    if end - start > timedelta(days=31):
        raise InvalidArgument("free-slot search window must not exceed 31 days")
    if duration_minutes < 15 or duration_minutes > 480:
        raise InvalidArgument("duration_minutes must be between 15 and 480")
    begin_clock = _clock(workday_start, "workday_start")
    end_clock = _clock(workday_end, "workday_end")
    if end_clock <= begin_clock:
        raise InvalidArgument("workday_end must be after workday_start")

    events = list_calendar_events(
        runtime,
        time_min=start.isoformat(),
        time_max=end.isoformat(),
        max_results=100,
    )["events"]
    busy = []
    for event in events:
        event_start = _parse_dt(event["start"], zone) if event.get("start") else None
        event_end = _parse_dt(event["end"], zone) if event.get("end") else None
        if event_start and event_end:
            busy.append((max(start, event_start), min(end, event_end)))
    busy.sort()

    duration = timedelta(minutes=duration_minutes)
    slots: list[dict[str, str]] = []
    current_day = start.date()
    last_day = end.date()
    while current_day <= last_day and len(slots) < max(1, min(max_slots, 50)):
        if current_day.weekday() < 5:
            cursor = max(datetime.combine(current_day, begin_clock, zone), start)
            window_end = min(datetime.combine(current_day, end_clock, zone), end)
            for busy_start, busy_end in busy:
                if busy_end <= cursor or busy_start >= window_end:
                    continue
                if busy_start - cursor >= duration:
                    slots.append(
                        {
                            "start": cursor.isoformat(),
                            "end": (cursor + duration).isoformat(),
                        }
                    )
                    if len(slots) >= max_slots:
                        break
                cursor = max(cursor, busy_end)
            if len(slots) < max_slots and window_end - cursor >= duration:
                slots.append(
                    {
                        "start": cursor.isoformat(),
                        "end": (cursor + duration).isoformat(),
                    }
                )
        current_day += timedelta(days=1)
    return {
        "timezone": str(zone),
        "duration_minutes": duration_minutes,
        "workday": {"start": workday_start, "end": workday_end},
        "attendees": list(attendees or []),
        "attendee_calendars_included": False,
        "slots": slots[:max_slots],
        "count": len(slots[:max_slots]),
        "note": "Slots use the configured user's primary calendar; attendee availability requires Feishu user IDs.",
    }


def create_calendar_event(
    runtime: FeishuRuntime,
    *,
    summary: str,
    start: str,
    end: str,
    timezone: str = DEFAULT_TIMEZONE,
    attendees: list[str] | None = None,
    description: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    zone = _zone(timezone)
    start_dt = _parse_dt(start, zone)
    end_dt = _parse_dt(end, zone)
    if end_dt <= start_dt:
        raise InvalidArgument("event end must be after start")
    if not summary.strip():
        raise InvalidArgument("event summary must not be empty")
    guests = [item.strip() for item in (attendees or []) if item.strip()]
    builder = (
        CalendarEvent.builder()
        .summary(summary.strip())
        .need_notification(bool(guests))
        .start_time(
            TimeInfo.builder()
            .date_time(start_dt.isoformat())
            .timezone(str(zone))
            .build()
        )
        .end_time(
            TimeInfo.builder()
            .date_time(end_dt.isoformat())
            .timezone(str(zone))
            .build()
        )
    )
    if description:
        builder.description(description.strip())
    if location:
        builder.location(EventLocation.builder().name(location.strip()).build())
    if guests:
        builder.attendees(
            [
                CalendarEventAttendee.builder()
                .type("third_party")
                .is_external(True)
                .third_party_email(email)
                .build()
                for email in guests
            ]
        )
    request = (
        CreateCalendarEventRequest.builder()
        .calendar_id(runtime.calendar())
        .idempotency_key(uuid.uuid4().hex)
        .user_id_type("open_id")
        .request_body(builder.build())
        .build()
    )
    response = runtime.client().calendar.v4.calendar_event.create(
        request, runtime.option()
    )
    check_response(response, "create Feishu calendar event")
    event = response.data.event if response.data else None
    if event is None:
        raise FeishuApiFailure("Feishu Calendar returned no event")
    return {
        **_event_summary(event, zone),
        "invitations_requested": bool(guests),
    }
