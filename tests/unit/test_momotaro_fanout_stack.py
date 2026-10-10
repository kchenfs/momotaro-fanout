import json

import aws_cdk as core
import aws_cdk.assertions as assertions

from momotaro_fanout.momotaro_fanout_stack import MomotaroFanoutStack, STAGING_PRINTER_TOPIC


def _template(stage):
    app = core.App()
    stack = MomotaroFanoutStack(app, f"test-{stage}", stage=stage)
    return assertions.Template.from_stack(stack)


def test_staging_never_reaches_the_real_kitchen_or_customers():
    template = _template("staging")
    rendered = json.dumps(template.to_json())

    # Kitchen tickets only go to the staging topic, never the printer's.
    assert '"printers/orders/print"' not in rendered
    assert STAGING_PRINTER_TOPIC in rendered

    # No email, SMS, contact opt-in or Telegram alerting in staging.
    for absent in ("ReceiptEmailSender", "OrderConfirmationApi", "PrintStatusAlerter",
                   "OpsAlerter", "SmsSpendAlarm", "SnsSmsDeliveryStatusRole"):
        assert absent not in rendered, absent
    assert "ses:SendEmail" not in rendered

    # Stripe secrets come from the staging (test-mode) params only.
    assert "/momotaro/prod/" not in rendered


def test_staging_has_its_own_orders_table_and_test_hooks():
    template = _template("staging")
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.has_resource_properties("AWS::IAM::Role", {"RoleName": "momotaro-e2e-staging"})
    template.has_resource_properties("AWS::IoT::TopicRule", {
        "RuleName": "MomotaroStagingKitchenTicketLog",
    })
    for output in ("OrdersTableName", "CheckoutApiUrl", "DineInApiUrl", "StripeWebhookUrl", "E2eRoleArn"):
        template.has_output(output, {})


def test_prod_has_no_staging_resources(monkeypatch):
    # Prod reads its config from .env, which CI's test job doesn't have.
    for key, value in {"DYNAMODB_TABLE_NAME": "orders", "PRINTER_MQTT_TOPIC": "printers/orders/print",
                       "VERIFIED_SENDER_EMAIL": "x@example.com", "TELEGRAM_CHAT_ID": "1"}.items():
        monkeypatch.setenv(key, value)
    template = _template("prod")
    template.resource_count_is("AWS::DynamoDB::Table", 0)
    rendered = json.dumps(template.to_json())
    assert "momotaro-e2e-staging" not in rendered
    assert STAGING_PRINTER_TOPIC not in rendered
