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
        # 0. CONFIGURATION VARIABLES (Loaded securely)
        # ==========================================
        DYNAMODB_TABLE_NAME = os.getenv("DYNAMODB_TABLE_NAME") 
        IOT_ENDPOINT_URL = os.getenv("IOT_ENDPOINT_URL")
        PRINTER_MQTT_TOPIC = os.getenv("PRINTER_MQTT_TOPIC")
        VERIFIED_SENDER_EMAIL = os.getenv("VERIFIED_SENDER_EMAIL")
        STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
        STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
        STRIPE_CHECKOUT_RAK = os.getenv("STRIPE_CHECKOUT_RAK")

        if not STRIPE_SECRET_KEY:
            raise ValueError("STRIPE_SECRET_KEY is missing. Check your .env file!")

        # ==========================================
        # 1. OPENTELEMETRY TRACING LAYER SETUP
        # ==========================================
        # Define the ADOT layer
        adot_layer_arn = "arn:aws:lambda:ca-central-1:901920570463:layer:aws-otel-python-amd64-ver-1-24-0:1"
        adot_layer = _lambda.LayerVersion.from_layer_version_arn(self, "AdotLayer", adot_layer_arn)

        # Define the common OTel environment variables to inject into all Lambdas
        otel_env_vars = {
            "OPENTELEMETRY_COLLECTOR_CONFIG_FILE": "/var/task/collector.yaml", 
            "AWS_LAMBDA_EXEC_WRAPPER": "/opt/otel-instrument" 
        }

        # ==========================================
        # 2. THE MESSAGE BUS
        # ==========================================
        order_topic = sns.Topic(self, "MomotaroOrderEventsTopic",
            display_name="Momotaro Real-Time Order Events"
        )

        # ==========================================
        # 3. THE SUBSCRIBERS (WORKER LAMBDAS)
        # ==========================================
        
        db_worker = _lambda.Function(self, "DbWorker",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="db_worker.handler",
            code=_lambda.Code.from_asset("lambda_workers"),
            layers=[adot_layer], # <--- Attach ADOT Layer
            environment={
                **otel_env_vars, # <--- Inject OTel variables
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME
            }
        )
        
        order_topic.add_subscription(
            subs.LambdaSubscription(
                db_worker,
                filter_policy={
                    "orderType": sns.SubscriptionFilter.string_filter(allowlist=["dine-in"])
                }
            )
        )
        
        db_worker.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))

        takeout_db_worker = _lambda.Function(self, "TakeoutDbWorker",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="db_worker_takeout.handler",
            code=_lambda.Code.from_asset("lambda_workers"),
            layers=[adot_layer], # <--- Attach ADOT Layer
            environment={
                **otel_env_vars, # <--- Inject OTel variables
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME
            }
        )
        
        order_topic.add_subscription(
            subs.LambdaSubscription(
                takeout_db_worker,
                filter_policy={
                    "orderType": sns.SubscriptionFilter.string_filter(allowlist=["takeout"])
                }
            )
        )
        
        takeout_db_worker.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:UpdateItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))

        kitchen_worker = _lambda.Function(self, "KitchenWorker",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="kitchen_worker.handler",
            code=_lambda.Code.from_asset("lambda_workers"),
            layers=[adot_layer], # <--- Attach ADOT Layer
            timeout=Duration.seconds(20),
            environment={
                **otel_env_vars, # <--- Inject OTel variables
                "IOT_ENDPOINT": "your-iot-endpoint.amazonaws.com",
                "PRINTER_TOPIC": "printers/orders/print",
            }
        )
        order_topic.add_subscription(subs.LambdaSubscription(kitchen_worker))

        kitchen_worker.add_to_role_policy(iam.PolicyStatement(
            actions=["iot:Publish"],
            resources=[f"arn:aws:iot:{self.region}:{self.account}:topic/{PRINTER_MQTT_TOPIC}"]
        ))

        email_worker = _lambda.Function(self, "EmailWorker",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="email_worker.handler",
            code=_lambda.Code.from_asset("lambda_workers"),
            layers=[adot_layer], # <--- Attach ADOT Layer
            environment={
                **otel_env_vars, # <--- Inject OTel variables
                "SENDER_EMAIL": VERIFIED_SENDER_EMAIL
            }
        )
        order_topic.add_subscription(
            subs.LambdaSubscription(
                email_worker,
                filter_policy={
                    "orderType": sns.SubscriptionFilter.string_filter(allowlist=["takeout"])
                }
            )
        )

        email_worker.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=["*"] 
        ))

        # ==========================================
        # 4. THE PUBLISHERS (INGRESS APIS)
        # ==========================================

        stripe_layer = _lambda.LayerVersion(self, "StripeLayer",
            code=_lambda.Code.from_asset("momotaro_layer"),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Contains Stripe and its dependencies"
        )    

        
        takeout_ingress = _lambda.Function(self, "TakeoutIngress",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="takeout_ingress.handler",
            code=_lambda.Code.from_asset("lambda_ingress"),
            layers=[stripe_layer, adot_layer],  # <--- Attach ADOT alongside Stripe
            timeout=Duration.seconds(20),
            environment={
                **otel_env_vars, # <--- Inject OTel variables
                "SNS_TOPIC_ARN": order_topic.topic_arn,
                "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
                "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME 
            }
        )
        order_topic.grant_publish(takeout_ingress)
        apigw.LambdaRestApi(self, "TakeoutApiEndpoint", handler=takeout_ingress)

        takeout_ingress.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))


        dine_in_ingress = _lambda.Function(self, "DineInIngress",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="dine_in_ingress.handler",
            code=_lambda.Code.from_asset("lambda_ingress"),
            layers=[adot_layer], # <--- Attach ADOT Layer
            environment={
                **otel_env_vars, # <--- Inject OTel variables
                "SNS_TOPIC_ARN": order_topic.topic_arn
            }
        )
        order_topic.grant_publish(dine_in_ingress)
        apigw.LambdaRestApi(self, "DineInApiEndpoint", handler=dine_in_ingress)

        checkout_api = _lambda.Function(self, "CheckoutApi",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="checkout_api.handler",
            code=_lambda.Code.from_asset("lambda_ingress"),
            layers=[stripe_layer, adot_layer], # <--- Attach ADOT alongside Stripe
            timeout=Duration.seconds(20),  
            memory_size=512,               
            environment={
                **otel_env_vars, # <--- Inject OTel variables
                "STRIPE_CHECKOUT_RAK": STRIPE_CHECKOUT_RAK,
                "DYNAMODB_TABLE_NAME": DYNAMODB_TABLE_NAME 
            }
        )

        checkout_api.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{DYNAMODB_TABLE_NAME}"]
        ))
        apigw.LambdaRestApi(self, "CheckoutApiEndpoint", handler=checkout_api)