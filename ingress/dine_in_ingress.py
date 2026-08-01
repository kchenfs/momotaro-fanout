import json
import os
import boto3
import uuid
from datetime import datetime

# Initialize SNS client
sns = boto3.client('sns')
TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')

def handler(event, context):
    # 1. LOG THE EVENT - Crucial for cloud debugging
    print(f"Received event: {json.dumps(event)}")

    # 2. THE BOUNCER: Handle CORS Preflight (OPTIONS)
    # Browsers send this first to verify 'x-api-key' is allowed.
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
        # 3. SAFE BODY PARSING
        # Prevents 'NoneType' error if body is missing
        body_raw = event.get('body')
        if not body_raw:
            return {
                'statusCode': 400,
                'headers': { 'Access-Control-Allow-Origin': '*' },
                'body': json.dumps({'error': 'Empty request body'})
            }
            
        body = json.loads(body_raw)

        # 4. MAP DATA TO FAN-OUT SCHEMA
        # We ensure a consistent schema for all workers (DB, Kitchen, Email)
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

        # 5. SNS PUBLISH (FAN-OUT)
        sns.publish(
            TopicArn=TOPIC_ARN,
            Message=json.dumps(order_event),
            MessageAttributes={
                'orderType': {'DataType': 'String', 'StringValue': 'dine-in'}
            }
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
        print(f"Error processing dine-in order: {str(e)}")
        return {
            'statusCode': 500,
            'headers': { 'Access-Control-Allow-Origin': '*' },
            'body': json.dumps({'error': 'Internal Server Error', 'details': str(e)})
        }