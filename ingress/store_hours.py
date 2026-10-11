"""Takeout opening hours, so checkout can refuse orders while the kitchen is closed.

WEEKLY_HOURS maps a weekday (Monday=0 ... Sunday=6) to a list of
("HH:MM", "HH:MM") open/close pairs in Toronto time. A day missing from the
map, or mapped to [], is closed all day. Two pairs model a lunch/dinner split.

WEEKLY_HOURS = None means "hours not set yet": is_open() is always True.
Only prod enforces the hours (ENFORCE_STORE_HOURS=1 on CheckoutApi); staging
stays open around the clock so its order tests pass at any hour. Mirrored in
TableTap's lib/storeHours.ts, which shows the same hours on the site; keep
the two in step.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")

# Takeout runs 11:50 AM to 10:28 PM (last orders 10:13 PM). On Friday and
# Saturday the restaurant closes at 11 PM and the last online order is 10:58 PM,
# so the close is set 15 min (LAST_ORDER_MINUTES) past that, at 23:13.
WEEKLY_HOURS = {day: [("11:50", "22:28")] for day in range(7)}
WEEKLY_HOURS[4] = WEEKLY_HOURS[5] = [("11:50", "23:13")]

# Stop taking orders this long before closing so the kitchen can finish them.
LAST_ORDER_MINUTES = 15


def _minutes(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def is_open(now=None, hours=WEEKLY_HOURS):
    """True if a takeout order placed at `now` can be accepted."""
    if hours is None:
        return True
    now = (now or datetime.now(TZ)).astimezone(TZ)
    minute_of_day = now.hour * 60 + now.minute
    for open_at, close_at in hours.get(now.weekday(), []):
        if _minutes(open_at) <= minute_of_day < _minutes(close_at) - LAST_ORDER_MINUTES:
            return True
    return False


def next_opening(now=None, hours=WEEKLY_HOURS):
    """The next time orders open, as a Toronto datetime, or None if never."""
    if hours is None:
        return None
    now = (now or datetime.now(TZ)).astimezone(TZ)
    for day_offset in range(8):
        day = (now + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        for open_at, _ in sorted(hours.get(day.weekday(), [])):
            opening = day + timedelta(minutes=_minutes(open_at))
            if opening > now:
                return opening
    return None
