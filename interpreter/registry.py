"""
Node type registry: free-string `type` -> handler function.

Adding a node type later = one entry here + (if it should be mandatory for a
"complete" flow) one line in validate_flow.EXPECTED_TYPES. No migration --
that's the whole point of the generic `type` column.

Handler contract:
    handler(state: CaseState, config: dict) -> dict
It returns a *partial* state update (shallow-merged by LangGraph). Every
handler also appends exactly one `trace` entry.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from . import groundedness, llm, salesforce
from .retrieval import hybrid_retrieve
from .state import CaseState

log = logging.getLogger("interpreter.registry")

Handler = Callable[[CaseState, dict], dict]

_REGISTRY: dict[str, Handler] = {}


def register(type_name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _REGISTRY[type_name] = fn
        return fn
    return deco


def get_handler(type_name: str) -> Handler:
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise KeyError(
            f"no handler registered for node type {type_name!r}; "
            f"known types: {sorted(_REGISTRY)}"
        ) from None


def known_types() -> set[str]:
    return set(_REGISTRY)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _dig(obj: Any, dotted: str) -> Any:
    """`_dig(case, 'account.customer_type')` -> nested value or None."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


_TIER_ALIASES = {
    "basic": "basic", "free": "basic", "starter": "basic", "standard": "basic",
    "premium": "premium", "professional": "premium", "pro": "premium", "team": "premium",
    "enterprise": "enterprise", "business": "enterprise", "company": "enterprise",
}
# fail *closed*: an unrecognised tier gets the strictest confidence bar, not
# the most permissive one. (Phase 7 — was defaulting to "basic".)
STRICTEST_TIER = "enterprise"


