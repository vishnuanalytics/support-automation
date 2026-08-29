"""
Multi-flow integration check — one interpreter, three flows across two
tenants. Marked `integration` (hits Supabase + real retrieval, ~30-60s);
run with `pytest tests/test_multiflow.py` or `python -m tests.test_multiflow`.

The point (Phase 4): the SAME case, run through each published flow, ends
in a different terminal purely because the flow rows differ. Assertions are
kept to what's *robust* — a structural fact per flow plus the cross-tenant
divergence — not the exact score-path (which shifts with the daily
re-ingest).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

for _k in ("GROQ_API_KEY", "SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN",
           "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE"):
    os.environ.pop(_k, None)
os.environ["RUNS_DISABLED"] = "1"

from interpreter.builder import build_graph  # noqa: E402
from interpreter.loader import load_flow  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("SUPABASE_URL"), reason="no SUPABASE_URL"),
]

ACME = "00000000-0000-0000-0000-000000000000"
GLOBEX = "22222222-2222-2222-2222-222222222222"
CASE = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "interpreter" / "cases" / "basic_howto.json").read_text()
)


def _run(tenant: str, team: str):
    flow = load_flow(tenant_id=tenant, team=team, status="published")
    final = build_graph(flow).invoke({"case": dict(CASE), "trace": []})
    return flow, final


@pytest.mark.parametrize("tenant, team, want_action, has_gate", [
    (ACME, "support", "auto_reply", True),      # lenient per-tier gate, score clears it
    (GLOBEX, "support", "ask_human", True),      # strict gate (basic 0.9), nothing auto-sends
    (ACME, "offboarding", "handover", False),    # no gate node -> always handover (structural)
])
def test_seeded_flow_routes_as_designed(tenant, team, want_action, has_gate):
    flow, final = _run(tenant, team)
    node_types = {n["type"] for n in flow["nodes"]}
    assert ("confidence_gate" in node_types) is has_gate
    assert (final.get("outcome") or {}).get("action") == want_action


def test_same_case_diverges_across_tenants():
    """The multi-tenant invariant: identical input, different tenant config,
    different outcome — same interpreter."""
    _, a = _run(ACME, "support")
    _, b = _run(GLOBEX, "support")
    assert a["outcome"]["action"] != b["outcome"]["action"]
    # and it's the gate config that causes it (Acme's bar is lower)
    assert a["confidence_gate"]["threshold"] < b["confidence_gate"]["threshold"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
