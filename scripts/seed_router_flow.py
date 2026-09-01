"""
Seed / re-seed the Case-router workflow (Phase 20i) — the single end-to-end
flow from the support design doc:

  identify -> sf_case -> retrieve -> classify -> team_route -> sf_writeback
    -> draft -> confidence_gate
        -> auto_reply                     (gate passes, not enterprise, not offboarding)
        -> ask_human   (queue_by_team)    (gate fails + routed to csm/sales — that team owns it, Case reassigned)
        -> notify      (target_by_type)   (gate fails + support + forced escalation — ping the Type's rep, Case stays put)
        -> clarify     (handover_queue)   (gate fails + support + not forced — ask the customer, then hand to Team_Support)
        -> handover    (queue_by_team)    (enterprise, or routed to offboarding — "dead end")

`team_route` picks support | csm | sales | offboarding from keyword rules
over the case; `classify` also sets `Case.Type` and `sf_writeback` writes it
every pass. `ask_human` / `handover` resolve the target Salesforce queue from
`routed_team`; `notify` pings the `Case.Type` rep WITHOUT changing the owner.

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

# csm / sales own their Cases (reassigned); offboarding is a handover target.
ASK_QUEUE_BY_TEAM = {"csm": "Team_CSM", "sales": "Team_Sales"}
HANDOVER_QUEUE_BY_TEAM = {"offboarding": "Team_Offboarding"}
CLARIFY_HANDOVER_QUEUE = "Team_Support"

n_identify, n_sf_case, n_retrieve, n_classify, n_route, n_writeback, \
    n_draft, n_gate, n_auto, n_ask, n_handover, n_notify, n_clarify, \
    n_case_lookup, n_human = (_nid(i) for i in range(1, 16))

NODES = [
    (n_identify, "identify", "Resolve the sender",
     {"email_field": "contact.email", "domain_match": True}),
    (n_sf_case, "sf_case", "Create / reuse the Salesforce Case",
     {"origin": "Web", "status": "New", "reuse": "thread"}),
    (n_retrieve, "retrieve", "Retrieve KB context",
     {"source": ["supabase"], "top_k": 5, "use_rerank": True}),
    (n_classify, "classify", "Classify tier / type / topic / urgency",
     {"tier_field": "account.customer_type", "region_field": "account.region",
      "default_tier": "basic"}),
    (n_route, "team_route", "Route to a team (design-doc rules)",
     {"default": "support"}),
    (n_writeback, "sf_writeback",
     "Write triage fields to the Case (Type, Module, Priority…)", {}),
    (n_case_lookup, "case_lookup", "Recall similar resolved Cases (Phase 21)",
     {"k": 3, "pool": 10, "min_similarity": 0.35, "min_memories": 3,
      "use_graph": True, "skip_modes": ["action"]}),
    (n_draft, "draft", "Draft the reply from context",
     {"model": "openai/gpt-oss-120b", "max_tokens": 700}),
    (n_gate, "confidence_gate", "Tag manager — score & decide",
     {"weights": {"retrieval": 0.55, "draft": 0.1, "groundedness": 0.35},
      "default_threshold": 0.5,
      "tier_overrides": {"basic": 0.5, "premium": 0.6, "enterprise": 0.75},
      "escalate_topics": ["billing", "refund", "pricing", "legal", "compliance",
                          "account-access", "data-export", "cancellation",
                          "sso", "saml", "login", "locked out", "lockout",
                          "2fa", "mfa", "password reset"],
      "escalate_modules": ["Billing & Plans", "Account & Login"],
      "escalate_types": ["Billing", "Account / Login"]}),
    (n_auto, "auto_reply", "Auto-reply to the customer", {"channel": "email"}),
    (n_ask, "ask_human",
     "Escalate to the owning team (csm / sales) — reassigns the Case",
     {"channel": "salesforce_chatter", "queue_by_team": ASK_QUEUE_BY_TEAM,
      "escalate_queue": "Billing_Escalations"}),
    (n_handover, "handover", "Full handover (enterprise / offboarding)",
     {"reason": "enterprise_or_offboarding", "queue_by_team": HANDOVER_QUEUE_BY_TEAM,
      "enterprise_queue": "Enterprise_Support"}),
    (n_notify, "notify", "Ping the Type's internal rep (Case stays in the queue)",
     {"channel": "salesforce_chatter", "target_by_type": {},
      "target_by_module": {}, "fallback_target": None}),
    (n_clarify, "clarify", "Ask the customer for missing detail",
     {"max_questions": 3, "max_rounds": 2, "auto_send": False, "channel": "email",
      "handover_queue": CLARIFY_HANDOVER_QUEUE}),
    (n_human, "notify_human", "Tag a human (Slack + / or Chatter)",
     {"channel": "both", "slack_channel": "#support-escalations",
      "mention": {"mention_id": "005jV000000fm5WQAQ"}}),
]

_LIVE = "tier != 'enterprise' and routed_team != 'offboarding'"
_SUPPORT_FAIL = "not confidence_gate.pass and tier != 'enterprise' and routed_team == 'support'"
EDGES = [
    (n_identify, n_sf_case, {}),
    (n_sf_case, n_retrieve, {}),
    (n_retrieve, n_classify, {}),
    (n_classify, n_route, {}),
    (n_route, n_writeback, {}),
    (n_writeback, n_case_lookup, {}),
    (n_case_lookup, n_draft, {}),
    (n_draft, n_gate, {}),
    (n_gate, n_handover, {"if": "tier == 'enterprise' or routed_team == 'offboarding'"}),
    (n_gate, n_auto, {"if": f"confidence_gate.pass and {_LIVE}"}),
    (n_gate, n_ask, {"if": f"not confidence_gate.pass and {_LIVE} and routed_team in ('csm', 'sales')"}),
    (n_gate, n_notify, {"if": f"{_SUPPORT_FAIL} and confidence_gate.forced_escalation"}),
    (n_gate, n_clarify, {"if": f"{_SUPPORT_FAIL} and not confidence_gate.forced_escalation"}),
    (n_ask, n_human, {}),
    (n_handover, n_human, {}),
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
