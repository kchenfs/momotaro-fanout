import json
import os
import urllib.parse
import urllib.request
import boto3

# Alerts staff via Telegram when the kitchen printer reports a problem.
#
# The printer-listener publishes {order_id, status, message, timestamp} to the
# kitchen/print/status MQTT topic after every print attempt. An IoT topic rule
# filters out the healthy "OK" case, so anything reaching this handler is a
# printer problem worth waking someone up for. Without this, paper-out and
# print errors were only visible in a log file on the restaurant machine.

_ssm = boto3.client('ssm')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
TOKEN_PARAM = os.environ.get('TELEGRAM_TOKEN_PARAM', '/momotaro/prod/TELEGRAM_BOT_TOKEN')
_TOKEN = None

# Printer-side status codes (see printer_status.py in the momotaro-kitchen repo)
# plus PRINT_ERROR, which listener.py emits when the print itself raises.
STATUS_HEADLINES = {
    'NO_PAPER': '\U0001F6A8 PRINTER OUT OF PAPER',
    'PRINTER_OFFLINE': '\U0001F6A8 PRINTER OFFLINE',
    'PRINT_ERROR': '\U0001F6A8 PRINT FAILED',
    'PAPER_LOW': '⚠️ Printer paper low',
}


def _token():
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = _ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)['Parameter']['Value']
    return _TOKEN


def _format_alert(event):
    status = event.get('status', 'UNKNOWN')
    order_id = event.get('order_id', 'UNKNOWN')
    message = event.get('message', '')

    headline = STATUS_HEADLINES.get(status, f'\U0001F6A8 PRINTER PROBLEM ({status})')
    lines = [headline, f'Order: {order_id}']
    if message:
        lines.append(message)

    # PAPER_LOW still printed successfully — say so, otherwise staff can't tell
    # this apart from the failures above and may re-make an order unnecessarily.
    if status == 'PAPER_LOW':
        lines.append('')
        lines.append('The ticket still printed. Reload paper soon.')
    else:
        lines.append('')
        lines.append('This order did NOT print. Check the Telegram order alert for details.')
    return '\n'.join(lines)


def _send(text):
    url = f"https://api.telegram.org/bot{_token()}/sendMessage"
    data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': text}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def handler(event, context):
    # IoT rule actions deliver the MQTT payload as the event itself (already
    # parsed from JSON), not wrapped in Records like SNS.
    try:
        status = _send(_format_alert(event))
        print(f"Printer alert sent for {event.get('order_id')} ({event.get('status')}): HTTP {status}")
    except Exception as e:
        # Never raise: a failed alert must not cause IoT to retry in a loop.
        print(f"Printer alert failed: {e}")
    return {'statusCode': 200}
