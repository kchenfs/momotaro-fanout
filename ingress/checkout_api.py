import json
import os
import re
import boto3
import stripe
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from botocore.exceptions import ClientError
from confirmation_token import make_confirmation_token

# Stripe key lives in an SSM SecureString param, never in a Lambda env var.
# Fetched on the first invocation and cached for the container's lifetime.
_ssm = boto3.client('ssm')
STRIPE_KEY_PARAM = os.environ.get('STRIPE_KEY_PARAM', '/momotaro/prod/STRIPE_SECRET_KEY')


def _load_stripe_key():
    if not stripe.api_key:
        stripe.api_key = _ssm.get_parameter(Name=STRIPE_KEY_PARAM, WithDecryption=True)['Parameter']['Value']

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE_NAME'))
menu_table = dynamodb.Table('MomotaroSushiMenu_DB')

TAX_RATE = Decimal('0.13')
MAX_ITEMS = 20
# Restrict browser callers to the storefront origin instead of '*'. The real
# checkout path reaches this lambda server-side via the Next.js proxy (no CORS
# involved), so this only tightens direct-from-browser calls.
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', 'https://take-out.momotarosushi.ca')

# Non-whitespace control characters to strip from client-supplied text.
# Whitespace (\t\n\r etc.) is handled by str.split() below, so it is not
# listed here — that lets "a\nb" collapse to "a b" instead of "ab".
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

def sanitize_customer_name(raw):
    """Normalize a client-supplied pickup name for storage and printing.

    Collapses whitespace, strips control characters, truncates to 40 chars.
    Returns None for anything empty or unusable — never '' — so an absent
    name lets the Stripe billing-name fallback fire downstream. Must never
    raise: a bad name may not block a paid order.
    """
    if raw is None:
        return None
    try:
        text = ' '.join(str(raw).split())          # collapse whitespace + trim
        text = _CONTROL_CHARS_RE.sub('', text)      # drop leftover control chars
        return text[:40] or None
    except Exception:
        return None

def float_to_decimal(payload):
    return json.loads(json.dumps(payload), parse_float=Decimal)

def _menu_lookup_by_item_number():
    """MomotaroSushiMenu_DB's key is ItemName, not ItemNumber, so we scan
    once and index by ItemNumber (the id the frontend actually sends)."""
    lookup = {}
    scan_kwargs = {}
    while True:
        resp = menu_table.scan(**scan_kwargs)
        for menu_item in resp.get('Items', []):
            item_number = menu_item.get('ItemNumber')
            if item_number is not None:
                lookup[str(item_number)] = menu_item
        if 'LastEvaluatedKey' not in resp:
            break
        scan_kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return lookup

def _compute_order_total_cents(items):
    """Recompute the order total from the canonical menu/options prices
    instead of trusting the client-supplied amount."""
    if not items:
        raise ValueError("No items in order")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"Too many items (max {MAX_ITEMS})")

    menu_lookup = _menu_lookup_by_item_number()
    subtotal = Decimal('0')

    for line in items:
        menu_item = menu_lookup.get(str(line.get('id')))
        if not menu_item:
            raise ValueError(f"Unknown menu item id: {line.get('id')}")

        quantity = int(line.get('quantity', 1))
        if quantity < 1 or quantity > 50:
            raise ValueError("Invalid quantity")

        line_price = Decimal(str(menu_item.get('Price', '0')))

        # Options arrive as "Group: OptionName; Group2: OptionName2" —
        # match each selection back against the canonical menu item so
        # the priceModifier always comes from our own data, never the client.
        selected = {}
        for part in (line.get('options') or '').split(';'):
            part = part.strip()
            if ':' in part:
                group_name, option_name = part.split(':', 1)
                selected[group_name.strip()] = option_name.strip()

        for group in menu_item.get('Options', []):
            chosen_name = selected.get(group.get('name'))
            if not chosen_name:
                continue
            for opt in group.get('items', []):
                if opt.get('name') == chosen_name:
                    line_price += Decimal(str(opt.get('priceModifier', 0)))
                    break

        subtotal += line_price * quantity

    total_with_tax = subtotal * (1 + TAX_RATE)
    return int((total_with_tax * 100).to_integral_value(rounding=ROUND_HALF_UP))

