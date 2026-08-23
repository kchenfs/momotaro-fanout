import json
import os
import boto3
import stripe
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal

stripe.api_key = os.environ.get('STRIPE_CHECKOUT_API_KEY')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE_NAME'))

def float_to_decimal(payload):
    return json.loads(json.dumps(payload), parse_float=Decimal)

def handler(event, context):
    print(f"Received event: {json.dumps(event)}")

    if event.get('httpMethod') == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST,OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type,Authorization'
            },
            'body': ''
        }

    try:
        body_raw = event.get('body')
        if not body_raw:
            return {'statusCode': 400, 'body': json.dumps({'error': 'Empty body'})}
            
        body = json.loads(body_raw)
        amount = int(body.get('amount', 0))
        
        # --- THE GHOST ORDER FIX ---
        metadata = body.get('metadata')
        
        # 1. If no metadata, it's just React warming up the UI. 
        # Generate a Stripe Intent to render the modal, but DO NOT write to the DB.
        if not metadata:
            intent = stripe.PaymentIntent.create(
                amount=amount,
                currency='cad',
                payment_method_types=['card']
            )
            return {
                'statusCode': 200,
                'headers': { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/json' },
                'body': json.dumps({'clientSecret': intent.client_secret})
            }

        # 2. If we reach here, the user clicked "Pay". This is the REAL order.
        frontend_order_id = metadata.get('order_id', 'UNKNOWN')
        order_id = f"TKOT-{frontend_order_id}"
        
        items = body.get('items', [])
        notes = body.get('notes', '')
        customer_email = body.get('customerDetails', {}).get('email')

        # 3. Write the confirmed order to DynamoDB
        eastern_time = datetime.now(ZoneInfo("America/Toronto"))
        order_item = float_to_decimal({
            'orderId': order_id,
            'orderType': 'takeout',
            'orderStatus': 'PENDING_PAYMENT', 
            'paymentStatus': 'UNPAID',
            'items': items,
            'notes': notes,
            'total': amount / 100,
            'orderDate': eastern_time.isoformat(),
            'receiptEmail': customer_email
        })
        
        table.put_item(Item=order_item)

        # 4. Create the final Stripe Intent linked to our DB record
        # receipt_email: Stripe sends its own receipt email directly (not through
        # our SES setup) once the payment succeeds. In live mode this is sent
        # regardless of Dashboard email settings, so it works today without
        # waiting on SES production access.
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency='cad',
            payment_method_types=['card'],
            metadata={'order_id': order_id},
            description=f"Momotaro Sushi Takeout — Order {order_id}",
            receipt_email=customer_email
        )

        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': '*',
                'Content-Type': 'application/json'
            },
            'body': json.dumps({'clientSecret': intent.client_secret, 'orderId': order_id})
        }

    except Exception as e:
        print("!!! CHECKOUT API ERROR !!!")
        print(traceback.format_exc()) 
        return {'statusCode': 500, 'headers': { 'Access-Control-Allow-Origin': '*' }, 'body': json.dumps({'error': str(e)})}