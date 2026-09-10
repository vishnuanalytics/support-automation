"""Billing chunk C — the payment-provider registry + Razorpay.

Real shapes (customer/plan/subscription create) were confirmed live against
Razorpay's test-mode API before writing this — see docs/PROJECT_SCOPE.md's
2026-09-10 entry. These tests stub `requests` (same pattern as
test_posthog.py) rather than hit the network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from interpreter import payments


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── registry ────────────────────────────────────────────────────────────
def test_razorpay_is_registered():
    spec = payments.get_provider("razorpay")
    assert spec.slug == "razorpay"


def test_stripe_is_registered():
    spec = payments.get_provider("stripe")
    assert spec.slug == "stripe"


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        payments.get_provider("paypal")


def test_provider_for_country_routes_india_to_razorpay_else_stripe():
    assert payments.provider_for_country("IN") == "razorpay"
    assert payments.provider_for_country("in") == "razorpay"   # case-insensitive
    assert payments.provider_for_country("US") == "stripe"
    assert payments.provider_for_country(None) == "stripe"
    assert payments.provider_for_country("") == "stripe"


# ── create_customer / create_subscription ──────────────────────────────
def test_razorpay_create_customer_posts_name_email_and_tenant_note(monkeypatch):
    captured = {}

    def fake_request(method, url, auth, timeout, json):
        captured.update(method=method, url=url, json=json)
        return _Resp(200, {"id": "cust_TaLNo5N2ICcgvt"})

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret_x")

    cid = payments.razorpay_create_customer("Acme Support", "owner@acme.test", "t1")
    assert cid == "cust_TaLNo5N2ICcgvt"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/customers")
    assert captured["json"] == {
        "name": "Acme Support", "email": "owner@acme.test", "notes": {"tenant_id": "t1"},
        "fail_existing": "0",
    }


def test_razorpay_create_customer_reuses_an_existing_customer_for_the_same_email(monkeypatch):
    """fail_existing="0" -- a second real subscribe attempt for an email
    Razorpay already has a customer for must hand back that customer, not
    500 the request (found live 2026-09-10: a real 400 without this)."""
    monkeypatch.setattr("requests.request",
                        lambda *a, **k: _Resp(200, {"id": "cust_existing"}))
    monkeypatch.setenv("RAZORPAY_KEY_ID", "x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "y")
    assert payments.razorpay_create_customer("Acme Support", "owner@acme.test", "t1") == "cust_existing"


def test_razorpay_create_customer_without_email_omits_it(monkeypatch):
    captured = {}
    monkeypatch.setattr("requests.request", lambda *a, **k: captured.update(k) or _Resp(200, {"id": "c1"}))
    monkeypatch.setenv("RAZORPAY_KEY_ID", "x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "y")
    payments.razorpay_create_customer("No Email Co", None, "t1")
    assert "email" not in captured["json"]


def test_razorpay_create_subscription_needs_a_provider_plan_id(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "y")
    with pytest.raises(RuntimeError, match="razorpay_plan_id"):
        payments.razorpay_create_subscription("cust_1", {"slug": "pro", "razorpay_plan_id": None}, "t1")


def test_razorpay_create_subscription_happy_path(monkeypatch):
    # a trimmed real response, captured live against api.razorpay.com
    captured = {}

    def fake_request(method, url, auth, timeout, json):
        captured.update(method=method, url=url, json=json)
        return _Resp(200, {
            "id": "sub_TaLOPhDA60q70S", "status": "created",
            "customer_id": "cust_TaLNo5N2ICcgvt",
            "short_url": "https://rzp.io/rzp/Zb02mBU",
        })

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "y")

    result = payments.razorpay_create_subscription(
        "cust_TaLNo5N2ICcgvt", {"slug": "pro", "razorpay_plan_id": "plan_TaLNxUHzhEj891"}, "t1")

    assert result.provider_subscription_id == "sub_TaLOPhDA60q70S"
    assert result.short_url == "https://rzp.io/rzp/Zb02mBU"
    assert result.status == "created"        # normalized (created -> created)
    assert result.provider_customer_id == "cust_TaLNo5N2ICcgvt"
    assert captured["json"]["plan_id"] == "plan_TaLNxUHzhEj891"
    assert captured["json"]["customer_id"] == "cust_TaLNo5N2ICcgvt"
    assert captured["json"]["notes"] == {"tenant_id": "t1"}


def test_razorpay_create_subscription_normalizes_active_status(monkeypatch):
    monkeypatch.setattr("requests.request", lambda *a, **k: _Resp(200, {
        "id": "sub_1", "status": "active", "short_url": "https://rzp.io/x", "customer_id": "c1",
    }))
    monkeypatch.setenv("RAZORPAY_KEY_ID", "x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "y")
    result = payments.razorpay_create_subscription("c1", {"razorpay_plan_id": "plan_1"}, "t1")
    assert result.status == "active"


def test_missing_credentials_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        payments.razorpay_create_customer("X", None, "t1")


# ── webhook signature verification ─────────────────────────────────────
def test_verify_webhook_accepts_a_correct_signature(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec_test")
    body = b'{"event":"subscription.activated"}'
    sig = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
    assert payments.razorpay_verify_webhook(body, {"x-razorpay-signature": sig}) is True


def test_verify_webhook_rejects_a_tampered_body(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec_test")
    body = b'{"event":"subscription.activated"}'
    sig = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
    tampered = b'{"event":"subscription.cancelled"}'
    assert payments.razorpay_verify_webhook(tampered, {"x-razorpay-signature": sig}) is False


def test_verify_webhook_refuses_everything_when_secret_unset(monkeypatch):
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    body = b'{"event":"subscription.activated"}'
    assert payments.razorpay_verify_webhook(body, {"x-razorpay-signature": "anything"}) is False


def test_verify_webhook_header_lookup_is_case_insensitive_key():
    # FastAPI's Request.headers is itself case-insensitive; this module
    # takes a plain dict, so it checks both the lower- and title-cased key.
    import os
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsec_test"
    try:
        body = b"{}"
        sig = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
        assert payments.razorpay_verify_webhook(body, {"X-Razorpay-Signature": sig}) is True
    finally:
        del os.environ["RAZORPAY_WEBHOOK_SECRET"]


# ── webhook event normalization ─────────────────────────────────────────
_ACTIVATED_PAYLOAD = {
    "entity": "event",
    "event": "subscription.activated",
    "payload": {"subscription": {"entity": {
        "id": "sub_TaLOPhDA60q70S", "status": "active",
        "current_start": 1789045500, "current_end": 1791723900,
    }}},
    "created_at": 1789045500,
}


def test_normalize_event_extracts_subscription_fields():
    ev = payments.razorpay_normalize_event(_ACTIVATED_PAYLOAD)
    assert ev.event_type == "subscription.activated"
    assert ev.provider_subscription_id == "sub_TaLOPhDA60q70S"
    assert ev.status == "active"
    assert ev.current_period_start == "2026-09-10T13:05:00+00:00"
    assert ev.current_period_end == "2026-10-11T13:05:00+00:00"
    assert ev.raw == _ACTIVATED_PAYLOAD


def test_normalize_event_maps_halted_to_past_due_and_cancelled_to_canceled():
    halted = {"event": "subscription.halted",
              "payload": {"subscription": {"entity": {"id": "s1", "status": "halted"}}}}
    cancelled = {"event": "subscription.cancelled",
                 "payload": {"subscription": {"entity": {"id": "s1", "status": "cancelled"}}}}
    assert payments.razorpay_normalize_event(halted).status == "past_due"
    assert payments.razorpay_normalize_event(cancelled).status == "canceled"


def test_normalize_event_id_is_deterministic_for_retries_but_differs_across_payloads():
    a = payments.razorpay_normalize_event(_ACTIVATED_PAYLOAD)
    b = payments.razorpay_normalize_event(dict(_ACTIVATED_PAYLOAD))  # Razorpay retrying the same delivery
    other = payments.razorpay_normalize_event({**_ACTIVATED_PAYLOAD, "event": "subscription.charged"})
    assert a.provider_event_id == b.provider_event_id
    assert a.provider_event_id != other.provider_event_id


def test_normalize_event_tolerates_no_subscription_in_payload():
    ev = payments.razorpay_normalize_event({"event": "payment.failed", "payload": {}})
    assert ev.event_type == "payment.failed"
    assert ev.provider_subscription_id is None
    assert ev.status is None


# ==========================================================================
# Stripe — real shapes (customer/product/price/checkout-session create)
# confirmed live against api.stripe.com before writing this.
# ==========================================================================
def test_stripe_flatten_nests_dicts_and_lists_in_bracket_notation():
    flat = dict(payments._stripe_flatten({
        "mode": "subscription",
        "metadata": {"tenant_id": "t1"},
        "line_items": [{"price": "price_1", "quantity": 1}],
    }))
    assert flat == {
        "mode": "subscription",
        "metadata[tenant_id]": "t1",
        "line_items[0][price]": "price_1",
        "line_items[0][quantity]": 1,
    }


def test_stripe_flatten_drops_none_values():
    flat = dict(payments._stripe_flatten({"name": "X", "email": None}))
    assert flat == {"name": "X"}


def test_stripe_create_customer_posts_form_encoded_name_and_metadata(monkeypatch):
    captured = {}

    def fake_request(method, url, auth, timeout, data):
        captured.update(method=method, url=url, auth=auth, data=dict(data))
        return _Resp(200, {"id": "cus_VEb3aKSDBEx6U6"})

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")

    cid = payments.stripe_create_customer("Acme Support", "owner@acme.test", "t1")
    assert cid == "cus_VEb3aKSDBEx6U6"
    assert captured["auth"] == ("sk_test_x", "")
    assert captured["url"].endswith("/customers")
    assert captured["data"] == {
        "name": "Acme Support", "email": "owner@acme.test", "metadata[tenant_id]": "t1",
    }


def test_stripe_create_subscription_needs_a_provider_price_id(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "x")
    with pytest.raises(RuntimeError, match="stripe_price_id"):
        payments.stripe_create_subscription("cus_1", {"slug": "pro", "stripe_price_id": None}, "t1")


def test_stripe_create_subscription_happy_path_returns_checkout_session_as_placeholder(monkeypatch):
    captured = {}

    def fake_request(method, url, auth, timeout, data):
        captured.update(method=method, url=url, data=dict(data))
        # a trimmed real response, captured live against api.stripe.com
        return _Resp(200, {
            "id": "cs_test_a1KBXTW0WlRnJi6OEsR2NwYzVpmp5ruOugieZ7fCzH8ZHsUYCKjlfGcoaq",
            "url": "https://checkout.stripe.com/c/pay/cs_test_a1KB...",
            "status": "open", "subscription": None,
            "client_reference_id": "t1",
        })

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "x")

    result = payments.stripe_create_subscription(
        "cus_VEb3aKSDBEx6U6", {"slug": "pro", "stripe_price_id": "price_1UE7rYQgoNUDQOam5RC5mFUc"}, "t1")

    assert result.provider_subscription_id.startswith("cs_test_")   # the *session* id, not a real sub yet
    assert result.short_url.startswith("https://checkout.stripe.com/")
    assert result.status == "created"
    assert result.provider_customer_id == "cus_VEb3aKSDBEx6U6"
    assert captured["data"]["mode"] == "subscription"
    assert captured["data"]["customer"] == "cus_VEb3aKSDBEx6U6"
    assert captured["data"]["line_items[0][price]"] == "price_1UE7rYQgoNUDQOam5RC5mFUc"
    assert captured["data"]["client_reference_id"] == "t1"
    assert captured["data"]["subscription_data[metadata][tenant_id]"] == "t1"


def test_stripe_missing_credentials_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        payments.stripe_create_customer("X", None, "t1")


# ── Stripe webhook signature verification ──────────────────────────────
def _stripe_sign(secret: bytes, body: bytes, timestamp: int) -> str:
    return hmac.new(secret, f"{timestamp}".encode() + b"." + body, hashlib.sha256).hexdigest()


def test_stripe_verify_webhook_accepts_a_correct_signature(monkeypatch):
    import time as _time

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    body = b'{"type":"customer.subscription.updated"}'
    ts = int(_time.time())
    sig = _stripe_sign(b"whsec_test", body, ts)
    header = f"t={ts},v1={sig}"
    assert payments.stripe_verify_webhook(body, {"stripe-signature": header}) is True


def test_stripe_verify_webhook_rejects_a_tampered_body(monkeypatch):
    import time as _time

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    ts = int(_time.time())
    sig = _stripe_sign(b"whsec_test", b'{"type":"a"}', ts)
    header = f"t={ts},v1={sig}"
    assert payments.stripe_verify_webhook(b'{"type":"b"}', {"stripe-signature": header}) is False


def test_stripe_verify_webhook_rejects_an_old_timestamp(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    body = b'{"type":"x"}'
    old_ts = 1000000000   # long past the 5-minute replay window
    sig = _stripe_sign(b"whsec_test", body, old_ts)
    header = f"t={old_ts},v1={sig}"
    assert payments.stripe_verify_webhook(body, {"stripe-signature": header}) is False


def test_stripe_verify_webhook_refuses_everything_when_secret_unset(monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    assert payments.stripe_verify_webhook(b"{}", {"stripe-signature": "t=1,v1=x"}) is False


def test_stripe_verify_webhook_rejects_a_malformed_header(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    assert payments.stripe_verify_webhook(b"{}", {"stripe-signature": "garbage"}) is False


# ── Stripe webhook event normalization ─────────────────────────────────
def test_stripe_normalize_subscription_updated_extracts_fields():
    payload = {
        "id": "evt_1ABC", "type": "customer.subscription.updated",
        "data": {"object": {
            "id": "sub_1XYZ", "status": "active",
            "current_period_start": 1789045500, "current_period_end": 1791723900,
            "metadata": {"tenant_id": "t1"},
        }},
    }
    ev = payments.stripe_normalize_event(payload)
    assert ev.provider_event_id == "evt_1ABC"
    assert ev.event_type == "customer.subscription.updated"
    assert ev.provider_subscription_id == "sub_1XYZ"
    assert ev.status == "active"
    assert ev.tenant_id == "t1"
    assert ev.current_period_start == "2026-09-10T13:05:00+00:00"
    assert ev.current_period_end == "2026-10-11T13:05:00+00:00"


def test_stripe_normalize_checkout_completed_links_session_to_tenant_with_no_status_claim():
    payload = {
        "id": "evt_2DEF", "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_a1KB", "subscription": "sub_1XYZ", "client_reference_id": "t1",
        }},
    }
    ev = payments.stripe_normalize_event(payload)
    assert ev.provider_subscription_id == "sub_1XYZ"   # the *real* sub id, not the session id
    assert ev.tenant_id == "t1"
    assert ev.status is None   # customer.subscription.* carries the real status, not this event


def test_stripe_normalize_invoice_payment_failed_maps_to_past_due():
    payload = {"id": "evt_3", "type": "invoice.payment_failed",
              "data": {"object": {"subscription": "sub_1XYZ"}}}
    ev = payments.stripe_normalize_event(payload)
    assert ev.status == "past_due"
    assert ev.provider_subscription_id == "sub_1XYZ"


def test_stripe_normalize_unknown_event_type_has_no_status_or_subscription():
    ev = payments.stripe_normalize_event({"id": "evt_4", "type": "payment_intent.created", "data": {}})
    assert ev.status is None
    assert ev.provider_subscription_id is None
    assert ev.provider_event_id == "evt_4"


def test_stripe_normalize_status_map_covers_incomplete_and_trialing():
    def _status_for(raw):
        return payments.stripe_normalize_event({
            "id": "evt_x", "type": "customer.subscription.updated",
            "data": {"object": {"id": "s1", "status": raw}},
        }).status
    assert _status_for("incomplete") == "created"
    assert _status_for("incomplete_expired") == "expired"
    assert _status_for("trialing") == "active"
    assert _status_for("unpaid") == "past_due"