def handler(event, context):
    print(f"Received event: {json.dumps(event)}")

    if event.get('httpMethod') == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
                'Access-Control-Allow-Methods': 'POST,OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type,Authorization'
            },
            'body': ''
        }

    try:
        _load_stripe_key()
        body_raw = event.get('body')
        if not body_raw:
            return {'statusCode': 400, 'body': json.dumps({'error': 'Empty body'})}
            
        body = json.loads(body_raw)

        # --- THE GHOST ORDER FIX ---
        metadata = body.get('metadata')

        # 1. If no metadata, it's just React warming up the UI.
        # Generate a Stripe Intent to render the modal, but DO NOT write to the DB.
        # This intent is never the one actually confirmed for payment (see step 4),
        # so a client-supplied amount here has no fraud/order impact.
        if not metadata:
            amount = int(body.get('amount', 0))
            intent = stripe.PaymentIntent.create(
                amount=amount,
                currency='cad',
                payment_method_types=['card']
            )
            return {
                'statusCode': 200,
                'headers': { 'Access-Control-Allow-Origin': ALLOWED_ORIGIN, 'Content-Type': 'application/json' },
                'body': json.dumps({'clientSecret': intent.client_secret})
            }

        # 2. If we reach here, the user clicked "Pay". This is the REAL order.
        frontend_order_id = metadata.get('order_id', 'UNKNOWN')
        order_id = f"TKOT-{frontend_order_id}"
        
        items = body.get('items', [])
        notes = body.get('notes', '')
        customer_email = body.get('customerDetails', {}).get('email')
        customer_name = sanitize_customer_name(body.get('customerName'))

        # Recompute the amount from canonical menu prices — never trust the
        # client-supplied amount for the intent that actually gets charged.
        try:
            amount = _compute_order_total_cents(items)
        except ValueError:
            return {'statusCode': 400, 'headers': { 'Access-Control-Allow-Origin': ALLOWED_ORIGIN }, 'body': json.dumps({'error': 'Invalid order items'})}

        # 3. Write the confirmed order to DynamoDB — reject if this orderId
        # already exists instead of silently overwriting someone else's order.
        eastern_time = datetime.now(ZoneInfo("America/Toronto"))
        order_payload = {
            'orderId': order_id,
            'orderType': 'takeout',
            'orderStatus': 'PENDING_PAYMENT',
            'paymentStatus': 'UNPAID',
            'items': items,
            'notes': notes,
            'total': amount / 100,
            'orderDate': eastern_time.isoformat(),
            'receiptEmail': customer_email
        }
        # Absent means "not collected" — only store the attribute when we
        # actually have a name, never an empty string.
        if customer_name is not None:
            order_payload['customerName'] = customer_name
        order_item = float_to_decimal(order_payload)

        try:
            table.put_item(Item=order_item, ConditionExpression='attribute_not_exists(orderId)')
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return {'statusCode': 409, 'headers': { 'Access-Control-Allow-Origin': ALLOWED_ORIGIN }, 'body': json.dumps({'error': 'Order already exists'})}
            raise

        # 4. Create the final Stripe Intent linked to our DB record
        # receipt_email: Stripe sends its own receipt email directly (not through
        # our SES setup) once the payment succeeds. In live mode this is sent
        # regardless of Dashboard email settings, so it works today without
        # waiting on SES production access.
        # Mirror the name into the intent metadata so it's visible in the
        # Stripe dashboard when reconciling or refunding.
        intent_metadata = {'order_id': order_id}
        if customer_name is not None:
            intent_metadata['customer_name'] = customer_name

        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency='cad',
            payment_method_types=['card'],
            metadata=intent_metadata,
            description=f"Momotaro Sushi Takeout — Order {order_id}",
            receipt_email=customer_email
        )

        # Token proves this caller went through checkout, so the confirmation
        # endpoint can trust a later SMS/email request for this order id.
        return {
            'statusCode': 200,
            'headers': {
                'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
                'Content-Type': 'application/json'
            },
            'body': json.dumps({
                'clientSecret': intent.client_secret,
                'orderId': order_id,
                'confirmationToken': make_confirmation_token(order_id),
            })
        }

    except Exception as e:
        print("!!! CHECKOUT API ERROR !!!")
        print(traceback.format_exc()) 
        return {'statusCode': 500, 'headers': { 'Access-Control-Allow-Origin': ALLOWED_ORIGIN }, 'body': json.dumps({'error': 'Internal server error'})}