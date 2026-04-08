import json
import os
import boto3

# Endpoint must be passed via env var
iot_client = boto3.client('iot-data', endpoint_url=f"https://{os.environ.get('IOT_ENDPOINT')}")
PRINTER_TOPIC = os.environ.get('PRINTER_TOPIC')

def handler(event, context):
    try:
        for record in event.get('Records', []):
            sns_message = record['Sns']['Message']
            order_data = json.loads(sns_message)

            print(f"Routing order {order_data.get('orderId')} to kitchen display...")

            # Format payload specifically for the PrepDeck Electron app
            kitchen_payload = {
                'order_id': order_data.get('orderId'),
                'table': order_data.get('tableId', 'Takeout'),
                'orderType': order_data.get('orderType'),
                'notes': order_data.get('notes', ''),
                'items': order_data.get('items', []),
                'total': float(order_data.get('total', 0))
            }

            # Publish to AWS IoT Core
            if PRINTER_TOPIC:
                iot_client.publish(
                    topic=PRINTER_TOPIC,
                    qos=1,
                    payload=json.dumps(kitchen_payload)
                )

        return {'statusCode': 200}

    except Exception as e:
        print(f"Kitchen worker failed: {e}")
        return {'statusCode': 500}