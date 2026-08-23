# Security hardening backlog — deployed checkout/notification lambdas

**Status:** open, un-deployed. Tracked separately from the takeout pickup-name
feature so it does not ride behind it.

**Discovered:** 2026-08-22, while diffing the deployed lambdas against the
`TableTap2.0/lambda/*.py` mirrors for the pickup-name work. Independently
confirmed by an automated security review.

## Key context

- The **deployed** production code (verified via `aws lambda get-function`,
  region `ca-central-1`, last deployed 2026-08-14) is **byte-identical to
  `ingress/*.py` in this repo**.
- The `TableTap2.0/lambda/checkout_api.py` and `takeout_ingress.py` files are a
  **more-hardened rewrite that was never ported here or deployed**. Findings #1
  and #2 already have a written fix there; #3 does not (it needs an auth
  mechanism the mirror does not implement).

## Findings (all HIGH, all live in production)

### 1. Price manipulation — `ingress/checkout_api.py`

The real-order branch does `amount = int(body.get('amount', 0))` and passes it
straight to `stripe.PaymentIntent.create(...)` and into the DynamoDB `total`. A
crafted request pays an arbitrary amount.

**Fix (exists, undeployed):** the mirror's `_compute_order_total_cents()`
recomputes the total server-side from `MomotaroSushiMenu_DB` (canonical price +
option modifiers + 13% tax, `MAX_ITEMS` cap), ignoring the client `amount`.
Port it here and deploy.

### 2. No order-id idempotency — `ingress/checkout_api.py`

`table.put_item(Item=order_item)` with no condition — a repeated/duplicate
`orderId` silently overwrites an existing order.

**Fix (exists, undeployed):** the mirror uses
`put_item(..., ConditionExpression='attribute_not_exists(orderId)')` and returns
409 on collision.

### 3. Unauthenticated IDOR / notification abuse — `ingress/order_confirmation_api.py`

The endpoint takes `orderId` from the request body, looks up the order, and will
**overwrite** `phoneNumber` / `receiptEmail` on the record and fire an SNS SMS /
SES email to attacker-supplied contact info — with nothing binding the caller to
the order. Short order ids make enumeration cheap. This is an SMS/email abuse
vector *and* a PII-overwrite vector.

**Partial mitigation already live:** SMS is gated behind
`SMS_NOTIFICATIONS_ENABLED=false`, which blunts the SMS-spam half today. The
**email path and the contact-field overwrite remain open**, and SMS re-opens the
moment that flag flips.

**Fix (needs new work — not in the mirror):** require a signed one-time token
bound to the specific `orderId` issued at checkout, or verify the Stripe
PaymentIntent id + client_secret against Stripe before allowing any
lookup/update/send. Only send to the contact captured at checkout (or require a
match). Add per-IP and per-orderId rate limits, and refuse to overwrite
already-set contact fields.

## Secondary hygiene (lower priority, also mirror-vs-deployed drift)

- Deployed CORS is `Access-Control-Allow-Origin: *`; mirror locks it to the
  storefront origin.
- Deployed 500 handler leaks `str(e)` in the response body; mirror returns a
  generic message.
- Deployed loads Stripe secrets from Lambda **env vars**; mirror reads them from
  SSM Parameter Store (decrypted). Related: live Stripe keys were previously
  committed to this repo's `.env` history — history has since been scrubbed, but
  the keys should still be rotated.

## Out of scope of the pickup-name feature

The pickup-name Task 4/5 changes are additive (`customerName`) and deliberately
do not touch the price/idempotency/auth logic above.
