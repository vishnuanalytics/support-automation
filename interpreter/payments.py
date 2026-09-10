"""
Billing chunks B/C — payment-provider registry (docs/BILLING_SETUP.md).

Mirrors `interpreter/kb_connectors.py`'s KBConnectorSpec/register() shape —
"a provider is data behind one narrow contract, not a hardcoded handler" —
applied to payment gateways instead of KB sources. Both providers are
implemented, each verified live against the real test-mode API before
being written (2026-09-10): Razorpay's `create_subscription` returns an
already-live Subscription with a hosted `short_url` immediately; Stripe's
returns a Checkout Session (its `id` and `url`) instead, because a Stripe
Subscription doesn't exist yet at that point — Stripe only creates the
real `sub_...` once the tenant finishes paying, and tells us via the
`checkout.session.completed` webhook. `WebhookEvent.tenant_id` is what
makes that two-step flow work through the same generic webhook-processing
code as Razorpay's one-step flow: api/main.py's shared handler falls back
to matching a `subscriptions` row by `tenant_id` when the direct
`provider_subscription_id` lookup misses (because the DB still has the
Checkout Session id, not the real subscription id yet), then backfills
the real id once it's known.

One *platform-level* account per provider — unlike Slack/Salesforce, where
each tenant connects their own external account, tenants here subscribe
*into* the platform's own Razorpay/Stripe account. `provider_for_country()`
picks which one a tenant bills through, once, at signup.

    create_customer()      -> a provider_customer_id, stored on the
                               `subscriptions` row once we have one
    create_subscription()  -> a hosted checkout URL (`short_url` /
                               Stripe's Checkout URL) to redirect the
                               tenant owner to — we never touch a card
    verify_webhook()       -> pure signature check (HMAC over the raw body)
    normalize_event()      -> the provider's own webhook payload shape ->
                               one common `WebhookEvent`, so
                               api/main.py's webhook route and
                               api/worker.py's subscription-sync logic
                               don't need a per-provider branch

Nothing downstream of these four functions is provider-specific.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("interpreter.payments")


@dataclass
class CheckoutResult:
    provider_subscription_id: str
    short_url: str            # hosted page to redirect the tenant owner to
    status: str               # the provider's own raw status at creation time
    provider_customer_id: str | None = None


@dataclass
class WebhookEvent:
    provider_event_id: str            # idempotency key -> billing_events unique(provider, provider_event_id)
    event_type: str                   # the provider's own event name, e.g. "subscription.activated"
    provider_subscription_id: str | None
    status: str | None                # normalized: created | active | past_due | canceled | expired
    current_period_start: str | None = None
    current_period_end: str | None = None
    tenant_id: str | None = None      # fallback match when provider_subscription_id can't be, yet (Stripe Checkout)
    raw: dict[str, Any] = field(default_factory=dict)


CreateCustomerFn = Callable[[str, "str | None", str], str]                  # (name, email, tenant_id) -> provider_customer_id
CreateSubscriptionFn = Callable[[str, dict[str, Any], str], CheckoutResult]  # (customer_id, plan_row, tenant_id) -> CheckoutResult
VerifyWebhookFn = Callable[[bytes, dict[str, str]], bool]                    # (raw_body, headers) -> ok
NormalizeEventFn = Callable[[dict[str, Any]], WebhookEvent]                  # parsed JSON body -> WebhookEvent


@dataclass
class PaymentProviderSpec:
    slug: str                         # "stripe" | "razorpay"
    create_customer: CreateCustomerFn
    create_subscription: CreateSubscriptionFn
    verify_webhook: VerifyWebhookFn
    normalize_event: NormalizeEventFn


_REGISTRY: dict[str, PaymentProviderSpec] = {}


def register(spec: PaymentProviderSpec) -> None:
    _REGISTRY[spec.slug] = spec


def get_provider(slug: "str | None") -> PaymentProviderSpec:
    spec = _REGISTRY.get(slug or "")
    if spec is None:
        raise KeyError(f"unknown payment provider {slug!r}")
    return spec


def provider_for_country(country: "str | None") -> str:
    """India -> razorpay (UPI/netbanking/cards + its own GST handling),
    everywhere else -> stripe. Decided once, at signup — see the billing
    plan's "Provider architecture" section."""
    return "razorpay" if (country or "").strip().upper() == "IN" else "stripe"


# ==========================================================================
# Razorpay
# ==========================================================================
_RAZORPAY_API = "https://api.razorpay.com/v1"

# created/authenticated: checkout not finished yet. active: paying.
# pending/halted: a charge failed, Razorpay is retrying — treat as past_due,
# not canceled, so a flaky card doesn't instantly lock the tenant out.
# cancelled: tenant or platform ended it. completed/expired: ran its full
# total_count of billing cycles (see the comment on `_TOTAL_COUNT` below).
_RAZORPAY_STATUS_MAP = {
    "created": "created", "authenticated": "created",
    "active": "active",
    "pending": "past_due", "halted": "past_due",
    "cancelled": "canceled",
    "completed": "expired", "expired": "expired",
}

