import json
import os
import re
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

# Non-whitespace control characters to strip from names. Kept identical to
# checkout_api.sanitize_customer_name so both ends agree on what a name is.
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

def sanitize_customer_name(raw):
    """Normalize a name (from Stripe billing) for storage and printing.

    Collapses whitespace, strips control characters, truncates to 40 chars.
    Returns None for anything empty or unusable — never ''. Must never raise:
    a bad name may not block a paid order from reaching the kitchen.
    """
    if raw is None:
        return None
    try:
        text = ' '.join(str(raw).split())
        text = _CONTROL_CHARS_RE.sub('', text)
        return text[:40] or None
    except Exception:
        return None

def _resolve_charge(payment_intent):
    """Return the charge dict for a succeeded PaymentIntent.

    Stripe removed the inline `charges` list from the PaymentIntent object in
    API version 2022-11-15 in favour of `latest_charge`. Try the legacy inline
    path first (still populated on older pinned versions); when it is empty,
    retrieve the intent with `latest_charge` expanded. Returns {} if no charge
    can be resolved.
    """
    charges_data = payment_intent.get('charges', {}).get('data', [])
    if charges_data:
        return charges_data[0]

    pi_id = payment_intent.get('id')
    if not pi_id:
        return {}
    refreshed = stripe.PaymentIntent.retrieve(pi_id, expand=['latest_charge'])
    latest = getattr(refreshed, 'latest_charge', None)
    if not latest or isinstance(latest, str):
        return {}
    return latest.to_dict() if hasattr(latest, 'to_dict') else dict(latest)

def _persist_name_enrichment(order_id, customer_name, name_source):
    """Write the pickup name + its source back to DynamoDB so the fallback
    rate (customerNameSource) is queryable later. Fail-open: a failed write
    must never stop the order from reaching the kitchen.
    """
    try:
        expr = 'SET customerNameSource = :s'
        values = {':s': name_source}
        if name_source == 'stripe' and customer_name:
            expr += ', customerName = :n'
            values[':n'] = customer_name
        table.update_item(
            Key={'orderId': order_id},
            UpdateExpression=expr,
            ExpressionAttributeValues=values,
        )
    except Exception:
        print(f"WARN: could not persist name enrichment for {order_id}")
        print(traceback.format_exc())

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

            # 2. Resolve the charge. This repairs paymentDetails (brand/last4
            # are silently blank on modern API versions where `charges` is
            # empty) AND enables the billing-name fallback below. A name/charge
            # lookup must never block a paid order, so fail open to {}.
            charge = {}
            try:
                charge = _resolve_charge(payment_intent)
            except Exception:
                print(f"WARN: charge lookup failed for {order_id}; proceeding without it")
                print(traceback.format_exc())

            card_info = charge.get('payment_method_details', {}).get('card', {})

            # 3. MERGE DB Data with Payment Data
            db_order['paymentStatus'] = 'PAID'
            db_order['orderStatus'] = 'PENDING_KITCHEN' # Now it's ready to cook!
            db_order['paymentId'] = payment_intent.get('id')
            db_order['paymentDetails'] = {
                'brand': card_info.get('brand', 'card'),
                'last4': card_info.get('last4', ''),
                'wallet': card_info.get('wallet', {}).get('type') if card_info.get('wallet') else None
            }

            # 3b. Pickup name. Prefer the customer-typed name already on the
            # order; otherwise fall back to the Stripe cardholder billing name.
            # customerNameSource is diagnostic only (never printed) so the
            # fallback rate is measurable.
            if db_order.get('customerName'):
                name_source = 'customer'
            else:
                billing_name = sanitize_customer_name(
                    charge.get('billing_details', {}).get('name'))
                if billing_name:
                    db_order['customerName'] = billing_name
                    name_source = 'stripe'
                else:
                    name_source = None

            if name_source:
                db_order['customerNameSource'] = name_source
                _persist_name_enrichment(order_id, db_order.get('customerName'), name_source)

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