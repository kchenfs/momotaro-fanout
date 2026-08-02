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

        # Shared branded-receipt email composition + SES send logic, used by
        # ReceiptEmailSender (SNS-triggered) and OrderConfirmationApi (below).
        email_receipt_layer = _lambda.LayerVersion(self, "EmailReceiptLayer",
            code=_lambda.Code.from_asset("email_lib"),
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
            code=_lambda.Code.from_asset("workers"),
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
            code=_lambda.Code.from_asset("workers"),
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
            code=_lambda.Code.from_asset("workers"),
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
            code=_lambda.Code.from_asset("workers"),
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

        # ==========================================
        # 5. THE PUBLISHERS (INGRESS APIS)
        # ==========================================
        stripe_layer = _lambda.LayerVersion(self, "StripeLayer",
            code=_lambda.Code.from_asset("momotaro_layer"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Contains Stripe and its dependencies"
        )    

        takeout_ingress = _lambda.Function(self, "TakeoutIngress",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="takeout_ingress.handler",
            code=_lambda.Code.from_asset("ingress"),
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
            code=_lambda.Code.from_asset("ingress"),
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
            code=_lambda.Code.from_asset("ingress"),
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
            code=_lambda.Code.from_asset("ingress"),
            layers=[email_receipt_layer, adot_layer],
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                **otel_env_vars,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME,
                "SENDER_EMAIL": VERIFIED_SENDER_EMAIL,
                "ALLOWED_ORIGIN": "*",
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