"""
Build "Multi Connection — Salesforce + HubSpot" — a demo flow for the
gunner/UrbanPiper tenant that genuinely exercises every registered node
type that fits a Case-based flow (28 of the 29 registered types;
`trigger` is deliberately excluded — it's for Case-less webhook/schedule
flows, structurally incompatible with a CASE_ACTIONS-oriented flow), and
demonstrates the real multi-connector capability (migration 110's
`channel_connector_map`): the flow branches early on `case.channel` to run
a Salesforce-only enrichment step (`sf_context`) only for Salesforce
cases, then converges back into one shared pipeline whose case-touching
nodes (`sf_case`/`sf_writeback`/...) resolve to whichever connector the
case's channel maps to — Salesforce or HubSpot — with no per-node
override needed (see `connectors.resolve_case_connector`).

Left as a DRAFT (never published, `sf_entry=False`, `team="demo"` so it
can never collide with the tenant's real entry-flow resolution) — this is
a showcase to open and read in the editor, not something meant to run
live over real customer traffic unsupervised. Node positions are left
`None` so the editor's own dagre auto-layout arranges it on first open.

    python -m scripts.build_multi_connection_demo_flow            # write it
    python -m scripts.build_multi_connection_demo_flow --print    # dump the graph, touch nothing
"""
from __future__ import annotations

import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

load_dotenv()

TENANT = "ee4102db-ab47-4deb-b28b-9efc5a02575b"
FLOW_ID = "8e8e8e8e-0000-4d00-8000-000000000001"
TEAM = "demo"
NAME = "Multi Connection — Salesforce + HubSpot"
_NS = uuid.UUID("11111111-2222-3333-4444-555555555557")


def _nid(key: str) -> str:
    return str(uuid.uuid5(_NS, f"{FLOW_ID}:node:{key}"))


# (node_id, type, label, config)
NODES = [
    ("identify", "identify", "Resolve the sender",
     {"domain_match": True, "create_lead_if_missing": False}),
    ("sf_case", "sf_case", "Create/reuse the case",
     {"origin": "Email", "status": "New", "reuse": "thread",
      "create_contact": True, "create_account": True}),
    ("attachments", "attachments", "OCR attachments",
     {"source": "salesforce", "max_images": 5, "ocr": True, "skip_signatures": True}),
    ("sf_context", "sf_context", "Salesforce context",
     {"want": ["account", "contacts", "cases", "team"]}),
    ("product_signal", "product_signal", "Product usage signal",
     {"email_field": "contact.email", "out_key": "product_signal"}),
    ("ai_prompt", "ai_prompt", "Detect urgency & language",
     {"system": "Classify the message's urgency (low/medium/high) and language.",
      "user": "Case: {case.subject}\n{case.body}",
      "model": "openai/gpt-oss-20b", "temperature": 0.0, "max_tokens": 200,
      "output_key": "ai_output", "json_schema": None, "images": "none",
      "cache": True, "on_error": "passthrough"}),
    ("extract", "extract", "Extract account ID",
     {"fields": {"account_id": "the customer's account ID or company name mentioned in the message"}}),
    ("transform", "transform", "Normalize extracted fields",
     {"map": {"account_id": "entities.account_id"}, "into": "context"}),
    ("classify", "classify", "Classify the case",
     {"tier_field": "account.customer_type", "region_field": "account.region"}),
    ("team_route", "team_route", "Route to team", {"default": "support"}),
    ("sf_writeback", "sf_writeback", "Write triage fields",
     {"field_map": {"urgency": "Priority", "topic": "Module__c", "region": "Region__c"},
      "value_maps": {"Priority": {"critical": "High", "high": "High", "normal": "Medium", "low": "Low"}},
      "append": {"Description": "summary"}}),
    ("policy_gate", "policy_gate", "Check policy rules", {}),
    ("task_dispatch", "task_dispatch", "Raise approval task", {}),
    ("kb_lookup", "kb_lookup", "Consult internal KB",
     {"collections": [], "top_k": 4, "use_rerank": True, "min_score": 0.0, "out_key": "internal_kb"}),
    ("agent", "agent", "Agentic retrieve + draft",
     {"retrieve": {"source": ["supabase", "neo4j"], "top_k": 5},
      "draft": {"model": "openai/gpt-oss-120b", "max_tokens": 900},
      "max_iterations": 3, "groundedness_threshold": 0.6}),
    ("retrieve", "retrieve", "Retrieve KB context", {"source": ["supabase", "neo4j"], "top_k": 5}),
    ("correction_exemplars", "correction_exemplars", "Past correction examples",
     {"k": 2, "pool": 50, "min_severity": 0.4, "max_age_days": 90}),
    ("case_lookup", "case_lookup", "Recall similar cases", {"k": 3, "pool": 10, "min_similarity": 0.35}),
    ("draft", "draft", "Draft the reply", {"model": "openai/gpt-oss-120b", "max_tokens": 900}),
    ("connector_action", "connector_action", "Post draft to Slack",
     {"connector": "slack", "action": "post_message",
      "params": {"text": "Draft ready for review", "channel": "#support-drafts"},
      "out_key": "connector_result", "on_error": "passthrough"}),
    ("http_request", "http_request", "Notify external webhook",
     {"connection": "internal-webhook", "method": "POST", "path": "/notify",
      "query": {}, "out_key": "http", "timeout": 15, "on_error": "passthrough"}),
    ("confidence_gate", "confidence_gate", "Gate on answer quality",
     {"default_threshold": 0.5,
      "tier_overrides": {"basic": 0.5, "premium": 0.55, "enterprise": 0.6},
      "weights": {"retrieval": 0.55, "draft": 0.1, "groundedness": 0.35},
      "escalate_topics": ["billing", "refund", "pricing", "compliance", "legal"]}),
    ("handover", "handover", "Full handover", {"reason": "policy"}),
    ("auto_reply", "auto_reply", "Auto-send reply", {}),
    ("notify_human", "notify_human", "Reason with human",
     {"channel": "both", "slack_channel": "#support-escalations", "max_rounds": 3, "mention": {}}),
    ("notify", "notify", "Notify internal rep",
     {"channel": "salesforce_chatter", "target_by_type": {}, "fallback_target": None}),
    ("clarify", "clarify", "Ask for more detail",
     {"max_questions": 3, "max_rounds": 2, "auto_send": False, "channel": "email"}),
    ("ask_human", "ask_human", "Escalate to human", {"channel": "salesforce_chatter"}),
]

