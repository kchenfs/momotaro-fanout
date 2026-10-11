import json
import os
import re
import boto3
import uuid
import time
from datetime import datetime
from decimal import Decimal
from botocore.exceptions import ClientError
from opentelemetry import metrics

import menu_pricing
import store_hours

# The ADOT layer sets up the MeterProvider automatically via /opt/otel-instrument.
# You just need to get a meter and define your instruments at module level.
meter = metrics.get_meter("dine_in_ingress")

order_counter = meter.create_counter(
    name="dine_in.orders.total",
    description="Total number of dine-in orders received",
    unit="1"
)
latency_histogram = meter.create_histogram(
    name="dine_in.handler.duration_ms",
    description="End-to-end handler latency",
    unit="ms"
)
sns_publish_histogram = meter.create_histogram(
    name="dine_in.sns_publish.duration_ms",
    description="Time spent publishing to SNS",
    unit="ms"
)

# Initialize SNS client
sns_client = boto3.client('sns')
TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME')
orders_table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None
menu_table = dynamodb.Table('MomotaroSushiMenu_DB')

# Prod refuses orders outside the hours in store_hours.py and from tables that
# don't exist. Staging leaves both off so its order tests (tables like
# "table-e2e" and "table-99") run at any hour.
ENFORCE_STORE_HOURS = os.environ.get('ENFORCE_STORE_HOURS') == '1'
TABLE_COUNT = int(os.environ.get('DINE_IN_TABLE_COUNT') or 0)

_TABLE_RE = re.compile(r'^table-([A-Za-z0-9]{1,12})$')
_ORDER_ID_RE = re.compile(r'^[A-Za-z0-9_-]{4,40}$')


def _response(status, body):
    return {
        'statusCode': status,
        'headers': {'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json'},
        'body': json.dumps(body),
    }


def valid_table(table_id):
    """'table-7' style ids only; in prod also 1..DINE_IN_TABLE_COUNT."""
    match = _TABLE_RE.match(table_id or '')
    if not match:
        return False
    if not TABLE_COUNT:
        return True
    number = match.group(1)
    return number.isdigit() and 1 <= int(number) <= TABLE_COUNT


def _claim_order_id(order_event):
    """Write the order before telling the kitchen, so a retried request with
    the same id can't print a second ticket.

    Returns 'new', 'duplicate' (same id from the same table: the earlier
    request already went through) or 'taken' (the id belongs to another order).
    """
    if orders_table is None:
        return 'new'
    item = json.loads(json.dumps(order_event), parse_float=Decimal)
    try:
        orders_table.put_item(Item=item, ConditionExpression='attribute_not_exists(orderId)')
        return 'new'
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
            raise
    existing = orders_table.get_item(Key={'orderId': order_event['orderId']}, ConsistentRead=True).get('Item') or {}
    if existing.get('tableId') == order_event['tableId']:
        return 'duplicate'
    return 'taken'

def handler(event, context):
    start_time = time.time()
    print(f"Received event: {json.dumps(event)}")

    if event.get('httpMethod') == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST,OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type,X-Api-Key',
                'Access-Control-Max-Age': '86400'
            },
            'body': ''
        }

    try:
        body_raw = event.get('body')
        if not body_raw:
            return {
                'statusCode': 400,
                'headers': { 'Access-Control-Allow-Origin': '*' },
                'body': json.dumps({'error': 'Empty request body'})
            }
            
        body = json.loads(body_raw)

        table_id = str(body.get('table') or '')
        if not valid_table(table_id):
            return _response(400, {'error': "We couldn't tell which table you're at. Please scan the QR code on your table again."})

        if ENFORCE_STORE_HOURS and not store_hours.is_open():
            return _response(409, {'error': "We've stopped taking orders for tonight. Please ask your server."})

        try:
            items, subtotal, total = menu_pricing.price_dine_in_items(
                body.get('items'), menu_pricing.menu_lookup(menu_table))
        except ValueError as e:
            return _response(400, {'error': f"{e}. Please refresh the menu and try again."})

        client_total = body.get('total')
        try:
            if client_total is not None and abs(float(client_total) - float(total)) > 0.01:
                print(f"Dine-in total mismatch on {table_id}: browser {client_total}, menu {total}")
        except (TypeError, ValueError):
            pass

        order_id = str(body.get('order_id') or '')
        if not _ORDER_ID_RE.match(order_id):
            order_id = f"DINE-{uuid.uuid4().hex[:6].upper()}"

        order_event = {
            'orderId': order_id,
            'orderType': 'dine-in',
            'tableId': table_id,
            'total': float(total),
            'notes': str(body.get('notes') or '')[:500],
            'items': items,
            'orderStatus': 'PENDING_KITCHEN',
            'paymentStatus': 'Dine-In',
            'orderDate': body.get('orderDate', datetime.now().isoformat())
        }

        claim = _claim_order_id(order_event)
        if claim == 'duplicate':
            print(f"Duplicate dine-in request for {order_id}; already sent to the kitchen")
            return _response(200, {'message': 'Order already received', 'orderId': order_id, 'total': float(total)})
        if claim == 'taken':
            return _response(409, {'error': 'Please tap Send to Kitchen again.', 'retryWithNewId': True})

        # Track SNS publish latency separately
        sns_start = time.time()
        try:
            sns_client.publish(
                TopicArn=TOPIC_ARN,
                Message=json.dumps(order_event),
                MessageAttributes={
                    'orderType': {'DataType': 'String', 'StringValue': 'dine-in'}
                }
            )
        except Exception:
            # Release the id so the customer's retry isn't mistaken for a
            # duplicate of an order the kitchen never got.
            if orders_table is not None:
                orders_table.delete_item(Key={'orderId': order_id})
            raise
        sns_publish_histogram.record(
            (time.time() - sns_start) * 1000,
            {"topic": "order_events"}
        )

        # Record custom metrics
        order_counter.add(1, {"orderType": "dine-in", "table": table_id})
        latency_histogram.record(
            (time.time() - start_time) * 1000,
            {"status": "success"}
        )

        return {
            'statusCode': 200,
            'headers': { 
                'Access-Control-Allow-Origin': '*',
                'Content-Type': 'application/json'
            },
            'body': json.dumps({
                'message': 'Order successfully routed to kitchen',
                'orderId': order_id,
                'total': float(total)
            })
        }

    except Exception as e:
        latency_histogram.record(
            (time.time() - start_time) * 1000,
            {"status": "error"}
        )
        print(f"Error processing dine-in order: {str(e)}")
        return {
            'statusCode': 500,
            'headers': { 'Access-Control-Allow-Origin': '*' },
            'body': json.dumps({'error': 'Internal Server Error'})
        }