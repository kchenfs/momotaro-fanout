import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import menu_api


@pytest.fixture
def menu(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(menu_api, "menu_table", table)
    return table


def _get(origin="https://take-out.momotarosushi.ca", method="GET"):
    return {"httpMethod": method, "headers": {"origin": origin} if origin else {}}


def test_projection_passes_sold_out_and_dine_in_fields_through():
    for name in ("SoldOut", "DineInPrice", "HiddenOnDineIn", "ImageUrl"):
        assert name in menu_api.PROJECTION


def test_returns_items_sorted_by_item_number_across_pages(menu):
    menu.scan.side_effect = [
        {"Items": [{"ItemNumber": Decimal("12"), "ItemName": "Spider Roll", "Price": "14.5", "SoldOut": True}],
         "LastEvaluatedKey": {"ItemName": "Spider Roll"}},
        {"Items": [{"ItemNumber": Decimal("3"), "ItemName": "Gyoza", "Price": Decimal("7.5")}]},
    ]
    resp = menu_api.handler(_get(), None)
    assert resp["statusCode"] == 200
    assert resp["headers"]["Access-Control-Allow-Origin"] == "https://take-out.momotarosushi.ca"
    body = json.loads(resp["body"])
    assert [i["ItemName"] for i in body] == ["Gyoza", "Spider Roll"]
    assert body[0]["Price"] == 7.5
    assert body[1]["SoldOut"] is True
    assert menu.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"ItemName": "Spider Roll"}


def test_unknown_origin_is_refused(menu):
    resp = menu_api.handler(_get("https://evil.example"), None)
    assert resp["statusCode"] == 403
    menu.scan.assert_not_called()


def test_no_origin_is_allowed_without_cors_header(menu):
    menu.scan.return_value = {"Items": []}
    resp = menu_api.handler(_get(origin=None), None)
    assert resp["statusCode"] == 200
    assert "Access-Control-Allow-Origin" not in resp["headers"]


def test_preflight_skips_the_scan(menu):
    resp = menu_api.handler(_get(method="OPTIONS"), None)
    assert resp["statusCode"] == 200
    menu.scan.assert_not_called()


def test_scan_failure_does_not_leak_the_error(menu):
    menu.scan.side_effect = RuntimeError("AccessDenied arn:aws:...")
    resp = menu_api.handler(_get(), None)
    assert resp["statusCode"] == 500
    assert "arn" not in resp["body"]
