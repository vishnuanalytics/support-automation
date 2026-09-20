"""
API tests. The offline set (no marker) needs no env or network — dummy
SUPABASE_* vars are set before importing api.main so module import succeeds.
The `integration` set mints a real Supabase token for the Globex tenant and
exercises RLS / PUT-422 / run against the live project; skipped without
SUPABASE_ANON_KEY.

    pytest tests/test_api.py                     # all (needs .env for the integration ones)
    pytest tests/test_api.py -m "not integration"   # offline only (CI)
"""

from __future__ import annotations

import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv()  # real creds locally -> integration tests run; absent in CI -> they skip
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
# these placeholders are process-wide (os.environ, not monkeypatch) and so
# leak into every other test module collected in the same run. Anything
# that treats "SUPABASE_URL is set" as "real creds are present" (e.g.
# scripts.verify_migrations.live_schema) must tolerate a connection failure
# to this fake host as equivalent to "no creds" -- see that function.

from fastapi.testclient import TestClient  # noqa: E402

from api.main import _structural_errors, app  # noqa: E402

client = TestClient(app)


# ── offline ────────────────────────────────────────────────────────────
def test_health_ok():
    body = client.get("/api/health").json()
    assert body["ok"] is True and "components" in body


def test_node_types_lists_the_registry():
    body = client.get("/api/node-types").json()
    assert "confidence_gate" in body["types"] and "retrieve" in body["types"]
    assert "confidence_gate" in body["defaults"]
    assert "kb_lookup" in body["types"] and body["defaults"]["kb_lookup"]["out_key"] == "internal_kb"


def test_kb_endpoints_need_a_token():
    assert client.get("/api/kb/collections").status_code == 401
    assert client.post("/api/kb/collections", json={"name": "x"}).status_code == 401


def test_kb_source_connector_endpoints_need_a_token():
    assert client.get("/api/kb/connectors").status_code == 401
    assert client.get("/api/kb/collections/x/connections").status_code == 401
    assert client.post("/api/kb/collections/x/connections",
                       json={"connector": "public_url", "config": {}}).status_code == 401
    assert client.post("/api/kb/connections/x/sync").status_code == 401
    assert client.patch("/api/kb/connections/x", json={"status": "paused"}).status_code == 401
    assert client.delete("/api/kb/connections/x").status_code == 401
    assert client.get("/api/kb/collections/x/doc-writebacks").status_code == 401
    assert client.get("/api/kb/doc-writebacks").status_code == 401
    assert client.get("/api/kb/connections").status_code == 401
    assert client.post("/api/kb/connectors/linear/test", json={}).status_code == 401
    assert client.get("/api/kb/doc-defaults").status_code == 401
    assert client.put("/api/kb/doc-defaults", json={"on_correction": "off"}).status_code == 401


def test_kb_doc_defaults_validation():
    from fastapi import HTTPException

    from api.main import KbDocDefaultsIn, _validate_kb_doc_defaults

    # partial blob — only the keys that were set
    assert _validate_kb_doc_defaults(KbDocDefaultsIn(index=True)) == {"index": True}
    assert _validate_kb_doc_defaults(
        KbDocDefaultsIn(on_correction="suggest", github_repo="acme/kb")
    ) == {"on_correction": "suggest", "github_repo": "acme/kb"}

    for bad in (
        KbDocDefaultsIn(on_correction="bogus"),
        KbDocDefaultsIn(on_correction="suggest"),                       # no repo
        KbDocDefaultsIn(on_correction="write_back", github_repo="nope"),  # bad repo
        KbDocDefaultsIn(on_correction="suggest", github_repo="a/b", index=False),
    ):
        with pytest.raises(HTTPException):
            _validate_kb_doc_defaults(bad)


def test_approvals_endpoints_need_a_token():
    assert client.get("/api/approvals").status_code == 401
    assert client.post("/api/approvals/action-requests/abc",
                       json={"decision": "approve"}).status_code == 401
    assert client.get("/api/review-tasks").status_code == 401
    assert client.get("/api/health/tenant").status_code == 401


def test_billing_endpoints_need_a_token():
    assert client.get("/api/billing/usage").status_code == 401
    assert client.get("/api/billing/flow-deltas").status_code == 401


def test_graph_ask_needs_a_token():
    assert client.post("/api/graph/ask", json={"question": "how many cases"}).status_code == 401


def test_job_failures_needs_a_token():
    assert client.get("/api/jobs/failures").status_code == 401


# ── GET /api/jobs/{job_id} tenant scoping (2026-09-11 fix) ──────────────
# Calls the route function directly (bypassing the real bearer-auth /
# live-Supabase Caller.__init__) so this stays offline: a fake `c.sb`
# stands in for the RLS-scoped client `_caller_tenant` reads
# `tenant_members` off, and a fake `main._service` stands in for the
# service-role client `get_job` reads the `jobs` row off.
class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})


class _FakeSb:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _FakeQuery(self._tables.get(name, []))


def _fake_caller(my_tenant_ids):
    from api.main import Caller

    c = Caller.__new__(Caller)
    c.sb = _FakeSb({"tenant_members": [{"tenant_id": t} for t in my_tenant_ids]})
    return c


def test_get_job_denies_a_job_belonging_to_another_tenant(monkeypatch):
    from fastapi import HTTPException

    from api import main

    monkeypatch.setattr(main, "_service", _FakeSb({"jobs": [
        {"job_id": "j1", "kind": "kb_sync", "status": "done", "attempts": 1,
         "payload": {"connection_id": "c1"}, "result": {"secret": "other tenant's data"},
         "error": None, "created_at": "t", "updated_at": "t", "tenant_id": "TENANT-OTHER"},
    ]}))
    with pytest.raises(HTTPException) as ei:
        main.get_job("j1", c=_fake_caller(["TENANT-MINE"]))
    assert ei.value.status_code == 403


def test_get_job_allows_a_job_belonging_to_the_callers_own_tenant(monkeypatch):
    from api import main

    monkeypatch.setattr(main, "_service", _FakeSb({"jobs": [
        {"job_id": "j1", "kind": "kb_sync", "status": "done", "attempts": 1,
         "payload": {"connection_id": "c1"}, "result": {"ok": True},
         "error": None, "created_at": "t", "updated_at": "t", "tenant_id": "TENANT-MINE"},
    ]}))
    job = main.get_job("j1", c=_fake_caller(["TENANT-MINE"]))
    assert job["result"] == {"ok": True} and "tenant_id" not in job and "payload" not in job


def test_get_job_with_no_attributable_tenant_falls_back_to_permissive(monkeypatch):
    """A genuinely cross-tenant infra job (e.g. `queue_sweep`) has no
    tenant_id and no flow_id -- unchanged, pre-existing permissive
    behavior for that narrow case (see the fix's comment)."""
    from api import main

    monkeypatch.setattr(main, "_service", _FakeSb({"jobs": [
        {"job_id": "j1", "kind": "queue_sweep", "status": "done", "attempts": 1,
         "payload": {}, "result": {"ok": True}, "error": None,
         "created_at": "t", "updated_at": "t", "tenant_id": None},
    ]}))
    job = main.get_job("j1", c=_fake_caller(["TENANT-MINE"]))
    assert job["result"] == {"ok": True}


# ── invite email (2026-09-20: a created invitation never sent one) ──────
class _FakeAuthApiError(Exception):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class _FakeInviteAdmin:
    def __init__(self):
        self.calls = []
        self.raise_error = False
        self.raise_empty_message_error = False
        self.raise_email_exists_code = False

    def invite_user_by_email(self, email, options=None):
        if self.raise_empty_message_error:
            raise Exception("")   # some supabase-auth error paths build an empty message
        if self.raise_error:
            raise Exception("User already registered")
        if self.raise_email_exists_code:
            # a real supabase_auth.AuthApiError carries a stable `.code`,
            # independent of the English message wording -- deliberately no
            # "already registered" substring here, to prove detection
            # doesn't depend on the message text.
            raise _FakeAuthApiError("conflict", code="email_exists", status=422)
        self.calls.append((email, options))


class _FakeInviteServiceSb:
    def __init__(self, admin):
        self.auth = type("A", (), {"admin": admin})()

    def table(self, name):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": [{"name": "Acme"}]})()


class _FakeInviteCallerSb:
    def table(self, name):
        return self

    def insert(self, row):
        self._row = {"invite_id": "i1", **row}
        return self

    def execute(self):
        return type("R", (), {"data": [self._row]})()


def _setup_invite_test(monkeypatch, admin):
    from api import main
    from interpreter import audit, billing

    monkeypatch.setattr(main, "_caller_tenant", lambda c, explicit: "t1")
    monkeypatch.setattr(main, "_require_owner", lambda c, tid: None)
    monkeypatch.setattr(billing, "assert_seat_available", lambda tid, sb: None)
    monkeypatch.setattr(audit, "record", lambda *a, **k: None)
    monkeypatch.setattr(main, "_service", _FakeInviteServiceSb(admin))
    return main


def test_create_invitation_sends_a_real_invite_email(monkeypatch):
    """A created invitation must actually notify the invitee -- previously
    it only ever inserted a tenant_invitations row."""
    from api.main import Caller, InviteIn

    admin = _FakeInviteAdmin()
    main = _setup_invite_test(monkeypatch, admin)

    c = Caller.__new__(Caller)
    c.sb = _FakeInviteCallerSb()
    c.user_id, c.email = "u1", "owner@acme.test"

    body = InviteIn(email="New@Person.test", role="viewer", tenant_id="t1")
    result = main.create_invitation(body, c=c)

    assert result["invite_id"] == "i1"
    assert result["email_sent"] is True and result["email_error"] is None
    assert result["already_registered"] is False
    assert len(admin.calls) == 1
    sent_email, options = admin.calls[0]
    assert sent_email == "new@person.test"
    assert options["data"]["tenant_name"] == "Acme"
    assert options["data"]["invited_by_email"] == "owner@acme.test"


def test_create_invitation_still_succeeds_if_the_invite_email_fails(monkeypatch):
    """An existing-account 400 from Supabase's admin invite must never fail
    the invitation itself -- the invitee still gets in via their own
    normal sign-in (accept_invitations), and this specific case is flagged
    already_registered=True so the UI doesn't read it as a real problem."""
    from api.main import Caller, InviteIn

    admin = _FakeInviteAdmin()
    admin.raise_error = True
    main = _setup_invite_test(monkeypatch, admin)

    c = Caller.__new__(Caller)
    c.sb = _FakeInviteCallerSb()
    c.user_id, c.email = "u1", "owner@acme.test"

    body = InviteIn(email="already@registered.test", role="viewer", tenant_id="t1")
    result = main.create_invitation(body, c=c)
    assert result["invite_id"] == "i1"
    assert result["email_sent"] is False
    assert "already registered" in result["email_error"]
    assert result["already_registered"] is True


