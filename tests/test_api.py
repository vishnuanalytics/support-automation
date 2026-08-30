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

from fastapi.testclient import TestClient  # noqa: E402

from api.main import _structural_errors, app  # noqa: E402

client = TestClient(app)


# ── offline ────────────────────────────────────────────────────────────
def test_health_ok():
    assert client.get("/api/health").json() == {"ok": True}


def test_node_types_lists_the_registry():
    body = client.get("/api/node-types").json()
    assert "confidence_gate" in body["types"] and "retrieve" in body["types"]
    assert "confidence_gate" in body["defaults"]
    assert "kb_lookup" in body["types"] and body["defaults"]["kb_lookup"]["out_key"] == "internal_kb"


def test_kb_endpoints_need_a_token():
    assert client.get("/api/kb/collections").status_code == 401
    assert client.post("/api/kb/collections", json={"name": "x"}).status_code == 401


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


def test_assist_endpoints_need_a_token():
    assert client.post("/api/flows/assist", json={"prompt": "x"}).status_code == 401
    assert client.post(
        "/api/flows/11111111-1111-1111-1111-111111111111/assist",
        json={"instruction": "x"},
    ).status_code == 401


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
                      "--> G[confidence_gate] --> A[auto_reply]"},
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
                                    "when confident else ask a human"})
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
    rows = client.get("/api/flows", headers=auth_headers).json()
    assert rows and all(r["tenant_id"] == "22222222-2222-2222-2222-222222222222" for r in rows)


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
    rows = client.get("/api/tenants", headers=auth_headers).json()
    assert rows and all(r["tenant_id"] == "22222222-2222-2222-2222-222222222222" for r in rows)
    assert all("role" in r for r in rows)


@pytest.mark.integration
def test_create_flow_infers_the_tenant_when_omitted(auth_headers):
    from supabase import create_client

    fid = client.post("/api/flows", headers=auth_headers,
                      json={"team": "csm", "name": "pytest-infer-tenant"}).json()["flow_id"]
    try:
        row = client.get("/api/flows", headers=auth_headers).json()
        mine = next(f for f in row if f["flow_id"] == fid)
        assert mine["tenant_id"] == "22222222-2222-2222-2222-222222222222"
    finally:
        create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]) \
            .table("flows").delete().eq("flow_id", fid).execute()


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
    flows = client.get("/api/flows", headers=auth_headers).json()
    assert flows, "a viewer still reads their tenant's flows"
    fid = flows[0]["flow_id"]
    draft = client.get(f"/api/flows/{fid}", headers=auth_headers).json()

    put = client.put(f"/api/flows/{fid}", headers=auth_headers, json=draft)
    assert put.status_code == 403 and "view-only" in str(put.json())
    assert client.post("/api/flows", headers=auth_headers,
                       json={"team": "csm", "name": "nope"}).status_code == 403
    assert client.post(f"/api/flows/{fid}/publish", headers=auth_headers).status_code == 403
    assert client.delete(f"/api/flows/{fid}", headers=auth_headers).status_code == 403
    assert client.post("/api/rules", headers=auth_headers,
                       json={"team": "csm", "name": "nope"}).status_code == 403


@pytest.mark.integration
def test_members_lists_the_caller_with_role(auth_headers):
    rows = client.get("/api/members", headers=auth_headers).json()
    me = next(r for r in rows if r["is_you"])
    assert me["role"] == "owner" and "email" in me


@pytest.mark.integration
def test_accept_invitations_noop_when_none_pending(auth_headers):
    assert client.post("/api/invitations/accept", headers=auth_headers).json() == {"accepted": 0}


@pytest.mark.integration
def test_invitation_create_list_revoke(auth_headers):
    email = f"pytest-{uuid.uuid4().hex[:8]}@example.test"
    inv = client.post("/api/invitations", headers=auth_headers,
                      json={"email": email, "role": "viewer"})
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
def test_non_owner_cannot_invite_or_list_members(globex_as_viewer, auth_headers):
    assert client.get("/api/members", headers=auth_headers).status_code == 403
    assert client.post("/api/invitations", headers=auth_headers,
                       json={"email": "x@example.test", "role": "viewer"}).status_code == 403


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
                    json={"name": name, "description": "pytest"})
    assert r.status_code == 201, r.text
    sid = r.json()["source_id"]
    yield sid, name
    svc = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    svc.table("zapier_docs").delete().like("url", f"kb://{sid}/%").execute()
    svc.table("sources").delete().eq("source_id", sid).execute()


@pytest.mark.integration
def test_kb_collection_shows_up_scoped(kb_collection, auth_headers):
    sid, name = kb_collection
    cols = client.get("/api/kb/collections", headers=auth_headers).json()
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
    for c in client.get("/api/kb/collections", headers=headers).json():
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
    })
    assert r.status_code == 201, r.text
    rid = r.json()["rule_id"]
    assert r.json()["tenant_id"] == "22222222-2222-2222-2222-222222222222"

    rows = client.get("/api/rules?team=support", headers=auth_headers).json()
    assert any(x["rule_id"] == rid for x in rows)

    p = client.patch(f"/api/rules/{rid}", headers=auth_headers, json={"status": "disabled"})
    assert p.status_code == 200 and p.json()["status"] == "disabled"

    assert client.delete(f"/api/rules/{rid}", headers=auth_headers).status_code == 204
    create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]) \
        .table("policy_rules").delete().eq("rule_id", rid).execute()
