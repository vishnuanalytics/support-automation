"""
Seed / re-seed the Case-router workflow (Phase 20i) — the single end-to-end
flow from the support design doc:

  identify -> sf_case -> retrieve -> classify -> team_route -> sf_writeback
    -> draft -> confidence_gate
        -> auto_reply                     (gate passes, not enterprise, not offboarding)
        -> ask_human   (queue_by_team)    (gate fails / forced topic — the "doubt" path)
        -> handover    (queue_by_team)    (enterprise, or routed to offboarding — "dead end")

`team_route` picks support | csm | sales | offboarding from keyword rules
over the case; `ask_human` / `handover` resolve the target Salesforce queue
from `routed_team` (Team_CSM / Team_Sales / Team_Offboarding / Support_Tier2,
Enterprise_Support for enterprise, Billing_Escalations for a forced billing
escalation).

    python scripts/seed_router_flow.py            # upsert + publish v-next
    python scripts/seed_router_flow.py --print    # dump the flow JSON, touch nothing

Idempotent: re-running replaces the draft graph and publishes a new
immutable snapshot only if the definition changed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
load_dotenv()

FLOW_ID = "f0f0f0f0-0000-4000-8000-000000000000"


def _nid(n: int) -> str:
    return f"f0000000-0000-4000-8000-0000000000{n:02d}"

TENANT = "00000000-0000-0000-0000-000000000000"
TEAM = "router"
NAME = "Case router — team routing + tag manager"

QUEUE_BY_TEAM = {
    "support": "Support_Tier2", "csm": "Team_CSM",
    "sales": "Team_Sales", "offboarding": "Team_Offboarding",
}

n_identify, n_sf_case, n_retrieve, n_classify, n_route, n_writeback, \
    n_draft, n_gate, n_auto, n_ask, n_handover = (_nid(i) for i in range(1, 12))

NODES = [
    (n_identify, "identify", "Resolve the sender",
     {"email_field": "contact.email", "domain_match": True}),
    (n_sf_case, "sf_case", "Create / reuse the Salesforce Case",
     {"origin": "Web", "status": "New", "reuse": "thread"}),
    (n_retrieve, "retrieve", "Retrieve KB context",
     {"source": ["supabase"], "top_k": 5, "use_rerank": True}),
    (n_classify, "classify", "Classify tier / topic / urgency",
     {"tier_field": "account.customer_type", "region_field": "account.region",
      "default_tier": "basic"}),
    (n_route, "team_route", "Route to a team (design-doc rules)",
     {"default": "support"}),
    (n_writeback, "sf_writeback", "Write triage fields to the Case", {}),
    (n_draft, "draft", "Draft the reply from context",
     {"model": "openai/gpt-oss-120b", "max_tokens": 700}),
    (n_gate, "confidence_gate", "Tag manager — score & decide",
     {"weights": {"retrieval": 0.55, "draft": 0.1, "groundedness": 0.35},
      "default_threshold": 0.5,
      "tier_overrides": {"basic": 0.5, "premium": 0.6, "enterprise": 0.75},
      "escalate_topics": ["billing", "refund", "pricing", "legal", "compliance",
                          "account-access", "data-export", "cancellation"],
      "escalate_modules": ["Billing & Plans"]}),
    (n_auto, "auto_reply", "Auto-reply to the customer", {"channel": "email"}),
    (n_ask, "ask_human", "Escalate — ask a human on the Case",
     {"channel": "salesforce_chatter", "queue_by_team": QUEUE_BY_TEAM,
      "escalate_queue": "Billing_Escalations"}),
    (n_handover, "handover", "Full handover to the team",
     {"reason": "enterprise_or_offboarding", "queue_by_team": QUEUE_BY_TEAM,
      "enterprise_queue": "Enterprise_Support"}),
]

_NOT_TERMINAL = "tier != 'enterprise' and routed_team != 'offboarding'"
EDGES = [
    (n_identify, n_sf_case, {}),
    (n_sf_case, n_retrieve, {}),
    (n_retrieve, n_classify, {}),
    (n_classify, n_route, {}),
    (n_route, n_writeback, {}),
    (n_writeback, n_draft, {}),
    (n_draft, n_gate, {}),
    (n_gate, n_auto, {"if": f"confidence_gate.pass and {_NOT_TERMINAL}"}),
    (n_gate, n_ask, {"if": f"not confidence_gate.pass and {_NOT_TERMINAL}"}),
    (n_gate, n_handover, {"if": "tier == 'enterprise' or routed_team == 'offboarding'"}),
]


def flow_json() -> dict:
    return {
        "flow_id": FLOW_ID, "tenant_id": TENANT, "team": TEAM, "name": NAME,
        "version": 1, "status": "draft",
        "_doc": ("The design doc's single end-to-end workflow: identify -> "
                 "sf_case -> retrieve -> classify -> team_route -> sf_writeback "
                 "-> draft -> confidence_gate -> {auto_reply | ask_human | "
                 "handover}. team_route sets routed_team from keyword rules; "
                 "ask_human/handover resolve the Salesforce queue from it."),
        "nodes": [{"node_id": nid, "type": t, "label": lbl, "config": cfg}
                  for nid, t, lbl, cfg in NODES],
        "edges": [{"source_node_id": s, "target_node_id": d, "condition": c}
                  for s, d, c in EDGES],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="dump the flow JSON, do nothing")
    args = ap.parse_args()
    fj = flow_json()
    if args.print:
        print(json.dumps(fj, indent=2))
        return 0

    from ingestion.scraper import get_supabase
    from interpreter.builder import build_graph
    from interpreter.flows.validate_flow import Flow, check_flow
    from interpreter.loader import definition_hash

    errs = check_flow(Flow.model_validate(fj), require_expected_types=False)
    if errs:
        sys.exit("flow invalid:\n  - " + "\n  - ".join(errs))
    build_graph(fj)                      # raises on a routing gap
    print("flow validates + compiles")

    sb = get_supabase()
    sb.table("flows").upsert({
        "flow_id": FLOW_ID, "tenant_id": TENANT, "team": TEAM, "name": NAME,
        "status": "published",
    }, on_conflict="flow_id").execute()

    nodes = [{"node_id": nid, "flow_id": FLOW_ID, "type": t, "label": lbl,
              "config": cfg} for nid, t, lbl, cfg in NODES]
    edges = [{"edge_id": f"f0000000-0000-4000-8000-0000000001{i:02d}",
              "flow_id": FLOW_ID, "source_node_id": s, "target_node_id": d,
              "condition": c} for i, (s, d, c) in enumerate(EDGES)]
    sb.rpc("replace_flow_graph", {"p_flow_id": FLOW_ID, "p_nodes": nodes,
                                  "p_edges": edges}).execute()
    print(f"draft graph written: {len(nodes)} nodes / {len(edges)} edges")

    # publish a snapshot if the hash changed
    live = (sb.table("flow_nodes").select("node_id,type,label,config")
            .eq("flow_id", FLOW_ID).execute().data)
    le = (sb.table("flow_edges").select("edge_id,source_node_id,target_node_id,condition")
          .eq("flow_id", FLOW_ID).execute().data)
    h = definition_hash(live, le)
    have = (sb.table("flow_versions").select("version,definition_hash")
            .eq("flow_id", FLOW_ID).order("version", desc=True).limit(1).execute().data)
    if have and have[0]["definition_hash"] == h:
        print(f"unchanged — still published v{have[0]['version']}")
        return 0
    nextv = (have[0]["version"] + 1) if have else 1
    sb.table("flow_versions").insert({
        "flow_id": FLOW_ID, "version": nextv, "name": NAME,
        "nodes": live, "edges": le, "definition_hash": h,
    }).execute()
    sb.table("flows").update({"published_version": nextv, "status": "published"}) \
        .eq("flow_id", FLOW_ID).execute()
    print(f"published v{nextv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
