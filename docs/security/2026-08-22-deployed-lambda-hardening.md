# Security hardening backlog — deployed checkout/notification lambdas

**Status:** #1, #2, and #3 are all **fixed in code, pending deploy.** #1/#2 in
`ingress/checkout_api.py` (+ a CDK `dynamodb:Scan` grant); #3 via an HMAC
confirmation token across `checkout_api`, `order_confirmation_api`, the CDK
stack, and the TableTap2.0 frontend. Secondary hygiene items remain open.
Tracked separately from the takeout pickup-name feature so it does not ride
behind it.

**Deploy requirements:** set `CONFIRMATION_TOKEN_SECRET` in the fanout `.env`
(a generated value is already there locally) — it is passed to both lambdas.
Deploy the fanout stack and the frontend close together: once
`order_confirmation_api` requires the token it fails closed, so the frontend
must be sending it. The only cost during any gap is that the *optional* SMS/email
receipt fails — no order or payment impact.

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

### 3. Unauthenticated IDOR / notification abuse — `ingress/order_confirmation_api.py` — FIXED IN CODE, PENDING DEPLOY

The endpoint takes `orderId` from the request body, looks up the order, and will
**overwrite** `phoneNumber` / `receiptEmail` on the record and fire an SNS SMS /
SES email to attacker-supplied contact info — with nothing binding the caller to
the order. Short order ids make enumeration cheap. This is an SMS/email abuse
vector *and* a PII-overwrite vector.

**Partial mitigation already live:** SMS is gated behind
`SMS_NOTIFICATIONS_ENABLED=false`, which blunts the SMS-spam half today. The
**email path and the contact-field overwrite remain open**, and SMS re-opens the
moment that flag flips.

**Fix (applied — HMAC token):** `checkout_api` mints a token bound to the
`orderId` (`HMAC-SHA256(CONFIRMATION_TOKEN_SECRET, order_id)`) and returns it;
the frontend caches it with the recap through the Stripe redirect and sends it
back; `order_confirmation_api` recomputes the HMAC and rejects a missing/invalid
token with **403 before any lookup, update, or send** (constant-time compare,
fails closed). Shared logic lives in `ingress/confirmation_token.py` (stdlib
`hmac` only). Defense-in-depth added: the endpoint **refuses to overwrite** a
`phoneNumber`/`receiptEmail` already on the order with a different value.

**Still open (deferred):** per-IP / per-orderId **rate limiting** — belongs at
API Gateway (usage plan / throttling), an infra task, not app code.

## Secondary hygiene (lower priority, also mirror-vs-deployed drift)

- **FIXED IN CODE, PENDING DEPLOY** — CORS: both `checkout_api` and
  `order_confirmation_api` now use an `ALLOWED_ORIGIN` env var (default
  `https://take-out.momotarosushi.ca`) instead of `*`. `checkout_api`'s real
  path is server-side via the Next.js proxy, so this only tightens direct
  browser calls; `order_confirmation_api` is browser-facing, so its origin must
  match the storefront — override `ALLOWED_ORIGIN` in `.env` if the domain differs.
- **FIXED IN CODE, PENDING DEPLOY** — `checkout_api`'s 500 handler no longer
  leaks `str(e)`; it returns a generic `Internal server error`.
- **STILL OPEN** — the lambdas load Stripe secrets from env vars; the mirror
  reads them from SSM Parameter Store (decrypted). Deferred: needs the SSM
  params + IAM and risks breaking auth if mis-sequenced. Related: live Stripe
  keys were previously committed to this repo's `.env` history — history has
  since been scrubbed, but the keys should still be rotated.

## Out of scope of the pickup-name feature

The pickup-name Task 4/5 changes are additive (`customerName`) and deliberately
do not touch the price/idempotency/auth logic above.
