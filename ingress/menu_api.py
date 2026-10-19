"""Public menu API: GET returns every menu item for the ordering site.

Moved here from the hand-deployed function (TableTap2.0's lambda.py was a copy
of it). The projection is a WHITELIST: an attribute not named here never
reaches the site, even though it exists in MomotaroSushiMenu_DB. Dropping
DineInPrice once made dine-in fall back to takeout prices with no error.
"""
import json
import os
import traceback
from decimal import Decimal

import boto3

menu_table = boto3.resource('dynamodb').Table(os.environ.get('MENU_TABLE_NAME', 'MomotaroSushiMenu_DB'))

ALLOWED_ORIGINS = {
    'https://dine-in.momotarosushi.ca',
    'https://take-out.momotarosushi.ca',
    'http://localhost:3000',
    'http://take-out.localhost:3000',
}

PROJECTION = (
    "ItemName, #cat, #desc, ItemNumber, #loc, #opt, Price, ImageUrl, #tags, "
    "DineInPrice, HiddenOnDineIn, SoldOut"
)
ATTRIBUTE_NAMES = {
    '#cat': 'Category',
    '#desc': 'Description',
    '#loc': 'Location',
    '#opt': 'Options',
    '#tags': 'Tags',
}


def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, set):
        return list(obj)
    raise TypeError


def _item_number(item):
    try:
        return int(item.get('ItemNumber', 0))
    except (TypeError, ValueError):
        return 0


def scan_menu():
    """Every item, sorted by ItemNumber. Items carry embedding vectors for the
    chatbot, so a scan can pass DynamoDB's 1 MB page limit; follow the pages."""
    items = []
    kwargs = {'ProjectionExpression': PROJECTION, 'ExpressionAttributeNames': ATTRIBUTE_NAMES}
    while True:
        resp = menu_table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return sorted(items, key=_item_number)


def handler(event, context):
    headers = (event or {}).get('headers') or {}
    origin = headers.get('origin') or headers.get('Origin')
    cors = {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Methods': 'GET,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Max-Age': '86400',
    }
    if origin:
        cors['Access-Control-Allow-Origin'] = origin
        if origin not in ALLOWED_ORIGINS:
            return {'statusCode': 403, 'headers': cors, 'body': json.dumps({'error': f"Unauthorized Origin: {origin}"})}

    if (event or {}).get('httpMethod') == 'OPTIONS':
        return {'statusCode': 200, 'headers': cors}

    try:
        return {'statusCode': 200, 'headers': cors, 'body': json.dumps(scan_menu(), default=_json_default)}
    except Exception:
        print("Menu scan failed:", traceback.format_exc())
        return {'statusCode': 500, 'headers': cors, 'body': json.dumps({'error': 'Menu unavailable'})}
