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

from typing import Any, Callable

from . import llm
from .retrieval import hybrid_retrieve
from .state import CaseState

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


def _norm_tier(raw: Any) -> str:
    return _TIER_ALIASES.get(str(raw or "").strip().lower(), "basic")


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
            classification,
        ),
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
    parsed = _safe_json(raw)
    reply = parsed.get("reply") or parsed.get("draft") or raw
    try:
        model_conf = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        model_conf = 0.5
    model_conf = max(0.0, min(1.0, model_conf))

    return {
        "draft": reply,
        "draft_confidence": round(model_conf, 4),
        **_trace(
            config["_node_id"], "draft",
            f"{len(reply)} chars, model_confidence={model_conf:.2f}, sources={len(retrieval[:5])}",
            {"draft_confidence": model_conf, "chars": len(reply)},
        ),
    }


@register("confidence_gate")
def h_confidence_gate(state: CaseState, config: dict) -> dict:
    tier = state.get("tier", "basic")
    overrides = config.get("tier_overrides", {})
    threshold = float(overrides.get(tier, config.get("default_threshold", 0.35)))

    retr = float(state.get("retrieval_score", 0.0))
    drft = float(state.get("draft_confidence", 0.0))
    w = float(config.get("retrieval_weight", 0.5))
    score = round(w * retr + (1.0 - w) * drft, 4)
    passed = score >= threshold

    gate = {
        "pass": passed,
        "threshold": threshold,
        "score": score,
        "tier": tier,
        "retrieval_score": round(retr, 4),
        "draft_confidence": round(drft, 4),
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
    outcome = {
        "action": "ask_human",
        "channel": config.get("channel", "salesforce_chatter"),
        "draft": state.get("draft", ""),
        "confidence": state.get("confidence"),
        "reason": "confidence below tier threshold",
    }
    return {
        "outcome": outcome,
        **_trace(config["_node_id"], "ask_human", f"handed to human via {outcome['channel']}"),
    }


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
