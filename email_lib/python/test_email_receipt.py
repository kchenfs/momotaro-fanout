from decimal import Decimal
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


def _render(order, monkeypatch):
    """Renders the receipt and returns its HTML without touching SES."""
    fake_ses = MagicMock()
    fake_ses.send_email.return_value = {"MessageId": "msg-123"}
    monkeypatch.setattr(email_receipt, "_ses_client", lambda: fake_ses)
    email_receipt.send_receipt_email(order, "customer@example.com", "orders@momotarosushi.ca")
    return fake_ses.send_email.call_args.kwargs["Message"]["Body"]["Html"]["Data"]


def test_multi_quantity_line_shows_line_total_not_unit_price(monkeypatch):
    order = {
        "orderId": "TKOT-QTY",
        "orderDate": "2026-08-02T18:17:22-04:00",
        "total": 11.30,  # 10.00 subtotal + 13% tax
        "items": [{"name": "Miso Soup", "quantity": 2, "price": 2.50, "subtotal": 5.00},
                  {"name": "Edamame", "quantity": 1, "price": 5.00, "subtotal": 5.00}],
    }
    html = _render(order, monkeypatch)

    # The 2x line costs $5.00, not the $2.50 unit price — showing the unit price
    # makes the line items visibly fail to add up to the total.
    assert "CA$5.00" in html
    assert "CA$2.50" not in html


def test_handles_decimal_values_from_dynamodb(monkeypatch):
    # boto3 returns every DynamoDB number as a Decimal, and Python refuses to
    # multiply float by Decimal. Rendering must survive an order read straight
    # from the table, including an item with no stored 'subtotal'.
    order = {
        "orderId": "TKOT-DEC",
        "orderDate": "2026-08-02T20:50:00-04:00",
        "total": Decimal("2.26"),
        "items": [
            {"name": "Wasabi", "quantity": Decimal("1"), "price": Decimal("1.00")},
            {"name": "Ginger", "quantity": Decimal("1"), "price": Decimal("1.00"),
             "subtotal": Decimal("1.00")},
        ],
    }
    html = _render(order, monkeypatch)

    assert "Wasabi" in html and "Ginger" in html
    assert "CA$1.00" in html
    assert "__" not in html


def test_line_total_falls_back_to_price_times_quantity(monkeypatch):
    # Older orders predate the 'subtotal' field being stored per item.
    order = {
        "orderId": "TKOT-OLD",
        "orderDate": "2026-08-02T18:17:22-04:00",
        "total": 11.30,
        "items": [{"name": "Miso Soup", "quantity": 2, "price": 2.50}],
    }
    html = _render(order, monkeypatch)

    assert "CA$5.00" in html


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


def test_tipped_order_shows_tip_row_and_stored_split(monkeypatch):
    order = {
        "orderId": "TKOT-TIP",
        "orderDate": "2026-10-10T18:00:00-04:00",
        "subtotal": Decimal("20"), "tax": Decimal("2.6"), "tip": Decimal("3"),
        "total": Decimal("25.6"),
        "items": [{"name": "Chirashi", "quantity": 1, "price": 20.00, "subtotal": 20.00}],
    }
    html = _render(order, monkeypatch)

    assert ">Tip<" in html and "CA$3.00" in html
    assert "CA$20.00" in html   # subtotal is not inflated by the tip
    assert "CA$2.60" in html    # tax is on food only
    assert "CA$25.60" in html
    assert "__" not in html


def test_untipped_order_has_no_tip_row(monkeypatch):
    order = {
        "orderId": "TKOT-NOTIP",
        "orderDate": "2026-10-10T18:00:00-04:00",
        "total": 11.30,
        "items": [{"name": "Miso Soup", "quantity": 2, "price": 2.50, "subtotal": 5.00}],
    }
    html = _render(order, monkeypatch)

    assert ">Tip<" not in html
    assert "__" not in html
