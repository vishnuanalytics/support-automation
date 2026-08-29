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


def test_flows_requires_a_bearer_token():
    assert client.get("/api/flows").status_code == 401
    assert client.get("/api/flows", headers={"Authorization": "Basic xyz"}).status_code == 401


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
