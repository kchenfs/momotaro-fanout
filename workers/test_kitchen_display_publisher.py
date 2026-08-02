import json
from unittest.mock import patch

import kitchen_display_publisher


def _sns_event(order_data):
    return {"Records": [{"Sns": {"Message": json.dumps(order_data)}}]}


@patch("kitchen_display_publisher.iot_client")
def test_forwards_orderDate_to_kitchen_payload(mock_iot_client, monkeypatch):
    monkeypatch.setattr(kitchen_display_publisher, "PRINTER_TOPIC", "printers/orders/print")
    order_data = {
        "orderId": "TKOT-AB3XZ",
        "tableId": "Takeout",
        "orderType": "takeout",
        "notes": "",
        "items": [],
        "total": 21.98,
        "orderDate": "2026-08-01T18:42:00-04:00",
    }

    kitchen_display_publisher.handler(_sns_event(order_data), None)

    mock_iot_client.publish.assert_called_once()
    published_payload = json.loads(mock_iot_client.publish.call_args.kwargs["payload"])
    assert published_payload["orderDate"] == "2026-08-01T18:42:00-04:00"


@patch("kitchen_display_publisher.iot_client")
def test_orderDate_defaults_to_none_when_missing(mock_iot_client, monkeypatch):
    monkeypatch.setattr(kitchen_display_publisher, "PRINTER_TOPIC", "printers/orders/print")
    order_data = {
        "orderId": "T1",
        "orderType": "dine-in",
        "items": [],
        "total": 0,
    }

    kitchen_display_publisher.handler(_sns_event(order_data), None)

    published_payload = json.loads(mock_iot_client.publish.call_args.kwargs["payload"])
    assert published_payload["orderDate"] is None
