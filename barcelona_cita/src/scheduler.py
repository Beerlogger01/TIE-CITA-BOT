from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Settings

MADRID = ZoneInfo("Europe/Madrid")
MIN_INTERVAL = timedelta(minutes=15)


def interval(settings: Settings, now: datetime, failures: int = 0) -> timedelta:
    local = now.astimezone(MADRID)
    minute = local.hour * 60 + local.minute
    minutes = settings.interval_minutes
    if settings.thursday_enabled and local.weekday() == 3 and 885 <= minute < 925:
        minutes = min(minutes, settings.priority_minutes)
    if settings.morning_enabled and 360 <= minute < 540:
        minutes = min(minutes, settings.morning_minutes)
    return max(MIN_INTERVAL, timedelta(minutes=min(1440, minutes * 2 ** min(failures, 6))))


def next_check(settings: Settings, last: datetime | None, now: datetime, failures=0):
    if last is None:
        return now
    # Evaluate upcoming windows too, without ever violating the global floor.
    earliest = max(now, last + MIN_INTERVAL)
    candidate = last + interval(settings, earliest, failures)
    cursor = earliest
    while cursor < candidate:
        if cursor >= last + interval(settings, cursor, failures):
            return cursor
        cursor += timedelta(minutes=1)
    return max(earliest, candidate)