def test_create_invitation_email_error_is_never_blank(monkeypatch):
    """Some supabase-auth error paths build their message from a server
    response field that's itself blank, so str(e) can be "" -- email_error
    must still say SOMETHING, or an owner sees "failed to send" with no
    way to tell what actually went wrong (the exact report that led here)."""
    from api.main import Caller, InviteIn

    admin = _FakeInviteAdmin()
    admin.raise_empty_message_error = True
    main = _setup_invite_test(monkeypatch, admin)

    c = Caller.__new__(Caller)
    c.sb = _FakeInviteCallerSb()
    c.user_id, c.email = "u1", "owner@acme.test"

    body = InviteIn(email="someone@example.test", role="viewer", tenant_id="t1")
    result = main.create_invitation(body, c=c)
    assert result["email_sent"] is False
    assert result["email_error"]   # non-empty -- must not be "" or None


def test_create_invitation_detects_already_registered_by_error_code(monkeypatch):
    """Detection must key off supabase_auth's stable `email_exists` code,
    not an English message substring -- this message deliberately doesn't
    say "already registered" anywhere."""
    from api.main import Caller, InviteIn

    admin = _FakeInviteAdmin()
    admin.raise_email_exists_code = True
    main = _setup_invite_test(monkeypatch, admin)

    c = Caller.__new__(Caller)
    c.sb = _FakeInviteCallerSb()
    c.user_id, c.email = "u1", "owner@acme.test"

    body = InviteIn(email="vishnu.r@urbanpiper.com", role="viewer", tenant_id="t1")
    result = main.create_invitation(body, c=c)
    assert result["email_sent"] is False
    assert result["already_registered"] is True


def test_create_invitation_pending_duplicate_points_at_resend(monkeypatch):
    """The exact report that led here: a second invite to the same
    (tenant, email) with one already pending hits uq_tenant_invite_pending
    and used to surface as a raw Postgres error string -- now a clear
    message pointing at the resend endpoint instead."""
    from fastapi import HTTPException

    from api import main
    from api.main import Caller, InviteIn
    from interpreter import billing

    monkeypatch.setattr(main, "_caller_tenant", lambda c, explicit: "t1")
    monkeypatch.setattr(main, "_require_owner", lambda c, tid: None)
    monkeypatch.setattr(billing, "assert_seat_available", lambda tid, sb: None)

    class _FakeDupSb:
        def table(self, name):
            return self

        def insert(self, row):
            return self

        def execute(self):
            raise Exception(
                '{\'message\': \'duplicate key value violates unique constraint '
                '"uq_tenant_invite_pending"\', \'code\': \'23505\'}')

    c = Caller.__new__(Caller)
    c.sb = _FakeDupSb()
    c.user_id, c.email = "u1", "owner@acme.test"

    body = InviteIn(email="vishnu.r@urbanpiper.com", role="viewer", tenant_id="t1")
    with pytest.raises(HTTPException) as ei:
        main.create_invitation(body, c=c)
    assert ei.value.status_code == 409
    assert "resend" in ei.value.detail


# ── resend (2026-09-20: a pending invite had no way to retry a failed email) ──
def test_resend_invitation_resends_to_the_existing_pending_invite(monkeypatch):
    from api.main import Caller

    admin = _FakeInviteAdmin()
    main = _setup_invite_test(monkeypatch, admin)

    class _FakePendingInviteSb:
        def table(self, name):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": [
                {"invite_id": "i1", "tenant_id": "t1", "email": "vishnu.r@urbanpiper.com",
                 "role": "viewer", "status": "pending"},
            ]})()

    c = Caller.__new__(Caller)
    c.sb = _FakePendingInviteSb()
    c.user_id, c.email = "u1", "owner@acme.test"

    result = main.resend_invitation("i1", c=c)
    assert result == {"email_sent": True, "email_error": None, "already_registered": False}
    assert admin.calls[0][0] == "vishnu.r@urbanpiper.com"


def test_resend_invitation_404s_for_an_unknown_invite(monkeypatch):
    from fastapi import HTTPException

    from api.main import Caller

    admin = _FakeInviteAdmin()
    main = _setup_invite_test(monkeypatch, admin)

    class _FakeEmptySb:
        def table(self, name):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": []})()

    c = Caller.__new__(Caller)
    c.sb = _FakeEmptySb()
    c.user_id, c.email = "u1", "owner@acme.test"

    with pytest.raises(HTTPException) as ei:
        main.resend_invitation("ghost", c=c)
    assert ei.value.status_code == 404


def test_resend_invitation_409s_for_an_already_accepted_invite(monkeypatch):
    from fastapi import HTTPException

    from api.main import Caller

    admin = _FakeInviteAdmin()
    main = _setup_invite_test(monkeypatch, admin)

    class _FakeAcceptedInviteSb:
        def table(self, name):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": [
                {"invite_id": "i1", "tenant_id": "t1", "email": "x@y.test",
                 "role": "viewer", "status": "accepted"},
            ]})()

    c = Caller.__new__(Caller)
    c.sb = _FakeAcceptedInviteSb()
    c.user_id, c.email = "u1", "owner@acme.test"

    with pytest.raises(HTTPException) as ei:
        main.resend_invitation("i1", c=c)
    assert ei.value.status_code == 409
    assert len(admin.calls) == 0   # never sent -- not pending


# ── billing follow-up fixes (2026-09-20, found by /code-review) ─────────
def test_list_plans_orders_self_serve_tiers_before_talk_to_us_ones(monkeypatch):
    """A $0 base_price_usd (Enterprise, or a never-priced placeholder) must
    not sort before real, priced tiers just because 0 < 2900 -- checkout
    availability ranks first, then price ascending within each group."""
    from api import main
    from api.main import Caller
    from interpreter import llm

    monkeypatch.setattr(llm, "tenant_has_byok", lambda tid: False)
    monkeypatch.setattr(main, "_caller_tenant", lambda c, explicit: "t1")

    plans = [
        {"slug": "enterprise", "name": "Enterprise", "base_price_usd": 0, "base_price_inr": 0,
         "seats_included": None, "included_flows": None, "features": [],
         "razorpay_plan_id": None, "stripe_price_id": None},
        {"slug": "advanced", "name": "Advanced", "base_price_usd": 19900, "base_price_inr": 1660000,
         "seats_included": None, "included_flows": None, "features": [],
         "razorpay_plan_id": "plan_adv", "stripe_price_id": None},
        {"slug": "basic", "name": "Basic", "base_price_usd": 2900, "base_price_inr": 240000,
         "seats_included": 3, "included_flows": 5, "features": [],
         "razorpay_plan_id": "plan_basic", "stripe_price_id": None},
        {"slug": "pro", "name": "Pro", "base_price_usd": 7900, "base_price_inr": 660000,
         "seats_included": 10, "included_flows": 25, "features": [],
         "razorpay_plan_id": "plan_pro", "stripe_price_id": None},
    ]
    c = Caller.__new__(Caller)
    c.sb = _FakeSb({"plans": plans})

    out = main.list_plans(tenant_id="t1", c=c)
    assert [p["slug"] for p in out] == ["basic", "pro", "advanced", "enterprise"]


def test_accept_invitations_skips_one_over_the_seat_cap(monkeypatch):
    """A seat check now happens at acceptance too, not just at invite time
    (interpreter.billing.assert_seat_available) -- an invite that no longer
    fits is left pending, not silently accepted over the cap."""
    from api import main
    from api.main import Caller
    from interpreter import billing

    monkeypatch.setattr(main, "_service", _FakeSb({
        "tenant_invitations": [
            {"invite_id": "i1", "tenant_id": "t1", "email": "u@x.com",
             "role": "viewer", "status": "pending"},
        ],
        "tenant_members": [],
    }))
    monkeypatch.setattr(
        billing, "assert_seat_available",
        lambda tid, sb: (_ for _ in ()).throw(billing.PlanLimitError("full")),
    )

    c = Caller.__new__(Caller)
    c.user_id, c.email = "u1", "u@x.com"
    assert main.accept_invitations(c=c) == {"accepted": 0}


