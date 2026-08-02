# Order Confirmation API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new endpoint that lets a customer, from the post-payment confirmation page, attach a phone number and/or email to their already-placed takeout order and immediately receive an SMS (via AWS SNS) and/or a branded receipt email (via SES) confirming it.

**Architecture:** A new `ingress/order_confirmation_api.py` Lambda behind its own API Gateway, following the exact pattern of the existing `checkout_api`/`takeout_ingress`/`dine_in_ingress` Lambdas in this CDK stack. It reads/writes the existing orders DynamoDB table directly (no SNS involved — the order has already been fanned out by the time this runs, so there's nothing to re-publish to). Along the way, this plan extracts the email-composition logic that `receipt_email_sender.py` already has into a shared Lambda Layer so both it and the new endpoint call the same code — which also fixes a live bug: `receipt_email_sender.py` currently looks for a file called `email_template.html`, but the actual file on disk is `emailtemplate.html` (no underscore), so every automatic receipt-email attempt has been silently failing.

**Tech Stack:** Python 3.12, AWS CDK (Python), boto3, pytest.

## Global Constraints

- Repo: `C:\Users\Ken\momotaro-fanout`. This repo currently has an **uncommitted, staged rename** (`lambda_ingress/` → `ingress/`) already present in the working tree — do not touch, unstage, or commit over those changes; only `git add` the specific new/modified files this plan creates.
- Orders table name comes from the `DYNAMODB_TABLE_NAME` env var (already set on every other Lambda in this stack via `.env`). Partition key is `orderId` (string).
- SES sender address comes from the `VERIFIED_SENDER_EMAIL` env var (already used by `receipt_email_sender`).
- No changes to the SNS `order_events` topic, its existing subscribers (`dine_in_order_writer`, `takeout_payment_updater`, `kitchen_display_publisher`, `receipt_email_sender`), or the existing ingress Lambdas (`checkout_api`, `takeout_ingress`, `dine_in_ingress`) beyond what's explicitly stated below.
- SMS body must stay short and factual: order id, total, "confirmed", pickup address (`2911 Dundas St West`), and the restaurant phone (`416-766-2888`) — no marketing content (this is a transactional message).
- Phone numbers are assumed Canadian/NANP (10 digits, or 11 with a leading `1`); anything else is treated as invalid rather than guessed at.
- No authentication on this endpoint — order IDs are random and not enumerable, matching the existing posture of the read-only order-status endpoint built in the `TableAI` repo. This endpoint can only attach contact info and trigger a courtesy message; it exposes no payment, item, or existing-contact data in its response.
- A failure to send SMS or email must never surface as an overall request failure — the order itself already succeeded before this endpoint is ever called.

---

## Task 1: Shared email-sending module (Lambda Layer) — and fix the template filename bug

**Files:**
- Create: `email_lib/python/email_receipt.py`
- Create: `email_lib/python/emailtemplate.html` (moved from `workers/emailtemplate.html`)
- Create: `email_lib/python/test_email_receipt.py`
- Modify: `workers/receipt_email_sender.py`
- Modify: `momotaro_fanout/momotaro_fanout_stack.py`

**Interfaces:**
- Produces: `email_receipt.generate_payment_chip(details: dict) -> str` — unchanged from the existing function of the same name in `receipt_email_sender.py`.
- Produces: `email_receipt.send_receipt_email(order_data: dict, recipient_email: str, sender_email: str) -> str` — builds and sends the branded HTML receipt via SES; returns the SES `MessageId`. Raises on any failure (SES error, template read error) — callers decide how to handle that.
- Consumes: `boto3` (a `ses` client is created inside the function so tests can monkeypatch it), and the co-located `emailtemplate.html`.

- [ ] **Step 1: Write the failing test**

Create `email_lib/python/test_email_receipt.py`:
```python
from unittest.mock import MagicMock
import email_receipt


def _sample_order():
    return {
        "orderId": "TKOT-ABCDE",
        "orderDate": "2026-07-30T18:00:00-04:00",
        "total": 22.60,
        "items": [
            {"name": "Green Dragon Roll", "quantity": 1, "price": 13.99},
            {"name": "Green Tea", "quantity": 1, "price": 2.00},
        ],
        "paymentDetails": {"brand": "visa", "last4": "4242"},
    }


def test_generate_payment_chip_visa():
    chip = email_receipt.generate_payment_chip({"brand": "visa", "last4": "4242"})
    assert "VISA" in chip
    assert "4242" in chip


def test_generate_payment_chip_apple_pay():
    chip = email_receipt.generate_payment_chip({"wallet": "apple_pay"})
    assert "Apple Pay" in chip


def test_send_receipt_email_calls_ses_with_populated_template(monkeypatch):
    fake_ses = MagicMock()
    fake_ses.send_email.return_value = {"MessageId": "msg-123"}
    monkeypatch.setattr(email_receipt, "_ses_client", lambda: fake_ses)

    message_id = email_receipt.send_receipt_email(
        _sample_order(), "customer@example.com", "orders@momotarosushi.ca")

    assert message_id == "msg-123"
    assert fake_ses.send_email.called
    call_kwargs = fake_ses.send_email.call_args.kwargs
    assert call_kwargs["Source"] == "orders@momotarosushi.ca"
    assert call_kwargs["Destination"] == {"ToAddresses": ["customer@example.com"]}
    html_body = call_kwargs["Message"]["Body"]["Html"]["Data"]
    assert "TKOT-ABCDE" in html_body
    assert "Green Dragon Roll" in html_body
    assert "__" not in html_body  # no placeholder tokens left unreplaced
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd "C:\Users\Ken\momotaro-fanout\email_lib\python" && py -3 -m pytest test_email_receipt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'email_receipt'`.

- [ ] **Step 3: Move the template file into the new layer directory**

Copy `workers/emailtemplate.html` to `email_lib/python/emailtemplate.html` (keep the original in `workers/` for now — Task 1 Step 5 removes it once `receipt_email_sender.py` no longer needs its own copy).

- [ ] **Step 4: Write `email_receipt.py`**

Create `email_lib/python/email_receipt.py` — this is `receipt_email_sender.py`'s existing `generate_payment_chip` function verbatim, plus the email-composition/send logic extracted from its handler (steps 1-6), reshaped into a reusable function, with the **corrected** template filename:
```python
import os
import boto3


def _ses_client():
    return boto3.client('ses', region_name='ca-central-1')


def generate_payment_chip(details):
    """
    Generates a styled HTML 'chip' for the payment method based on
    brand (Visa, Mastercard, etc.) or digital wallet (Apple Pay).
    """
    brand = str(details.get('brand', 'card')).lower()
    last4 = details.get('last4', '')
    wallet = details.get('wallet')

    base_style = (
        "display: inline-block; border-radius: 9999px; padding: 4px 12px; "
        "font-size: 11px; font-weight: 600; color: #ffffff; "
        "font-family: 'Inter', sans-serif; text-transform: uppercase; letter-spacing: 0.05em;"
    )

    if wallet == 'apple_pay':
        return f'<div style="{base_style} background-color: #000000;">Apple Pay</div>'
    if wallet == 'google_pay':
        return f'<div style="{base_style} background-color: #3c4043;">Google Pay</div>'

    if brand == 'visa':
        return f'<div style="{base_style} background-color: #2563eb;">VISA •••• {last4}</div>'
    elif brand == 'mastercard':
        return f'<div style="{base_style} background-color: #ea580c;">Mastercard •••• {last4}</div>'
    elif brand == 'amex':
        return f'<div style="{base_style} background-color: #0284c7;">AMEX •••• {last4}</div>'

    return f'<div style="{base_style} background-color: #64748b;">Card •••• {last4}</div>'


def send_receipt_email(order_data, recipient_email, sender_email):
    """Builds the branded HTML receipt for order_data and sends it via SES to
    recipient_email. Returns the SES MessageId. Raises on failure — the caller
    decides how to handle a send failure (it must never block the order itself)."""
    # NOTE: the actual file on disk is "emailtemplate.html" (no underscore).
    # A previous version of this logic looked for "email_template.html" and
    # silently failed every single send (caught by a broad except upstream).
    template_path = os.path.join(os.path.dirname(__file__), "emailtemplate.html")
    with open(template_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    items_html_rows = []
    for item in order_data.get('items', []):
        name = item.get('name', 'Item')
        qty = item.get('quantity', 1)
        price = float(item.get('price', 0))
        items_html_rows.append(f"""
        <tr>
            <td style="padding: 12px 0; font-size: 14px; color: #0f172a; border-bottom: 1px solid #f1f5f9;">
                {qty}x {name}
            </td>
            <td style="padding: 12px 0; font-size: 14px; color: #0f172a; font-weight: 500; text-align: right; border-bottom: 1px solid #f1f5f9;">
                CA${price:.2f}
            </td>
        </tr>
        """)
    final_items_html = "".join(items_html_rows)

    total = float(order_data.get('total', 0))
    subtotal = total / 1.13
    tax = total - subtotal

    payment_details = order_data.get('paymentDetails', {}) or {}
    chip_html = generate_payment_chip(payment_details)

    html_body = html_content
    html_body = html_body.replace('__RECEIPT_ID_PLACEHOLDER__', str(order_data.get('orderId', 'N/A')))
    html_body = html_body.replace('__AMOUNT_PLACEHOLDER__', f"CA${total:.2f}")
    html_body = html_body.replace('__DATE_PLACEHOLDER__', order_data.get('orderDate', 'N/A')[:10])
    html_body = html_body.replace('__ITEMS_LIST_PLACEHOLDER__', final_items_html)
    html_body = html_body.replace('__SUBTOTAL_PLACEHOLDER__', f"CA${subtotal:.2f}")
    html_body = html_body.replace('__TAX_PLACEHOLDER__', f"CA${tax:.2f}")
    html_body = html_body.replace('__PAYMENT_METHOD_CHIPS_PLACEHOLDER__', chip_html)

    response = _ses_client().send_email(
        Source=sender_email,
        Destination={'ToAddresses': [recipient_email]},
        Message={
            'Subject': {
                'Data': f"Receipt: Your Momotaro Sushi Order [{order_data.get('orderId')}]",
                'Charset': 'UTF-8',
            },
            'Body': {'Html': {'Data': html_body, 'Charset': 'UTF-8'}},
        },
    )
    return response['MessageId']
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd "C:\Users\Ken\momotaro-fanout\email_lib\python" && py -3 -m pytest test_email_receipt.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 6: Update `receipt_email_sender.py` to use the shared module and delete its own copy of the logic**

Replace the entire contents of `workers/receipt_email_sender.py` with:
```python
import json
import os
from email_receipt import send_receipt_email

SENDER_EMAIL = os.environ.get('SENDER_EMAIL')


def handler(event, context):
    try:
        for record in event.get('Records', []):
            sns_message = record['Sns']['Message']
            order_data = json.loads(sns_message)

            recipient = order_data.get('receiptEmail')
            if not recipient:
                print(f"Skipping SES: No recipient email for order {order_data.get('orderId')}")
                continue

            message_id = send_receipt_email(order_data, recipient, SENDER_EMAIL)
            print(f"Sent branded receipt to {recipient}. MessageId: {message_id}")

        return {'statusCode': 200}

    except Exception as e:
        print(f"CRITICAL ERROR in Email Worker: {str(e)}")
        # Returning 200 to SNS to prevent infinite retry loops on formatting bugs
        return {'statusCode': 200}
```

Delete `workers/emailtemplate.html` (its content now lives at `email_lib/python/emailtemplate.html`, which `receipt_email_sender` reaches via the layer once wired up in Task 3 — until Task 3 attaches the layer, this Lambda's code technically won't run correctly, but that's fine since Task 3 wires the layer in the same deploy).

- [ ] **Step 7: Commit**

```bash
cd "C:\Users\Ken\momotaro-fanout"
git add email_lib/python/email_receipt.py email_lib/python/emailtemplate.html email_lib/python/test_email_receipt.py workers/receipt_email_sender.py
git rm workers/emailtemplate.html
git commit -m "refactor: extract email-sending into a shared layer, fix emailtemplate.html filename bug"
```

---

## Task 2: Order confirmation API Lambda

**Files:**
- Create: `ingress/order_confirmation_api.py`
- Create: `ingress/test_order_confirmation_api.py`

**Interfaces:**
- Consumes: `email_receipt.send_receipt_email` (from Task 1's layer, importable once Task 3 attaches the layer to this function — for unit testing in this task, the layer's directory is added to `sys.path` directly, see Step 1).
- Produces: `handler(event, context)` — API Gateway (Lambda proxy integration) handler. `POST` body `{"orderId": str, "phone": str?, "email": str?}` → `200 {"success": true, "smsSent": bool, "emailSent": bool, "errors": [str]}`, `404` if the order doesn't exist, `400` for a malformed body.

- [ ] **Step 1: Write the failing tests**

Create `ingress/test_order_confirmation_api.py`:
```python
import json
import sys
import os
from unittest.mock import MagicMock

# Make the Task 1 layer's module importable for this test, mirroring how the
# real Lambda has it available once the layer is attached (Task 3).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "email_lib", "python"))

import order_confirmation_api as api


def _order_item():
    return {
        "orderId": "TKOT-ABCDE", "orderType": "takeout", "total": "22.60",
        "items": [{"name": "Green Dragon Roll", "quantity": 1, "price": "13.99"}],
        "orderDate": "2026-07-30T18:00:00-04:00",
    }


def _event(body):
    return {"httpMethod": "POST", "body": json.dumps(body)}


def test_missing_order_id_returns_400():
    resp = api.handler(_event({}), None)
    assert resp["statusCode"] == 400


def test_order_not_found_returns_404(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {}
    monkeypatch.setattr(api, "table", fake_table)

    resp = api.handler(_event({"orderId": "TKOT-NOPE", "phone": "4165551234"}), None)
    assert resp["statusCode"] == 404


def test_no_contact_info_is_a_no_op(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(api, "table", fake_table)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body == {"success": True, "smsSent": False, "emailSent": False}
    assert not fake_table.get_item.called  # no order lookup needed if nothing to send


def test_valid_phone_sends_sms_and_updates_order(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "phone": "416-555-1234"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["smsSent"] is True
    assert fake_sns.publish.called
    publish_kwargs = fake_sns.publish.call_args.kwargs
    assert publish_kwargs["PhoneNumber"] == "+14165551234"
    assert "TKOT-ABCDE" in publish_kwargs["Message"]
    assert "22.60" in publish_kwargs["Message"]

    update_kwargs = fake_table.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":p"] == "+14165551234"


def test_invalid_phone_reports_error_without_crashing(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "phone": "123"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["smsSent"] is False
    assert "Invalid phone number" in body["errors"]
    assert not fake_sns.publish.called


def test_valid_email_sends_receipt_and_updates_order(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    monkeypatch.setattr(api, "send_receipt_email", lambda order, email, sender: "msg-1")

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "email": "customer@example.com"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["emailSent"] is True
    update_kwargs = fake_table.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":e"] == "customer@example.com"


def test_invalid_email_reports_error_without_crashing(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "email": "not-an-email"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200
    assert body["emailSent"] is False
    assert "Invalid email address" in body["errors"]


def test_email_send_failure_does_not_fail_the_whole_request(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)

    def _boom(order, email, sender):
        raise RuntimeError("SES is down")
    monkeypatch.setattr(api, "send_receipt_email", _boom)

    resp = api.handler(_event({"orderId": "TKOT-ABCDE", "email": "customer@example.com"}), None)
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 200  # still a success response overall
    assert body["emailSent"] is False
    assert "Email failed to send" in body["errors"]


def test_both_phone_and_email_sends_both(monkeypatch):
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": _order_item()}
    monkeypatch.setattr(api, "table", fake_table)
    fake_sns = MagicMock()
    monkeypatch.setattr(api, "sns_client", fake_sns)
    monkeypatch.setattr(api, "send_receipt_email", lambda order, email, sender: "msg-1")

    resp = api.handler(_event({
        "orderId": "TKOT-ABCDE", "phone": "4165551234", "email": "customer@example.com",
    }), None)
    body = json.loads(resp["body"])

    assert body["smsSent"] is True
    assert body["emailSent"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd "C:\Users\Ken\momotaro-fanout\ingress" && py -3 -m pytest test_order_confirmation_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'order_confirmation_api'`.

- [ ] **Step 3: Write `order_confirmation_api.py`**

Create `ingress/order_confirmation_api.py`:
```python
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

    if raw_phone:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd "C:\Users\Ken\momotaro-fanout\ingress" && py -3 -m pytest test_order_confirmation_api.py -v`
Expected: PASS — 9 passed, output pristine.

- [ ] **Step 5: Commit**

```bash
cd "C:\Users\Ken\momotaro-fanout"
git add ingress/order_confirmation_api.py ingress/test_order_confirmation_api.py
git commit -m "feat: order confirmation API (SMS via SNS + email via shared layer)"
```

---

## Task 3: Wire into the CDK stack, deploy, and verify

**Files:**
- Modify: `momotaro_fanout/momotaro_fanout_stack.py`

**Interfaces:**
- Produces: a deployed `OrderConfirmationApi` Lambda + its own `apigw.LambdaRestApi`, plus an `EmailReceiptLayer` Lambda Layer attached to both it and the (modified) `receipt_email_sender`.
- Consumes: `order_events_topic`, `DYNAMODB_TABLE_NAME`, `VERIFIED_SENDER_EMAIL` (all already defined in this stack).

- [ ] **Step 1: Add the shared email layer**

In `momotaro_fanout/momotaro_fanout_stack.py`, near the existing `stripe_layer` definition (inside section `5. THE PUBLISHERS (INGRESS APIS)`, right before `takeout_ingress` is defined — this layer needs to exist before both `receipt_email_sender` (section 4) and the new Lambda reference it, so define it right after the ADOT layer setup at the top of `__init__`, alongside `adot_layer`):
```python
        email_receipt_layer = _lambda.LayerVersion(self, "EmailReceiptLayer",
            code=_lambda.Code.from_asset("email_lib"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Shared branded-receipt email composition + SES send logic"
        )
```

- [ ] **Step 2: Attach the layer to `receipt_email_sender` and give it the fixed asset reference**

Find the existing `receipt_email_sender` Lambda definition:
```python
        receipt_email_sender = _lambda.Function(self, "ReceiptEmailSender",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="receipt_email_sender.handler",
            code=_lambda.Code.from_asset("workers"),
            layers=[adot_layer],
            environment={
                **otel_env_vars,
                "SENDER_EMAIL": VERIFIED_SENDER_EMAIL
            }
        )
```
Change its `layers` line to include the new layer:
```python
            layers=[adot_layer, email_receipt_layer],
```
(No other change to this function — it already reads `workers/receipt_email_sender.py`, which Task 1 updated to `import email_receipt` from the layer.)

- [ ] **Step 3: Add the new Order Confirmation API Lambda + API Gateway**

Add this new block in section `5. THE PUBLISHERS (INGRESS APIS)`, after the existing `checkout_api` block (same section, same style — code asset `"ingress"`, own IAM role via `add_to_role_policy`, own `apigw.LambdaRestApi`):
```python
        order_confirmation_api = _lambda.Function(self, "OrderConfirmationApi",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="order_confirmation_api.handler",
            code=_lambda.Code.from_asset("ingress"),
            layers=[email_receipt_layer, adot_layer],
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                **otel_env_vars,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME,
                "SENDER_EMAIL": VERIFIED_SENDER_EMAIL,
                "ALLOWED_ORIGIN": "*",
            }
        )
        attach_adot_policy(order_confirmation_api)
        order_confirmation_api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))
        order_confirmation_api.add_to_role_policy(iam.PolicyStatement(
            actions=["sns:Publish"],
            resources=["*"]  # SNS SMS publish targets a phone number, not a topic ARN
        ))
        order_confirmation_api.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=["*"]
        ))
        apigw.LambdaRestApi(self, "OrderConfirmationApiEndpoint", handler=order_confirmation_api)
```

- [ ] **Step 4: Synthesize to catch structural errors before deploying**

Run: `cd "C:\Users\Ken\momotaro-fanout" && py -3 -m pip install -r requirements.txt -q && cdk synth MomotaroFanoutStack > /dev/null`
Expected: no errors (a CloudFormation template prints to `cdk.out/`; the `> /dev/null` just suppresses the large template text — a nonzero exit code or a Python traceback means something is structurally wrong).

- [ ] **Step 5: Deploy**

Run: `cd "C:\Users\Ken\momotaro-fanout" && cdk deploy MomotaroFanoutStack`
Review the diff CDK prints (expect: 1 new Lambda Layer, 1 new Lambda function + role + 3 policy statements, 1 new API Gateway + deployment, 1 changed Lambda — `receipt_email_sender`'s layer list). Confirm the prompt to apply.

- [ ] **Step 6: Note the new endpoint URL**

After deploy, `cdk deploy` prints the new `OrderConfirmationApiEndpoint`'s invoke URL (e.g. `https://xxxxxxxxxx.execute-api.ca-central-1.amazonaws.com/prod/`). Record this — the frontend plan needs it as `NEXT_PUBLIC_ORDER_CONFIRMATION_API`.

- [ ] **Step 7: Request an SNS SMS spending quota increase if needed**

Check the current limit: `aws service-quotas get-service-quota --service-code sns --quota-code L-BCD4AAF3 --region ca-central-1 --query "Quota.Value" --output text` (this is the "SNS SMS monthly spend limit" quota). If it's at or near the AWS default (often as low as $1.00), request an increase: `aws service-quotas request-service-quota-increase --service-code sns --quota-code L-BCD4AAF3 --desired-value 25 --region ca-central-1` (adjust `--desired-value` in USD/month to a level comfortable for expected order volume — $25/month covers roughly 4,000+ transactional texts at AWS's per-message SMS rate to Canada). This is usually auto-approved within minutes to a day; verify with `aws service-quotas get-requested-service-quota-change --request-id <id-from-previous-command> --region ca-central-1`.

- [ ] **Step 8: Verify end-to-end with a real order**

Using an order id that actually exists in the orders table (e.g. from a recent real or test order), send:
```bash
curl -X POST "<OrderConfirmationApiEndpoint-URL>" -H "Content-Type: application/json" -d '{"orderId":"<REAL-ORDER-ID>","phone":"<YOUR-OWN-PHONE>","email":"<YOUR-OWN-EMAIL>"}'
```
Expected: `{"success": true, "smsSent": true, "emailSent": true, "errors": []}`, a real text arrives on the given phone within a few seconds, and a branded receipt email arrives (confirming the `emailtemplate.html` filename fix actually works end-to-end, not just in the unit test).
