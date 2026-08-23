import os                             
from dotenv import load_dotenv        
from aws_cdk import (
    Stack,
    Duration,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_iot as iot,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
)
from constructs import Construct

load_dotenv()

class MomotaroFanoutStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ==========================================
        # 0. CONFIGURATION VARIABLES
        # ==========================================
        DYNAMODB_TABLE_NAME = os.getenv("DYNAMODB_TABLE_NAME") 
        IOT_ENDPOINT_URL = os.getenv("IOT_ENDPOINT_URL")
        PRINTER_MQTT_TOPIC = os.getenv("PRINTER_MQTT_TOPIC")
        VERIFIED_SENDER_EMAIL = os.getenv("VERIFIED_SENDER_EMAIL")
        STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
        STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
        STRIPE_CHECKOUT_API_KEY = os.getenv("STRIPE_CHECKOUT_API_KEY")
        TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
        PRINT_STATUS_TOPIC = os.getenv("PRINT_STATUS_TOPIC", "kitchen/print/status")
        # Alarm when month-to-date SMS spend reaches this. Must be raised in step
        # with the SNS MonthlySpendLimit quota — the point is to fire BEFORE the
        # cap, since past the cap SMS fails silently (publish succeeds, nothing sends).
        SMS_SPEND_ALARM_USD = float(os.getenv("SMS_SPEND_ALARM_USD", "0.80"))

        if not STRIPE_SECRET_KEY:
            raise ValueError("STRIPE_SECRET_KEY is missing. Check your .env file!")

        # ==========================================
        # 1. OPENTELEMETRY LAYER SETUP (Updated)
        # ==========================================
        # NEW recommended layer for ca-central-1 (replaces legacy aws-otel-python-amd64-ver-1-24-0)
        adot_layer_arn = "arn:aws:lambda:ca-central-1:615299751070:layer:AWSOpenTelemetryDistroPython:21"
        adot_layer = _lambda.LayerVersion.from_layer_version_arn(self, "AdotLayer", adot_layer_arn)

        # IAM policy required by the new ADOT layer
        adot_policy = iam.ManagedPolicy.from_aws_managed_policy_name(
            "CloudWatchLambdaApplicationSignalsExecutionRolePolicy"
        )

        # OTel environment variables — metrics go to CloudWatch via EMF
        # No collector.yaml needed for metrics with the new layer
        otel_env_vars = {
            "AWS_LAMBDA_EXEC_WRAPPER": "/opt/otel-instrument",
            "OTEL_METRICS_EXPORTER": "awsemf",                      # Enables metrics via CloudWatch EMF
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",         # Required alongside awsemf
            "OTEL_SERVICE_NAME": "momotaro-sushi",                   # Groups all lambdas in one service
            "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS": "none",         # Enable all auto-instrumentation
        }

        # __pycache__/.pytest_cache regenerate with new content on every local
        # test run, so without this exclude the asset hash (and therefore the
        # "needs redeploy" diff) drifts on every `cdk diff`/`cdk deploy` even
        # when no source file actually changed.
        ASSET_EXCLUDES = ["__pycache__", "*.pyc", ".pytest_cache"]

        # Shared branded-receipt email composition + SES send logic, used by
        # ReceiptEmailSender (SNS-triggered) and OrderConfirmationApi (below).
        email_receipt_layer = _lambda.LayerVersion(self, "EmailReceiptLayer",
            code=_lambda.Code.from_asset("email_lib", exclude=ASSET_EXCLUDES),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Shared branded-receipt email composition + SES send logic"
        )

        # ==========================================
        # 2. THE MESSAGE BUS
        # ==========================================
        order_topic = sns.Topic(self, "MomotaroOrderEventsTopic",
            display_name="Momotaro Real-Time Order Events"
        )

        # ==========================================
        # 3. HELPER: attach ADOT policy to a function
        # ==========================================
        def attach_adot_policy(fn: _lambda.Function):
            fn.role.add_managed_policy(adot_policy)

        # ==========================================
        # 4. THE SUBSCRIBERS (WORKER LAMBDAS)
        # ==========================================
        
        dine_in_order_writer = _lambda.Function(self, "DineInOrderWriter",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="dine_in_order_writer.handler",
            code=_lambda.Code.from_asset("workers", exclude=ASSET_EXCLUDES),
            layers=[adot_layer],
            environment={
                **otel_env_vars,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME
            }
        )
        attach_adot_policy(dine_in_order_writer)

        order_topic.add_subscription(
            subs.LambdaSubscription(
                dine_in_order_writer,
                filter_policy={
                    "orderType": sns.SubscriptionFilter.string_filter(allowlist=["dine-in"])
                }
            )
        )
        dine_in_order_writer.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))

        takeout_payment_updater = _lambda.Function(self, "TakeoutPaymentUpdater",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="takeout_payment_updater.handler",
            code=_lambda.Code.from_asset("workers", exclude=ASSET_EXCLUDES),
            layers=[adot_layer],
            environment={
                **otel_env_vars,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME
            }
        )
        attach_adot_policy(takeout_payment_updater)

        order_topic.add_subscription(
            subs.LambdaSubscription(
                takeout_payment_updater,
                filter_policy={
                    "orderType": sns.SubscriptionFilter.string_filter(allowlist=["takeout"])
                }
            )
        )
        takeout_payment_updater.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))

        kitchen_display_publisher = _lambda.Function(self, "KitchenDisplayPublisher",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="kitchen_display_publisher.handler",
            code=_lambda.Code.from_asset("workers", exclude=ASSET_EXCLUDES),
            layers=[adot_layer],
            timeout=Duration.seconds(20),
            environment={
                **otel_env_vars,
                "IOT_ENDPOINT": "a2ucwnwtscss1f-ats.iot.ca-central-1.amazonaws.com",
                "PRINTER_TOPIC": "printers/orders/print",
            }
        )
        attach_adot_policy(kitchen_display_publisher)
        order_topic.add_subscription(subs.LambdaSubscription(kitchen_display_publisher))
        kitchen_display_publisher.add_to_role_policy(iam.PolicyStatement(
            actions=["iot:Publish"],
            resources=[f"arn:aws:iot:{self.region}:{self.account}:topic/{PRINTER_MQTT_TOPIC}"]
        ))

        receipt_email_sender = _lambda.Function(self, "ReceiptEmailSender",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="receipt_email_sender.handler",
            code=_lambda.Code.from_asset("workers", exclude=ASSET_EXCLUDES),
            layers=[adot_layer, email_receipt_layer],
            environment={
                **otel_env_vars,
                "SENDER_EMAIL": VERIFIED_SENDER_EMAIL
            }
        )
        attach_adot_policy(receipt_email_sender)
        order_topic.add_subscription(
            subs.LambdaSubscription(
                receipt_email_sender,
                filter_policy={
                    "orderType": sns.SubscriptionFilter.string_filter(allowlist=["takeout"])
                }
            )
        )
        receipt_email_sender.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=["*"] 
        ))

        # Printer health alerting. The printer-listener reports the outcome of
        # every print attempt on the kitchen/print/status MQTT topic; until now
        # nothing consumed it, so paper-out and print failures were only visible
        # in a log file on the restaurant machine. The IoT rule below filters out
        # the healthy case so this only fires on real problems.
        print_status_alerter = _lambda.Function(self, "PrintStatusAlerter",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="print_status_alerter.handler",
            code=_lambda.Code.from_asset("workers", exclude=ASSET_EXCLUDES),
            layers=[adot_layer],
            timeout=Duration.seconds(15),
            environment={
                **otel_env_vars,
                "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
                "TELEGRAM_TOKEN_PARAM": "/momotaro/prod/TELEGRAM_BOT_TOKEN",
            }
        )
        attach_adot_policy(print_status_alerter)
        print_status_alerter.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/momotaro/prod/TELEGRAM_BOT_TOKEN"]
        ))

        print_status_rule = iot.CfnTopicRule(self, "PrintStatusAlertRule",
            rule_name="MomotaroPrintStatusAlert",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql=f"SELECT * FROM '{PRINT_STATUS_TOPIC}' WHERE status <> 'OK'",
                aws_iot_sql_version="2016-03-23",
                actions=[iot.CfnTopicRule.ActionProperty(
                    lambda_=iot.CfnTopicRule.LambdaActionProperty(
                        function_arn=print_status_alerter.function_arn
                    )
                )]
            )
        )
        print_status_alerter.add_permission("AllowIotInvoke",
            principal=iam.ServicePrincipal("iot.amazonaws.com"),
            source_arn=print_status_rule.attr_arn
        )

        # Infrastructure alarms -> Telegram. Kept on its own topic rather than
        # reusing order_topic, which fans out to the order workers.
        ops_alert_topic = sns.Topic(self, "OpsAlertsTopic",
            display_name="Momotaro Infrastructure Alerts"
        )

        ops_alerter = _lambda.Function(self, "OpsAlerter",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="ops_alerter.handler",
            code=_lambda.Code.from_asset("workers", exclude=ASSET_EXCLUDES),
            layers=[adot_layer],
            timeout=Duration.seconds(15),
            environment={
                **otel_env_vars,
                "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
                "TELEGRAM_TOKEN_PARAM": "/momotaro/prod/TELEGRAM_BOT_TOKEN",
            }
        )
        attach_adot_policy(ops_alerter)
        ops_alerter.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/momotaro/prod/TELEGRAM_BOT_TOKEN"]
        ))
        ops_alert_topic.add_subscription(subs.LambdaSubscription(ops_alerter))

        # SNS publishes SMS spend to CloudWatch only in us-east-1, regardless of
        # the region the message was sent from — so the alarm must live there too.
        sms_spend_alarm = cloudwatch.Alarm(self, "SmsSpendAlarm",
            alarm_name="MomotaroSmsSpendApproachingLimit",
            alarm_description=(
                "SNS SMS month-to-date spend is approaching the account limit. "
                "Once the limit is reached, texts stop sending silently — order "
                "confirmation SMS will fail with no error. Raise the SNS "
                "MonthlySpendLimit quota, then raise SMS_SPEND_ALARM_USD to match."
            ),
            metric=cloudwatch.Metric(
                namespace="AWS/SNS",
                metric_name="SMSMonthToDateSpentUSD",
                statistic="Maximum",
                period=Duration.hours(1),
                region="us-east-1",
            ),
            threshold=SMS_SPEND_ALARM_USD,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            # No SMS activity publishes no datapoints; that is healthy, not unknown.
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        sms_spend_alarm.add_alarm_action(cw_actions.SnsAction(ops_alert_topic))
        sms_spend_alarm.add_ok_action(cw_actions.SnsAction(ops_alert_topic))

        # Role SNS assumes to write SMS delivery receipts to CloudWatch Logs.
        # Without this there is no record of whether a text reached the handset —
        # only that the publish call didn't error. Wiring this role into the
        # account's SMS attributes is a one-time CLI step (see README/runbook),
        # as SMS attributes aren't exposed to CloudFormation.
        sns_delivery_status_role = iam.Role(self, "SnsSmsDeliveryStatusRole",
            role_name="MomotaroSnsSmsDeliveryStatusRole",
            assumed_by=iam.ServicePrincipal("sns.amazonaws.com"),
            description="Lets SNS write SMS delivery status logs to CloudWatch Logs",
        )
        sns_delivery_status_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:PutMetricFilter",
                "logs:PutRetentionPolicy",
            ],
            resources=["*"],
        ))

        # ==========================================
        # 5. THE PUBLISHERS (INGRESS APIS)
        # ==========================================
        stripe_layer = _lambda.LayerVersion(self, "StripeLayer",
            code=_lambda.Code.from_asset("momotaro_layer", exclude=ASSET_EXCLUDES),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Contains Stripe and its dependencies"
        )    

        takeout_ingress = _lambda.Function(self, "TakeoutIngress",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="takeout_ingress.handler",
            code=_lambda.Code.from_asset("ingress", exclude=ASSET_EXCLUDES),
            layers=[stripe_layer, adot_layer],
            timeout=Duration.seconds(20),
            environment={
                **otel_env_vars,
                "SNS_TOPIC_ARN": order_topic.topic_arn,
                "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
                "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME 
            }
        )
        attach_adot_policy(takeout_ingress)
        order_topic.grant_publish(takeout_ingress)
        apigw.LambdaRestApi(self, "TakeoutApiEndpoint", handler=takeout_ingress)
        takeout_ingress.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))

        dine_in_ingress = _lambda.Function(self, "DineInIngress",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="dine_in_ingress.handler",
            code=_lambda.Code.from_asset("ingress", exclude=ASSET_EXCLUDES),
            layers=[adot_layer],
            timeout=Duration.seconds(20),
            memory_size=512,
            environment={
                **otel_env_vars,
                "SNS_TOPIC_ARN": order_topic.topic_arn
            }
        )
        attach_adot_policy(dine_in_ingress)
        order_topic.grant_publish(dine_in_ingress)
        apigw.LambdaRestApi(self, "DineInApiEndpoint", handler=dine_in_ingress)

        checkout_api = _lambda.Function(self, "CheckoutApi",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="checkout_api.handler",
            code=_lambda.Code.from_asset("ingress", exclude=ASSET_EXCLUDES),
            layers=[stripe_layer, adot_layer],
            timeout=Duration.seconds(20),  
            memory_size=512,               
            environment={
                **otel_env_vars,
                "STRIPE_CHECKOUT_API_KEY": STRIPE_CHECKOUT_API_KEY,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME 
            }
        )
        attach_adot_policy(checkout_api)
        checkout_api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))
        apigw.LambdaRestApi(self, "CheckoutApiEndpoint", handler=checkout_api)

        order_confirmation_api = _lambda.Function(self, "OrderConfirmationApi",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="order_confirmation_api.handler",
            code=_lambda.Code.from_asset("ingress", exclude=ASSET_EXCLUDES),
            layers=[email_receipt_layer, adot_layer],
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                **otel_env_vars,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME,
                "SENDER_EMAIL": VERIFIED_SENDER_EMAIL,
                "ALLOWED_ORIGIN": "*",
                # Off until the SNS SMS Sandbox restriction clears - see
                # order_confirmation_api.py for why every real send fails today.
                "SMS_NOTIFICATIONS_ENABLED": "false",
            }
        )
        attach_adot_policy(order_confirmation_api)
        order_confirmation_api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))
        order_confirmation_api.add_to_role_policy(iam.PolicyStatement(
            actions=["sns:Publish"],
            resources=["*"]  # SNS SMS publish targets a phone number, not a topic ARN
        ))
        order_confirmation_api.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=["*"]
        ))
        apigw.LambdaRestApi(self, "OrderConfirmationApiEndpoint", handler=order_confirmation_api)