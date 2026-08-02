import json
import os
import boto3
import uuid
import time
from datetime import datetime
from opentelemetry import metrics

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

        order_id = body.get('order_id') or f"DINE-{uuid.uuid4().hex[:6].upper()}"
        
        order_event = {
            'orderId': order_id,
            'orderType': 'dine-in',
            'tableId': str(body.get('table', 'Unknown')),
            'total': float(body.get('total', 0)),
            'notes': body.get('notes', ''),
            'items': body.get('items', []),
            'orderStatus': 'PENDING_KITCHEN',
            'paymentStatus': 'Dine-In',
            'orderDate': body.get('orderDate', datetime.now().isoformat())
        }

        # Track SNS publish latency separately
        sns_start = time.time()
        sns_client.publish(
            TopicArn=TOPIC_ARN,
            Message=json.dumps(order_event),
            MessageAttributes={
                'orderType': {'DataType': 'String', 'StringValue': 'dine-in'}
            }
        )
        sns_publish_histogram.record(
            (time.time() - sns_start) * 1000,
            {"topic": "order_events"}
        )

        # Record custom metrics
        order_counter.add(1, {"orderType": "dine-in", "table": str(body.get('table', 'unknown'))})
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
                'orderId': order_id
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
            'body': json.dumps({'error': 'Internal Server Error', 'details': str(e)})
        }