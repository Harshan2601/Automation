from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.scheduling import default_scheduled_time, parse_user_schedule


def test_default_schedule_is_future_weekday():
    value = default_scheduled_time("Europe/Berlin")
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    assert value > now
    assert value.weekday() < 5
    assert (value.hour, value.minute) == (9, 0)


def test_parse_custom_future_schedule():
    candidate = datetime.now(ZoneInfo("Europe/Berlin")) + timedelta(days=3)
    text = "OK " + candidate.strftime("%Y-%m-%d") + " 15:30"
    result = parse_user_schedule(text, "Europe/Berlin", prefix="OK")
    assert result is not None
    assert result.hour == 15 and result.minute == 30
    assert result.tzinfo is not None


def test_parse_schedule_rejects_past_time():
    old = (datetime.now(ZoneInfo("Europe/Berlin")) - timedelta(days=2)).strftime("%Y-%m-%d")
    with pytest.raises(ValueError):
        parse_user_schedule(f"OK {old} 09:00", "Europe/Berlin", prefix="OK")


def test_unrecognized_schedule_returns_none():
    assert parse_user_schedule("tomorrow morning", "Europe/Berlin", prefix="OK") is None