# Razorpay subscriptions are not open-ended — they run for `total_count`
# billing cycles then stop. There's no "forever" value, so this is a large
# stand-in (10 years of monthly cycles) rather than a real product decision;
# revisit once annual/multi-year plans exist.
_TOTAL_COUNT = 120


def _razorpay_auth() -> tuple[str, str]:
    key_id, key_secret = os.environ.get("RAZORPAY_KEY_ID"), os.environ.get("RAZORPAY_KEY_SECRET")
    if not (key_id and key_secret):
        raise RuntimeError("Razorpay is not configured — set RAZORPAY_KEY_ID / "
                           "RAZORPAY_KEY_SECRET (docs/BILLING_SETUP.md)")
    return key_id, key_secret


def _razorpay_call(method: str, path: str, **kw) -> dict[str, Any]:
    import requests

    r = requests.request(method, f"{_RAZORPAY_API}{path}", auth=_razorpay_auth(), timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def razorpay_create_customer(name: str, email: "str | None", tenant_id: str) -> str:
    body: dict[str, Any] = {"name": name, "notes": {"tenant_id": tenant_id}}
    if email:
        body["email"] = email
    return _razorpay_call("POST", "/customers", json=body)["id"]


def razorpay_create_subscription(customer_id: str, plan_row: dict[str, Any],
                                 tenant_id: str) -> CheckoutResult:
    razorpay_plan_id = plan_row.get("razorpay_plan_id")
    if not razorpay_plan_id:
        raise RuntimeError(
            f"plan {plan_row.get('slug')!r} has no razorpay_plan_id yet — "
            "create the matching Plan in the Razorpay dashboard (or via the "
            "API) once its price is signed off, then set plans.razorpay_plan_id.")
    r = _razorpay_call("POST", "/subscriptions", json={
        "plan_id": razorpay_plan_id, "customer_id": customer_id,
        "customer_notify": 1, "total_count": _TOTAL_COUNT,
        "notes": {"tenant_id": tenant_id},
    })
    return CheckoutResult(
        provider_subscription_id=r["id"], short_url=r["short_url"],
        status=_RAZORPAY_STATUS_MAP.get(r["status"], r["status"]),
        provider_customer_id=r.get("customer_id") or customer_id,
    )


def razorpay_verify_webhook(raw_body: bytes, headers: dict[str, str]) -> bool:
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        log.warning("razorpay webhook received but RAZORPAY_WEBHOOK_SECRET is unset — refusing")
        return False
    signature = headers.get("x-razorpay-signature") or headers.get("X-Razorpay-Signature") or ""
    mine = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, signature)


def razorpay_normalize_event(payload: dict[str, Any]) -> WebhookEvent:
    # Razorpay's webhook body carries no stable event id of its own to key
    # idempotency on (unlike Stripe's `evt_...`) — a sha256 of the exact
    # raw body is deterministic across Razorpay's own retries of the same
    # delivery and needs no undocumented header to exist.
    import json

    raw_bytes = json.dumps(payload, sort_keys=True).encode()
    event_id = hashlib.sha256(raw_bytes).hexdigest()

    sub = (((payload.get("payload") or {}).get("subscription") or {}).get("entity")) or {}
    raw_status = sub.get("status")
    return WebhookEvent(
        provider_event_id=event_id,
        event_type=payload.get("event", ""),
        provider_subscription_id=sub.get("id"),
        status=_RAZORPAY_STATUS_MAP.get(raw_status) if raw_status else None,
        current_period_start=_epoch_to_iso(sub.get("current_start")),
        current_period_end=_epoch_to_iso(sub.get("current_end")),
        tenant_id=(sub.get("notes") or {}).get("tenant_id"),
        raw=payload,
    )


def _epoch_to_iso(epoch: "int | None") -> "str | None":
    if not epoch:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


register(PaymentProviderSpec(
    slug="razorpay",
    create_customer=razorpay_create_customer,
    create_subscription=razorpay_create_subscription,
    verify_webhook=razorpay_verify_webhook,
    normalize_event=razorpay_normalize_event,
))


# ==========================================================================
# Stripe
# ==========================================================================
_STRIPE_API = "https://api.stripe.com/v1"

# incomplete: Checkout not finished / first payment still processing.
# trialing: only reachable if we ever set a *Stripe-side* trial (we don't
# today — our trial is platform-level, tracked on `tenants` independent of
# the provider) — mapped to active so it wouldn't wrongly read as "not
# paying" if that ever changes. paused/unpaid: a charge is failing — same
# past_due treatment as Razorpay's pending/halted.
_STRIPE_STATUS_MAP = {
    "incomplete": "created", "incomplete_expired": "expired",
    "trialing": "active", "active": "active",
    "past_due": "past_due", "unpaid": "past_due", "paused": "past_due",
    "canceled": "canceled",
}


