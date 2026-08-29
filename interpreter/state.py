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

    # ---- derived by nodes ------------------------------------------------------
    query: str                 # search query built by the retrieve node
    retrieval: list[dict[str, Any]]   # ranked chunks: {doc_url, chunk_text, score, ...}
    retrieval_score: float     # 0..1, top reranked chunk -- the "do the docs cover this?" signal

    tier: str                  # 'basic' | 'premium' | 'enterprise'
    region: str
    classification: dict[str, Any]    # {topic, urgency, summary, ...}

    sf_writeback: dict[str, Any]      # {target, written, skipped, dry_run, ...} from the sf_writeback node

    draft: str                 # proposed reply text
    draft_confidence: float    # 0..1, model's own confidence in the draft

    confidence: float          # 0..1, combined gate score
    confidence_gate: dict[str, Any]   # {pass: bool, threshold: float, score: float, tier: str}

    outcome: dict[str, Any]    # terminal result: {action, ...}

    # ---- record ----------------------------------------------------------------
    trace: Annotated[list[TraceEntry], operator.add]
