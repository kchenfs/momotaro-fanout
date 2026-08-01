import json
import os
import boto3
from decimal import Decimal

# Initialize DynamoDB
dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME')
table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None

def replace_floats_with_decimals(obj):
    """Helper to convert float values to Decimal for DynamoDB compatibility."""
    if isinstance(obj, list):
        return [replace_floats_with_decimals(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: replace_floats_with_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, float):
        return Decimal(str(obj))
    return obj

def handler(event, context):
    try:
        for record in event.get('Records', []):
            sns_message = record['Sns']['Message']
            # Parse the order data
            raw_data = json.loads(sns_message)
            
            # --- THE FIX ---
            # Convert all floats to Decimals before saving to DynamoDB
            order_data = replace_floats_with_decimals(raw_data)
            
            order_id = order_data.get('orderId')
            print(f"Saving order {order_id} to DynamoDB...")

            if table:
                table.put_item(Item=order_data)
            else:
                print("Error: DYNAMODB_TABLE_NAME missing.")

        return {'statusCode': 200}
        
    except Exception as e:
        print(f"Database worker failed: {e}")
        return {'statusCode': 500}