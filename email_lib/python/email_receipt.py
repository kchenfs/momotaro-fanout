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
        # Show what the LINE cost, not the unit price: a "2x Miso Soup" row
        # priced at the $2.50 unit price makes the items visibly fail to add up
        # to the total. 'subtotal' is stored per item at checkout; fall back to
        # price * quantity for orders written before that field existed.
        # Coerce to float BEFORE arithmetic: boto3 hands back DynamoDB numbers as
        # Decimal, and float * Decimal raises TypeError. qty itself is left alone
        # so a Decimal('1') still renders as "1x", not "1.0x".
        line_total = item.get('subtotal')
        if line_total is None:
            line_total = float(item.get('price', 0)) * float(qty)
        line_total = float(line_total)
        items_html_rows.append(f"""
        <tr>
            <td style="padding: 12px 0; font-size: 14px; color: #0f172a; border-bottom: 1px solid #f1f5f9;">
                {qty}x {name}
            </td>
            <td style="padding: 12px 0; font-size: 14px; color: #0f172a; font-weight: 500; text-align: right; border-bottom: 1px solid #f1f5f9;">
                CA${line_total:.2f}
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
