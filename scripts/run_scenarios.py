"""
Scenario harness for the comprehensive email sf_entry flow (Phase 20p).

Fast mode (default): runs `team_route` + `confidence_gate` on a synthetic
`classification` / tier / retrieval-score for each scenario, then evaluates the
flow's real confidence_gate edges to see which terminal node it lands on. No
LLM, no retrieval, no Salesforce — milliseconds, fully deterministic. This is
the check that every team / tier / Case.Type branch routes where it should.

    python scripts/run_scenarios.py                 # fast routing check (DB flow)
    python scripts/run_scenarios.py --from-file     # against interpreter/flows/flow_email_l0l1.json
    python scripts/run_scenarios.py --only 3,7,9

Scenarios 11-13 (clarify-exhausted, agent-CaseComment re-engage, customer
reply) need DB/SF state and are exercised live against the worker, not here.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

for _k in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
          "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE",
          "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()
for _k in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_CONSUMER_KEY",
          "SF_CONSUMER_SECRET", "SF_PRIVATE_KEY", "SF_PRIVATE_KEY_FILE",
          "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

FLOW_ID = "e5e5e5e5-5555-4555-8555-555555555555"

# label, subject, body, tier, topic (classifier slug), retrieval_score, expected node
# retrieval_score: 0.9 = KB covers it well (gate PASS at basic/premium); 0.15 = thin.
SCENARIOS = [
    ("01 how-to KB-covered / basic",        "How do I turn on a Zap?",
     "I built a Zap but it shows as off, how do I publish it", "basic", "zap-activation", 0.92, "auto_reply"),
    ("02 vague no detail / basic",           "it's broken",
     "nothing is working right now please help", "basic", "unclear", 0.10, "clarify"),
    ("03 billing double charge / basic",     "Charged twice",
     "you charged my card twice for the annual plan, refund one", "basic", "billing-refund", 0.20, "notify"),
    ("04 account/login SSO / basic",         "Locked out",
     "cannot log in, our SSO SAML with Okta stopped working", "basic", "account-access-sso", 0.20, "notify"),
    ("05 bug webhook 500 KB-thin / basic",   "Webhook 500",
     "our webhook endpoint intermittently returns a 500 error since yesterday", "basic", "webhook-error", 0.15, "clarify"),
    ("06 renewal + add seats / premium",     "Renew contract and add seats",
     "we want to renew our contract and add 10 more seats before it expires", "premium", "contract-renewal", 0.30, "ask_human"),
    ("07 pre-sales pricing / basic",         "Enterprise pricing",
     "how much does the Enterprise plan cost, send a quote", "basic", "pricing-quote", 0.30, "ask_human"),
    ("08 cancellation + GDPR / premium",     "Cancel our account",
     "please cancel our account and export all our data, GDPR request", "premium", "cancellation-data-export", 0.30, "handover"),
    ("09 enterprise tier any question",      "Trigger timezones",
     "how do scheduled triggers handle daylight saving timezone changes", "enterprise", "trigger-timezone", 0.90, "handover"),
    ("10 how-to KB-covered / premium",       "Filter step",
     "how do I add a Filter step so my Zap only runs for paid orders", "premium", "zap-filter-step", 0.93, "auto_reply"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-file", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    from interpreter.builder import _context
    from interpreter.conditions import evaluate
    from interpreter.registry import h_confidence_gate, h_team_route

    if args.from_file:
        p = pathlib.Path(__file__).resolve().parents[1] / "interpreter/flows/flow_email_l0l1.json"
        flow = json.loads(p.read_text())
        by_id = {n["node_id"]: n for n in flow["nodes"]}
    else:
        from interpreter.loader import load_flow
        flow = load_flow(flow_id=FLOW_ID, status="published")
        by_id = {n["node_id"]: n for n in flow["nodes"]}

    gate = next(n for n in flow["nodes"] if n["type"] == "confidence_gate")
    route = next(n for n in flow["nodes"] if n["type"] == "team_route")
    gate_edges = [e for e in flow["edges"] if e["source_node_id"] == gate["node_id"]]
    print(f"flow: {flow['name']}  v{flow.get('version') or flow.get('flow_version')}  "
          f"({len(flow['nodes'])} nodes, {len(gate_edges)} gate edges)\n")

    only = {int(x) for x in args.only.split(",") if x.strip()} if args.only else None
    ok = 0
    total = 0
    for i, (label, subj, body, tier, topic, rscore, expect) in enumerate(SCENARIOS, 1):
        if only and i not in only:
            continue
        total += 1
        state = {
            "case": {"subject": subj, "body": body},
            "classification": {"topic": topic},
            "tier": tier,
            "retrieval_score": rscore,
            "draft_confidence": 0.95,
            "groundedness": {"score": rscore},
        }
        state.update(h_team_route(state, {**(route.get("config") or {}), "_node_id": "r"}))
        state.update(h_confidence_gate(state, {**(gate.get("config") or {}), "_node_id": "g"}))
        ctx = _context(state)
        landed = "?"
        for e in gate_edges:
            if evaluate(e["condition"]["if"], ctx):
                landed = by_id[e["target_node_id"]]["type"]
                break
        g = state["confidence_gate"]
        hit = "OK " if landed == expect else "XX "
        ok += landed == expect
        print(f"  {hit}{label:<34} -> {landed:<11} (want {expect:<10}) "
              f"team={state['routed_team']:<10} "
              f"gate={'PASS' if g['pass'] else 'FAIL'} forced={g.get('forced_escalation') or '-'}")

    print(f"\n{ok}/{total} landed as expected")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
