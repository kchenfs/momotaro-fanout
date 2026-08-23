import json
import sys
import os
from unittest.mock import MagicMock

# Make the Task 1 layer's module importable for this test, mirroring how the
# real Lambda has it available once the layer is attached (Task 3).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "email_lib", "python"))

import order_confirmation_api as api


def _order_item():
    return {
        "orderId": "TKOT-ABCDE", "orderType": "takeout", "total": "22.60",
        "items": [{"name": "Green Dragon Roll", "quantity": 1, "price": "13.99"}],
        "orderDate": "2026-07-30T18:00:00-04:00",
    }


def _event(body):
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