# (source, target, condition_expr_or_None) -- None means the unconditional
# default/else edge out of that node (builder.py's _make_router: at most
# one default per node, tried last).
EDGES = [
    ("identify", "sf_case", None),
    ("sf_case", "attachments", None),
    ("attachments", "sf_context", "case.channel == 'salesforce'"),
    ("attachments", "product_signal", None),
    ("sf_context", "product_signal", None),
    ("product_signal", "ai_prompt", None),
    ("ai_prompt", "extract", None),
    ("extract", "transform", None),
    ("transform", "classify", None),
    ("classify", "team_route", None),
    ("team_route", "sf_writeback", None),
    ("sf_writeback", "policy_gate", None),
    ("policy_gate", "task_dispatch", "policy.task != None"),
    ("policy_gate", "kb_lookup", None),
    ("task_dispatch", "kb_lookup", None),
    ("kb_lookup", "agent", "answer_mode == 'diagnostic'"),
    ("kb_lookup", "retrieve", None),
    ("retrieve", "correction_exemplars", None),
    ("correction_exemplars", "case_lookup", None),
    ("case_lookup", "draft", None),
    ("agent", "connector_action", None),
    ("draft", "connector_action", None),
    ("connector_action", "http_request", None),
    ("http_request", "confidence_gate", None),
    ("confidence_gate", "handover",
     "tier == 'enterprise' or routed_team == 'offboarding' or answer_mode == 'action'"),
    ("confidence_gate", "auto_reply",
     "confidence_gate.pass and tier == 'basic' and routed_team == 'support' and answer_mode != 'action'"),
    ("confidence_gate", "notify_human",
     "confidence_gate.pass and tier != 'enterprise' and tier != 'basic' and routed_team == 'support' "
     "and answer_mode != 'action'"),
    ("confidence_gate", "notify",
     "not confidence_gate.pass and tier != 'enterprise' and routed_team == 'support' "
     "and confidence_gate.forced_escalation and answer_mode != 'action'"),
    ("confidence_gate", "clarify",
     "not confidence_gate.pass and tier != 'enterprise' and routed_team == 'support' "
     "and not confidence_gate.forced_escalation and answer_mode != 'action'"),
    ("confidence_gate", "ask_human",
     "routed_team in ('csm', 'sales') and tier != 'enterprise' and answer_mode != 'action'"),
    ("handover", "notify_human", None),
    ("ask_human", "notify_human", None),
]


def build() -> dict:
    out_nodes = [
        {"node_id": _nid(nid), "flow_id": FLOW_ID, "type": t, "label": label, "config": cfg,
         "position_x": None, "position_y": None}
        for nid, t, label, cfg in NODES
    ]
    out_edges = [
        {"edge_id": _nid(f"edge:{s}__{t}"), "flow_id": FLOW_ID,
         "source_node_id": _nid(s), "target_node_id": _nid(t),
         "condition": ({"if": cond} if cond else {})}
        for s, t, cond in EDGES
    ]
    return {"nodes": out_nodes, "edges": out_edges}


def main() -> int:
    graph = build()
    if "--print" in sys.argv:
        print(json.dumps(graph, indent=2))
        print(f"\n{len(graph['nodes'])} nodes / {len(graph['edges'])} edges")
        types_used = sorted({n["type"] for n in graph["nodes"]})
        print(f"{len(types_used)} distinct node types used: {types_used}")
        return 0

    from interpreter.builder import build_graph
    from interpreter.loader import definition_hash
    from ingestion.scraper import get_supabase

    try:
        build_graph({"tenant_id": TENANT, "team": TEAM,
                     "nodes": graph["nodes"], "edges": graph["edges"]})
        print("graph compiles (structurally valid, no cycles, all referenced nodes exist)")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"graph does NOT compile: {e}")

    sb = get_supabase()
    sb.table("flows").upsert({
        "flow_id": FLOW_ID, "tenant_id": TENANT, "team": TEAM, "name": NAME,
        "status": "draft", "sf_entry": False,
    }, on_conflict="flow_id").execute()
    sb.rpc("replace_flow_graph", {
        "p_flow_id": FLOW_ID, "p_nodes": graph["nodes"], "p_edges": graph["edges"],
    }).execute()
    print(f"draft written: {len(graph['nodes'])} nodes / {len(graph['edges'])} edges  (flow {FLOW_ID})")

    live_n = (sb.table("flow_nodes").select("node_id,type,label,config")
              .eq("flow_id", FLOW_ID).execute().data)
    live_e = (sb.table("flow_edges").select("edge_id,source_node_id,target_node_id,condition")
              .eq("flow_id", FLOW_ID).execute().data)
    h = definition_hash(live_n, live_e)
    sb.table("flow_versions").insert({
        "flow_id": FLOW_ID, "version": 1, "name": NAME,
        "nodes": live_n, "edges": live_e, "definition_hash": h,
    }).execute()
    print(f"'{NAME}' saved as a DRAFT (never published, sf_entry=False, team={TEAM!r}) -- "
          f"open it in the editor to review; publishing is left to you.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
