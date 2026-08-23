import json
import sys
import os
from unittest.mock import MagicMock

# Make the Task 1 layer's module importable for this test, mirroring how the
# real Lambda has it available once the layer is attached (Task 3).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "email_lib", "python"))
os.environ.setdefault("CONFIRMATION_TOKEN_SECRET", "test-secret")

import order_confirmation_api as api
from confirmation_token import make_confirmation_token


def _order_item():
    return {
        "orderId": "TKOT-ABCDE", "orderType": "takeout", "total": "22.60",
        "items": [{"name": "Green Dragon Roll", "quantity": 1, "price": "13.99"}],
        "orderDate": "2026-07-30T18:00:00-04:00",
    }


def _event(body):
    # Inject a valid confirmation token for the body's orderId unless the test
    # set 'token' explicitly (so missing/invalid-token cases can be exercised).
    body = dict(body)
    if "token" not in body and body.get("orderId"):
        body["token"] = make_confirmation_token(body["orderId"])
    return {"httpMethod": "POST", "body": json.dumps(body)}


def test_missing_order_id_returns_400():
    resp = api.handler(_event({}), None)
    assert resp["statusCode"] == 400


def test_order_not_found_returns_404(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {}
    monkeypatch.setattr(api, "table", fake_table)

    resp = api.handler(_event({"orderId": "TKOT-NOPE", "phone": "4165551234"}), None)
    assert resp["statusCode"] == 404


def test_no_contact_info_is_a_no_op(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(api, "table", fake_table)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body == {"success": True, "smsSent": False, "emailSent": False}
    assert not fake_table.get_item.called  # no order lookup needed if nothing to send


def test_phone_skipped_when_sms_disabled(monkeypatch):
    """SMS_NOTIFICATIONS_ENABLED defaults to off (SNS SMS Sandbox rejects every
    real customer number), so a phone should be a silent no-op, not an error."""
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    assert api.SMS_NOTIFICATIONS_ENABLED is False  # the actual default, not just this test's setup

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "phone": "416-555-1234"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["smsSent"] is False
    assert body["errors"] == []
    assert not fake_sns.publish.called
    assert not fake_table.update_item.called


def test_valid_phone_sends_sms_and_updates_order(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    monkeypatch.setattr(api, "SMS_NOTIFICATIONS_ENABLED", True)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "phone": "416-555-1234"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["smsSent"] is True
    assert fake_sns.publish.called
    publish_kwargs = fake_sns.publish.call_args.kwargs
    assert publish_kwargs["PhoneNumber"] == "+14165551234"
    assert "TKOT-ABCDE" in publish_kwargs["Message"]
    assert "22.60" in publish_kwargs["Message"]

    update_kwargs = fake_table.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":p"] == "+14165551234"


def test_invalid_phone_reports_error_without_crashing(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    monkeypatch.setattr(api, "SMS_NOTIFICATIONS_ENABLED", True)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "phone": "123"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["smsSent"] is False
    assert "Invalid phone number" in body["errors"]
    assert not fake_sns.publish.called


def test_valid_email_sends_receipt_and_updates_order(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    monkeypatch.setattr(api, "send_receipt_email", lambda order, email, sender: "msg-1")

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "email": "customer@example.com"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["emailSent"] is True
    update_kwargs = fake_table.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":e"] == "customer@example.com"


def test_invalid_email_reports_error_without_crashing(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "email": "not-an-email"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["emailSent"] is False
    assert "Invalid email address" in body["errors"]


def test_email_send_failure_does_not_fail_the_whole_request(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)

    def _boom(order, email, sender):
        raise RuntimeError("SES is down")
    monkeypatch.setattr(api, "send_receipt_email", _boom)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "email": "customer@example.com"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200  # still a success response overall
    assert body["emailSent"] is False
    assert "Email failed to send" in body["errors"]


def test_both_phone_and_email_sends_both(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    monkeypatch.setattr(api, "send_receipt_email", lambda order, email, sender: "msg-1")
    monkeypatch.setattr(api, "SMS_NOTIFICATIONS_ENABLED", True)

    resp = api.handler(_event({
        "orderId": "TKOT-ABCDE", "phone": "4165551234", "email": "customer@example.com",
    }), None)
    body = json.loads(resp["body"])

    assert body["smsSent"] is True
    assert body["emailSent"] is True


# ---------------------------------------------------------------------------
# #3 IDOR — a send/update requires a valid confirmation token
# ---------------------------------------------------------------------------

def test_missing_token_is_rejected(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    monkeypatch.setattr(api, "SMS_NOTIFICATIONS_ENABLED", True)

    # token=None keeps _event from injecting a valid one
    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "phone": "4165551234", "token": None}), None)

    assert resp["statusCode"] == 403
    assert not fake_table.get_item.called   # rejected before any lookup
    assert not fake_sns.publish.called


def test_wrong_token_is_rejected(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    monkeypatch.setattr(api, "SMS_NOTIFICATIONS_ENABLED", True)

    resp = api.handler(_event(
        {"orderId": "TKOT-ABCDE", "phone": "4165551234", "token": "deadbeef"}), None)

    assert resp["statusCode"] == 403
    assert not fake_sns.publish.called


def test_token_for_a_different_order_is_rejected(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(api, "table", fake_table)
    monkeypatch.setattr(api, "sns_client", MagicMock())
    monkeypatch.setattr(api, "SMS_NOTIFICATIONS_ENABLED", True)

    # A valid token, but minted for someone else's order id.
    other = make_confirmation_token("TKOT-OTHER")
    resp = api.handler(_event(
        {"orderId": "TKOT-ABCDE", "phone": "4165551234", "token": other}), None)

    assert resp["statusCode"] == 403


def test_refuses_to_overwrite_existing_phone(monkeypatch):
    # Even with a valid token, a request must not reassign a number already on
    # file to a different one.
    item = _order_item()
    item["phoneNumber"] = "+14160000000"
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": item}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    monkeypatch.setattr(api, "SMS_NOTIFICATIONS_ENABLED", True)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "phone": "4165551234"}), None)
    body = json.loads(resp["body"])

    assert body["smsSent"] is False
    assert "already on file" in " ".join(body["errors"]).lower()
    assert not fake_sns.publish.called
    assert not fake_table.update_item.called