def _norm_tier(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    if key in _TIER_ALIASES:
        return _TIER_ALIASES[key]
    log.warning("unknown customer tier %r -> treating as %r (strictest)", raw, STRICTEST_TIER)
    return STRICTEST_TIER


def _trace(node_id: str, type_: str, msg: str, data: dict[str, Any] | None = None) -> dict:
    return {"trace": [{"node_id": node_id, "type": type_, "summary": msg, "data": data or {}}]}


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------
@register("retrieve")
def h_retrieve(state: CaseState, config: dict) -> dict:
    case = state.get("case", {})
    query = " ".join(
        p for p in (case.get("subject", ""), case.get("body", "")) if p
    ).strip() or case.get("subject", "") or ""
    sources = config.get("source", ["supabase"])
    results, score = hybrid_retrieve(
        query,
        top_k=int(config.get("top_k", 5)),
        use_sparse=config.get("use_sparse", True),
        use_graph=config.get("use_graph", "neo4j" in sources),
        use_rerank=config.get("use_rerank", True),
        kb_sources=config.get("kb_sources"),   # e.g. ["zapier-public", "globex-sop"]
        tenant_id=state.get("tenant_id"),      # scopes reachable sources (Phase 12)
    )
    top_url = results[0]["doc_url"] if results else None
    return {
        "query": query,
        "retrieval": results,
        "retrieval_score": score,
        **_trace(
            config["_node_id"], "retrieve",
            f"{len(results)} chunks, top_score={score:.3f}, top={top_url}",
            {"top_score": score, "top_doc": top_url, "docs": [r["doc_url"] for r in results]},
        ),
    }


@register("classify")
def h_classify(state: CaseState, config: dict) -> dict:
    case = state.get("case", {})
    tier_raw = _dig(case, config.get("tier_field", "account.customer_type"))
    region = _dig(case, config.get("region_field", "account.region"))
    tier = _norm_tier(tier_raw)

    body = f"{case.get('subject', '')}\n\n{case.get('body', '')}".strip()
    raw = llm.complete(
        system=(
            "You triage inbound support cases. Return a JSON object with keys: "
            "topic (short slug), urgency (one of low|normal|high|critical), "
            "summary (<=200 chars)."
        ),
        user=body or "(empty case)",
        model=config.get("model", llm.FAST_MODEL),
        json_object=True,
        max_tokens=300,
    )
    parsed = _safe_json(raw)
    classification = {
        "topic": parsed.get("topic", "unknown"),
        "urgency": str(parsed.get("urgency", "normal")).lower(),
        "summary": parsed.get("summary", body[:200]),
        "tier_raw": tier_raw,
        "stub": parsed.get("_stub", False),
    }
    return {
        "tier": tier,
        "region": region,
        "classification": classification,
        **_trace(
            config["_node_id"], "classify",
            f"tier={tier} region={region} urgency={classification['urgency']} topic={classification['topic']}",
            {**classification, "tokens": llm.last_usage},
        ),
    }


@register("sf_writeback")
def h_sf_writeback(state: CaseState, config: dict) -> dict:
    """
    Push triage output onto the Salesforce Case. Config-driven so Phase 5's
    UI can edit the mapping without code:

      config = {
        "field_map":   {"urgency": "Priority", "topic": "Module__c",
                        "region": "Region__c"},
        "value_maps":  {"Priority": {"critical": "High", "high": "High",
                                     "normal": "Medium", "low": "Low"}},
        "append":      {"Description": "summary"}   # src key -> Case field
      }

    No `sf_id` on the case (synthetic/offline run) -> records a skip and
    moves on. No SF creds -> salesforce.py dry-runs (logs the intended write).
    """
    case = state.get("case", {})
    sf_id = case.get("sf_id") or case.get("id")
    classification = state.get("classification") or {}
    ctx: dict[str, Any] = {
        "classification": classification,
        "tier": state.get("tier"),
        "region": state.get("region"),
        "urgency": classification.get("urgency"),
        "topic": classification.get("topic"),
        "summary": classification.get("summary"),
    }

    field_map = config.get("field_map") or {
        "urgency": "Priority", "topic": "Module__c", "region": "Region__c",
    }
    value_maps = config.get("value_maps") or {
        "Priority": {"critical": "High", "high": "High", "normal": "Medium", "low": "Low"},
    }
    append_cfg = config.get("append") or {"Description": "summary"}

    fields: dict[str, Any] = {}
    for src, dest in field_map.items():
        val = _dig(ctx, src) if "." in src else ctx.get(src)
        if val in (None, ""):
            continue
        vm = value_maps.get(dest)
        fields[dest] = vm.get(str(val).lower(), val) if vm else val

    append = {}
    for dest, src in append_cfg.items():
        text = _dig(ctx, src) if "." in src else ctx.get(src)
        if text:
            append[dest] = f"[triage] {text}"

    if not sf_id:
        info = {
            "target": None, "written": {}, "skipped": {}, "dry_run": True,
            "planned": fields, "status": "no sf_id on case",
        }
        return {
            "sf_writeback": info,
            **_trace(config["_node_id"], "sf_writeback", "no sf_id — nothing written", info),
        }

    result = salesforce.update_case_fields(
        sf_id, fields, append=append, tenant_id=state.get("tenant_id")
    )
    result["target"] = sf_id
    if result["dry_run"]:
        summary = f"Case {sf_id} [dry-run] would write {list(result.get('planned') or {})}"
    else:
        summary = f"Case {sf_id} [live] wrote {list(result['written'])}"
        if result["skipped"]:
            summary += f", skipped {list(result['skipped'])}"
    return {
        "sf_writeback": result,
        **_trace(config["_node_id"], "sf_writeback", summary, result),
    }


@register("draft")
def h_draft(state: CaseState, config: dict) -> dict:
    case = state.get("case", {})
    retrieval = state.get("retrieval", [])
    context = "\n\n---\n\n".join(
        f"[{i+1}] {r['doc_url']}\n{r['chunk_text']}" for i, r in enumerate(retrieval[:5])
    ) or "(no retrieved context)"
    body = f"Subject: {case.get('subject','')}\n\n{case.get('body','')}".strip()

    raw = llm.complete(
        system=(
            "You are a support agent. Using ONLY the provided documentation context, "
            "write a concise, friendly reply that resolves the customer's issue. "
            "Return a JSON object: {\"reply\": string, \"confidence\": number 0..1} "
            "where confidence reflects how well the context actually answers the case."
        ),
        user=f"# Case\n{body}\n\n# Documentation context\n{context}",
        model=config.get("model", llm.DEFAULT_MODEL),
        json_object=True,
        max_tokens=int(config.get("max_tokens", 500)),
    )
    tokens = llm.last_usage
    parsed = _safe_json(raw)
    reply = parsed.get("reply") or parsed.get("draft") or raw
    try:
        model_conf = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        model_conf = 0.5
    model_conf = max(0.0, min(1.0, model_conf))

    grounded = groundedness.check(reply, retrieval[:5])

    return {
        "draft": reply,
        "draft_confidence": round(model_conf, 4),
        "groundedness": grounded,
        **_trace(
            config["_node_id"], "draft",
            f"{len(reply)} chars, model_confidence={model_conf:.2f}, "
            f"groundedness={grounded['score']:.2f} ({grounded['backend']}), sources={len(retrieval[:5])}",
            {"draft_confidence": model_conf, "chars": len(reply),
             "groundedness": grounded, "tokens": tokens},
        ),
    }


@register("confidence_gate")
def h_confidence_gate(state: CaseState, config: dict) -> dict:
    tier = state.get("tier", "basic")
    overrides = config.get("tier_overrides", {})
    threshold = float(overrides.get(tier, config.get("default_threshold", 0.35)))

    retr = float(state.get("retrieval_score", 0.0))
    drft = float(state.get("draft_confidence", 0.0))
    grnd = float((state.get("groundedness") or {}).get("score", 0.0))
    w = float(config.get("retrieval_weight", 0.5))
    gw = float(config.get("groundedness_weight", 0.0))   # 0 -> unchanged from Phase 2

    base = w * retr + (1.0 - w) * drft
    score = round((1.0 - gw) * base + gw * grnd, 4)
    passed = score >= threshold

    gate = {
        "pass": passed,
        "threshold": threshold,
        "score": score,
        "tier": tier,
        "retrieval_score": round(retr, 4),
        "draft_confidence": round(drft, 4),
        "groundedness": round(grnd, 4),
        "groundedness_weight": gw,
    }
    return {
        "confidence": score,
        "confidence_gate": gate,
        **_trace(
            config["_node_id"], "confidence_gate",
            f"score={score:.3f} vs threshold={threshold:.2f} ({tier}) -> {'PASS' if passed else 'FAIL'}",
            gate,
        ),
    }


@register("auto_reply")
def h_auto_reply(state: CaseState, config: dict) -> dict:
    outcome = {
        "action": "auto_reply",
        "reply": state.get("draft", ""),
        "confidence": state.get("confidence"),
        "channel": config.get("channel", "email"),
    }
    return {"outcome": outcome, **_trace(config["_node_id"], "auto_reply", "reply sent automatically")}


@register("ask_human")
def h_ask_human(state: CaseState, config: dict) -> dict:
    channel = config.get("channel", "salesforce_chatter")
    case = state.get("case", {})
    sf_id = case.get("sf_id") or case.get("id")
    confidence = state.get("confidence")
    outcome = {
        "action": "ask_human",
        "channel": channel,
        "draft": state.get("draft", ""),
        "confidence": confidence,
        "reason": "confidence below tier threshold",
    }

    if channel == "salesforce_chatter" and sf_id:
        body = (
            f"Support bot needs a human on this case (confidence {confidence}). "
            f"Suggested draft below — please review before sending.\n\n"
            f"{state.get('draft', '')}"
        )
        chatter = salesforce.post_chatter(
            sf_id, body, mention_id=config.get("mention_id"), tenant_id=state.get("tenant_id")
        )
        outcome["chatter"] = chatter
        mode = "dry-run" if chatter.get("dry_run") else "posted"
        summary = f"Chatter {mode} on Case {sf_id}"
    else:
        summary = f"handed to human via {channel}"
        if channel == "salesforce_chatter":
            summary += " (no sf_id — not posted)"

    return {"outcome": outcome, **_trace(config["_node_id"], "ask_human", summary, outcome)}


@register("handover")
def h_handover(state: CaseState, config: dict) -> dict:
    outcome = {
        "action": "handover",
        "reason": config.get("reason", "policy"),
        "draft": state.get("draft", ""),
        "confidence": state.get("confidence"),
        "tier": state.get("tier"),
    }
    return {
        "outcome": outcome,
        **_trace(config["_node_id"], "handover", f"full handover ({outcome['reason']})"),
    }


def _safe_json(s: str) -> dict:
    import json
    import re

    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", s or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}
