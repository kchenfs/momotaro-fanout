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
