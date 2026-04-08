import json
import os
import boto3

# Initialize SES client for the Toronto region
ses_client = boto3.client('ses', region_name='ca-central-1')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')

def generate_payment_chip(details):
    """
    Generates a styled HTML 'chip' for the payment method based on 
    brand (Visa, Mastercard, etc.) or digital wallet (Apple Pay).
    """
    brand = str(details.get('brand', 'card')).lower()
    last4 = details.get('last4', '')
    wallet = details.get('wallet')

    # Base styling for the chip
    base_style = (
        "display: inline-block; border-radius: 9999px; padding: 4px 12px; "
        "font-size: 11px; font-weight: 600; color: #ffffff; "
        "font-family: 'Inter', sans-serif; text-transform: uppercase; letter-spacing: 0.05em;"
    )

    # 1. Digital Wallets (Priority)
    if wallet == 'apple_pay':
        return f'<div style="{base_style} background-color: #000000;">Apple Pay</div>'
    if wallet == 'google_pay':
        return f'<div style="{base_style} background-color: #3c4043;">Google Pay</div>'

    # 2. Card Brands
    if brand == 'visa':
        return f'<div style="{base_style} background-color: #2563eb;">VISA •••• {last4}</div>'
    elif brand == 'mastercard':
        return f'<div style="{base_style} background-color: #ea580c;">Mastercard •••• {last4}</div>'
    elif brand == 'amex':
        return f'<div style="{base_style} background-color: #0284c7;">AMEX •••• {last4}</div>'
    
    # Fallback for generic card
    return f'<div style="{base_style} background-color: #64748b;">Card •••• {last4}</div>'

def handler(event, context):
    try:
        # SNS delivers an array of records
        for record in event.get('Records', []):
            # Unwrap the SNS envelope to get your Momotaro Order JSON
            sns_message = record['Sns']['Message']
            order_data = json.loads(sns_message)
            
            recipient = order_data.get('receiptEmail')
            if not recipient:
                print(f"Skipping SES: No recipient email for order {order_data.get('orderId')}")
                continue

            # 1. LOAD THE HTML TEMPLATE
            # Looks for the file in the same directory as this script
            template_path = os.path.join(os.path.dirname(__file__), "email_template.html")
            with open(template_path, 'r', encoding='utf-8') as f:
                html_content = f.read()

            # 2. GENERATE ITEMIZED SUMMARY HTML
            items_html_rows = []
            items = order_data.get('items', [])
            for item in items:
                name = item.get('name', 'Item')
                qty = item.get('quantity', 1)
                price = float(item.get('price', 0))
                
                row = f"""
                <tr>
                    <td style="padding: 12px 0; font-size: 14px; color: #0f172a; border-bottom: 1px solid #f1f5f9;">
                        {qty}x {name}
                    </td>
                    <td style="padding: 12px 0; font-size: 14px; color: #0f172a; font-weight: 500; text-align: right; border-bottom: 1px solid #f1f5f9;">
                        CA${price:.2f}
                    </td>
                </tr>
                """
                items_html_rows.append(row)
            
            final_items_html = "".join(items_html_rows)

            # 3. FINANCIAL CALCULATIONS (Ontario HST 13%)
            total = float(order_data.get('total', 0))
            subtotal = total / 1.13
            tax = total - subtotal

            # 4. GENERATE THE PAYMENT METHOD CHIP
            payment_details = order_data.get('paymentDetails', {})
            chip_html = generate_payment_chip(payment_details)

            # 5. REPLACE PLACEHOLDERS
            html_body = html_content
            html_body = html_body.replace('__RECEIPT_ID_PLACEHOLDER__', str(order_data.get('orderId', 'N/A')))
            html_body = html_body.replace('__AMOUNT_PLACEHOLDER__', f"CA${total:.2f}")
            html_body = html_body.replace('__DATE_PLACEHOLDER__', order_data.get('orderDate', 'N/A')[:10])
            html_body = html_body.replace('__ITEMS_LIST_PLACEHOLDER__', final_items_html)
            html_body = html_body.replace('__SUBTOTAL_PLACEHOLDER__', f"CA${subtotal:.2f}")
            html_body = html_body.replace('__TAX_PLACEHOLDER__', f"CA${tax:.2f}")
            html_body = html_body.replace('__PAYMENT_METHOD_CHIPS_PLACEHOLDER__', chip_html)

            # 6. SEND VIA SES
            response = ses_client.send_email(
                Source=SENDER_EMAIL,
                Destination={'ToAddresses': [recipient]},
                Message={
                    'Subject': {
                        'Data': f"Receipt: Your Momotaro Sushi Order [{order_data.get('orderId')}]",
                        'Charset': 'UTF-8'
                    },
                    'Body': {
                        'Html': {
                            'Data': html_body,
                            'Charset': 'UTF-8'
                        }
                    }
                }
            )
            
            print(f"Sent branded receipt to {recipient}. MessageId: {response['MessageId']}")

        return {'statusCode': 200}

    except Exception as e:
        print(f"CRITICAL ERROR in Email Worker: {str(e)}")
        # Returning 200 to SNS to prevent infinite retry loops on formatting bugs
        return {'statusCode': 200}