def _stripe_secret_key() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("Stripe is not configured — set STRIPE_SECRET_KEY (docs/BILLING_SETUP.md)")
    return key


def _stripe_flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Stripe's v1 API takes classic form-encoded bodies, not JSON — nested
    dicts/lists become bracket-notation keys (`metadata[tenant_id]=t1`,
    `line_items[0][price]=price_1`)."""
    out: list[tuple[str, Any]] = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out.extend(_stripe_flatten(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                idx_key = f"{key}[{i}]"
                out.extend(_stripe_flatten(item, idx_key) if isinstance(item, dict)
                          else [(idx_key, item)])
        elif v is not None:
            out.append((key, v))
    return out


def _stripe_call(method: str, path: str, body: "dict[str, Any] | None" = None) -> dict[str, Any]:
    import requests

    r = requests.request(method, f"{_STRIPE_API}{path}", auth=(_stripe_secret_key(), ""),
                         data=_stripe_flatten(body or {}), timeout=15)
    r.raise_for_status()
    return r.json()


def stripe_create_customer(name: str, email: "str | None", tenant_id: str) -> str:
    body: dict[str, Any] = {"name": name, "metadata": {"tenant_id": tenant_id}}
    if email:
        body["email"] = email
    return _stripe_call("POST", "/customers", body)["id"]


def stripe_create_subscription(customer_id: str, plan_row: dict[str, Any],
                               tenant_id: str) -> CheckoutResult:
    price_id = plan_row.get("stripe_price_id")
    if not price_id:
        raise RuntimeError(
            f"plan {plan_row.get('slug')!r} has no stripe_price_id yet — create the matching "
            "Price in the Stripe dashboard (or via the API) once its price is signed off, "
            "then set plans.stripe_price_id.")
    r = _stripe_call("POST", "/checkout/sessions", {
        "mode": "subscription", "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": os.environ.get("STRIPE_CHECKOUT_SUCCESS_URL", "http://localhost:5173/billing?checkout=success"),
        "cancel_url": os.environ.get("STRIPE_CHECKOUT_CANCEL_URL", "http://localhost:5173/billing?checkout=cancel"),
        "client_reference_id": tenant_id,
        "subscription_data": {"metadata": {"tenant_id": tenant_id}},
    })
    # `r["id"]` (a Checkout Session, "cs_...") stands in for the real
    # subscription id until checkout completes — see the module docstring.
    return CheckoutResult(
        provider_subscription_id=r["id"], short_url=r["url"], status="created",
        provider_customer_id=customer_id,
    )


def stripe_verify_webhook(raw_body: bytes, headers: dict[str, str]) -> bool:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        log.warning("stripe webhook received but STRIPE_WEBHOOK_SECRET is unset — refusing")
        return False
    header = headers.get("stripe-signature") or headers.get("Stripe-Signature") or ""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp, sig = parts.get("t"), parts.get("v1")
    if not (timestamp and sig):
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:  # 5 min replay window, same as slack.verify_signature
            return False
    except ValueError:
        return False
    mine = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, sig)


def stripe_normalize_event(payload: dict[str, Any]) -> WebhookEvent:
    event_type = payload.get("type", "")
    obj = ((payload.get("data") or {}).get("object")) or {}
    event_id = payload.get("id", "")   # Stripe events have a real, stable id ("evt_...") -- unlike Razorpay's

    if event_type.startswith("customer.subscription."):
        return WebhookEvent(
            provider_event_id=event_id, event_type=event_type,
            provider_subscription_id=obj.get("id"),
            status=_STRIPE_STATUS_MAP.get(obj.get("status")),
            current_period_start=_epoch_to_iso(obj.get("current_period_start")),
            current_period_end=_epoch_to_iso(obj.get("current_period_end")),
            tenant_id=(obj.get("metadata") or {}).get("tenant_id"),
            raw=payload,
        )
    if event_type == "checkout.session.completed":
        # links our placeholder (the Checkout Session id) to the real
        # subscription Stripe just created for it. No status claim here —
        # the customer.subscription.* event Stripe fires around the same
        # time carries the actual, authoritative status.
        return WebhookEvent(
            provider_event_id=event_id, event_type=event_type,
            provider_subscription_id=obj.get("subscription"), status=None,
            tenant_id=obj.get("client_reference_id"), raw=payload,
        )
    if event_type == "invoice.payment_failed":
        return WebhookEvent(
            provider_event_id=event_id, event_type=event_type,
            provider_subscription_id=obj.get("subscription"), status="past_due", raw=payload,
        )
    return WebhookEvent(provider_event_id=event_id, event_type=event_type,
                        provider_subscription_id=None, status=None, raw=payload)


register(PaymentProviderSpec(
    slug="stripe",
    create_customer=stripe_create_customer,
    create_subscription=stripe_create_subscription,
    verify_webhook=stripe_verify_webhook,
    normalize_event=stripe_normalize_event,
))
