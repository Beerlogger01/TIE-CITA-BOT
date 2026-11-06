from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.models import Settings
from src.scheduler import MADRID, interval, next_check


@pytest.mark.parametrize("stamp", ["2026-11-05T13:45:00+00:00", "2026-07-02T12:45:00+00:00"])
def test_thursday_madrid_winter_and_summer(stamp):
    now = datetime.fromisoformat(stamp)
    assert now.astimezone(MADRID).hour == 14
    s = Settings(interval_minutes=60, priority_minutes=15)
    assert interval(s, now) == timedelta(minutes=15)
    assert interval(s, now + timedelta(hours=1)) == timedelta(minutes=60)


def test_floor_backoff_and_future_window():
    with pytest.raises(ValidationError):
        Settings(interval_minutes=1)
    now = datetime(2026, 11, 5, 13, 30, tzinfo=timezone.utc)
    settings = Settings(interval_minutes=60)
    assert next_check(settings, now, now) == now + timedelta(minutes=15)
    assert interval(Settings(), now, 2) == timedelta(minutes=60)
    assert next_check(Settings(), now, now + timedelta(minutes=2)) >= now + timedelta(minutes=15)


def test_optional_morning():
    now = datetime(2026, 11, 6, 6, tzinfo=timezone.utc)
    assert interval(Settings(interval_minutes=60), now) == timedelta(minutes=60)
    assert interval(Settings(interval_minutes=60, morning_enabled=True), now) == timedelta(minutes=30)
