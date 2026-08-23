import json
import os
from unittest.mock import MagicMock

# checkout_api builds its DynamoDB table handle at import time and does not
# guard a missing name, so give it a dummy one before import. Every test
# monkeypatches api.table anyway, so this value is never actually used.
os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import checkout_api as api


# ---------------------------------------------------------------------------
# sanitize_customer_name — the pure normalization used on the money path
# ---------------------------------------------------------------------------

def test_sanitize_plain_name():
    assert api.sanitize_customer_name("Ken") == "Ken"


def test_sanitize_none_is_none():
    assert api.sanitize_customer_name(None) is None


def test_sanitize_empty_string_is_none():
    assert api.sanitize_customer_name("") is None


def test_sanitize_whitespace_only_is_none():
    assert api.sanitize_customer_name("   \t\n ") is None


def test_sanitize_trims_and_collapses_internal_whitespace():
    assert api.sanitize_customer_name("  Ken   Chen \n") == "Ken Chen"


def test_sanitize_strips_control_characters():
    # bell, null, escape removed; the surrounding text survives
    assert api.sanitize_customer_name("Ke\x07n\x00\x1b") == "Ken"


def test_sanitize_truncates_to_40():
    result = api.sanitize_customer_name("A" * 200)
    assert result == "A" * 40
    assert len(result) == 40


def test_sanitize_never_raises_on_non_string():
    # A name must never block a paid order — non-str coerces, doesn't throw.
    assert api.sanitize_customer_name(12345) == "12345"
    assert api.sanitize_customer_name(["x"]) is not None  # coerced, not raised


# ---------------------------------------------------------------------------
# handler — customerName is stored on the record and mirrored to Stripe
# ---------------------------------------------------------------------------

def _event(body):
    return {"httpMethod": "POST", "body": json.dumps(body)}


def _real_order_body(**overrides):
    body = {
        "amount": 2260,
        "metadata": {"order_id": "ABCDE"},
        "items": [{"id": "1", "quantity": 1}],
        "notes": "no wasabi",
        "customerDetails": {"email": "c@example.com"},
    }
    body.update(overrides)
    return body


def _patch_stripe_and_table(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(api, "table", fake_table)

    fake_intent = MagicMock()
    fake_intent.client_secret = "cs_test_123"
    fake_create = MagicMock(return_value=fake_intent)
    monkeypatch.setattr(api.stripe.PaymentIntent, "create", fake_create)
    return fake_table, fake_create


def test_customer_name_stored_when_supplied(monkeypatch):
    fake_table, fake_create = _patch_stripe_and_table(monkeypatch)

    resp = api.handler(_event(_real_order_body(customerName="  Ken  ")), None)

    assert resp["statusCode"] == 200
    stored = fake_table.put_item.call_args.kwargs["Item"]
    assert stored["customerName"] == "Ken"
    # ...and mirrored into the confirmed PaymentIntent's metadata
    assert fake_create.call_args.kwargs["metadata"]["customer_name"] == "Ken"


def test_customer_name_absent_when_field_missing(monkeypatch):
    fake_table, fake_create = _patch_stripe_and_table(monkeypatch)

    resp = api.handler(_event(_real_order_body()), None)

    assert resp["statusCode"] == 200
    stored = fake_table.put_item.call_args.kwargs["Item"]
    assert "customerName" not in stored  # absent means "not collected", never ""
    assert "customer_name" not in fake_create.call_args.kwargs["metadata"]


def test_customer_name_absent_when_whitespace_only(monkeypatch):
    fake_table, fake_create = _patch_stripe_and_table(monkeypatch)

    resp = api.handler(_event(_real_order_body(customerName="   ")), None)

    assert resp["statusCode"] == 200
    stored = fake_table.put_item.call_args.kwargs["Item"]
    assert "customerName" not in stored


def test_long_name_truncated_when_stored(monkeypatch):
    fake_table, _ = _patch_stripe_and_table(monkeypatch)

    api.handler(_event(_real_order_body(customerName="B" * 200)), None)

    stored = fake_table.put_item.call_args.kwargs["Item"]
    assert stored["customerName"] == "B" * 40


def test_order_without_name_still_succeeds(monkeypatch):
    fake_table, fake_create = _patch_stripe_and_table(monkeypatch)

    resp = api.handler(_event(_real_order_body()), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["orderId"] == "TKOT-ABCDE"
    assert fake_table.put_item.called
    assert fake_create.called