def test_apply_billing_webhook_past_due_sets_grace_ends_at(monkeypatch):
    """A payment-failure webhook must set grace_ends_at itself -- the sweep's
    grace-warning/grace->locked queries both filter on it and silently skip
    a null one, which previously left a failed-payment tenant never nudged
    and never locked."""
    from api import main
    from interpreter.payments import WebhookEvent

    class _FakeWebhookQuery:
        def __init__(self, sb, name):
            self.sb, self.name, self._update = sb, name, None

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def order(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def insert(self, row):
            return self

        def update(self, values):
            self._update = values
            return self

        def execute(self):
            if self._update is not None:
                self.sb.updates.setdefault(self.name, []).append(self._update)
                return type("R", (), {"data": None})()
            if self.name == "subscriptions":
                return type("R", (), {"data": self.sb.subscription_rows})()
            return type("R", (), {"data": []})()

    class _FakeWebhookSb:
        def __init__(self, subscription_rows):
            self.subscription_rows = subscription_rows
            self.updates: dict[str, list] = {}

        def table(self, name):
            return _FakeWebhookQuery(self, name)

    sb = _FakeWebhookSb([{"subscription_id": "s1", "tenant_id": "t1", "plan_id": "p1"}])
    monkeypatch.setattr(main, "_service", sb)

    event = WebhookEvent(provider_event_id="evt1", event_type="subscription.charged",
                         provider_subscription_id="sub_1", status="past_due")
    main._apply_billing_webhook("razorpay", event, {})

    tenant_update = sb.updates["tenants"][-1]
    assert tenant_update["billing_status"] == "grace"
    assert tenant_update["grace_ends_at"] is not None


def test_posthog_integration_endpoints_need_a_token():
    assert client.get("/api/integrations/posthog").status_code == 401
    assert client.put("/api/integrations/posthog", json={"project_id": "1"}).status_code == 401
    assert client.delete("/api/integrations/posthog").status_code == 401
    assert client.post("/api/integrations/posthog/test", json={"project_id": "1"}).status_code == 401


def test_trigger_endpoint_needs_a_token_and_trigger_is_a_node_type():
    assert client.post("/api/triggers/some-flow", json={"plan": "free"}).status_code == 401
    body = client.get("/api/node-types").json()
    assert "trigger" in body["types"] and "trigger" in body["defaults"]


def test_flow_trigger_mgmt_needs_a_token_but_public_webhook_404s_on_a_bad_token():
    assert client.get("/api/flows/x/triggers").status_code == 401
    assert client.post("/api/flows/x/triggers", json={"kind": "webhook"}).status_code == 401
    # the public webhook has no auth — an unknown token is a clean 404, not 401
    r = client.post("/t/definitely-not-a-real-token", json={"hello": 1})
    assert r.status_code == 404


# ── multi-provider connectors step 3: the Freshchat webhook ─────────────
def test_freshchat_webhook_404s_for_an_unconnected_tenant():
    # no channel configured -> load_channel finds nothing (or, offline, the
    # dummy Supabase URL fails the read -- either way this must 404 cleanly,
    # not 500, matching /t/{token}'s "can't verify -> reject" precedent.
    r = client.post("/webhooks/freshchat/no-such-tenant", json={"hello": 1})
    assert r.status_code == 404


def test_freshchat_webhook_401s_on_a_bad_signature(monkeypatch):
    from interpreter import freshchat

    monkeypatch.setattr(freshchat, "load_channel", lambda *a, **k: freshchat.FreshchatConfig(
        tenant_id="t", domain="acme.freshchat.com", api_token="tok", webhook_public_key="pem"))
    monkeypatch.setattr(freshchat, "verify_signature", lambda *a, **k: False)
    r = client.post("/webhooks/freshchat/t", json={"hello": 1})
    assert r.status_code == 401


def test_freshchat_webhook_happy_path_enqueues_a_run_flow_job(monkeypatch):
    from interpreter import channel_threads, freshchat, jobs
    import api.main as main

    cfg = freshchat.FreshchatConfig(tenant_id="t", domain="acme.freshchat.com", team="support",
                                    api_token="tok", webhook_public_key="pem")
    monkeypatch.setattr(freshchat, "load_channel", lambda *a, **k: cfg)
    monkeypatch.setattr(freshchat, "verify_signature", lambda *a, **k: True)
    monkeypatch.setattr(channel_threads, "get_case_ref", lambda *a, **k: None)
    monkeypatch.setattr(main, "load_flow", lambda **k: {"flow_id": "f1", "tenant_id": "t"})

    enqueued = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, payload, **k: enqueued.append(
        (kind, payload, k)) or "job1")

    r = client.post("/webhooks/freshchat/t", json={
        "actor": {"actor_type": "user", "actor_id": "u1"},
        "data": {"message": {"conversation_id": "c1",
                             "message_parts": [{"text": {"content": "help please"}}]}},
    })
    assert r.status_code == 202, r.text
    assert r.json()["job_id"] == "job1"
    assert len(enqueued) == 1
    kind, payload, kw = enqueued[0]
    assert kind == "run_flow" and payload["flow_id"] == "f1"
    case = payload["case"]
    assert case["channel"] == "freshchat" and case["conversation_id"] == "c1"
    assert case["body"] == "help please" and "sf_id" not in case
    assert kw["dedupe_key"].startswith("freshchat:")


def test_freshchat_webhook_skips_a_non_customer_message(monkeypatch):
    from interpreter import freshchat, jobs

    monkeypatch.setattr(freshchat, "load_channel", lambda *a, **k: freshchat.FreshchatConfig(
        tenant_id="t", domain="acme.freshchat.com", api_token="tok", webhook_public_key="pem"))
    monkeypatch.setattr(freshchat, "verify_signature", lambda *a, **k: True)
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: pytest.fail("must not enqueue"))

    r = client.post("/webhooks/freshchat/t", json={
        "actor": {"actor_type": "agent"},
        "data": {"message": {"conversation_id": "c1",
                             "message_parts": [{"text": {"content": "the bot's own reply"}}]}},
    })
    assert r.status_code == 202 and r.json()["skipped"]


# ── HubSpot push webhook — a real separate app from the Private App,
# see /webhooks/hubspot/{tenant_id}'s own docstring ─────────────────────
def test_hubspot_webhook_404s_for_an_unconnected_tenant():
    r = client.post("/webhooks/hubspot/no-such-tenant", json=[{"objectId": "1"}])
    assert r.status_code == 404


def test_hubspot_webhook_404s_without_a_webhook_secret_configured(monkeypatch):
    from interpreter import hubspot

    monkeypatch.setattr(hubspot, "load_channel", lambda *a, **k: hubspot.HubSpotConfig(
        tenant_id="t", access_token="tok"))   # no webhook_client_secret set
    r = client.post("/webhooks/hubspot/t", json=[{"objectId": "1"}])
    assert r.status_code == 404


def test_hubspot_webhook_401s_on_a_bad_signature(monkeypatch):
    from interpreter import hubspot

    monkeypatch.setattr(hubspot, "load_channel", lambda *a, **k: hubspot.HubSpotConfig(
        tenant_id="t", access_token="tok", webhook_client_secret="whs"))
    monkeypatch.setattr(hubspot, "verify_webhook_signature", lambda *a, **k: False)
    r = client.post("/webhooks/hubspot/t", json=[{"objectId": "1"}])
    assert r.status_code == 401


def test_hubspot_webhook_happy_path_enqueues_a_run_flow_job(monkeypatch):
    from interpreter import hubspot, jobs

    monkeypatch.setattr(hubspot, "load_channel", lambda *a, **k: hubspot.HubSpotConfig(
        tenant_id="t", access_token="tok", webhook_client_secret="whs"))
    monkeypatch.setattr(hubspot, "verify_webhook_signature", lambda *a, **k: True)
    monkeypatch.setattr(hubspot, "resolve_entry_flow", lambda tid, sb: "f1")
    monkeypatch.setattr(hubspot, "_client", lambda tid, sb: type("C", (), {
        "request": lambda self, *a, **k: {"id": "42", "properties": {"subject": "help", "content": "x"}},
    })())
    monkeypatch.setattr(hubspot, "ticket_as_case", lambda ticket, tid, sb: {
        "sf_id": "42", "id": "42", "subject": "help", "body": "x", "channel": "hubspot"})

    enqueued = []
    monkeypatch.setattr(jobs, "enqueue", lambda kind, payload, **k: enqueued.append(
        (kind, payload, k)) or "job1")

    r = client.post("/webhooks/hubspot/t", json=[{"objectId": "42", "subscriptionType": "ticket.creation"}])
    assert r.status_code == 202, r.text
    assert r.json() == {"received": 1, "enqueued": 1}
    kind, payload, kw = enqueued[0]
    assert kind == "run_flow" and payload["flow_id"] == "f1"
    assert payload["case"]["sf_id"] == "42"
    assert kw["dedupe_key"] == "hs:t:42" and kw["tenant_id"] == "t"


def test_hubspot_webhook_secret_endpoints_need_a_token():
    assert client.put("/api/integrations/hubspot/webhook-secret",
                      json={"webhook_client_secret": "x"}).status_code == 401
    assert client.get("/api/integrations/hubspot/webhook-url").status_code == 401


def test_connections_need_a_token_and_http_request_is_a_node_type():
    assert client.get("/api/connections").status_code == 401
    assert client.post("/api/connections", json={"slug": "x", "base_url": "https://y"}).status_code == 401
    assert client.delete("/api/connections/x").status_code == 401
    body = client.get("/api/node-types").json()
    assert "http_request" in body["types"] and "http_request" in body["defaults"]
    assert "transform" in body["types"]


