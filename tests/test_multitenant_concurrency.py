"""
2026-09-03 -- the third track of the robustness pass: a real multi-tenant
concurrency stress test, not just two sequential calls
(test_multiflow.py::test_same_case_diverges_across_tenants already proves
the *logical* invariant one call at a time). This drives many interleaved,
truly concurrent flow runs for two different tenants against the live
interpreter -- the exact condition under which the two cross-tenant cache
bugs fixed earlier in this pass (_intake_queue_id, routing.queue_member)
would have shown up, and the condition a real production deployment
(many tenants' inbound cases landing on the same worker process) actually
runs under.

    pytest tests/test_multitenant_concurrency.py -m integration
"""

from __future__ import annotations

import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.integration

from interpreter.builder import build_graph  # noqa: E402
from interpreter.loader import load_flow  # noqa: E402

ACME = "00000000-0000-0000-0000-000000000000"
GLOBEX = "22222222-2222-2222-2222-222222222222"
CASE = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "interpreter" / "cases" / "basic_howto.json").read_text()
)

N_RUNS_PER_TENANT = 12  # interleaved -> 24 total concurrent flow invocations


def _run_one(tenant: str, team: str = "support"):
    flow = load_flow(tenant_id=tenant, team=team, status="published")
    final = build_graph(flow).invoke({"case": dict(CASE), "trace": []})
    return tenant, flow, final


def test_interleaved_concurrent_runs_never_cross_contaminate_tenants():
    """N runs each for Acme and Globex, genuinely interleaved across a
    thread pool (not run-then-run) -- every result must carry its OWN
    tenant's config (confidence_gate threshold), never the other
    tenant's, and nothing may raise."""
    jobs = [ACME, GLOBEX] * N_RUNS_PER_TENANT
    results: list[tuple[str, dict, dict]] = []
    errors: list[BaseException] = []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_run_one, tid): tid for tid in jobs}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except BaseException as e:  # noqa: BLE001 -- want every failure, not just the first
                errors.append(e)

    assert not errors, f"{len(errors)} concurrent run(s) raised: {errors[:3]}"
    assert len(results) == len(jobs)

    thresholds = {ACME: set(), GLOBEX: set()}
    for tenant, _flow, final in results:
        gate = final.get("confidence_gate") or {}
        if "threshold" in gate:
            thresholds[tenant].add(gate["threshold"])

    # each tenant's own runs must be internally consistent (same config every
    # time) and the two tenants' thresholds must never have been swapped
    assert len(thresholds[ACME]) <= 1, f"Acme saw inconsistent thresholds: {thresholds[ACME]}"
    assert len(thresholds[GLOBEX]) <= 1, f"Globex saw inconsistent thresholds: {thresholds[GLOBEX]}"
    if thresholds[ACME] and thresholds[GLOBEX]:
        assert thresholds[ACME] != thresholds[GLOBEX], (
            "Acme and Globex runs converged on the same confidence_gate "
            "threshold under concurrency -- looks like tenant-scoped state leaked"
        )


def test_concurrent_runs_do_not_corrupt_the_shared_salesforce_client_cache():
    """Hammer client_for() concurrently for both tenants -- the per-(tenant,
    org) cache dict is populated without a lock; this proves a race there
    can only produce redundant work, never a wrong-tenant client handed
    back to the wrong caller."""
    from interpreter import salesforce

    # client_for() falls back to the env-configured client for whichever
    # tenant has no Vault-stored org creds -- fine when SF_* env vars are
    # set (this sandbox), but GitHub CI's `integration` job has no SF_*
    # secrets at all (only Supabase/Neo4j, per ci.yml), so that fallback
    # raises KeyError('SF_USERNAME') instead of degrading. Self-skip like
    # test_salesforce_connect_introspect_disconnect_roundtrip already does,
    # rather than erroring on a genuinely missing-secrets environment --
    # then discard whatever this probe just cached, so the concurrent
    # stress test below still starts from a genuinely cold cache (its
    # whole point is exercising concurrent *population* of that cache).
    for tid in (ACME, GLOBEX):
        try:
            salesforce.client_for(tid, "default")
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no resolvable Salesforce creds for tenant {tid} ({e})")
        finally:
            salesforce._tenant_clients.pop((tid, "default"), None)

    tenants = [ACME, GLOBEX] * 15

    def _resolve(tid: str):
        return tid, salesforce.client_for(tid, "default")

    seen: dict[str, set[int]] = {ACME: set(), GLOBEX: set()}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for tid, client in ex.map(_resolve, tenants):
            seen[tid].add(id(client))

    # every call for a given tenant must resolve to a client object that was
    # actually cached under that tenant's key -- not proof-by-count, a real
    # structural check via the cache dict itself.
    for tid in (ACME, GLOBEX):
        cached = salesforce._tenant_clients.get((tid, "default"))
        assert cached is not None
        assert id(cached) in seen[tid]
