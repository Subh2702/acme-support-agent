"""Time helpers: store UTC, display Asia/Kolkata."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo; treat naive datetimes from the DB as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_ist(value: datetime) -> datetime:
    return ensure_utc(value).astimezone(IST)


def format_ist(value: datetime | None) -> str | None:
    if value is None:
        return None
    return to_ist(value).strftime("%d %b %Y, %I:%M %p IST")


def start_of_ist_day(now: datetime) -> datetime:
    """UTC instant of the most recent IST midnight (budget day boundary)."""
    local = to_ist(now)
    midnight = datetime.combine(local.date(), time.min, tzinfo=IST)
    return midnight.astimezone(UTC)
