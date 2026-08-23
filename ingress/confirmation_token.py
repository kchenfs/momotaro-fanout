"""Shared HMAC token that proves a caller actually went through checkout.

checkout_api mints the token for an order id; order_confirmation_api verifies
it before doing anything with contact info. Both lambdas deploy from the same
`ingress` asset, so they share this module and the CONFIRMATION_TOKEN_SECRET
env var. Stdlib only — no AWS service or Stripe dependency.
"""
import hashlib
import hmac
import os

_SECRET = os.environ.get('CONFIRMATION_TOKEN_SECRET', '')


def make_confirmation_token(order_id):
    """HMAC-SHA256 over the order id, hex-encoded.

    Returns '' when no secret is configured or no order id is given, so
    checkout still succeeds; the confirmation endpoint then rejects such a
    token, failing closed rather than open.
    """
    if not _SECRET or not order_id:
        return ''
    return hmac.new(_SECRET.encode(), str(order_id).encode(), hashlib.sha256).hexdigest()


def verify_confirmation_token(order_id, token):
    """Constant-time check that token matches the one minted for order_id.

    False whenever the secret or the supplied token is missing/empty, so a
    request that omits the token is rejected exactly like a forged one.
    """
    if not _SECRET or not token:
        return False
    expected = make_confirmation_token(order_id)
    if not expected:
        return False
    return hmac.compare_digest(expected, str(token))
