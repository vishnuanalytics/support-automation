"""
Multi-tenant robustness audit follow-up (2026-09-04): the existing
tests/test_multitenant_concurrency.py hammers the *interpreter* layer
directly (build_graph(flow).invoke(...)), bypassing FastAPI entirely --
no auth, no rate limiting, no RLS-scoped `c.sb` per request, no
`_caller_tenant`/`_require_visible` gating. This test hits the real HTTP
API instead, which is how production traffic actually arrives, and is a
strictly stronger test of tenant_id-keyed cache isolation than the
interpreter-level one: both concurrent tenants are run through the SAME
authenticated identity (the real globex-owner@example.test test fixture,
made a member of a second, freshly-created tenant) so any bleed can only
be explained by a tenant_id mix-up in a cache/lookup, never "it's just a
different auth token."

Setup is idempotent (safe to run repeatedly): finds-or-creates a second
tenant + a minimal published flow owned by the same test account, then
runs many genuinely interleaved concurrent POST /api/flows/{id}/run
calls against it and the real, already-seeded Globex flow.

The cross-tenant tell is the confidence_gate's `default_threshold`,
which the run response echoes back verbatim (no dependence on live LLM
confidence output, so no flakiness from that -- unlike the interpreter
test's earlier auto_reply-vs-ask_human flakiness): Globex's real seeded
flow resolves to 1.01 for this sample case's tier (its tier_overrides),
the fresh second flow is built with a deliberately non-overlapping 0.02.

    pytest tests/test_multitenant_concurrency_api.py -m integration
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

client = TestClient(app)
pytestmark = pytest.mark.integration

GLOBEX_TENANT = "22222222-2222-2222-2222-222222222222"
GLOBEX_FLOW = "a2a2a2a2-2222-4222-8222-222222222222"
GLOBEX_THRESHOLD = 1.01   # tier_overrides.basic on the real seeded Globex flow

SECOND_TENANT_NAME = "concurrency-test-tenant-b"
SECOND_FLOW_NAME = "concurrency-test-flow-b"
SECOND_THRESHOLD = 0.02   # deliberately far from anything Globex's flow could produce

CASE = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "interpreter" / "cases" / "basic_howto.json").read_text()
)

N_RUNS_PER_FLOW = 10  # interleaved -> 20 total concurrent HTTP run requests


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


def _minimal_graph(threshold: float) -> tuple[list[dict], list[dict]]:
    """retrieve -> classify -> draft -> confidence_gate -> (auto_reply | ask_human),
    the same shape as llm.py's own _STUB_FLOW, in the real NodeIn/EdgeIn wire
    shape (confirmed against the live Globex flow's actual JSON)."""
    ids = {k: str(uuid.uuid4()) for k in ("retrieve", "classify", "draft", "gate", "send", "human")}
    nodes = [
        {"node_id": ids["retrieve"], "type": "retrieve", "label": "Retrieve", "config": {"top_k": 3}},
        {"node_id": ids["classify"], "type": "classify", "label": "Classify", "config": {}},
        {"node_id": ids["draft"], "type": "draft", "label": "Draft", "config": {}},
        {"node_id": ids["gate"], "type": "confidence_gate", "label": "Gate",
         "config": {"default_threshold": threshold}},
        {"node_id": ids["send"], "type": "auto_reply", "label": "Auto reply", "config": {}},
        {"node_id": ids["human"], "type": "ask_human", "label": "Ask human", "config": {"post_note": False}},
    ]
    edges = [
        {"edge_id": str(uuid.uuid4()), "source_node_id": ids["retrieve"], "target_node_id": ids["classify"], "condition": {}},
        {"edge_id": str(uuid.uuid4()), "source_node_id": ids["classify"], "target_node_id": ids["draft"], "condition": {}},
        {"edge_id": str(uuid.uuid4()), "source_node_id": ids["draft"], "target_node_id": ids["gate"], "condition": {}},
        {"edge_id": str(uuid.uuid4()), "source_node_id": ids["gate"], "target_node_id": ids["send"],
         "condition": {"if": "confidence_gate.pass"}},
        {"edge_id": str(uuid.uuid4()), "source_node_id": ids["gate"], "target_node_id": ids["human"], "condition": {}},
    ]
    return nodes, edges


@pytest.fixture(scope="module")
def second_tenant_flow(auth_headers):
    """Find-or-create a second tenant + a published flow with a known,
    distinctive confidence_gate threshold, owned by the same real test
    identity Globex already belongs to."""
    tenants = client.get("/api/tenants", headers=auth_headers).json()
    match = next((t for t in tenants if t.get("name") == SECOND_TENANT_NAME), None)
    if match:
        tid = match["tenant_id"]
    else:
        r = client.post("/api/tenants", headers=auth_headers, json={"name": SECOND_TENANT_NAME})
        assert r.status_code == 201, r.text
        tid = r.json()["tenant_id"]

    flows = client.get("/api/flows", headers=auth_headers).json()
    match = next((f for f in flows if f["tenant_id"] == tid and f["name"] == SECOND_FLOW_NAME), None)
    if match:
        fid = match["flow_id"]
    else:
        r = client.post("/api/flows", headers=auth_headers,
                        json={"team": "support", "name": SECOND_FLOW_NAME, "tenant_id": tid})
        assert r.status_code in (200, 201), r.text
        fid = r.json()["flow_id"]

    # (re)write the graph every run so the threshold is guaranteed correct
    # even if a prior run left something else — replace_flow_graph is a
    # full replace, so this is safe to repeat.
    cur = client.get(f"/api/flows/{fid}", headers=auth_headers).json()
    nodes, edges = _minimal_graph(SECOND_THRESHOLD)
    r = client.put(f"/api/flows/{fid}", headers=auth_headers, json={
        "name": SECOND_FLOW_NAME, "status": "draft", "version": cur["version"],
        "nodes": nodes, "edges": edges,
    })
    assert r.status_code == 200, r.text

    r = client.post(f"/api/flows/{fid}/publish", headers=auth_headers)
    assert r.status_code == 200, r.text
    return fid


def _run_one(flow_id: str, headers: dict) -> tuple[str, dict]:
    r = client.post(f"/api/flows/{flow_id}/run", headers=headers, json={"case": dict(CASE)})
    assert r.status_code == 200, r.text
    return flow_id, r.json()


def test_interleaved_http_runs_never_cross_contaminate_tenants(auth_headers, second_tenant_flow):
    """The real API layer, real auth, real RLS-scoped client per request --
    genuinely interleaved concurrent POST /api/flows/{id}/run calls for two
    tenants (same identity, different tenant_id) must never see each
    other's confidence_gate config."""
    second_flow = second_tenant_flow
    jobs = [GLOBEX_FLOW, second_flow] * N_RUNS_PER_FLOW
    results: list[tuple[str, dict]] = []
    errors: list[BaseException] = []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_run_one, fid, auth_headers): fid for fid in jobs}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except BaseException as e:  # noqa: BLE001 -- want every failure, not just the first
                errors.append(e)

    assert not errors, f"{len(errors)} concurrent HTTP run(s) raised: {errors[:3]}"
    assert len(results) == len(jobs)

    thresholds: dict[str, set[float]] = {GLOBEX_FLOW: set(), second_flow: set()}
    for flow_id, body in results:
        gate = body.get("confidence_gate") or {}
        if "threshold" in gate:
            thresholds[flow_id].add(round(float(gate["threshold"]), 4))

    assert thresholds[GLOBEX_FLOW] == {GLOBEX_THRESHOLD}, (
        f"Globex runs saw threshold(s) {thresholds[GLOBEX_FLOW]}, expected only {GLOBEX_THRESHOLD} "
        "— looks like the second tenant's config leaked in under concurrency"
    )
    assert thresholds[second_flow] == {SECOND_THRESHOLD}, (
        f"the second tenant's runs saw threshold(s) {thresholds[second_flow]}, expected only "
        f"{SECOND_THRESHOLD} — looks like Globex's config leaked in under concurrency"
    )
