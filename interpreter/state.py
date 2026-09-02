"""
The shared graph state. Every handler receives the whole `CaseState` and
returns a partial dict; LangGraph shallow-merges it in. `trace` is the one
field with a reducer -- each node appends one entry, so the final state
carries a full step-by-step record of what the flow did and why (feeds the
Phase 6 "why did the bot respond this way" view).
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class TraceEntry(TypedDict, total=False):
    node_id: str
    type: str
    summary: str
    data: dict[str, Any]


class CaseState(TypedDict, total=False):
    # ---- input -------------------------------------------------------------
    # case: {case_id, subject, body, account: {name, customer_type, region},
    #        contact: {name, email}}
    case: dict[str, Any]
    tenant_id: str             # the flow's tenant — for per-tenant integration creds (Phase 12)
    team: str                  # the flow's team — scopes policy_rules lookup (Phase 16)

    # P5 — the generic run payload. A flow that does NOT operate on a Salesforce
    # Case reads its input from here (`context.*`, or `input.*` in an edge
    # condition) instead of `case.*`; a trigger / webhook adapter populates it.
    # `sf_case` still fills `case`. Merge-friendly (operator.or_) like `ai`, so
    # any node can add derived values without a dedicated state key.
    context: Annotated[dict[str, Any], operator.or_]

    # ---- derived by nodes ------------------------------------------------------
    query: str                 # search query built by the retrieve node
    retrieval: list[dict[str, Any]]   # ranked chunks: {doc_url, chunk_text, score, ...}
    retrieval_score: float     # 0..1, top reranked chunk -- the "do the docs cover this?" signal

    tier: str                  # 'basic' | 'premium' | 'enterprise'
    region: str
    classification: dict[str, Any]    # {topic, urgency, summary, ...}

    # written by an `extract` node — named fields pulled from the case so
    # policy rules can key on them (Phase 16), e.g. {report_age_years: 6}
    entities: dict[str, Any]

    # written by a `policy_gate` node (Phase 16):
    # {matched: rule name|None, action: 'ask_human'|…|None, task: {...}|None}
    policy: dict[str, Any]
    action_request_id: str     # set by task_dispatch; runs.record_run links it to the run

    # written by a `kb_lookup` node when the flow routes through one (Phase 14):
    # {checked: bool, collections: [str], score: float, matches: [chunk dicts]}
    internal_kb: dict[str, Any]

    # written by a `clarify` node on the low-confidence recovery path (Phase 17):
    # {questions: [str], missing: [str], channel: str, auto_send: bool, posted: bool,
    #  round: int, exhausted: bool}
    clarification: dict[str, Any]
    clarify_round: int         # 1-based; how many times we've gone back to this customer (Phase 17d)

    # written by an `identify` node (Phase 17b) — who is the sender:
    # {email, domain, is_free_domain, known, account_matched,
    #  match: 'contact'|'lead'|'domain'|'lead_created'|'none',
    #  contact_id, lead_id, name, account_id, account_name}
    sender: dict[str, Any]

    # written by a `team_route` node (Phase 20i) — which team owns this case:
    # 'support' | 'csm' | 'sales' | 'offboarding' (the design doc's routing).
    # `ask_human` / `handover` resolve the target queue from it.
    routed_team: str

    sf_writeback: dict[str, Any]      # {target, written, skipped, dry_run, ...} from the sf_writeback node

    # written by an `sf_case` node (Phase 20e/f) — the inbound message resolved
    # to a real Salesforce Case: {sf_id, case_number, contact_id, account_id,
    #  account: {name, customer_type, region}, created, reused,
    #  contact_created, account_created, inbound_email: {id, ...}, dry_run}
    sf_case: dict[str, Any]

    # Phase 21 — Case-resolution memory (case_lookup node)
    prior_resolutions: list[dict[str, Any]]   # citable past resolutions the draft may quote
    investigation_hints: list[str]            # leads for a human / evidence step — never reply copy

    # Phase 23d — notify_human node: {slack: {...}, chatter: {...}, mention: {...}}
    human_alert: dict[str, Any]

    # Phase 25 — image attachments (attachments node). `_attachment_blobs` is
    # {blob_key: bytes} for the ai_prompt vision path; it is never persisted.
    attachments: list[dict[str, Any]]
    attachment_text: str
    _attachment_blobs: dict[str, Any]

    # Phase 25 — sf_context node: {account, organization, contact, siblings,
    # lead, cases: {open,total,recent}, account_team}
    sf_context: dict[str, Any]

    # Phase 25 — every `ai_prompt` node writes {output_key: value} in here
    # (a declared channel so a dynamic key isn't dropped by the graph merge).
    # Edges / templates read `ai.<output_key>...`.
    ai: Annotated[dict[str, Any], operator.or_]

    draft: str                 # proposed reply text
    draft_confidence: float    # 0..1, model's own confidence in the draft
    groundedness: dict[str, Any]      # {score 0..1, backend, unsupported[]} — is the draft supported by the context

    # KIL-b — the contradiction judge. {draft: {...}, inbound: {...}} where each
    # is {relation, flagged, novel, verdicts, backend}. A `draft` that
    # contradicts the KB / case history forces the gate to escalate.
    integrity: dict[str, Any]

    confidence: float          # 0..1, combined gate score
    confidence_gate: dict[str, Any]   # {pass: bool, threshold: float, score: float, tier: str}

    outcome: dict[str, Any]    # terminal result: {action, ...}

    # ---- record ----------------------------------------------------------------
    trace: Annotated[list[TraceEntry], operator.add]
