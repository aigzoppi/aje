"""Cron expressions: validation and next fire time (timezone and DST aware)."""

from __future__ import annotations

import datetime
import zoneinfo

import croniter


def check_cron(expr: str) -> str:
    """Standard 5-field cron or a macro (@hourly, @daily, @weekly, @monthly, @yearly)."""
    expr = " ".join(expr.split())
    if len(expr.split()) not in (1, 5) or not croniter.croniter.is_valid(expr):
        raise ValueError(
            f"invalid cron expression {expr!r}: use 5 fields "
            "(minute hour day-of-month month day-of-week) or @hourly/@daily/..."
        )
    return expr


def check_timezone(name: str) -> str:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"unknown timezone {name!r} (use an IANA name such as 'Europe/Rome' or 'UTC')"
        ) from None
    return name


def as_utc(dt: datetime.datetime) -> datetime.datetime:
    """Converts an aware datetime to UTC.

    Raises:
        ValueError: ``dt`` is naive (PostgreSQL ``timestamptz`` values never are).
    """
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime {dt.isoformat()}: a time zone is required")
    return dt.astimezone(datetime.UTC)


def next_fire(expr: str, tz: str, after: datetime.datetime) -> datetime.datetime:
    """First fire time strictly after ``after``, in UTC."""
    local = as_utc(after).astimezone(zoneinfo.ZoneInfo(tz))
    return croniter.croniter(expr, local).get_next(datetime.datetime).astimezone(datetime.UTC)
