"""Fixtures for the end-to-end order tests against MomotaroFanoutStaging.

These tests place real orders through the deployed staging backend: Stripe in
test mode, the staging orders table, and a kitchen topic nothing prints from.
They are skipped unless E2E_STACK_NAME is set, so a plain `pytest` never
reaches AWS. CI sets it after deploying staging; see .github/workflows/deploy.yml.
"""
import os

import boto3
import pytest
import stripe

STACK_NAME = os.environ.get("E2E_STACK_NAME")
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ca-central-1"
STRIPE_KEY_PARAM = "/momotaro/staging/STRIPE_SECRET_KEY"
WEBHOOK_SECRET_PARAM = "/momotaro/staging/STRIPE_WEBHOOK_SECRET"
MENU_TABLE = "MomotaroSushiMenu_DB"


def pytest_collection_modifyitems(config, items):
    if STACK_NAME:
        return
    skip = pytest.mark.skip(reason="E2E_STACK_NAME not set; end-to-end tests need a deployed staging stack")
    for item in items:
        if "tests/e2e" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def staging():
    """Stack outputs as a dict: CheckoutApiUrl, DineInApiUrl, StripeWebhookUrl, OrdersTableName."""
    cfn = boto3.client("cloudformation", region_name=REGION)
    stack = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


@pytest.fixture(scope="session")
def orders_table(staging):
    return boto3.resource("dynamodb", region_name=REGION).Table(staging["OrdersTableName"])


@pytest.fixture(scope="session")
def stripe_test_mode():
    """Load the staging Stripe key and refuse to run with anything but a test key."""
    ssm = boto3.client("ssm", region_name=REGION)
    key = ssm.get_parameter(Name=STRIPE_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
    if not (key.startswith("sk_test_") or key.startswith("rk_test_")):
        pytest.fail(f"{STRIPE_KEY_PARAM} must hold a Stripe TEST key; refusing to place orders with it")
    stripe.api_key = key
    return stripe


@pytest.fixture(scope="session")
def stripe_webhook(staging, stripe_test_mode):
    """Make sure Stripe test mode sends payment_intent.succeeded to staging.

    Registers the webhook endpoint on first run and stores its signing secret
    where the staging TakeoutIngress Lambda reads it. Idempotent: an existing
    enabled endpoint for this URL is reused as-is.
    """
    url = staging["StripeWebhookUrl"]
    for endpoint in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter():
        if endpoint.url == url and endpoint.status == "enabled":
            return endpoint.id

    endpoint = stripe.WebhookEndpoint.create(
        url=url,
        enabled_events=["payment_intent.succeeded"],
        description="Momotaro staging (end-to-end tests)",
    )
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=WEBHOOK_SECRET_PARAM, Value=endpoint.secret, Type="SecureString", Overwrite=True,
    )
    return endpoint.id


@pytest.fixture(scope="session")
def menu_item():
    """A real, orderable menu item with no option groups, so its price is just Price."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(MENU_TABLE)
    candidates = []
    # Items carry large embedding vectors; fetch only what an order line needs.
    kwargs = {
        "ProjectionExpression": "ItemNumber, ItemName, Price, Options, #loc",
        "ExpressionAttributeNames": {"#loc": "Location"},
    }
    while True:
        resp = table.scan(**kwargs)
        candidates.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    for item in sorted(candidates, key=lambda i: str(i.get("ItemNumber"))):
        if item.get("ItemNumber") is not None and float(item.get("Price", 0) or 0) > 0 and not item.get("Options"):
            return item
    pytest.fail("no orderable menu item without options found in MomotaroSushiMenu_DB")
