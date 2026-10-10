import json
import os
from decimal import Decimal
from unittest.mock import MagicMock

# takeout_ingress builds its table handle at import time without guarding a
# missing name; give it a dummy one. Every test monkeypatches api.table.
os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")

import takeout_ingress as api


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _db_order(**overrides):
    o = {
        "orderId": "TKOT-ABCDE",
        "orderType": "takeout",
        "orderStatus": "PENDING_PAYMENT",
        "paymentStatus": "UNPAID",
        "items": [{"name": "Green Dragon Roll", "quantity": 1}],
        "total": Decimal("22.60"),
        "orderDate": "2026-08-22T18:00:00-04:00",
    }
    o.update(overrides)
    return o


def _charge(name=None, brand="visa", last4="4242"):
    return {
        "payment_method_details": {"card": {"brand": brand, "last4": last4}},
        "billing_details": {"name": name},
    }


def _pi(charges=None, pi_id="pi_123"):
    d = {"id": pi_id, "metadata": {"order_id": "TKOT-ABCDE"}}
    if charges is not None:
        d["charges"] = {"data": charges}
    return d


def _retrieve_returning(charge_dict):
    """Mock stripe.PaymentIntent.retrieve → object exposing .latest_charge."""
    latest = MagicMock()
    latest.to_dict.return_value = charge_dict
    refreshed = MagicMock()
    refreshed.latest_charge = latest
    return MagicMock(return_value=refreshed)


def _run(monkeypatch, pi_dict, db_order, retrieve=None, fake_table=None):
    if fake_table is None:
        fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": db_order}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    # Pretend the SSM fetch already ran so the handler never calls AWS.
    monkeypatch.setattr(api, "_webhook_secret", "whsec_test")

    class _Obj:
        def to_dict(self):
            return pi_dict

    event = {"type": "payment_intent.succeeded", "data": {"object": _Obj()}}
    monkeypatch.setattr(api.stripe.Webhook, "construct_event", lambda **kw: event)

    if retrieve is not None:
        monkeypatch.setattr(api.stripe.PaymentIntent, "retrieve", retrieve)

    resp = api.handler({"body": "{}", "headers": {"stripe-signature": "sig"}}, None)
    return resp, fake_table, fake_sns


def _published(fake_sns):
    return json.loads(fake_sns.publish.call_args.kwargs["Message"])


# ---------------------------------------------------------------------------
# name source: customer-typed name wins and is not overwritten
# ---------------------------------------------------------------------------

def test_existing_customer_name_is_kept_and_tagged_customer(monkeypatch):
    db = _db_order(customerName="Ken")
    pi = _pi(charges=[_charge(name="CARDHOLDER LEGAL NAME")])

    resp, table, sns = _run(monkeypatch, pi, db)

    assert resp["statusCode"] == 200
    msg = _published(sns)
    assert msg["customerName"] == "Ken"                # not overwritten
    assert msg["customerNameSource"] == "customer"
    # persisted so the source is queryable, without rewriting the name
    update_kwargs = table.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":s"] == "customer"
    assert ":n" not in update_kwargs["ExpressionAttributeValues"]


# ---------------------------------------------------------------------------
# fallback: billing name via the legacy inline charges list
# ---------------------------------------------------------------------------

def test_fallback_to_billing_name_from_inline_charge(monkeypatch):
    db = _db_order()  # no customerName
    pi = _pi(charges=[_charge(name="  John   Doe  ")])

    resp, table, sns = _run(monkeypatch, pi, db)

    msg = _published(sns)
    assert msg["customerName"] == "John Doe"           # sanitized
    assert msg["customerNameSource"] == "stripe"
    update_kwargs = table.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":n"] == "John Doe"
    assert update_kwargs["ExpressionAttributeValues"][":s"] == "stripe"


# ---------------------------------------------------------------------------
# fallback: charges empty → retrieve with latest_charge expanded
# ---------------------------------------------------------------------------

def test_fallback_retrieves_latest_charge_when_charges_empty(monkeypatch):
    db = _db_order()
    pi = _pi(charges=[])  # modern API: inline list empty
    retrieve = _retrieve_returning(_charge(name="Wallet User", brand="amex", last4="0005"))

    resp, table, sns = _run(monkeypatch, pi, db, retrieve=retrieve)

    retrieve.assert_called_once()
    assert retrieve.call_args.args[0] == "pi_123"
    assert retrieve.call_args.kwargs.get("expand") == ["latest_charge"]
    msg = _published(sns)
    assert msg["customerName"] == "Wallet User"
    assert msg["customerNameSource"] == "stripe"
    # paymentDetails repaired from the retrieved charge too
    assert msg["paymentDetails"]["brand"] == "amex"
    assert msg["paymentDetails"]["last4"] == "0005"


# ---------------------------------------------------------------------------
# no name available anywhere → order still flows, no name attrs
# ---------------------------------------------------------------------------

def test_no_name_anywhere_still_publishes(monkeypatch):
    db = _db_order()
    pi = _pi(charges=[_charge(name=None)])

    resp, table, sns = _run(monkeypatch, pi, db)

    assert resp["statusCode"] == 200
    msg = _published(sns)
    assert "customerName" not in msg
    assert "customerNameSource" not in msg
    assert not table.update_item.called  # nothing to enrich
    assert sns.publish.called


# ---------------------------------------------------------------------------
# fail open: a Stripe error during lookup must not stop the SNS publish
# ---------------------------------------------------------------------------

def test_stripe_lookup_failure_fails_open(monkeypatch):
    db = _db_order()
    pi = _pi(charges=[])  # forces a retrieve...

    def _boom(*a, **k):
        raise RuntimeError("stripe is down")

    resp, table, sns = _run(monkeypatch, pi, db, retrieve=_boom)

    assert resp["statusCode"] == 200          # order still acknowledged
    assert sns.publish.called                 # and still reaches the kitchen
    msg = _published(sns)
    assert msg["paymentStatus"] == "PAID"
    assert "customerName" not in msg          # no name, but no crash


def test_persist_failure_does_not_break_publish(monkeypatch):
    db = _db_order(customerName="Ken")
    pi = _pi(charges=[_charge(name=None)])

    raising_table = MagicMock()
    raising_table.update_item.side_effect = RuntimeError("dynamo down")

    resp, table, sns = _run(monkeypatch, pi, db, fake_table=raising_table)

    assert resp["statusCode"] == 200          # write-back failure is swallowed
    assert table.update_item.called           # we did attempt the enrichment
    assert sns.publish.called                 # order still reaches the kitchen
    assert _published(sns)["customerName"] == "Ken"


# ---------------------------------------------------------------------------
# Stripe secrets come from SSM, fetched once per container
# ---------------------------------------------------------------------------

def test_stripe_secrets_loaded_from_ssm_once(monkeypatch):
    monkeypatch.setattr(api, "_webhook_secret", None)
    monkeypatch.setattr(api.stripe, "api_key", None)
    values = {api.STRIPE_KEY_PARAM: "sk_from_ssm", api.WEBHOOK_SECRET_PARAM: "whsec_from_ssm"}
    fake_ssm = MagicMock()
    fake_ssm.get_parameter.side_effect = lambda Name, WithDecryption: {"Parameter": {"Value": values[Name]}}
    monkeypatch.setattr(api, "_ssm", fake_ssm)

    assert api._load_stripe_secrets() == "whsec_from_ssm"
    assert api._load_stripe_secrets() == "whsec_from_ssm"

    assert api.stripe.api_key == "sk_from_ssm"
    assert fake_ssm.get_parameter.call_count == 2  # one per param, not per call
