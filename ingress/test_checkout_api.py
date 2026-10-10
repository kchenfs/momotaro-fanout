import json
import os
from unittest.mock import MagicMock

# checkout_api builds its DynamoDB table handle at import time and does not
# guard a missing name, so give it a dummy one before import. Every test
# monkeypatches api.table anyway, so this value is never actually used.
os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ.setdefault("CONFIRMATION_TOKEN_SECRET", "test-secret")

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


# Item id "1" priced at $20.00 → subtotal 20.00 * 1.13 tax = $22.60 = 2260 cents.
_FAKE_MENU_ITEMS = [{"ItemNumber": 1, "Price": "20.00", "Options": []}]


def _patch_stripe_and_table(monkeypatch, menu_items=None):
    fake_table = MagicMock()
    monkeypatch.setattr(api, "table", fake_table)
    # Pretend the SSM fetch already ran so the handler never calls AWS.
    monkeypatch.setattr(api.stripe, "api_key", "sk_test_123")

    fake_menu = MagicMock()
    fake_menu.scan.return_value = {"Items": _FAKE_MENU_ITEMS if menu_items is None else menu_items}
    monkeypatch.setattr(api, "menu_table", fake_menu)

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


# ---------------------------------------------------------------------------
# #1 price manipulation — the charged amount is recomputed server-side
# ---------------------------------------------------------------------------

def test_amount_recomputed_and_client_amount_ignored(monkeypatch):
    from decimal import Decimal
    fake_table, fake_create = _patch_stripe_and_table(monkeypatch)

    # Client tries to pay 1 cent for a $22.60 order.
    resp = api.handler(_event(_real_order_body(amount=1)), None)

    assert resp["statusCode"] == 200
    # The confirmed intent is charged the server-computed amount, not the client's.
    assert fake_create.call_args.kwargs["amount"] == 2260
    stored = fake_table.put_item.call_args.kwargs["Item"]
    assert stored["total"] == Decimal("22.6")


def test_unknown_item_returns_400(monkeypatch):
    fake_table, fake_create = _patch_stripe_and_table(monkeypatch)

    resp = api.handler(_event(_real_order_body(items=[{"id": "999", "quantity": 1}])), None)

    assert resp["statusCode"] == 400
    assert not fake_table.put_item.called
    assert not fake_create.called


def test_too_many_items_returns_400(monkeypatch):
    fake_table, fake_create = _patch_stripe_and_table(monkeypatch)

    resp = api.handler(_event(_real_order_body(
        items=[{"id": "1", "quantity": 1} for _ in range(21)])), None)

    assert resp["statusCode"] == 400
    assert not fake_table.put_item.called


# ---------------------------------------------------------------------------
# #2 idempotency — a duplicate orderId is rejected, not silently overwritten
# ---------------------------------------------------------------------------

def test_duplicate_order_id_returns_409(monkeypatch):
    fake_table, fake_create = _patch_stripe_and_table(monkeypatch)
    fake_table.put_item.side_effect = api.ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")

    resp = api.handler(_event(_real_order_body()), None)

    assert resp["statusCode"] == 409
    # No payment intent is confirmed for a duplicate order.
    assert not fake_create.called


def test_put_item_uses_idempotency_condition(monkeypatch):
    fake_table, _ = _patch_stripe_and_table(monkeypatch)

    api.handler(_event(_real_order_body()), None)

    assert fake_table.put_item.call_args.kwargs["ConditionExpression"] == "attribute_not_exists(orderId)"


# ---------------------------------------------------------------------------
# #3 confirmation token — checkout mints a token bound to the order id
# ---------------------------------------------------------------------------

def test_confirmation_token_returned_and_valid(monkeypatch):
    from confirmation_token import make_confirmation_token
    _patch_stripe_and_table(monkeypatch)

    resp = api.handler(_event(_real_order_body()), None)
    body = json.loads(resp["body"])

    assert body["confirmationToken"]  # non-empty
    assert body["confirmationToken"] == make_confirmation_token(body["orderId"])


# ---------------------------------------------------------------------------
# Stripe key comes from SSM, fetched once per container
# ---------------------------------------------------------------------------

def test_stripe_key_loaded_from_ssm_once(monkeypatch):
    monkeypatch.setattr(api.stripe, "api_key", None)
    fake_ssm = MagicMock()
    fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "sk_from_ssm"}}
    monkeypatch.setattr(api, "_ssm", fake_ssm)

    api._load_stripe_key()
    api._load_stripe_key()

    assert api.stripe.api_key == "sk_from_ssm"
    fake_ssm.get_parameter.assert_called_once_with(Name=api.STRIPE_KEY_PARAM, WithDecryption=True)
