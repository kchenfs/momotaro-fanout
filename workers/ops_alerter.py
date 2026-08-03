import json
import os
import urllib.parse
import urllib.request
import boto3

# Relays CloudWatch alarm state changes to Telegram.
#
# Distinct from print_status_alerter (printer hardware faults, arrives via an
# IoT rule) and telegram_notifier (per-order alerts, arrives via the order SNS
# topic). This one carries infrastructure alarms — currently the SNS SMS
# month-to-date spend approaching its cap, which otherwise fails silently:
# once the cap is hit, publish() still succeeds but no text is ever delivered.

_ssm = boto3.client('ssm')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
TOKEN_PARAM = os.environ.get('TELEGRAM_TOKEN_PARAM', '/momotaro/prod/TELEGRAM_BOT_TOKEN')
_TOKEN = None


def _token():
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = _ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)['Parameter']['Value']
    return _TOKEN


def _format_alarm(alarm):
    name = alarm.get('AlarmName', 'Unknown alarm')
    state = alarm.get('NewStateValue', 'UNKNOWN')
    reason = alarm.get('NewStateReason', '')
    description = alarm.get('AlarmDescription') or ''

    if state == 'ALARM':
        headline = f'\U0001F6A8 {name}'
    elif state == 'OK':
        headline = f'✅ RESOLVED: {name}'
    else:
        headline = f'⚠️ {name} ({state})'

    lines = [headline]
    if description:
        lines.append('')
        lines.append(description)
    if reason:
        lines.append('')
        lines.append(reason)
    return '\n'.join(lines)


def _send(text):
    url = f"https://api.telegram.org/bot{_token()}/sendMessage"
    data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': text}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def handler(event, context):
    for record in event.get('Records', []):
        try:
            alarm = json.loads(record['Sns']['Message'])
            status = _send(_format_alarm(alarm))
            print(f"Ops alert sent for {alarm.get('AlarmName')} [{alarm.get('NewStateValue')}]: HTTP {status}")
        except Exception as e:
            # Never raise: a failed alert must not cause SNS to retry in a loop.
            print(f"Ops alert failed: {e}")
    return {'statusCode': 200}
