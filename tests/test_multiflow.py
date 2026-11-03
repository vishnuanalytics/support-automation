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

# Every LLM provider key, not just Groq's: llm.py falls back to OpenRouter /
# Anthropic when Groq is absent, and a real model's draft can trip the
# integrity check and flip the routing (2026-09-23: Acme went to ask_human).
# These assertions are about the flow rows, so the run must use the stub.
for _k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
           "SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN",
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
# These runs are on the deterministic stub (see the fixture below), whose
# keyword classifier tags any "plan" as topic=billing -- and billing is a
# forced escalation in every flow, which would mask the gate-config
# difference this module is about. Drop the incidental "free plan" aside;
# it's still a plain how-to (the shared case file is left alone -- the
# concurrency tests use it too).
CASE["body"] = CASE["body"].replace("I'm on the free plan and want", "I want")
assert "plan" not in CASE["body"].lower()


@pytest.fixture(autouse=True)
def _stub_llm_even_with_workspace_keys(monkeypatch):
    """Popping the env keys isn't enough on its own: llm.py also uses a
    workspace's own BYOK key from Vault (the demo workspaces have one since
    2026-09-23). Hide those too, for this module only -- the billing tests
    elsewhere in the same run still need to see them.

    Also start from an empty response cache: `llm._cache` is keyed by
    model + prompt only and is checked before any key lookup, so an earlier
    test module in the same pytest process that ran this case with a real
    key would otherwise hand back its real-model draft (2026-09-23: CI had
    Acme routing to ask_human while this module alone passed)."""
    from collections import OrderedDict

    from interpreter import llm

    monkeypatch.setattr(llm, "_tenant_keys", lambda tenant_id: {})
    monkeypatch.setattr(llm, "_cache", OrderedDict())
    # the import-time pop above isn't durable: another module's
    # load_dotenv() puts a local .env's keys straight back, so locally this
    # quietly ran against real Groq while CI (no keys) got the stub
    for k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    # And no KIL contradiction verdict. On the stub draft that's the
    # polarity heuristic: >=3 shared words + a "not"/"only" in just one side
    # = "contradicts" -- so it flips with whichever doc chunks today's live
    # re-ingest returns, and a flagged draft force-escalates in every flow.
    # The integrity check has its own tests (test_integrity.py); here it'd
    # only mask the gate-config difference. No contexts -> its own neutral
    # result, real shape.
    from interpreter import integrity

    real_check_many = integrity.check_many
    monkeypatch.setattr(integrity, "check_many",
                        lambda statements, contexts=None, **kw: real_check_many(statements, [], **kw))


def _run(tenant: str, team: str):
    flow = load_flow(tenant_id=tenant, team=team, status="published")
    final = build_graph(flow).invoke({"case": dict(CASE), "trace": []})
    return flow, final


def _why(final: dict) -> str:
    """Enough of the run to tell *why* it routed where it did when an
    assertion fails -- the gate's score breakdown / forced-escalation reason
    plus the trace summaries (a bare 'ask_human != auto_reply' is opaque)."""
    steps = [f"{t.get('type')}: {t.get('summary') or t.get('detail') or ''}"
             for t in final.get("trace") or [] if isinstance(t, dict)]
    return (f"\n gate={final.get('confidence_gate')}"
            f"\n integrity={final.get('integrity')}"
            f"\n trace=\n  " + "\n  ".join(str(s)[:300] for s in steps))


@pytest.mark.parametrize("tenant, team, want_action, has_gate", [
    (ACME, "support", "auto_reply", True),      # lenient per-tier gate, score clears it
    (GLOBEX, "support", "ask_human", True),      # strict gate (basic 0.9), nothing auto-sends
    (ACME, "offboarding", "handover", False),    # no gate node -> always handover (structural)
])
def test_seeded_flow_routes_as_designed(tenant, team, want_action, has_gate):
    flow, final = _run(tenant, team)
    node_types = {n["type"] for n in flow["nodes"]}
    assert ("confidence_gate" in node_types) is has_gate
    assert (final.get("outcome") or {}).get("action") == want_action, _why(final)


def test_same_case_diverges_across_tenants():
    """The multi-tenant invariant: identical input, different tenant config,
    different outcome — same interpreter."""
    _, a = _run(ACME, "support")
    _, b = _run(GLOBEX, "support")
    assert a["outcome"]["action"] != b["outcome"]["action"], _why(a) + _why(b)
    # and it's the gate config that causes it (Acme's bar is lower)
    assert a["confidence_gate"]["threshold"] < b["confidence_gate"]["threshold"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
