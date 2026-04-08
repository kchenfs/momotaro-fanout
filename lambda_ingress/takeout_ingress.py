import json
import os
import boto3
import stripe
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal

sns_client = boto3.client('sns')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE_NAME'))

SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY') # <--- Changed this! 
webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')

# Helper to serialize DynamoDB Decimals back to floats for SNS JSON
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

def handler(event, context):
    print(f"RAW EVENT: {json.dumps(event)}")
    try:
        payload = event.get('body') or ''
        headers = event.get('headers') or {}
        sig_header = headers.get('stripe-signature') or headers.get('Stripe-Signature') or ''

        stripe_event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=webhook_secret)

        if stripe_event['type'] == 'payment_intent.succeeded':
            payment_intent = stripe_event['data']['object'].to_dict()
            metadata = payment_intent.get('metadata', {})
            order_id = metadata.get('order_id', 'UNKNOWN')
            
            # 1. READ THE FULL ORDER FROM DYNAMODB
            response = table.get_item(Key={'orderId': order_id})
            db_order = response.get('Item')
            
            if not db_order:
                print(f"CRITICAL ERROR: Order {order_id} paid but not found in DB!")
                return {'statusCode': 200, 'body': 'Acknowledged but DB record missing'}

            # 2. Extract new Stripe data
            charges_data = payment_intent.get('charges', {}).get('data', [])
            card_info = {}
            
            if charges_data:
                charge = charges_data[0] # Grab the first charge in the list
                pm_details = charge.get('payment_method_details', {})
                card_info = pm_details.get('card', {})

            # 3. MERGE DB Data with Payment Data
            db_order['paymentStatus'] = 'PAID'
            db_order['orderStatus'] = 'PENDING_KITCHEN' # Now it's ready to cook!
            db_order['paymentId'] = payment_intent.get('id')
            db_order['paymentDetails'] = {
                'brand': card_info.get('brand', 'card'),
                'last4': card_info.get('last4', ''),
                'wallet': card_info.get('wallet', {}).get('type') if card_info.get('wallet') else None
            }

            # 4. Fan-out the fully hydrated order to SNS
            sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Message=json.dumps(db_order, cls=DecimalEncoder),
                MessageAttributes={
                    'orderType': {'DataType': 'String', 'StringValue': 'takeout'}
                }
            )

        return {'statusCode': 200, 'body': json.dumps({'message': 'Webhook received'})}

    except stripe.error.SignatureVerificationError:
        return {'statusCode': 400, 'body': json.dumps('Invalid signature')}
    except Exception as e:
        print(traceback.format_exc())
        return {'statusCode': 500, 'body': json.dumps('Internal Server Error')}