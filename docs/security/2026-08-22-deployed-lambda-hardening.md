# Security hardening backlog — deployed checkout/notification lambdas

**Status:** partially addressed. #1 and #2 are **fixed in code (authoritative
`ingress/checkout_api.py` + a CDK `dynamodb:Scan` grant), pending `cdk deploy`.**
#3 is still open (approach chosen: HMAC token — see below). Secondary hygiene
items remain open. Tracked separately from the takeout pickup-name feature so it
does not ride behind it.

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

### 1. Price manipulation — `ingress/checkout_api.py` — FIXED IN CODE, PENDING DEPLOY

The real-order branch did `amount = int(body.get('amount', 0))` and passed it
straight to `stripe.PaymentIntent.create(...)` and into the DynamoDB `total`. A
crafted request paid an arbitrary amount.

**Fix (applied):** `_compute_order_total_cents()` now recomputes the total
server-side from `MomotaroSushiMenu_DB` (canonical price + option modifiers +
13% tax, `MAX_ITEMS` cap), ignoring the client `amount`; unknown item / bad
quantity / too many items returns 400. The CheckoutApi role gained a
`dynamodb:Scan` grant on `MomotaroSushiMenu_DB` in the CDK stack — **this and the
code must deploy together** or the live lambda throws AccessDenied on every
takeout order. Post-deploy, sanity-check one real order: the charged amount is
now the server value from the menu table.

### 2. No order-id idempotency — `ingress/checkout_api.py` — FIXED IN CODE, PENDING DEPLOY

`table.put_item(Item=order_item)` had no condition — a repeated/duplicate
`orderId` silently overwrote an existing order.

**Fix (applied):** `put_item(..., ConditionExpression='attribute_not_exists(orderId)')`,
returning 409 on collision. No IAM change (still `PutItem`).

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

**Fix (chosen approach — HMAC token, not yet implemented):** `checkout_api`
mints a signed token bound to the `orderId` (HMAC over the id with a shared
secret) and returns it; the frontend caches it with the recap and sends it back;
`order_confirmation_api` recomputes the HMAC and rejects a mismatch. Stdlib-only
(`hmac`), no Stripe SDK or new AWS service on that lambda; needs one shared
secret. Also: only send to the contact captured at checkout (or require a match),
refuse to overwrite already-set contact fields, and add per-IP / per-orderId rate
limits. Spans the frontend (TableTap2.0) too, so tracked as its own piece.

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
