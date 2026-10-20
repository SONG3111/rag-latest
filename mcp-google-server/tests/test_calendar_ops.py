from __future__ import annotations

from typing import Any

from mcp_google_server import calendar_ops


class _Request:
    def __init__(self, value: Any) -> None:
        self.value = value

    def execute(self) -> Any:
        return self.value


class FakeEvents:
    def __init__(self) -> None:
        self.inserted: dict[str, Any] | None = None

    def list(self, **kwargs):
        return _Request(
            {
                "items": [
                    {
                        "id": "event-1",
                        "summary": "Existing",
                        "start": {
                            "dateTime": "2026-10-21T10:00:00+08:00",
                            "timeZone": "Asia/Shanghai",
                        },
                        "end": {
                            "dateTime": "2026-10-21T11:00:00+08:00",
                            "timeZone": "Asia/Shanghai",
                        },
                        "attendees": [{"email": "bob@example.com"}],
                    }
                ]
            }
        )

    def insert(self, **kwargs):
        self.inserted = kwargs
        return _Request(
            {
                "id": "new-event",
                "summary": kwargs["body"]["summary"],
                "start": kwargs["body"]["start"],
                "end": kwargs["body"]["end"],
                "htmlLink": "https://calendar.example/event",
            }
        )


class FakeFreebusy:
    def __init__(self, busy: list[dict[str, str]]) -> None:
        self.busy = busy
        self.body: dict[str, Any] | None = None

    def query(self, **kwargs):
        self.body = kwargs["body"]
        return _Request(
            {
                "calendars": {
                    "primary": {"busy": self.busy},
                    "bob@example.com": {
                        "busy": [
                            {
                                "start": "2026-10-21T13:00:00+08:00",
                                "end": "2026-10-21T14:00:00+08:00",
                            }
                        ]
                    },
                }
            }
        )


class FakeCalendar:
    def __init__(self, busy: list[dict[str, str]] | None = None) -> None:
        self._events = FakeEvents()
        self._freebusy = FakeFreebusy(
            busy
            or [
                {
                    "start": "2026-10-21T10:00:00+08:00",
                    "end": "2026-10-21T11:00:00+08:00",
                }
            ]
        )

    def events(self):
        return self._events

    def freebusy(self):
        return self._freebusy


def test_list_events_shapes_attendees_and_times() -> None:
    service = FakeCalendar()
    result = calendar_ops.list_calendar_events(
        service,
        time_min="2026-10-21T00:00:00+08:00",
        time_max="2026-10-22T00:00:00+08:00",
    )
    assert result["count"] == 1
    assert result["events"][0]["id"] == "event-1"
    assert result["events"][0]["attendees"][0]["email"] == "bob@example.com"


def test_find_free_slots_skips_busy_intervals() -> None:
    service = FakeCalendar()
    result = calendar_ops.find_free_slots(
        service,
        time_min="2026-10-21T09:00:00+08:00",
        time_max="2026-10-21T18:00:00+08:00",
        duration_minutes=60,
        attendees=["bob@example.com"],
        max_slots=3,
    )
    starts = [item["start"] for item in result["slots"]]
    assert starts[0] == "2026-10-21T09:00:00+08:00"
    assert "2026-10-21T10:00:00+08:00" not in starts
    assert "2026-10-21T13:00:00+08:00" not in starts
    assert service.freebusy().body["items"] == [
        {"id": "primary"},
        {"id": "bob@example.com"},
    ]


def test_create_event_requests_invitations() -> None:
    service = FakeCalendar()
    result = calendar_ops.create_calendar_event(
        service,
        summary="Planning",
        start="2026-10-21T15:00:00+08:00",
        end="2026-10-21T16:00:00+08:00",
        attendees=["bob@example.com"],
    )
    assert result["id"] == "new-event"
    assert result["invitations_requested"] is True
    inserted = service.events().inserted
    assert inserted is not None
    assert inserted["sendUpdates"] == "all"
    assert inserted["body"]["attendees"] == [{"email": "bob@example.com"}]
