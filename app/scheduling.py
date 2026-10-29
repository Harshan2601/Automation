from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def customer_zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid customer timezone: {timezone_name}") from exc


def default_scheduled_time(timezone_name: str) -> datetime:
    """Default: next weekday at 09:00 in the customer's timezone."""
    zone = customer_zone(timezone_name)
    today = datetime.now(zone).date()
    candidate = today + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return datetime.combine(candidate, time(hour=9, minute=0), tzinfo=zone)


def parse_user_schedule(text: str, timezone_name: str, prefix: str = "OK") -> datetime | None:
    match = re.fullmatch(rf"{re.escape(prefix)}\s+(\d{{4}}-\d{{2}}-\d{{2}})\s+(\d{{2}}:\d{{2}})", text.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    zone = customer_zone(timezone_name)
    parsed = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M")
    scheduled = parsed.replace(tzinfo=zone)
    if scheduled <= datetime.now(zone) + timedelta(minutes=1):
        raise ValueError("Please choose a future time at least two minutes from now.")
    return scheduled


def format_local(value: str, timezone_name: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(customer_zone(timezone_name)).strftime("%a, %d %b %Y at %H:%M %Z")
