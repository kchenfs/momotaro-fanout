from unittest.mock import MagicMock
import email_receipt


def _sample_order():
    return {
        "orderId": "TKOT-ABCDE",
        "orderDate": "2026-07-30T18:00:00-04:00",
        "total": 22.60,
        "items": [
            {"name": "Green Dragon Roll", "quantity": 1, "price": 13.99},
            {"name": "Green Tea", "quantity": 1, "price": 2.00},
        ],
        "paymentDetails": {"brand": "visa", "last4": "4242"},
    }


def test_generate_payment_chip_visa():
    chip = email_receipt.generate_payment_chip({"brand": "visa", "last4": "4242"})
    assert "VISA" in chip
    assert "4242" in chip


def test_generate_payment_chip_apple_pay():
    chip = email_receipt.generate_payment_chip({"wallet": "apple_pay"})
    assert "Apple Pay" in chip


def test_send_receipt_email_calls_ses_with_populated_template(monkeypatch):
    fake_ses = MagicMock()
    fake_ses.send_email.return_value = {"MessageId": "msg-123"}
    monkeypatch.setattr(email_receipt, "_ses_client", lambda: fake_ses)

    message_id = email_receipt.send_receipt_email(
        _sample_order(), "customer@example.com", "orders@momotarosushi.ca")

    assert message_id == "msg-123"
    assert fake_ses.send_email.called
    call_kwargs = fake_ses.send_email.call_args.kwargs
    assert call_kwargs["Source"] == "orders@momotarosushi.ca"
    assert call_kwargs["Destination"] == {"ToAddresses": ["customer@example.com"]}
    html_body = call_kwargs["Message"]["Body"]["Html"]["Data"]
    assert "TKOT-ABCDE" in html_body
    assert "Green Dragon Roll" in html_body
    assert "__" not in html_body  # no placeholder tokens left unreplaced
