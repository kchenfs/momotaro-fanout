"""End-to-end: place a takeout and a dine-in order on staging and follow them
all the way to the kitchen.

Takeout: checkout API -> Stripe test-mode payment -> Stripe webhook ->
TakeoutIngress -> SNS -> order marked PAID + kitchen ticket published.
Dine-in: dine-in API -> SNS -> order written + kitchen ticket published.

Kitchen tickets are captured by a staging-only IoT rule that stores each one
in the orders table as KITCHEN#<order id>.
"""
import json
import time
import urllib.request
import uuid
from decimal import Decimal, ROUND_HALF_UP

import pytest

TIMEOUT_S = 90
POLL_S = 2


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def _wait_for(table, order_id, check, what):
    """Poll the orders table until check(item) is true, or fail with the last item seen."""
    deadline = time.time() + TIMEOUT_S
    item = None
    while time.time() < deadline:
        item = table.get_item(Key={"orderId": order_id}, ConsistentRead=True).get("Item")
        if item and check(item):
            return item
        time.sleep(POLL_S)
    pytest.fail(f"timed out after {TIMEOUT_S}s waiting for {what} ({order_id}); last seen: {item}")


@pytest.fixture
def cleanup(orders_table):
    ids = []
    yield ids
    for order_id in ids:
        for key in (order_id, f"KITCHEN#{order_id}"):
            orders_table.delete_item(Key={"orderId": key})


def _line(menu_item, quantity=2):
    price = float(menu_item["Price"])
    return {
        "id": str(menu_item["ItemNumber"]),
        "name": menu_item.get("ItemName", "E2E item"),
        "price": price,
        "quantity": quantity,
        "subtotal": price * quantity,
        "location": menu_item.get("Location", ""),
        "options": "",
    }


def test_takeout_order_is_paid_and_reaches_kitchen(
        staging, orders_table, stripe_test_mode, stripe_webhook, menu_item, cleanup):
    stripe = stripe_test_mode
    line = _line(menu_item)
    expected_cents = int(
        (Decimal(str(menu_item["Price"])) * line["quantity"] * Decimal("1.13") * 100)
        .to_integral_value(rounding=ROUND_HALF_UP)
    )

    status, body = _post(staging["CheckoutApiUrl"], {
        "amount": 1,  # deliberately wrong: the backend must recompute from the menu
        "items": [line],
        "metadata": {"order_id": f"E2E{uuid.uuid4().hex[:6].upper()}"},
        "notes": "automated end-to-end test",
        "customerName": "E2E Test",
    })
    assert status == 200, body
    order_id = body["orderId"]
    cleanup.append(order_id)
    assert order_id.startswith("TKOT-E2E")

    pending = orders_table.get_item(Key={"orderId": order_id}, ConsistentRead=True)["Item"]
    assert pending["paymentStatus"] == "UNPAID"

    # Pay exactly as the browser would, with Stripe's always-succeeds test card.
    intent_id = body["clientSecret"].split("_secret_")[0]
    intent = stripe.PaymentIntent.confirm(intent_id, payment_method="pm_card_visa")
    assert intent.status == "succeeded"
    assert intent.amount == expected_cents

    paid = _wait_for(
        orders_table, order_id,
        lambda i: i.get("paymentStatus") == "PAID" and i.get("orderStatus") == "PENDING_KITCHEN",
        "the Stripe webhook to mark the order paid",
    )
    assert paid["paymentId"] == intent_id
    assert paid["customerName"] == "E2E Test"

    ticket = _wait_for(orders_table, f"KITCHEN#{order_id}", lambda i: True, "the kitchen ticket")
    assert ticket["order_id"] == order_id
    assert ticket["orderType"] == "takeout"
    assert ticket["customerName"] == "E2E Test"
    assert [i["id"] for i in ticket["items"]] == [line["id"]]


def test_dine_in_order_reaches_kitchen(staging, orders_table, menu_item, cleanup):
    line = _line(menu_item, quantity=1)
    order_id = f"E2E-DINE-{uuid.uuid4().hex[:6].upper()}"
    cleanup.append(order_id)

    status, body = _post(staging["DineInApiUrl"], {
        "items": [line],
        "total": round(line["subtotal"] * 1.13, 2),
        "orderDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "order_id": order_id,
        "notes": "automated end-to-end test",
        "table": "table-e2e",
        "orderType": "dine-in",
    })
    assert status == 200, body
    assert body["orderId"] == order_id

    saved = _wait_for(orders_table, order_id, lambda i: True, "the dine-in order to be saved")
    assert saved["orderType"] == "dine-in"
    assert saved["tableId"] == "table-e2e"
    assert saved["orderStatus"] == "PENDING_KITCHEN"

    ticket = _wait_for(orders_table, f"KITCHEN#{order_id}", lambda i: True, "the kitchen ticket")
    assert ticket["table"] == "table-e2e"
    assert ticket["orderType"] == "dine-in"
