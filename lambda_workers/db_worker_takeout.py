import json
import os
import boto3

dynamodb = boto3.resource('dynamodb')
# --- FIX: Updated to match our standard env variable ---
table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE_NAME'))

def handler(event, context):
    try:
        for record in event.get('Records', []):
            order_data = json.loads(record['Sns']['Message'])
            order_id = order_data.get('orderId')
            
            if not order_id:
                continue

            print(f"Updating takeout order {order_id} to PAID...")
            
            table.update_item(
                Key={'orderId': str(order_id)},
                UpdateExpression="SET orderStatus = :s, paymentStatus = :p, paymentId = :pid, paymentDetails = :pd",
                ExpressionAttributeValues={
                    ':s': order_data.get('orderStatus', 'PENDING_KITCHEN'),
                    ':p': 'PAID',
                    ':pid': order_data.get('paymentId'),
                    ':pd': order_data.get('paymentDetails')
                }
            )
            print(f"Successfully patched {order_id}")
            
        return {'statusCode': 200}
    except Exception as e:
        print(f"Takeout DB worker failed: {e}")
        return {'statusCode': 500}