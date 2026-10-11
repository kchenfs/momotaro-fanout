import json
from datetime import datetime

import store_hours as sh

TZ = sh.TZ
# Mon-Fri lunch and dinner, Saturday dinner only, Sunday closed.
HOURS = {d: [("11:30", "14:30"), ("17:00", "21:30")] for d in range(5)}
HOURS[5] = [("17:00", "22:00")]


def at(day, hhmm):
    """2026-10-05 is a Monday, so day 0..6 maps to Mon..Sun of that week."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, 5 + day, h, m, tzinfo=TZ)


def test_hours_not_set_never_blocks():
    assert sh.is_open(at(6, "03:00"), hours=None) is True
    assert sh.next_opening(at(6, "03:00"), hours=None) is None


def test_open_during_lunch_and_dinner():
    assert sh.is_open(at(0, "11:30"), HOURS)
    assert sh.is_open(at(2, "19:00"), HOURS)


def test_closed_between_services_and_overnight():
    assert not sh.is_open(at(0, "15:00"), HOURS)
    assert not sh.is_open(at(0, "23:00"), HOURS)
    assert not sh.is_open(at(0, "11:29"), HOURS)


def test_last_orders_stop_before_closing():
    # 21:30 close minus 15 minutes: 21:14 is fine, 21:15 is too late.
    assert sh.is_open(at(0, "21:14"), HOURS)
    assert not sh.is_open(at(0, "21:15"), HOURS)


def test_day_missing_from_map_is_closed():
    assert not sh.is_open(at(6, "12:00"), HOURS)


def test_utc_input_is_judged_in_toronto_time():
    # 16:00 UTC on a Monday in October is 12:00 in Toronto: open.
    from datetime import timezone
    assert sh.is_open(datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc), HOURS)


def test_next_opening_same_day_and_after_sunday():
    assert sh.next_opening(at(0, "15:00"), HOURS) == at(0, "17:00")
    # Saturday night -> Sunday closed -> Monday lunch.
    assert sh.next_opening(at(5, "23:00"), HOURS) == at(7, "11:30")


def test_checkout_refuses_orders_when_closed(monkeypatch):
    import os
    os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
    os.environ.setdefault("CONFIRMATION_TOKEN_SECRET", "test-secret")
    import checkout_api as api

    monkeypatch.setattr(api, "ENFORCE_STORE_HOURS", True)
    monkeypatch.setattr(api.store_hours, "is_open", lambda: False)
    monkeypatch.setattr(api.store_hours, "next_opening", lambda: at(0, "17:00"))
    # Nothing may be charged or written while closed.
    monkeypatch.setattr(api, "_load_stripe_key", lambda: (_ for _ in ()).throw(AssertionError("touched Stripe")))

    res = api.handler({"httpMethod": "POST", "body": json.dumps({"metadata": {"order_id": "ABCDE"}})}, None)
    assert res["statusCode"] == 409
    body = json.loads(res["body"])
    assert body["code"] == "STORE_CLOSED"
    assert body["error"] == "Sorry, we're closed for online orders right now. We open again Monday at 5:00 PM."


def test_checkout_ignores_hours_when_not_enforced(monkeypatch):
    import checkout_api as api
    monkeypatch.setattr(api, "ENFORCE_STORE_HOURS", False)
    monkeypatch.setattr(api.store_hours, "is_open", lambda: False)
    monkeypatch.setattr(api, "_load_stripe_key", lambda: None)
    res = api.handler({"httpMethod": "POST", "body": ""}, None)
    # Falls through to normal validation instead of the closed response.
    assert res["statusCode"] == 400


def test_real_hours_last_orders_1013pm_and_1043pm_on_fri_sat():
    for day in range(7):
        last = "22:43" if day in (4, 5) else "22:13"
        just_before = "22:42" if day in (4, 5) else "22:12"
        assert not sh.is_open(at(day, "11:49"))
        assert sh.is_open(at(day, "11:50"))
        assert sh.is_open(at(day, just_before))
        assert not sh.is_open(at(day, last))
    # Thursday at 10:20 PM is closed; Friday at 10:20 PM is still open.
    assert not sh.is_open(at(3, "22:20"))
    assert sh.is_open(at(4, "22:20"))
    assert sh.next_opening(at(0, "23:00")) == at(1, "11:50")
