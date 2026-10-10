import os                             
from dotenv import load_dotenv        
from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
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

# Kitchen tickets in staging go to their own MQTT topic. No printer or kitchen
# display subscribes to it, so a test order can never print in the restaurant.
STAGING_PRINTER_TOPIC = "staging/printers/orders/print"
STAGING_STRIPE_PUBLISHABLE_KEY_PARAM = "/momotaro/staging/STRIPE_PUBLISHABLE_KEY"

# GitHub OIDC `sub` claims allowed to assume the staging end-to-end test role.
# Any branch or PR of these two repos: the role only reads/cleans the staging
# orders table and reads the Stripe *test-mode* key, so PR runs are safe.
# Both the immutable-ID form (which this account's OIDC setup uses for
# momotaro-fanout) and the default name form are listed.
E2E_GITHUB_SUBJECTS = [
    "repo:kchenfs@126819830/momotaro-fanout@1334580475:*",
    "repo:kchenfs/momotaro-fanout:*",
    "repo:kchenfs@126819830/TableTap2.0@1195703218:*",
    "repo:kchenfs/TableTap2.0:*",
]


class MomotaroFanoutStack(Stack):
    """The order backend.

    stage="prod" is the live restaurant. stage="staging" is a throwaway copy
    the end-to-end tests place orders against before every prod deploy: it
    has its own orders table, uses Stripe test mode, publishes kitchen tickets
    to a topic nothing prints from, and leaves out email, SMS and alerting.
    """

    def __init__(self, scope: Construct, construct_id: str, *, stage: str = "prod", **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if stage not in ("prod", "staging"):
            raise ValueError(f"unknown stage {stage!r}")
        is_prod = stage == "prod"

        # ==========================================
        # 0. CONFIGURATION VARIABLES
        # ==========================================
        DYNAMODB_TABLE_NAME = os.getenv("DYNAMODB_TABLE_NAME") 
        IOT_ENDPOINT_URL = os.getenv("IOT_ENDPOINT_URL")
        PRINTER_MQTT_TOPIC = os.getenv("PRINTER_MQTT_TOPIC")
        VERIFIED_SENDER_EMAIL = os.getenv("VERIFIED_SENDER_EMAIL")
        # Stripe secrets are SSM SecureString parameter NAMES, not values. The
        # lambdas fetch them at runtime, so no secret lands in Lambda config or
        # the CloudFormation template. Override the names in .env if needed.
        STRIPE_KEY_PARAM = os.getenv("STRIPE_KEY_PARAM", "/momotaro/prod/STRIPE_SECRET_KEY")
        STRIPE_WEBHOOK_SECRET_PARAM = os.getenv("STRIPE_WEBHOOK_SECRET_PARAM", "/momotaro/prod/STRIPE_WEBHOOK_SECRET")
        # CheckoutApi has used its own key (formerly STRIPE_CHECKOUT_API_KEY), so
        # it gets its own param rather than sharing the webhook lambda's key.
        STRIPE_CHECKOUT_KEY_PARAM = os.getenv("STRIPE_CHECKOUT_KEY_PARAM", "/momotaro/prod/STRIPE_CHECKOUT_API_KEY")
        # Shared HMAC secret: checkout_api mints a confirmation token, the
        # order-confirmation API verifies it. Both lambdas must get the same value.
        CONFIRMATION_TOKEN_SECRET = os.getenv("CONFIRMATION_TOKEN_SECRET", "")
        # Storefront origin the browser-facing APIs accept, instead of '*'.
        ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://take-out.momotarosushi.ca")
        TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
        PRINT_STATUS_TOPIC = os.getenv("PRINT_STATUS_TOPIC", "kitchen/print/status")
        # Alarm when month-to-date SMS spend reaches this. Must be raised in step
        # with the SNS MonthlySpendLimit quota — the point is to fire BEFORE the
        # cap, since past the cap SMS fails silently (publish succeeds, nothing sends).
        SMS_SPEND_ALARM_USD = float(os.getenv("SMS_SPEND_ALARM_USD", "0.80"))

        orders_table = None
        if not is_prod:
            # Staging never touches the prod orders table or live Stripe keys.
            orders_table = dynamodb.Table(self, "StagingOrdersTable",
                partition_key=dynamodb.Attribute(name="orderId", type=dynamodb.AttributeType.STRING),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                time_to_live_attribute="expiresAt",
                removal_policy=RemovalPolicy.DESTROY,
            )
            DYNAMODB_TABLE_NAME = orders_table.table_name
            PRINTER_MQTT_TOPIC = STAGING_PRINTER_TOPIC
            STRIPE_KEY_PARAM = "/momotaro/staging/STRIPE_SECRET_KEY"
            STRIPE_WEBHOOK_SECRET_PARAM = "/momotaro/staging/STRIPE_WEBHOOK_SECRET"
            STRIPE_CHECKOUT_KEY_PARAM = STRIPE_KEY_PARAM
            CONFIRMATION_TOKEN_SECRET = "staging-only-not-a-secret"
            ALLOWED_ORIGIN = "http://localhost:3000"

        def grant_ssm_read(fn, *param_names):
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/{name.lstrip('/')}"
                           for name in param_names]
            ))
            # SecureString decrypt, only when the request comes through SSM.
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=["*"],
                conditions={"StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}}
            ))

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
                "PRINTER_TOPIC": "printers/orders/print" if is_prod else STAGING_PRINTER_TOPIC,
            }
        )
        attach_adot_policy(kitchen_display_publisher)
        order_topic.add_subscription(subs.LambdaSubscription(kitchen_display_publisher))
        kitchen_display_publisher.add_to_role_policy(iam.PolicyStatement(
            actions=["iot:Publish"],
            resources=[f"arn:aws:iot:{self.region}:{self.account}:topic/{PRINTER_MQTT_TOPIC}"]
        ))

        # Customer email, printer-health and infrastructure alerting are
        # prod-only: a staging test order must never email anyone or page
        # the Telegram channel.
        if is_prod:
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
                "STRIPE_KEY_PARAM": STRIPE_KEY_PARAM,
                "STRIPE_WEBHOOK_SECRET_PARAM": STRIPE_WEBHOOK_SECRET_PARAM,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME 
            }
        )
        attach_adot_policy(takeout_ingress)
        grant_ssm_read(takeout_ingress, STRIPE_KEY_PARAM, STRIPE_WEBHOOK_SECRET_PARAM)
        order_topic.grant_publish(takeout_ingress)
        takeout_api = apigw.LambdaRestApi(self, "TakeoutApiEndpoint", handler=takeout_ingress)
        takeout_ingress.add_to_role_policy(iam.PolicyStatement(
            # UpdateItem: _persist_name_enrichment records where the pickup
            # name came from after payment (it fails open without it).
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
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
        dine_in_api = apigw.LambdaRestApi(self, "DineInApiEndpoint", handler=dine_in_ingress)

        checkout_api = _lambda.Function(self, "CheckoutApi",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="checkout_api.handler",
            code=_lambda.Code.from_asset("ingress", exclude=ASSET_EXCLUDES),
            layers=[stripe_layer, adot_layer],
            timeout=Duration.seconds(20),  
            memory_size=512,               
            environment={
                **otel_env_vars,
                "STRIPE_KEY_PARAM": STRIPE_CHECKOUT_KEY_PARAM,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME,
                "CONFIRMATION_TOKEN_SECRET": CONFIRMATION_TOKEN_SECRET,
                "ALLOWED_ORIGIN": ALLOWED_ORIGIN,
                # Prod refuses orders outside the hours in ingress/store_hours.py.
                # Staging takes orders around the clock for the e2e tests.
                "ENFORCE_STORE_HOURS": "1" if is_prod else "0",
            }
        )
        attach_adot_policy(checkout_api)
        grant_ssm_read(checkout_api, STRIPE_CHECKOUT_KEY_PARAM)
        checkout_api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))
        # Server-side price recompute scans the canonical menu table so the
        # charged amount never trusts the client-supplied total.
        checkout_api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:Scan"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/MomotaroSushiMenu_DB"]
        ))
        checkout_rest_api = apigw.LambdaRestApi(self, "CheckoutApiEndpoint", handler=checkout_api)

        # Contact opt-in sends real email/SMS, so staging leaves it out.
        if is_prod:
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
                    "ALLOWED_ORIGIN": ALLOWED_ORIGIN,
                    # Off until the SNS SMS Sandbox restriction clears - see
                    # order_confirmation_api.py for why every real send fails today.
                    "SMS_NOTIFICATIONS_ENABLED": "false",
                    # Same secret as checkout_api — used to verify the confirmation token.
                    "CONFIRMATION_TOKEN_SECRET": CONFIRMATION_TOKEN_SECRET,
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

        if not is_prod:
            self._add_staging_test_hooks(
                orders_table=orders_table,
                stripe_key_param=STRIPE_KEY_PARAM,
                webhook_secret_param=STRIPE_WEBHOOK_SECRET_PARAM,
                takeout_api=takeout_api,
                dine_in_api=dine_in_api,
                checkout_api=checkout_rest_api,
            )

    def _add_staging_test_hooks(self, *, orders_table, stripe_key_param, webhook_secret_param,
                                takeout_api, dine_in_api, checkout_api):
        """What the end-to-end tests need to see inside staging."""

        # Record every kitchen ticket in the staging orders table as
        # KITCHEN#<order id>, so a test can prove the ticket was published
        # without an MQTT client. Prod has no such rule.
        ticket_log_role = iam.Role(self, "KitchenTicketLogRole",
            assumed_by=iam.ServicePrincipal("iot.amazonaws.com"),
        )
        orders_table.grant_write_data(ticket_log_role)
        iot.CfnTopicRule(self, "KitchenTicketLogRule",
            rule_name="MomotaroStagingKitchenTicketLog",
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                sql=(f"SELECT *, concat('KITCHEN#', order_id) AS orderId "
                     f"FROM '{STAGING_PRINTER_TOPIC}'"),
                aws_iot_sql_version="2016-03-23",
                actions=[iot.CfnTopicRule.ActionProperty(
                    dynamo_d_bv2=iot.CfnTopicRule.DynamoDBv2ActionProperty(
                        role_arn=ticket_log_role.role_arn,
                        put_item=iot.CfnTopicRule.PutItemInputProperty(
                            table_name=orders_table.table_name),
                    )
                )],
            ),
        )

        # Role the GitHub Actions end-to-end jobs assume (keyless, via OIDC).
        # It can read the staging stack's outputs, read and clean up the
        # staging orders table, read the menu (to pick real items), read the
        # Stripe test-mode key, and store the test-mode webhook secret it
        # registers on first run. It has no write access to anything in prod.
        oidc_provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self, "GitHubOidc",
            f"arn:aws:iam::{self.account}:oidc-provider/token.actions.githubusercontent.com",
        )
        e2e_role = iam.Role(self, "E2eTestRole",
            role_name="momotaro-e2e-staging",
            description="GitHub Actions end-to-end tests against MomotaroFanoutStaging",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                oidc_provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                    "StringLike": {"token.actions.githubusercontent.com:sub": E2E_GITHUB_SUBJECTS},
                },
            ),
        )
        e2e_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:DeleteItem"],
            resources=[orders_table.table_arn],
        ))
        e2e_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:Scan"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/MomotaroSushiMenu_DB"],
        ))
        e2e_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:PutParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/{webhook_secret_param.lstrip('/')}"],
        ))
        e2e_role.add_to_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks"],
            resources=[self.stack_id],
        ))
        # The publishable key is what the site's browser tests build with.
        e2e_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/{name.lstrip('/')}"
                       for name in (stripe_key_param, STAGING_STRIPE_PUBLISHABLE_KEY_PARAM)],
        ))
        e2e_role.add_to_policy(iam.PolicyStatement(
            actions=["kms:Decrypt"],
            resources=["*"],
            conditions={"StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}},
        ))

        CfnOutput(self, "OrdersTableName", value=orders_table.table_name)
        CfnOutput(self, "CheckoutApiUrl", value=checkout_api.url)
        CfnOutput(self, "DineInApiUrl", value=dine_in_api.url)
        # Point the Stripe *test-mode* webhook (payment_intent.succeeded) here.
        CfnOutput(self, "StripeWebhookUrl", value=takeout_api.url)
        CfnOutput(self, "E2eRoleArn", value=e2e_role.role_arn)
