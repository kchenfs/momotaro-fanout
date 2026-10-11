import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import dine_in_ingress as ingress
import menu_pricing

MENU = [
    {"ItemNumber": Decimal("101"), "ItemName": "Spicy Tuna Roll", "Price": "8.50", "Location": "sushi"},
    {"ItemNumber": Decimal("202"), "ItemName": "Gyoza", "Price": "7.00", "DineInPrice": "6.50", "Location": "kitchen",
     "Options": [{"name": "Filling", "items": [
         {"name": "Pork", "priceModifier": 0},
         {"name": "Beef", "priceModifier": 2, "dineInPriceModifier": 1.5},
     ]}]},
]


def _line(item_id, quantity=1, options="", price=0.01, name="whatever"):
    return {"id": item_id, "name": name, "price": price, "quantity": quantity,
            "subtotal": price, "location": "spoofed", "options": options}


def _event(**overrides):
    body = {"items": [_line("101", 2)], "total": 1.0, "order_id": "AB1CD",
            "table": "table-7", "notes": "no wasabi", "orderType": "dine-in"}
    body.update(overrides)
    return {"httpMethod": "POST", "body": json.dumps(body)}


def _conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    menu = MagicMock()
    menu.scan.return_value = {"Items": MENU}
    orders = MagicMock()
    sns = MagicMock()
    monkeypatch.setattr(ingress, "menu_table", menu)
    monkeypatch.setattr(ingress, "orders_table", orders)
    monkeypatch.setattr(ingress, "sns_client", sns)
    monkeypatch.setattr(ingress, "TABLE_COUNT", 11)
    monkeypatch.setattr(ingress, "ENFORCE_STORE_HOURS", False)
    monkeypatch.setitem(menu_pricing._cache, "lookup", None)
    return {"menu": menu, "orders": orders, "sns": sns}


def _published(fakes):
    assert fakes["sns"].publish.call_count == 1
    return json.loads(fakes["sns"].publish.call_args.kwargs["Message"])


def test_prices_come_from_the_menu_not_the_browser(fakes):
    resp = ingress.handler(_event(items=[_line("101", 2), _line("202", 1, "Filling: Beef")]), None)
    assert resp["statusCode"] == 200
    sent = _published(fakes)
    tuna, gyoza = sent["items"]
    assert (tuna["price"], tuna["subtotal"], tuna["name"], tuna["location"]) == (8.5, 17.0, "Spicy Tuna Roll", "sushi")
    # Dine-in price 6.50 plus the dine-in Beef modifier 1.50.
    assert (gyoza["price"], gyoza["options"]) == (8.0, "Filling: Beef")
    # (17.00 + 8.00) * 1.13
    assert sent["total"] == 28.25
    assert json.loads(resp["body"])["total"] == 28.25


def test_unknown_item_is_refused(fakes):
    resp = ingress.handler(_event(items=[_line("999")]), None)
    assert resp["statusCode"] == 400
    fakes["sns"].publish.assert_not_called()


@pytest.mark.parametrize("quantity", [0, -1, 51, "lots"])
def test_bad_quantity_is_refused(fakes, quantity):
    resp = ingress.handler(_event(items=[_line("101", quantity)]), None)
    assert resp["statusCode"] == 400
    fakes["sns"].publish.assert_not_called()


@pytest.mark.parametrize("table", ["table-12", "table-0", "table-e2e", "Table 7", "", None, "table-7; drop"])
def test_tables_that_do_not_exist_are_refused_in_prod(fakes, table):
    resp = ingress.handler(_event(table=table), None)
    assert resp["statusCode"] == 400
    fakes["sns"].publish.assert_not_called()


def test_staging_accepts_test_tables(fakes, monkeypatch):
    monkeypatch.setattr(ingress, "TABLE_COUNT", 0)
    assert ingress.handler(_event(table="table-e2e"), None)["statusCode"] == 200
    assert ingress.handler(_event(table="table-99", order_id="ZZ9ZZ"), None)["statusCode"] == 200
    assert ingress.handler(_event(table="not-a-table", order_id="YY9YY"), None)["statusCode"] == 400


def test_closed_hours_are_refused_when_enforced(fakes, monkeypatch):
    monkeypatch.setattr(ingress, "ENFORCE_STORE_HOURS", True)
    monkeypatch.setattr(ingress.store_hours, "is_open", lambda now=None: False)
    resp = ingress.handler(_event(), None)
    assert resp["statusCode"] == 409
    assert "stopped taking orders" in json.loads(resp["body"])["error"]
    fakes["sns"].publish.assert_not_called()


def test_retry_of_the_same_order_does_not_reach_the_kitchen_twice(fakes):
    fakes["orders"].put_item.side_effect = _conditional_failure()
    fakes["orders"].get_item.return_value = {"Item": {"orderId": "AB1CD", "tableId": "table-7"}}
    resp = ingress.handler(_event(), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["orderId"] == "AB1CD"
    fakes["sns"].publish.assert_not_called()


def test_id_used_by_another_table_asks_for_a_new_id(fakes):
    fakes["orders"].put_item.side_effect = _conditional_failure()
    fakes["orders"].get_item.return_value = {"Item": {"orderId": "AB1CD", "tableId": "table-3"}}
    resp = ingress.handler(_event(), None)
    assert resp["statusCode"] == 409
    assert json.loads(resp["body"])["retryWithNewId"] is True
    fakes["sns"].publish.assert_not_called()


def test_new_order_is_claimed_before_the_kitchen_hears_of_it(fakes):
    assert ingress.handler(_event(), None)["statusCode"] == 200
    put = fakes["orders"].put_item.call_args.kwargs
    assert put["ConditionExpression"] == "attribute_not_exists(orderId)"
    assert put["Item"]["orderId"] == "AB1CD"
    assert put["Item"]["total"] == Decimal("19.21")


def test_failed_publish_releases_the_order_id(fakes):
    fakes["sns"].publish.side_effect = RuntimeError("sns down")
    resp = ingress.handler(_event(), None)
    assert resp["statusCode"] == 500
    fakes["orders"].delete_item.assert_called_once_with(Key={"orderId": "AB1CD"})


def test_malformed_order_id_gets_a_server_id(fakes):
    ingress.handler(_event(order_id="<script>"), None)
    assert _published(fakes)["orderId"].startswith("DINE-")


def test_menu_is_scanned_once_per_cache_window(fakes):
    ingress.handler(_event(), None)
    ingress.handler(_event(order_id="EF2GH"), None)
    assert fakes["menu"].scan.call_count == 1


def test_resolve_price_falls_back_to_takeout():
    assert menu_pricing.resolve_price("7.00", None) == Decimal("7.00")
    assert menu_pricing.resolve_price("7.00", "") == Decimal("7.00")
    assert menu_pricing.resolve_price("7.00", "abc") == Decimal("7.00")
    assert menu_pricing.resolve_price("7.00", "6.5") == Decimal("6.5")
    assert menu_pricing.resolve_price(None, None) == Decimal("0")
