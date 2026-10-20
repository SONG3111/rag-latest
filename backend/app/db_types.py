"""SQLite-compatible UTC timestamp type.

This project shared a database with an older Java backend that stored timestamps
as epoch milliseconds. The Python models used plain ``DateTime`` and therefore
failed on those integers, while Python's own rows were written as ISO strings.
The mixed representation also made ordering unreliable.

``EpochMillisDateTime`` makes epoch milliseconds the single SQLite storage
format. It still accepts ISO strings during migration/readback, so a partially
migrated database remains diagnosable instead of crashing with a raw
``fromisoformat`` TypeError.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer
from sqlalchemy.types import TypeDecorator

_MILLISECONDS_THRESHOLD = 100_000_000_000


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def datetime_to_epoch_millis(value: datetime) -> int:
    return int(round(_as_utc(value).timestamp() * 1000))


def to_epoch_millis(value: Any) -> int:
    """Coerce a datetime, epoch seconds/milliseconds, or ISO string to UTC ms."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a timestamp")
    if isinstance(value, datetime):
        return datetime_to_epoch_millis(value)
    if isinstance(value, (int, float)):
        number = float(value)
        return int(round(number if abs(number) >= _MILLISECONDS_THRESHOLD else number * 1000))
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty timestamp")
        try:
            number = float(text)
        except ValueError:
            pass
        else:
            return to_epoch_millis(number)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return datetime_to_epoch_millis(parsed)
    raise TypeError(f"unsupported timestamp value: {type(value).__name__}")


def from_epoch_millis(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)


class EpochMillisDateTime(TypeDecorator[datetime]):
    """UTC datetime stored as epoch milliseconds on SQLite."""

    impl = Integer
    cache_ok = True

    def load_dialect_impl(self, dialect):
        # Non-SQLite deployments keep a native timestamp column. The local app is
        # SQLite-first, but this avoids forcing INTEGER onto Postgres/MySQL.
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Integer())
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "sqlite":
            return to_epoch_millis(value)
        if isinstance(value, datetime):
            return _as_utc(value)
        return from_epoch_millis(to_epoch_millis(value))

    def process_result_value(self, value: Any, dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            parsed = from_epoch_millis(value)
        return _as_utc(parsed)

    @property
    def python_type(self):
        return datetime
