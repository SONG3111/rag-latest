"""Stateless Google Calendar read/create/free-busy operations."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import DEFAULT_TIMEZONE
from .errors import GoogleApiFailure, InvalidArgument, sanitize_error


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
                f"invalid ISO datetime: {text}; use e.g. 2026-10-21T15:00:00+08:00"
            ) from exc
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def _clock(value: str, field: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except Exception as exc:
        raise InvalidArgument(f"{field} must use HH:MM, got {value!r}") from exc


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    attendees = [
        {
            "email": person.get("email"),
            "display_name": person.get("displayName"),
            "response_status": person.get("responseStatus"),
        }
        for person in event.get("attendees") or []
        if person.get("email")
    ]
    return {
        "id": event.get("id"),
        "summary": event.get("summary", ""),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "start": (event.get("start") or {}).get("dateTime")
        or (event.get("start") or {}).get("date"),
        "end": (event.get("end") or {}).get("dateTime")
        or (event.get("end") or {}).get("date"),
        "timezone": (event.get("start") or {}).get("timeZone"),
        "status": event.get("status"),
        "html_link": event.get("htmlLink"),
        "attendees": attendees,
    }


def list_calendar_events(
    service,
    *,
    time_min: str,
    time_max: str,
    max_results: int = 50,
) -> dict[str, Any]:
    zone = _zone(None)
    start = _parse_dt(time_min, zone)
    end = _parse_dt(time_max, zone)
    if end <= start:
        raise InvalidArgument("time_max must be after time_min")
    if end - start > timedelta(days=90):
        raise InvalidArgument("calendar query window must not exceed 90 days")
    limit = max(1, min(int(max_results), 100))
    try:
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=limit,
                timeZone=str(zone),
            )
            .execute()
            or {}
        )
    except Exception as exc:
        raise GoogleApiFailure(
            "Google Calendar could not list events.", detail=sanitize_error(exc)
        ) from exc
    events = [_event_summary(item) for item in result.get("items") or []]
    return {
        "time_min": start.isoformat(),
        "time_max": end.isoformat(),
        "timezone": str(zone),
        "events": events,
        "count": len(events),
    }


def _busy_intervals(
    service,
    *,
    start: datetime,
    end: datetime,
    attendees: list[str],
    zone: ZoneInfo,
) -> list[tuple[datetime, datetime]]:
    items = [{"id": "primary"}]
    for email in attendees:
        if email and all(item["id"] != email for item in items):
            items.append({"id": email})
    try:
        result = (
            service.freebusy()
            .query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "timeZone": str(zone),
                    "items": items,
                }
            )
            .execute()
            or {}
        )
    except Exception as exc:
        raise GoogleApiFailure(
            "Google Calendar free/busy query failed.", detail=sanitize_error(exc)
        ) from exc

    intervals: list[tuple[datetime, datetime]] = []
    for calendar in (result.get("calendars") or {}).values():
        for item in (calendar or {}).get("busy") or []:
            try:
                busy_start = _parse_dt(item["start"], zone)
                busy_end = _parse_dt(item["end"], zone)
            except Exception:
                continue
            if busy_end > busy_start:
                intervals.append(
                    (max(start, busy_start), min(end, busy_end))
                )
    intervals.sort()
    return intervals


def find_free_slots(
    service,
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
    attendees = [email.strip() for email in (attendees or []) if email.strip()]
    busy = _busy_intervals(
        service, start=start, end=end, attendees=attendees, zone=zone
    )
    duration = timedelta(minutes=duration_minutes)
    slots: list[dict[str, str]] = []
    current_day = start.date()
    last_day = end.date()
    while current_day <= last_day and len(slots) < max(1, min(max_slots, 50)):
        if current_day.weekday() < 5:
            window_start = datetime.combine(current_day, begin_clock, zone)
            window_end = datetime.combine(current_day, end_clock, zone)
            cursor = max(window_start, start)
            window_end = min(window_end, end)
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
        "attendees": attendees,
        "slots": slots[:max_slots],
        "count": len(slots[:max_slots]),
    }


def create_calendar_event(
    service,
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
    guests = [email.strip() for email in (attendees or []) if email.strip()]
    body: dict[str, Any] = {
        "summary": summary.strip(),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": str(zone)},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": str(zone)},
    }
    if description:
        body["description"] = description.strip()
    if location:
        body["location"] = location.strip()
    if guests:
        body["attendees"] = [{"email": email} for email in guests]
    try:
        created = (
            service.events()
            .insert(
                calendarId="primary",
                body=body,
                sendUpdates="all" if guests else "none",
            )
            .execute()
            or {}
        )
    except Exception as exc:
        raise GoogleApiFailure(
            "Google Calendar could not create the event.", detail=sanitize_error(exc)
        ) from exc
    return {
        **_event_summary(created),
        "invitations_requested": bool(guests),
    }
