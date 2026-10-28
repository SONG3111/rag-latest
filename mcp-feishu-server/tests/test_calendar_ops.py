from __future__ import annotations

from types import SimpleNamespace

from lark_oapi.api.calendar.v4 import CalendarEvent, TimeInfo

from mcp_feishu_server import calendar_ops


def _response(data):
    return SimpleNamespace(data=data, success=lambda: True, code=0, msg="ok")


class FakeCalendarEvent:
    def __init__(self) -> None:
        self.created = None

    def list(self, request, option):
        event = (
            CalendarEvent.builder()
            .event_id("e1")
            .summary("Existing")
            .start_time(
                TimeInfo.builder()
                .date_time("2026-10-26T10:00:00+08:00")
                .timezone("Asia/Shanghai")
                .build()
            )
            .end_time(
                TimeInfo.builder()
                .date_time("2026-10-26T11:00:00+08:00")
                .timezone("Asia/Shanghai")
                .build()
            )
            .build()
        )
        return _response(SimpleNamespace(items=[event], has_more=False))

    def create(self, request, option):
        self.created = request
        event = (
            CalendarEvent.builder()
            .event_id("new-1")
            .summary(request.request_body.summary)
            .start_time(request.request_body.start_time)
            .end_time(request.request_body.end_time)
            .app_link("https://applink.feishu.cn/calendar/new-1")
            .build()
        )
        return _response(SimpleNamespace(event=event))


class FakeRuntime:
    def __init__(self) -> None:
        self.events = FakeCalendarEvent()
        self._client = SimpleNamespace(
            calendar=SimpleNamespace(
                v4=SimpleNamespace(calendar_event=self.events)
            )
        )

    def client(self):
        return self._client

    def option(self):
        return object()

    def calendar(self):
        return "calendar-1"


def test_list_events_and_free_slots() -> None:
    runtime = FakeRuntime()
    events = calendar_ops.list_calendar_events(
        runtime,
        time_min="2026-10-26T09:00:00+08:00",
        time_max="2026-10-26T18:00:00+08:00",
    )
    assert events["count"] == 1
    slots = calendar_ops.find_free_slots(
        runtime,
        time_min="2026-10-26T09:00:00+08:00",
        time_max="2026-10-26T18:00:00+08:00",
        duration_minutes=60,
        max_slots=3,
    )
    starts = [item["start"] for item in slots["slots"]]
    assert starts[0] == "2026-10-26T09:00:00+08:00"
    assert "2026-10-26T10:00:00+08:00" not in starts


def test_create_event_invites_external_attendees() -> None:
    runtime = FakeRuntime()
    result = calendar_ops.create_calendar_event(
        runtime,
        summary="Planning",
        start="2026-10-26T15:00:00+08:00",
        end="2026-10-26T16:00:00+08:00",
        attendees=["bob@example.com"],
    )
    assert result["id"] == "new-1"
    assert result["invitations_requested"] is True
    assert runtime.events.created.request_body.need_notification is True
