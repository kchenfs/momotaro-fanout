import json
import os
import re
import boto3
from email_receipt import send_receipt_email

sns_client = boto3.client('sns')
dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME')
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '*')
# SNS SMS is stuck in the account's SMS Sandbox (only a pre-verified test number
# can receive texts), so every real customer send fails. Off by default until
# the account clears the sandbox — flip via the Lambda's SMS_NOTIFICATIONS_ENABLED
# env var, no code change needed to re-enable.
SMS_NOTIFICATIONS_ENABLED = os.environ.get('SMS_NOTIFICATIONS_ENABLED', 'false').lower() == 'true'

_PHONE_DIGITS_RE = re.compile(r'\D')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

RESTAURANT_ADDRESS = "2911 Dundas St West"
RESTAURANT_PHONE = "416-766-2888"


def _cors_headers():
    return {
        'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
        'Access-Control-Allow-Methods': 'POST,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Content-Type': 'application/json',
    }


def _resp(status, body):
    return {'statusCode': status, 'headers': _cors_headers(), 'body': json.dumps(body)}


def _normalize_phone(raw):
    """Returns an E.164 NANP number, or None if raw doesn't look like one."""
    digits = _PHONE_DIGITS_RE.sub('', raw or '')
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith('1'):
        return f"+{digits}"
    return None


def handler(event, context):
    if event.get('httpMethod') == 'OPTIONS':
        return {'statusCode': 200, 'headers': _cors_headers(), 'body': ''}

    try:
        body = json.loads(event.get('body') or '{}')
    except json.JSONDecodeError:
        return _resp(400, {'error': 'Invalid JSON body'})

    order_id = body.get('orderId')
    if not order_id:
        return _resp(400, {'error': 'orderId is required'})

    raw_phone = body.get('phone')
    raw_email = body.get('email')

    if not raw_phone and not raw_email:
        return _resp(200, {'success': True, 'smsSent': False, 'emailSent': False})

    if table is None:
        return _resp(500, {'error': 'Server misconfigured'})

    order = table.get_item(Key={'orderId': order_id}).get('Item')
    if not order:
        return _resp(404, {'error': 'Order not found'})

    sms_sent = False
    email_sent = False
    errors = []

    if raw_phone and not SMS_NOTIFICATIONS_ENABLED:
        print(f"SMS skipped for {order_id}: SMS_NOTIFICATIONS_ENABLED is off")
    elif raw_phone:
        phone_e164 = _normalize_phone(raw_phone)
        if not phone_e164:
            errors.append('Invalid phone number')
        else:
            try:
                table.update_item(
                    Key={'orderId': order_id},
                    UpdateExpression='SET phoneNumber = :p',
                    ExpressionAttributeValues={':p': phone_e164},
                )
                total = float(order.get('total', 0))
                message = (
                    f"Momotaro Sushi: Order #{order_id} (${total:.2f}) is confirmed! "
                    f"We're preparing it now. Pickup: {RESTAURANT_ADDRESS}. "
                    f"Questions? {RESTAURANT_PHONE}."
                )
                sns_client.publish(
                    PhoneNumber=phone_e164,
                    Message=message,
                    MessageAttributes={
                        'AWS.SNS.SMS.SMSType': {'DataType': 'String', 'StringValue': 'Transactional'},
                    },
                )
                sms_sent = True
            except Exception as e:
                print(f"SMS send failed for {order_id}: {e}")
                errors.append('SMS failed to send')

    if raw_email:
        if not _EMAIL_RE.match(raw_email):
            errors.append('Invalid email address')
        else:
            try:
                table.update_item(
                    Key={'orderId': order_id},
                    UpdateExpression='SET receiptEmail = :e',
                    ExpressionAttributeValues={':e': raw_email},
                )
                send_receipt_email(order, raw_email, SENDER_EMAIL)
                email_sent = True
            except Exception as e:
                print(f"Email send failed for {order_id}: {e}")
                errors.append('Email failed to send')

    return _resp(200, {'success': True, 'smsSent': sms_sent, 'emailSent': email_sent, 'errors': errors})
