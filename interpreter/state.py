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

    sf_writeback: dict[str, Any]      # {target, written, skipped, dry_run, ...} from the sf_writeback node

    # written by an `sf_case` node (Phase 20e/f) — the inbound message resolved
    # to a real Salesforce Case: {sf_id, case_number, contact_id, account_id,
    #  account: {name, customer_type, region}, created, reused,
    #  contact_created, account_created, inbound_email: {id, ...}, dry_run}
    sf_case: dict[str, Any]

    draft: str                 # proposed reply text
    draft_confidence: float    # 0..1, model's own confidence in the draft
    groundedness: dict[str, Any]      # {score 0..1, backend, unsupported[]} — is the draft supported by the context

    confidence: float          # 0..1, combined gate score
    confidence_gate: dict[str, Any]   # {pass: bool, threshold: float, score: float, tier: str}

    outcome: dict[str, Any]    # terminal result: {action, ...}

    # ---- record ----------------------------------------------------------------
    trace: Annotated[list[TraceEntry], operator.add]
