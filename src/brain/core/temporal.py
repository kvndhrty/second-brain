"""Dates and ages for anything shown to a model.

Every note given to a model carries the date it was written and its age
relative to today, so the model can tell newer information from older without
doing date arithmetic. Summaries and other derived text carry `as_of`: the date
of the newest entry they cover.

Dates are local calendar dates in the machine's time zone (set `TZ` to change
it). Stored timestamps stay UTC; only what is shown is converted, so an entry
written in the evening is dated the day it was written.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

When = str | datetime | date


def to_local(value: str | datetime) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone()


def local_date(value: When) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and len(value) == 10:
        return date.fromisoformat(value)  # already a calendar date, such as an OBE date
    return to_local(value).date()


def iso_day(value: When) -> str:
    return local_date(value).isoformat()


def age_days(value: When, today: date) -> int:
    return max(0, (today - local_date(value)).days)


def age(value: When, today: date) -> str:
    """Days up to 13, weeks up to 8, then months: 9d, 3w, 5mo."""
    days = age_days(value, today)
    if days < 14:
        return f"{days}d"
    if days < 60:
        return f"{days // 7}w"
    return f"{days // 30}mo"


def dated(value: When, today: date) -> str:
    """Attributes for a dated element: date="2026-10-30" age="9d"."""
    return f'date="{iso_day(value)}" age="{age(value, today)}"'


def as_of(value: When | None, today: date) -> str:
    """Attributes for derived text: as_of="2026-11-04" age="4d", or as_of="unknown"."""
    if value is None:
        return 'as_of="unknown"'
    return f'as_of="{iso_day(value)}" age="{age(value, today)}"'


def last_mentioned(value: When | None, today: date) -> str:
    """Attributes for an entity: when a fragment last mentioned it."""
    if value is None:
        return 'last_mentioned="never"'
    return f'last_mentioned="{iso_day(value)}" age="{age(value, today)}"'


def today_el(now: datetime) -> str:
    """The first line of every model input."""
    d = local_date(now)
    return f'<today date="{d.isoformat()}" weekday="{d.strftime("%A")}"/>'