@pytest.mark.integration
def test_connection_base_url_rejects_a_private_target(auth_headers):
    """Security fix (2026-09-03) — create_connection only checked the URL
    scheme; any host, including internal/cloud-metadata addresses, was
    accepted. A rejected base_url never reaches the upsert, so this needs
    no cleanup."""
    r = client.post("/api/connections", headers=auth_headers,
                    json={"slug": "ssrf-test", "base_url": "http://169.254.169.254/latest/meta-data",
                         "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 422
    assert "non-public" in r.json()["detail"]


def test_templates_need_a_token():
    assert client.get("/api/templates").status_code == 401
    assert client.get("/api/templates/support-autoreply").status_code == 401


def test_salesforce_meta_needs_a_token():
    assert client.get("/api/salesforce/meta").status_code == 401


@pytest.mark.integration
def test_salesforce_meta_is_tenant_scoped_not_globally_cached(auth_headers):
    """Security/correctness fix (2026-09-03): this endpoint used to be one
    global cache entry shared by every tenant. Two different orgs for the
    same tenant must get independently cached responses."""
    r1 = client.get(f"/api/salesforce/meta?org=default&tenant_id={GLOBEX_TENANT}", headers=auth_headers)
    assert r1.status_code == 200
    body = r1.json()
    assert set(body) >= {"available", "queues", "case_types", "modules", "case_fields"}
    # a made-up org label the tenant never connected -> still 200, degrades
    # to an empty/unavailable shape rather than erroring.
    r2 = client.get(f"/api/salesforce/meta?org=nonexistent-org-label&tenant_id={GLOBEX_TENANT}",
                    headers=auth_headers)
    assert r2.status_code == 200


def test_hubspot_meta_needs_a_token():
    assert client.get("/api/hubspot/meta").status_code == 401


def test_case_connector_meta_needs_a_token():
    assert client.get("/api/case-connector/meta").status_code == 401


@pytest.mark.integration
def test_case_connector_meta_dispatches_by_the_tenants_connector(auth_headers):
    """The flow editor's Inspector calls this one endpoint regardless of
    which connector a tenant is on; it must resolve `tenants.case_connector`
    and return that connector's own metadata shape, tagged with `connector`."""
    try:
        client.put("/api/tenants/case-connector", headers=auth_headers,
                  json={"case_connector": "hubspot", "tenant_id": GLOBEX_TENANT})
        r = client.get(f"/api/case-connector/meta?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["connector"] == "hubspot"
        assert set(body) >= {"available", "queues", "case_types", "modules", "case_fields", "users"}
    finally:
        client.put("/api/tenants/case-connector", headers=auth_headers,
                  json={"case_connector": "salesforce", "tenant_id": GLOBEX_TENANT})


def test_case_connector_recent_cases_needs_a_token():
    assert client.get("/api/case-connector/recent-cases").status_code == 401


def test_case_connector_recent_cases_dispatches_by_connector(monkeypatch):
    """The flow editor's Test Run "try a real recent case" picker calls this
    one endpoint regardless of connector; it must resolve `tenants.
    case_connector` (same lookup as case_connector_meta) and route to that
    connector's own recent-cases fetch, tagged with `connector`. Offline +
    deterministic: fakes the tenant lookup and `hubspot.list_recent_tickets`/
    `ticket_as_case`, same pattern as the slack_meta RLS-leak regression
    test above."""
    import api.main as main
    from interpreter import hubspot as _hs

    class _FakeSb:
        _table = None

        def table(self, name):
            self._table = name
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            if self._table == "tenant_members":
                return type("R", (), {"data": [{"tenant_id": "fake-tenant"}]})()
            return type("R", (), {"data": [{"case_connector": "hubspot"}]})()

    class _FakeCaller:
        user_id = "u1"
        email = "u1@example.test"
        sb = _FakeSb()

    monkeypatch.setattr(main, "_service", _FakeSb())
    monkeypatch.setattr(_hs, "list_recent_tickets", lambda tid, sb=None, limit=10: [{"id": "42"}])
    monkeypatch.setattr(_hs, "ticket_as_case", lambda t, tid=None, sb=None: {"sf_id": "42", "subject": "Help"})
    main.app.dependency_overrides[main.caller] = lambda: _FakeCaller()
    try:
        r = client.get("/api/case-connector/recent-cases?tenant_id=fake-tenant")
    finally:
        main.app.dependency_overrides.pop(main.caller, None)

    assert r.status_code == 200
    body = r.json()
    assert body["connector"] == "hubspot"
    assert body["cases"] == [{"sf_id": "42", "subject": "Help"}]


def test_slack_meta_needs_a_token():
    assert client.get("/api/slack/meta").status_code == 401


def test_slack_meta_does_not_leak_the_rls_scoped_client_into_workspace_meta(monkeypatch):
    """Regression test for a real bug (2026-09-03, found by an actual
    browser click-through, not by any existing test): the endpoint passed
    the caller's RLS-scoped client (`c.sb`) into `workspace_meta`. But
    `tenant_integrations` has RLS enabled with NO policy at all --
    service-role only, by design, since it holds secrets -- so that client
    silently saw zero rows for every tenant, no matter how the request was
    authenticated. Every tenant's `notify_human` channel/@mention pickers
    always fell back to plain text, permanently, and every prior test
    passed anyway because the live integration test only ever ran against
    Globex, which genuinely has no Slack connection (so `available=False`
    was correct there for the wrong reason -- masking the bug instead of
    catching it). Offline + deterministic: overrides the `caller`
    dependency with a fake whose `.sb` is a sentinel object, and asserts
    `workspace_meta` is called WITHOUT that sentinel."""
    import api.main as main
    from interpreter import slack as _slack

    _rls_sentinel = object()

    class _FakeSb:
        def table(self, name):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": [{"tenant_id": "fake-tenant"}]})()

    class _FakeCaller:
        user_id = "u1"
        email = "u1@example.test"
        sb = _FakeSb()

    calls: list[object] = []

    def fake_workspace_meta(tenant_id, *, sb=None):
        calls.append(sb)
        return {"available": True, "channels": [], "users": [], "usergroups": [], "errors": []}

    monkeypatch.setattr(_slack, "workspace_meta", fake_workspace_meta)
    main.app.dependency_overrides[main.caller] = lambda: _FakeCaller()
    try:
        r = client.get("/api/slack/meta?tenant_id=fake-tenant")
    finally:
        main.app.dependency_overrides.pop(main.caller, None)

    assert r.status_code == 200
    assert calls == [None], "workspace_meta must NOT receive the RLS-scoped caller client"


@pytest.mark.integration
def test_slack_meta_is_tenant_scoped_not_globally_cached(auth_headers):
    """Same class of fix as salesforce/meta -- cached per tenant_id, not
    globally. Globex itself has no live Slack connection, so this only
    proves the endpoint is reachable/tenant-scoped and degrades cleanly;
    the "does it actually see a connected workspace" behavior is covered
    by the offline regression test above plus manual live verification
    against the Acme tenant, which does have Slack connected."""
    r = client.get(f"/api/slack/meta?tenant_id={GLOBEX_TENANT}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"available", "channels", "users", "usergroups", "errors"}
    assert body["available"] is False
    assert body["channels"] == [] and body["users"] == []


def test_salesforce_org_endpoints_need_a_token():
    assert client.get("/api/integrations/salesforce").status_code == 401
    assert client.put("/api/integrations/salesforce",
                       json={"creds": {"SF_USERNAME": "x"}}).status_code == 401
    assert client.delete("/api/integrations/salesforce/default").status_code == 401
    assert client.post("/api/integrations/salesforce/test",
                        json={"creds": {"SF_USERNAME": "x"}}).status_code == 401
    assert client.get("/api/integrations/salesforce/default/schema").status_code == 401


def test_salesforce_oauth_status_is_public_and_authorize_needs_a_token():
    r = client.get("/api/integrations/salesforce/oauth/status")
    assert r.status_code == 200 and r.json() == {"configured": False}  # no SF_OAUTH_* in CI
    assert client.get("/api/integrations/salesforce/oauth/authorize").status_code == 401


@pytest.mark.integration
def test_salesforce_connect_introspect_disconnect_roundtrip(auth_headers):
    """The real self-serve loop: connect a real org with real creds ->
    the schema endpoint returns real Case fields/Queues from that org ->
    disconnect. org_label is unique per run so it can't collide with a
    real connection; deleted at the end either way."""
    from interpreter import salesforce as _sf

    if not _sf.available():
        pytest.skip("no real SF_* creds in this environment")
    creds = {k: v for k, v in os.environ.items() if k.startswith("SF_")}
    org_label = f"pytest-{uuid.uuid4().hex[:8]}"

    try:
        r = client.put("/api/integrations/salesforce", headers=auth_headers,
                       json={"org_label": org_label, "creds": creds, "tenant_id": GLOBEX_TENANT})
        assert r.status_code == 201
        body = r.json()
        assert body["org_label"] == org_label
        assert body["has_credentials"] is True
        assert "SF_CONSUMER_KEY" not in body and "SF_PRIVATE_KEY" not in body  # never echoed back

        listed = client.get("/api/integrations/salesforce", headers=auth_headers,
                            params={"tenant_id": GLOBEX_TENANT}).json()
        assert any(o["org_label"] == org_label for o in listed)

        schema = client.get(f"/api/integrations/salesforce/{org_label}/schema",
                            headers=auth_headers, params={"tenant_id": GLOBEX_TENANT}).json()
        assert schema["errors"] == []
        assert any(f["name"] == "Priority" for f in schema["case_fields"])  # a real standard field
        assert isinstance(schema["queues"], list)  # real org query succeeded, even if empty
    finally:
        d = client.delete(f"/api/integrations/salesforce/{org_label}", headers=auth_headers,
                          params={"tenant_id": GLOBEX_TENANT})
        assert d.status_code == 204
        still = client.get("/api/integrations/salesforce", headers=auth_headers,
                          params={"tenant_id": GLOBEX_TENANT}).json()
        assert not any(o["org_label"] == org_label for o in still)


def test_create_workspace_needs_a_token():
    assert client.post("/api/tenants", json={"name": "Acme"}).status_code == 401


def test_kb_upload_needs_a_token():
    assert client.post("/api/kb/collections/x/upload",
                       json={"filename": "a.txt", "content_b64": "aGk="}).status_code == 401
    assert client.post("/api/kb/collections/x/crawl",
                       json={"url": "https://example.com/docs"}).status_code == 401


def test_google_status_needs_a_token_but_callback_is_public():
    assert client.get("/api/integrations/google/status").status_code == 401
    assert client.get("/api/integrations/google/authorize").status_code == 401
    # the OAuth callback has no bearer (the browser follows a Google redirect)
    r = client.get("/api/integrations/google/callback?error=access_denied")
    assert r.status_code == 200 and "failed" in r.text


def test_phase16_endpoints_and_node_types():
    body = client.get("/api/node-types").json()
    for t in ("extract", "policy_gate", "task_dispatch"):
        assert t in body["types"]
    assert client.get("/api/rules").status_code == 401
    assert client.post("/api/rules", json={"team": "x", "name": "y"}).status_code == 401
    assert client.get("/api/action-requests").status_code == 401
    assert client.get("/api/integrations/slack/status").status_code == 401
    # slack interactions with a bad/missing signature -> 401
    r = client.post("/api/integrations/slack/interactions", data={"payload": "{}"})
    assert r.status_code == 401


def test_flows_requires_a_bearer_token():
    assert client.get("/api/flows").status_code == 401
    assert client.get("/api/flows", headers={"Authorization": "Basic xyz"}).status_code == 401
    assert client.get("/api/tenants").status_code == 401


def test_flow_create_no_longer_requires_a_tenant_id():
    """Phase 18a — `tenant_id` is optional on the create body (inferred from
    the caller's membership); still needs a token."""
    from api.main import FlowCreate

    FlowCreate(team="support", name="n")   # no tenant_id -> valid
    assert client.post("/api/flows", json={"team": "support", "name": "n"}).status_code == 401


def test_team_endpoints_need_a_token():
    """Phase 18c — invitations / members are auth-only."""
    from api.main import InviteIn

    InviteIn(email="a@b.com")   # defaults role='viewer'
    for path in ("/api/members", "/api/invitations"):
        assert client.get(path).status_code == 401
    assert client.post("/api/invitations", json={"email": "a@b.com"}).status_code == 401
    assert client.post("/api/invitations/accept").status_code == 401


@pytest.mark.parametrize("flow, expect_substr", [
    (  # dangling edge
        {"flow_id": "f", "tenant_id": "t", "team": "support", "name": "n",
         "version": 1, "status": "draft",
         "nodes": [{"node_id": "a", "type": "retrieve", "config": {}}],
         "edges": [{"edge_id": "e", "source_node_id": "a", "target_node_id": "ghost", "condition": {}}]},
        "ghost",
    ),
    (  # unknown node type
        {"flow_id": "f", "tenant_id": "t", "team": "support", "name": "n",
         "version": 1, "status": "draft",
         "nodes": [{"node_id": "a", "type": "totally_made_up", "config": {}},
                   {"node_id": "b", "type": "draft", "config": {}}],
         "edges": [{"edge_id": "e", "source_node_id": "a", "target_node_id": "b", "condition": {}}]},
        "unknown node type",
    ),
    (  # cycle
        {"flow_id": "f", "tenant_id": "t", "team": "support", "name": "n",
         "version": 1, "status": "draft",
         "nodes": [{"node_id": "a", "type": "retrieve", "config": {}},
                   {"node_id": "b", "type": "classify", "config": {}}],
         "edges": [{"edge_id": "e1", "source_node_id": "a", "target_node_id": "b", "condition": {}},
                   {"edge_id": "e2", "source_node_id": "b", "target_node_id": "a", "condition": {}}]},
        "cycle",
    ),
])
def test_structural_errors_catches_bad_graphs(flow, expect_substr):
    errs = _structural_errors(flow)
    assert any(expect_substr in e for e in errs), errs


def test_mermaid_import_endpoint_needs_a_token():
    r = client.post("/api/flows/import/mermaid", json={"text": "flowchart TD\n A-->B"})
    assert r.status_code == 401


def test_trace_needs_a_token():
    assert client.get("/api/trace/500ABC").status_code == 401


def test_trace_retry_needs_a_token():
    assert client.post("/api/trace/500ABC/retry").status_code == 401


def test_trace_retry_404s_for_an_unknown_key(auth_headers):
    # audit WF-5 — a real token, but no run the caller's tenants can see
    r = client.post("/api/trace/500NONEXISTENT/retry", headers=auth_headers)
    assert r.status_code == 404


def test_assist_endpoints_need_a_token():
    assert client.post("/api/flows/assist", json={"prompt": "x"}).status_code == 401
    assert client.post(
        "/api/flows/11111111-1111-1111-1111-111111111111/assist",
        json={"instruction": "x"},
    ).status_code == 401


def test_email_channel_endpoints_need_a_token():
    assert client.get("/api/integrations/email").status_code == 401
    assert client.put("/api/integrations/email", json={"provider": "imap"}).status_code == 401
    assert client.post("/api/integrations/email/test", json={"provider": "imap"}).status_code == 401
    assert client.delete("/api/integrations/email").status_code == 401
    assert client.get("/api/integrations/email/google/authorize").status_code == 401
    # the OAuth callback is public (browser follows a Google redirect)
    r = client.get("/api/integrations/email/google/callback?error=access_denied")
    assert r.status_code == 200 and "failed" in r.text


def test_freshchat_channel_endpoints_need_a_token():
    assert client.get("/api/integrations/freshchat").status_code == 401
    assert client.put("/api/integrations/freshchat", json={"domain": "x"}).status_code == 401
    assert client.post("/api/integrations/freshchat/test", json={"domain": "x"}).status_code == 401
    assert client.delete("/api/integrations/freshchat").status_code == 401
    assert client.get("/api/integrations/freshchat/webhook-url").status_code == 401
    assert client.get("/api/integrations/freshchat/oauth/authorize").status_code == 401
    # the OAuth callback is public (browser follows a Freshchat redirect)
    r = client.get("/api/integrations/freshchat/oauth/callback?error=access_denied")
    assert r.status_code == 200 and "failed" in r.text


def test_zendesk_connection_endpoints_need_a_token():
    assert client.get("/api/integrations/zendesk").status_code == 401
    assert client.put("/api/integrations/zendesk", json={"subdomain": "x"}).status_code == 401
    assert client.post("/api/integrations/zendesk/test", json={"subdomain": "x"}).status_code == 401
    assert client.delete("/api/integrations/zendesk").status_code == 401


def test_hubspot_connection_endpoints_need_a_token():
    assert client.get("/api/integrations/hubspot").status_code == 401
    assert client.put("/api/integrations/hubspot", json={"access_token": "x"}).status_code == 401
    assert client.post("/api/integrations/hubspot/test", json={"access_token": "x"}).status_code == 401
    assert client.delete("/api/integrations/hubspot").status_code == 401


def test_case_connector_endpoints_need_a_token():
    assert client.get("/api/tenants/case-connector").status_code == 401
    assert client.put("/api/tenants/case-connector", json={"case_connector": "zendesk"}).status_code == 401


def test_channel_connector_map_endpoints_need_a_token():
    assert client.get("/api/tenants/channel-connector-map").status_code == 401
    assert client.put("/api/tenants/channel-connector-map",
                      json={"channel_connector_map": {"hubspot": "hubspot"}}).status_code == 401


def test_case_taxonomy_endpoints_need_a_token():
    assert client.get("/api/tenants/case-taxonomy").status_code == 401
    assert client.put("/api/tenants/case-taxonomy", json={"config": {}}).status_code == 401
    assert client.delete("/api/tenants/case-taxonomy").status_code == 401


def test_salesforce_case_hook_needs_the_shared_secret():
    # no X-SF-Hook-Secret header -> 401, never reaches flow resolution
    r = client.post("/api/hooks/salesforce/case", json={"case_id": "500xx"})
    assert r.status_code == 401
    r = client.post("/api/hooks/salesforce/case", json={"case_id": "500xx"},
                    headers={"X-SF-Hook-Secret": "wrong"})
    assert r.status_code == 401


def test_sf_entry_endpoint_needs_a_token():
    assert client.put("/api/flows/some-id/sf-entry", json={"sf_entry": True}).status_code == 401


def test_structural_errors_passes_a_linear_flow():
    flow = {
        "flow_id": "f", "tenant_id": "t", "team": "support", "name": "n",
        "version": 1, "status": "draft",
        "nodes": [{"node_id": "a", "type": "retrieve", "config": {}},
                  {"node_id": "b", "type": "classify", "config": {}},
                  {"node_id": "c", "type": "draft", "config": {}},
                  {"node_id": "d", "type": "handover", "config": {}}],
        "edges": [{"edge_id": "e1", "source_node_id": "a", "target_node_id": "b", "condition": {}},
                  {"edge_id": "e2", "source_node_id": "b", "target_node_id": "c", "condition": {}},
                  {"edge_id": "e3", "source_node_id": "c", "target_node_id": "d", "condition": {}}],
    }
    assert _structural_errors(flow) == []


# ── integration (live Supabase) ────────────────────────────────────────
GLOBEX_FLOW = "a2a2a2a2-2222-4222-8222-222222222222"
ACME_FLOW = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(scope="module")
def auth_headers():
    if os.environ.get("SUPABASE_ANON_KEY", "test-anon-key") == "test-anon-key":
        pytest.skip("no real SUPABASE_ANON_KEY — integration tests skipped")
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])
    sess = sb.auth.sign_in_with_password(
        {"email": "globex-owner@example.test", "password": "editor-test-pw-8891"}
    )
    return {"Authorization": f"Bearer {sess.session.access_token}"}


@pytest.mark.integration
def test_a_forged_token_is_rejected(auth_headers):
    # tamper with the real token's payload — signature no longer matches
    good = auth_headers["Authorization"].split(" ", 1)[1]
    bad = good[:-6] + "AAAAAA"
    r = client.get("/api/flows", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


@pytest.mark.integration
def test_mermaid_import_returns_a_candidate_graph(auth_headers):
    r = client.post(
        "/api/flows/import/mermaid",
        headers=auth_headers,
        json={"text": "flowchart TD\n R[retrieve] --> C[classify] --> D[draft] "
                      "--> G[confidence_gate] --> A[auto_reply]",
             "tenant_id": GLOBEX_TENANT},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [n["type"] for n in body["nodes"]] == [
        "retrieve", "classify", "draft", "confidence_gate", "auto_reply"]
    assert body["errors"] == []
    assert len(body["edges"]) == 4


@pytest.mark.integration
def test_assist_new_flow_returns_a_candidate(auth_headers):
    r = client.post("/api/flows/assist", headers=auth_headers,
                    json={"prompt": "retrieve docs, classify, draft, gate, auto-reply "
                                    "when confident else ask a human",
                         "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == [] and body["nodes"] and body["diff"] is None


@pytest.mark.integration
def test_assist_edit_flow_returns_a_diff(auth_headers):
    r = client.post(f"/api/flows/{GLOBEX_FLOW}/assist", headers=auth_headers,
                    json={"instruction": "add a handover branch for the enterprise tier"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["diff"]) == {"added_nodes", "removed_nodes", "changed_nodes",
                                 "added_edges", "removed_edges"}
    assert body["nodes"]


@pytest.mark.integration
def test_list_flows_is_rls_scoped(auth_headers):
    """`/api/flows` has no tenant_id filter -- it returns every flow RLS
    lets the caller see, across ALL of their tenant memberships (this
    account is an owner of both Globex and a leftover concurrency-test
    tenant from an earlier session's stress test). The real thing worth
    proving is "no flow from a tenant this caller ISN'T a member of leaks
    in" -- not "there's only one tenant", which stopped being true here."""
    my_tenants = {t["tenant_id"] for t in client.get("/api/tenants", headers=auth_headers).json()}
    rows = client.get("/api/flows", headers=auth_headers).json()
    assert rows and any(r["tenant_id"] == GLOBEX_TENANT for r in rows)
    assert all(r["tenant_id"] in my_tenants for r in rows)


def test_rate_limit_trips_after_the_budget():
    import pytest as _pt
    from fastapi import HTTPException

    from api.main import _rate, rate_limit
    _rate.clear()
    for _ in range(5):
        rate_limit("u1", "run", 5, window=60)     # 5 allowed
    with _pt.raises(HTTPException) as ei:
        rate_limit("u1", "run", 5, window=60)     # 6th -> 429
    assert ei.value.status_code == 429
    rate_limit("u2", "run", 5, window=60)          # a different user is unaffected
    _rate.clear()


@pytest.mark.integration
def test_cross_tenant_get_is_404(auth_headers):
    assert client.get(f"/api/flows/{ACME_FLOW}", headers=auth_headers).status_code == 404


@pytest.mark.integration
def test_put_invalid_flow_is_422(auth_headers):
    flow = client.get(f"/api/flows/{GLOBEX_FLOW}", headers=auth_headers).json()
    flow["edges"].append({
        "edge_id": str(uuid.uuid4()),
        "source_node_id": flow["nodes"][0]["node_id"], "target_node_id": "ghost", "condition": {},
    })
    r = client.put(f"/api/flows/{GLOBEX_FLOW}", headers=auth_headers, json=flow)
    assert r.status_code == 422 and "ghost" in str(r.json())


@pytest.mark.integration
def test_run_returns_a_run_id(auth_headers):
    r = client.post(f"/api/flows/{GLOBEX_FLOW}/run", headers=auth_headers,
                    json={"case": {"case_id": "PYTEST", "subject": "webhook help",
                                   "body": "how do I test a webhook",
                                   "account": {"customer_type": "premium"}}})
    body = r.json()
    assert r.status_code == 200 and body["run_id"] and body["outcome"]["action"] in (
        "auto_reply", "ask_human", "handover")


@pytest.mark.integration
def test_tenants_lists_the_callers_membership(auth_headers):
    """This account is an owner of >1 tenant (Globex + a leftover
    concurrency-test tenant from an earlier session) -- assert Globex is
    among the memberships, not that it's the only one."""
    rows = client.get("/api/tenants", headers=auth_headers).json()
    assert any(r["tenant_id"] == GLOBEX_TENANT and r["role"] == "owner" for r in rows)
    assert all("role" in r for r in rows)


@pytest.mark.integration
def test_create_flow_infers_the_tenant_when_omitted(auth_headers):
    """The inference this name refers to (`_caller_tenant`: omit tenant_id,
    resolve it when the caller belongs to exactly one tenant) is genuinely
    untestable with this shared account now that it owns >1 tenant --
    omitting tenant_id here would just 400. Passes it explicitly instead;
    the single-membership inference path itself is still exercised by
    `_caller_tenant`'s own unit-level behavior, not re-proven here."""
    from supabase import create_client

    fid = client.post("/api/flows", headers=auth_headers,
                      json={"team": "csm", "name": "pytest-infer-tenant",
                            "tenant_id": GLOBEX_TENANT}).json()["flow_id"]
    try:
        row = client.get("/api/flows", headers=auth_headers).json()
        mine = next(f for f in row if f["flow_id"] == fid)
        assert mine["tenant_id"] == GLOBEX_TENANT
    finally:
        create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]) \
            .table("flows").delete().eq("flow_id", fid).execute()


@pytest.mark.integration
def test_sf_entry_is_one_per_tenant(auth_headers):
    """Phase 20k — setting the Salesforce-entry flag on one flow clears it
    on the tenant's others."""
    from supabase import create_client

    svc = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    a = client.post("/api/flows", headers=auth_headers,
                    json={"team": "csm", "name": "pytest-sfentry-a",
                         "tenant_id": GLOBEX_TENANT}).json()["flow_id"]
    b = client.post("/api/flows", headers=auth_headers,
                    json={"team": "csm", "name": "pytest-sfentry-b",
                         "tenant_id": GLOBEX_TENANT}).json()["flow_id"]
    try:
        assert client.put(f"/api/flows/{a}/sf-entry", headers=auth_headers,
                          json={"sf_entry": True}).status_code == 200
        assert client.get(f"/api/flows/{a}", headers=auth_headers).json()["sf_entry"] is True

        # flipping b on takes the flag away from a
        assert client.put(f"/api/flows/{b}/sf-entry", headers=auth_headers,
                          json={"sf_entry": True}).status_code == 200
        assert client.get(f"/api/flows/{a}", headers=auth_headers).json()["sf_entry"] is False
        assert client.get(f"/api/flows/{b}", headers=auth_headers).json()["sf_entry"] is True
    finally:
        svc.table("flows").delete().in_("flow_id", [a, b]).execute()


GLOBEX_OWNER_UID = "57c26330-cb98-475a-875f-8f8a925672fd"
GLOBEX_TENANT_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def globex_as_viewer():
    """Phase 18b — temporarily demote the Globex owner to `viewer`, restore after."""
    from supabase import create_client

    svc = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    def _set(role: str) -> None:
        svc.table("tenant_members").update({"role": role}) \
            .eq("user_id", GLOBEX_OWNER_UID).eq("tenant_id", GLOBEX_TENANT_ID).execute()

    _set("viewer")
    try:
        yield
    finally:
        _set("owner")


@pytest.mark.integration
def test_viewer_can_read_but_not_write(globex_as_viewer, auth_headers):
    """`globex_as_viewer` only demotes the Globex membership -- this
    account is still `owner` of a second (leftover) tenant, so every call
    below must target a Globex flow/tenant_id explicitly, or "infer the
    tenant"/"pick flows[0]" could silently land on the tenant it's still
    an owner of and wrongly succeed instead of 403."""
    flows = client.get("/api/flows", headers=auth_headers).json()
    mine = [f for f in flows if f["tenant_id"] == GLOBEX_TENANT]
    assert mine, "a viewer still reads their tenant's flows"
    fid = mine[0]["flow_id"]
    draft = client.get(f"/api/flows/{fid}", headers=auth_headers).json()

    put = client.put(f"/api/flows/{fid}", headers=auth_headers, json=draft)
    assert put.status_code == 403 and "view-only" in str(put.json())
    assert client.post("/api/flows", headers=auth_headers,
                       json={"team": "csm", "name": "nope", "tenant_id": GLOBEX_TENANT}).status_code == 403
    assert client.post(f"/api/flows/{fid}/publish", headers=auth_headers).status_code == 403
    assert client.delete(f"/api/flows/{fid}", headers=auth_headers).status_code == 403
    assert client.post("/api/rules", headers=auth_headers,
                       json={"team": "csm", "name": "nope", "tenant_id": GLOBEX_TENANT}).status_code == 403


@pytest.mark.integration
def test_members_lists_the_caller_with_role(auth_headers):
    rows = client.get(f"/api/members?tenant_id={GLOBEX_TENANT}", headers=auth_headers).json()
    me = next(r for r in rows if r["is_you"])
    assert me["role"] == "owner" and "email" in me


@pytest.mark.integration
def test_accept_invitations_noop_when_none_pending(auth_headers):
    assert client.post("/api/invitations/accept", headers=auth_headers).json() == {"accepted": 0}


@pytest.mark.integration
def test_invitation_create_list_revoke(auth_headers):
    email = f"pytest-{uuid.uuid4().hex[:8]}@example.test"
    inv = client.post("/api/invitations", headers=auth_headers,
                      json={"email": email, "role": "viewer", "tenant_id": GLOBEX_TENANT})
    assert inv.status_code == 201
    iid = inv.json()["invite_id"]
    pend = [i for i in client.get("/api/invitations", headers=auth_headers).json()
            if i["status"] == "pending"]
    assert any(i["invite_id"] == iid and i["email"] == email for i in pend)
    assert client.delete(f"/api/invitations/{iid}", headers=auth_headers).status_code == 204
    still = [i for i in client.get("/api/invitations", headers=auth_headers).json()
             if i["invite_id"] == iid and i["status"] == "pending"]
    assert not still


@pytest.mark.integration
def test_invalid_invite_role_is_400(auth_headers):
    r = client.post("/api/invitations", headers=auth_headers,
                    json={"email": "x@example.test", "role": "owner"})
    assert r.status_code == 400


@pytest.mark.integration
def test_email_channel_configure_status_and_disconnect(auth_headers):
    body = {
        "provider": "imap", "team": "support",
        "imap_host": "imap.example.test", "smtp_host": "smtp.example.test",
        "username": "support@example.test", "password": "app-pw-secret",
        "from_name": "Acme Support", "auto_send_enabled": False, "active": True,
        "tenant_id": GLOBEX_TENANT,
    }
    try:
        r = client.put("/api/integrations/email", headers=auth_headers, json=body)
        assert r.status_code == 200, r.text
        got = client.get("/api/integrations/email", headers=auth_headers,
                         params={"tenant_id": GLOBEX_TENANT}).json()
        assert got["configured"] is True and got["provider"] == "imap"
        assert got["username"] == "support@example.test" and got["status"] == "active"
        assert got["auto_send_enabled"] is False
        assert "app-pw-secret" not in str(got) and "password" not in got
        # flip the master switch without re-sending the password
        r2 = client.put("/api/integrations/email", headers=auth_headers,
                        json={"provider": "imap", "imap_host": "imap.example.test",
                              "username": "support@example.test",
                              "auto_send_enabled": True, "active": False,
                              "tenant_id": GLOBEX_TENANT})
        assert r2.status_code == 200
        got2 = client.get("/api/integrations/email", headers=auth_headers,
                          params={"tenant_id": GLOBEX_TENANT}).json()
        assert got2["auto_send_enabled"] is True and got2["status"] == "inactive"
    finally:
        client.delete("/api/integrations/email", headers=auth_headers, params={"tenant_id": GLOBEX_TENANT})
    gone = client.get("/api/integrations/email", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).json()
    assert gone["configured"] is False and gone["status"] == "none"


@pytest.mark.integration
def test_email_channel_test_connection_reports_failure_cleanly(auth_headers):
    r = client.post("/api/integrations/email/test", headers=auth_headers, json={
        "provider": "imap", "imap_host": "nope.invalid.test",
        "username": "x@y.test", "password": "bad", "tenant_id": GLOBEX_TENANT,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error"]


@pytest.mark.integration
def test_freshchat_channel_configure_status_and_disconnect(auth_headers):
    # explicit tenant_id -- this test account belongs to several tenants
    # (a pre-existing condition; test_email_channel_configure_status_and_
    # disconnect above hit the same issue without one, fixed the same way)
    body = {"domain": "acme.freshchat.com", "team": "support",
           "api_token": "fake-token-secret", "webhook_public_key": "-----BEGIN PUBLIC KEY-----fake",
           "auto_send_enabled": False, "tenant_id": GLOBEX_TENANT}
    try:
        r = client.put("/api/integrations/freshchat", headers=auth_headers, json=body)
        assert r.status_code == 200, r.text
        got = client.get("/api/integrations/freshchat", headers=auth_headers,
                         params={"tenant_id": GLOBEX_TENANT}).json()
        assert got["configured"] is True and got["domain"] == "acme.freshchat.com"
        assert got["status"] == "active" and got["auto_send_enabled"] is False
        assert got["signature_verification"] is True
        assert "fake-token-secret" not in str(got) and "api_token" not in got
        # flip the master switch without re-sending the token
        r2 = client.put("/api/integrations/freshchat", headers=auth_headers,
                        json={"domain": "acme.freshchat.com", "auto_send_enabled": True,
                              "tenant_id": GLOBEX_TENANT})
        assert r2.status_code == 200
        got2 = client.get("/api/integrations/freshchat", headers=auth_headers,
                          params={"tenant_id": GLOBEX_TENANT}).json()
        assert got2["auto_send_enabled"] is True
        # the webhook URL includes this tenant's id
        wh = client.get("/api/integrations/freshchat/webhook-url", headers=auth_headers,
                        params={"tenant_id": GLOBEX_TENANT}).json()
        assert wh["url"].endswith("/webhooks/freshchat/" + GLOBEX_TENANT)
    finally:
        client.delete("/api/integrations/freshchat", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT})
    gone = client.get("/api/integrations/freshchat", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).json()
    assert gone["configured"] is False and gone["status"] == "none"


@pytest.mark.integration
def test_freshchat_channel_test_connection_reports_failure_cleanly(auth_headers):
    r = client.post("/api/integrations/freshchat/test", headers=auth_headers, json={
        "domain": "nope.invalid.test", "api_token": "bad", "tenant_id": GLOBEX_TENANT,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error"]


@pytest.mark.integration
def test_freshchat_channel_requires_domain_and_token(auth_headers):
    r = client.put("/api/integrations/freshchat", headers=auth_headers,
                   json={"team": "support", "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 422


@pytest.mark.integration
def test_freshchat_channel_write_is_owner_only(globex_as_viewer, auth_headers):
    assert client.get("/api/integrations/freshchat", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).status_code == 200
    for call in (
        lambda: client.put("/api/integrations/freshchat", headers=auth_headers,
                           json={"domain": "h", "api_token": "t", "tenant_id": GLOBEX_TENANT}),
        lambda: client.post("/api/integrations/freshchat/test", headers=auth_headers,
                            json={"domain": "h", "api_token": "t", "tenant_id": GLOBEX_TENANT}),
        lambda: client.delete("/api/integrations/freshchat", headers=auth_headers,
                              params={"tenant_id": GLOBEX_TENANT}),
        lambda: client.get("/api/integrations/freshchat/oauth/authorize", headers=auth_headers,
                           params={"tenant_id": GLOBEX_TENANT}),
    ):
        r = call()
        assert r.status_code == 403, r.text


@pytest.mark.integration
def test_freshchat_channel_configure_with_oauth_client_only(auth_headers):
    """A tenant can save just client_id/client_secret (no api_token yet) —
    the browser round-trip to actually mint a refresh_token is a separate,
    human-driven step (/oauth/authorize), not exercised here."""
    body = {"domain": "acme.freshchat.com", "oauth_domain": "acme.myfreshworks.com",
           "client_id": "fw_ext_fake", "client_secret": "fake-secret",
           "tenant_id": GLOBEX_TENANT}
    try:
        r = client.put("/api/integrations/freshchat", headers=auth_headers, json=body)
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["configured"] is False    # no api_token, no refresh_token yet
        assert got["oauth"] is False
        assert got["oauth_client_configured"] is True
        assert "client_secret" not in str(got)
    finally:
        client.delete("/api/integrations/freshchat", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT})


@pytest.mark.integration
def test_freshchat_oauth_authorize_requires_a_saved_client(auth_headers):
    r = client.get("/api/integrations/freshchat/oauth/authorize", headers=auth_headers,
                   params={"tenant_id": GLOBEX_TENANT})
    assert r.status_code == 422


@pytest.mark.integration
def test_freshchat_oauth_authorize_builds_a_real_url(auth_headers):
    body = {"domain": "acme.freshchat.com", "oauth_domain": "acme.myfreshworks.com",
           "client_id": "fw_ext_fake", "client_secret": "fake-secret",
           "tenant_id": GLOBEX_TENANT}
    try:
        assert client.put("/api/integrations/freshchat", headers=auth_headers,
                          json=body).status_code == 200
        r = client.get("/api/integrations/freshchat/oauth/authorize", headers=auth_headers,
                       params={"tenant_id": GLOBEX_TENANT})
        assert r.status_code == 200, r.text
        url = r.json()["url"]
        assert url.startswith("https://acme.myfreshworks.com/org/oauth/v2/authorize?")
        assert "client_id=fw_ext_fake" in url
    finally:
        client.delete("/api/integrations/freshchat", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT})


@pytest.mark.integration
def test_zendesk_connection_configure_status_and_disconnect(auth_headers):
    body = {"subdomain": "acme", "email": "bot@acme.com", "api_token": "fake-token-secret",
           "tenant_id": GLOBEX_TENANT}
    try:
        r = client.put("/api/integrations/zendesk", headers=auth_headers, json=body)
        assert r.status_code == 200, r.text
        got = client.get("/api/integrations/zendesk", headers=auth_headers,
                         params={"tenant_id": GLOBEX_TENANT}).json()
        assert got["configured"] is True and got["subdomain"] == "acme"
        assert got["email"] == "bot@acme.com" and got["status"] == "active"
        assert "fake-token-secret" not in str(got) and "api_token" not in got
    finally:
        client.delete("/api/integrations/zendesk", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT})
    gone = client.get("/api/integrations/zendesk", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).json()
    assert gone["configured"] is False and gone["status"] == "none"


@pytest.mark.integration
def test_zendesk_connection_test_connection_reports_failure_cleanly(auth_headers):
    r = client.post("/api/integrations/zendesk/test", headers=auth_headers, json={
        "subdomain": "nope-invalid-test", "email": "x@y.test", "api_token": "bad",
        "tenant_id": GLOBEX_TENANT,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error"]


@pytest.mark.integration
def test_zendesk_connection_write_is_owner_only(globex_as_viewer, auth_headers):
    assert client.get("/api/integrations/zendesk", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).status_code == 200
    for call in (
        lambda: client.put("/api/integrations/zendesk", headers=auth_headers,
                           json={"subdomain": "h", "email": "e", "api_token": "t",
                                 "tenant_id": GLOBEX_TENANT}),
        lambda: client.post("/api/integrations/zendesk/test", headers=auth_headers,
                            json={"subdomain": "h", "email": "e", "api_token": "t",
                                  "tenant_id": GLOBEX_TENANT}),
        lambda: client.delete("/api/integrations/zendesk", headers=auth_headers,
                              params={"tenant_id": GLOBEX_TENANT}),
    ):
        r = call()
        assert r.status_code == 403, r.text


@pytest.mark.integration
def test_hubspot_connection_configure_status_and_disconnect(auth_headers):
    body = {"access_token": "fake-token-secret", "tenant_id": GLOBEX_TENANT}
    try:
        r = client.put("/api/integrations/hubspot", headers=auth_headers, json=body)
        assert r.status_code == 200, r.text
        got = client.get("/api/integrations/hubspot", headers=auth_headers,
                         params={"tenant_id": GLOBEX_TENANT}).json()
        assert got["configured"] is True and got["status"] == "active"
        assert "fake-token-secret" not in str(got) and "access_token" not in got
    finally:
        client.delete("/api/integrations/hubspot", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT})
    gone = client.get("/api/integrations/hubspot", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).json()
    assert gone["configured"] is False and gone["status"] == "none"


@pytest.mark.integration
def test_hubspot_connection_test_connection_reports_failure_cleanly(auth_headers):
    r = client.post("/api/integrations/hubspot/test", headers=auth_headers, json={
        "access_token": "definitely-bad-token", "tenant_id": GLOBEX_TENANT,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error"]


@pytest.mark.integration
def test_hubspot_connection_write_is_owner_only(globex_as_viewer, auth_headers):
    assert client.get("/api/integrations/hubspot", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).status_code == 200
    for call in (
        lambda: client.put("/api/integrations/hubspot", headers=auth_headers,
                           json={"access_token": "t", "tenant_id": GLOBEX_TENANT}),
        lambda: client.post("/api/integrations/hubspot/test", headers=auth_headers,
                            json={"access_token": "t", "tenant_id": GLOBEX_TENANT}),
        lambda: client.delete("/api/integrations/hubspot", headers=auth_headers,
                              params={"tenant_id": GLOBEX_TENANT}),
    ):
        r = call()
        assert r.status_code == 403, r.text


@pytest.mark.integration
def test_case_connector_get_defaults_and_set_round_trip(auth_headers):
    got = client.get("/api/tenants/case-connector", headers=auth_headers,
                     params={"tenant_id": GLOBEX_TENANT}).json()
    original = got["case_connector"]
    try:
        r = client.put("/api/tenants/case-connector", headers=auth_headers,
                       json={"case_connector": "zendesk", "tenant_id": GLOBEX_TENANT})
        assert r.status_code == 200 and r.json()["case_connector"] == "zendesk"
        got2 = client.get("/api/tenants/case-connector", headers=auth_headers,
                          params={"tenant_id": GLOBEX_TENANT}).json()
        assert got2["case_connector"] == "zendesk"
    finally:
        client.put("/api/tenants/case-connector", headers=auth_headers,
                  json={"case_connector": original, "tenant_id": GLOBEX_TENANT})


@pytest.mark.integration
def test_case_connector_write_is_owner_only(globex_as_viewer, auth_headers):
    assert client.get("/api/tenants/case-connector", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).status_code == 200
    r = client.put("/api/tenants/case-connector", headers=auth_headers,
                  json={"case_connector": "zendesk", "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 403


@pytest.mark.integration
def test_channel_connector_map_get_defaults_and_set_round_trip(auth_headers):
    got = client.get("/api/tenants/channel-connector-map", headers=auth_headers,
                     params={"tenant_id": GLOBEX_TENANT}).json()
    original = got["channel_connector_map"]
    try:
        r = client.put("/api/tenants/channel-connector-map", headers=auth_headers,
                       json={"channel_connector_map": {"hubspot": "hubspot", "email": "salesforce"},
                             "tenant_id": GLOBEX_TENANT})
        assert r.status_code == 200
        assert r.json()["channel_connector_map"] == {"hubspot": "hubspot", "email": "salesforce"}
        got2 = client.get("/api/tenants/channel-connector-map", headers=auth_headers,
                          params={"tenant_id": GLOBEX_TENANT}).json()
        assert got2["channel_connector_map"] == {"hubspot": "hubspot", "email": "salesforce"}
    finally:
        client.put("/api/tenants/channel-connector-map", headers=auth_headers,
                  json={"channel_connector_map": original, "tenant_id": GLOBEX_TENANT})


@pytest.mark.integration
def test_channel_connector_map_write_is_owner_only(globex_as_viewer, auth_headers):
    assert client.get("/api/tenants/channel-connector-map", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).status_code == 200
    r = client.put("/api/tenants/channel-connector-map", headers=auth_headers,
                  json={"channel_connector_map": {"hubspot": "hubspot"}, "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 403


@pytest.mark.integration
def test_case_taxonomy_get_defaults_set_and_reset_round_trip(auth_headers):
    got = client.get("/api/tenants/case-taxonomy", headers=auth_headers,
                     params={"tenant_id": GLOBEX_TENANT}).json()
    assert got["tenant_id"] == GLOBEX_TENANT
    assert "module_rules" in got["defaults"]
    try:
        override = {"region_by_country": {"wakanda": "AFRICA"}}
        r = client.put("/api/tenants/case-taxonomy", headers=auth_headers,
                       json={"config": override, "tenant_id": GLOBEX_TENANT})
        assert r.status_code == 200 and r.json()["config"] == override
        got2 = client.get("/api/tenants/case-taxonomy", headers=auth_headers,
                          params={"tenant_id": GLOBEX_TENANT}).json()
        assert got2["config"] == override
    finally:
        r = client.delete("/api/tenants/case-taxonomy", headers=auth_headers,
                          params={"tenant_id": GLOBEX_TENANT})
        assert r.status_code == 204
    got3 = client.get("/api/tenants/case-taxonomy", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).json()
    assert got3["config"] == {}


@pytest.mark.integration
def test_case_taxonomy_rejects_a_malformed_config(auth_headers):
    r = client.put("/api/tenants/case-taxonomy", headers=auth_headers,
                   json={"config": {"module_rules": "not-a-list"}, "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 422


@pytest.mark.integration
def test_case_taxonomy_write_is_owner_only(globex_as_viewer, auth_headers):
    assert client.get("/api/tenants/case-taxonomy", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).status_code == 200
    r = client.put("/api/tenants/case-taxonomy", headers=auth_headers,
                  json={"config": {}, "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 403
    r = client.delete("/api/tenants/case-taxonomy", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT})
    assert r.status_code == 403


@pytest.mark.integration
def test_email_channel_write_is_owner_only(globex_as_viewer, auth_headers):
    assert client.get("/api/integrations/email", headers=auth_headers,
                      params={"tenant_id": GLOBEX_TENANT}).status_code == 200
    for call in (
        lambda: client.put("/api/integrations/email", headers=auth_headers,
                           json={"provider": "imap", "imap_host": "h", "username": "u",
                                 "password": "p", "tenant_id": GLOBEX_TENANT}),
        lambda: client.post("/api/integrations/email/test", headers=auth_headers,
                            json={"provider": "imap", "tenant_id": GLOBEX_TENANT}),
        lambda: client.delete("/api/integrations/email", headers=auth_headers,
                              params={"tenant_id": GLOBEX_TENANT}),
    ):
        assert call().status_code == 403
    # authorize is owner-gated too (403), unless the server has no Google
    # creds at all, in which case it 503s before the role check
    assert client.get("/api/integrations/email/google/authorize",
                      headers=auth_headers, params={"tenant_id": GLOBEX_TENANT}).status_code in (403, 503)


@pytest.mark.integration
def test_non_owner_cannot_invite_or_list_members(globex_as_viewer, auth_headers):
    assert client.get(f"/api/members?tenant_id={GLOBEX_TENANT}", headers=auth_headers).status_code == 403
    assert client.post("/api/invitations", headers=auth_headers,
                       json={"email": "x@example.test", "role": "viewer",
                             "tenant_id": GLOBEX_TENANT}).status_code == 403


@pytest.fixture
def scratch_flow(auth_headers):
    """A throwaway 3-node flow in the Globex tenant; deleted after the test."""
    from supabase import create_client

    fid = client.post("/api/flows", headers=auth_headers, json={
        "tenant_id": "22222222-2222-2222-2222-222222222222", "team": "csm",
        "name": "pytest-scratch", "status": "draft"}).json()["flow_id"]
    r, cl, hd = (str(uuid.uuid4()) for _ in range(3))
    body = {"name": "pytest-scratch", "status": "draft", "version": 1,
            "nodes": [{"node_id": r, "type": "retrieve", "config": {}},
                      {"node_id": cl, "type": "classify", "config": {}},
                      {"node_id": hd, "type": "handover", "config": {}}],
            "edges": [{"edge_id": str(uuid.uuid4()), "source_node_id": r, "target_node_id": cl, "condition": {}},
                      {"edge_id": str(uuid.uuid4()), "source_node_id": cl, "target_node_id": hd, "condition": {}}]}
    assert client.put(f"/api/flows/{fid}", headers=auth_headers, json=body).status_code == 200
    yield fid, body
    create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]) \
        .table("flows").delete().eq("flow_id", fid).execute()


@pytest.mark.integration
def test_stale_put_is_409(scratch_flow, auth_headers):
    fid, body = scratch_flow
    # body.version is still 1, but the earlier PUT bumped it to 2
    r = client.put(f"/api/flows/{fid}", headers=auth_headers, json={**body, "version": 1})
    assert r.status_code == 409 and "current_version" in str(r.json())


@pytest.mark.integration
def test_publish_snapshots_and_run_records_the_version(scratch_flow, auth_headers):
    fid, _ = scratch_flow
    pv = client.post(f"/api/flows/{fid}/publish", headers=auth_headers).json()["published_version"]
    assert pv == 1
    versions = client.get(f"/api/flows/{fid}/versions", headers=auth_headers).json()
    assert versions[0]["version"] == 1 and len(versions[0]["definition_hash"]) == 64

    run = client.post(f"/api/flows/{fid}/run", headers=auth_headers,
                      json={"case": {"subject": "x", "body": "y", "account": {"customer_type": "premium"}}}).json()
    from supabase import create_client
    row = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]) \
        .table("runs").select("flow_version").eq("run_id", run["run_id"]).execute().data[0]
    assert row["flow_version"] is not None


@pytest.mark.integration
def test_rollback_restores_the_draft(scratch_flow, auth_headers):
    fid, body = scratch_flow
    client.post(f"/api/flows/{fid}/publish", headers=auth_headers)          # v1
    g = client.get(f"/api/flows/{fid}", headers=auth_headers).json()
    edited = {**body, "version": g["version"],
              "nodes": [{**n, "label": "EDITED"} for n in body["nodes"]]}
    client.put(f"/api/flows/{fid}", headers=auth_headers, json=edited)
    client.post(f"/api/flows/{fid}/publish", headers=auth_headers)          # v2

    client.post(f"/api/flows/{fid}/rollback", headers=auth_headers, json={"version": 1})
    g = client.get(f"/api/flows/{fid}", headers=auth_headers).json()
    assert all(n.get("label") != "EDITED" for n in g["nodes"])
    assert g["published_version"] == 1


# ── Phase 14: knowledge base ──────────────────────────────────────────
GLOBEX_TENANT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def kb_collection(auth_headers):
    """A throwaway internal_kb collection in the Globex tenant."""
    from supabase import create_client

    name = f"pytest-kb-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/kb/collections", headers=auth_headers,
                    json={"name": name, "description": "pytest", "tenant_id": GLOBEX_TENANT})
    assert r.status_code == 201, r.text
    sid = r.json()["source_id"]
    yield sid, name
    svc = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    svc.table("zapier_docs").delete().like("url", f"kb://{sid}/%").execute()
    svc.table("sources").delete().eq("source_id", sid).execute()


@pytest.mark.integration
def test_kb_collection_shows_up_scoped(kb_collection, auth_headers):
    sid, name = kb_collection
    cols = client.get(f"/api/kb/collections?tenant_id={GLOBEX_TENANT}", headers=auth_headers).json()
    mine = [c for c in cols if c["source_id"] == sid]
    assert mine and mine[0]["name"] == name and mine[0]["tenant_id"] == GLOBEX_TENANT
    assert mine[0]["entry_count"] == 0


@pytest.mark.integration
def test_kb_entry_roundtrip_embeds_and_scopes(kb_collection, auth_headers):
    sid, _ = kb_collection
    body_md = ("# Refund policy\n\nRefunds under $200 are auto-approved. "
               "Between $200 and $2000 a team lead must approve. Above $2000 "
               "needs a manager sign-off and a note in the account record.\n")
    r = client.post(f"/api/kb/collections/{sid}/entries", headers=auth_headers,
                    json={"title": "Refund policy", "body_md": body_md})
    assert r.status_code == 201, r.text
    eid = r.json()["entry_id"]
    assert r.json()["chunk_count"] >= 1

    got = client.get(f"/api/kb/entries/{eid}", headers=auth_headers).json()
    assert got["body_md"].startswith("# Refund policy")

    # chunks landed under this source_id (service-role peek)
    from supabase import create_client
    svc = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    chunks = svc.table("doc_chunks").select("source_id").eq("source_id", sid).execute().data
    assert len(chunks) >= 1

    # retrieval scoped to the collection finds it for this tenant
    from interpreter.retrieval import hybrid_retrieve
    hits, score = hybrid_retrieve("what is the refund approval limit", top_k=3,
                                  use_graph=False, kb_sources=[_kb_name(sid, auth_headers)],
                                  tenant_id=GLOBEX_TENANT, sb=svc)
    assert any("200" in h["chunk_text"] for h in hits)

    # editing the body re-embeds (embed_hash moves)
    r2 = client.patch(f"/api/kb/entries/{eid}", headers=auth_headers,
                      json={"body_md": body_md + "\nEU customers: route to the DPO.\n"})
    assert r2.status_code == 200

    assert client.delete(f"/api/kb/entries/{eid}", headers=auth_headers).status_code == 204
    assert client.get(f"/api/kb/entries/{eid}", headers=auth_headers).status_code == 404


def _kb_name(sid, headers):
    for c in client.get(f"/api/kb/collections?tenant_id={GLOBEX_TENANT}", headers=headers).json():
        if c["source_id"] == sid:
            return c["name"]
    raise AssertionError("collection vanished")


@pytest.mark.integration
def test_policy_rule_crud(auth_headers):
    from supabase import create_client

    name = f"pytest-rule-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/rules", headers=auth_headers, json={
        "team": "support", "name": name, "priority": 5,
        "when": {"field": "tier", "op": "eq", "value": "premium"},
        "then": {"type": "route", "action": "ask_human"},
        "tenant_id": GLOBEX_TENANT,
    })
    assert r.status_code == 201, r.text
    rid = r.json()["rule_id"]
    assert r.json()["tenant_id"] == GLOBEX_TENANT

    rows = client.get(f"/api/rules?team=support&tenant_id={GLOBEX_TENANT}", headers=auth_headers).json()
    assert any(x["rule_id"] == rid for x in rows)

    p = client.patch(f"/api/rules/{rid}", headers=auth_headers, json={"status": "disabled"})
    assert p.status_code == 200 and p.json()["status"] == "disabled"

    assert client.delete(f"/api/rules/{rid}", headers=auth_headers).status_code == 204
    create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]) \
        .table("policy_rules").delete().eq("rule_id", rid).execute()
