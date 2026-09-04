"""
KIL-b — the contradiction / integrity judge.

`groundedness.check()` asks "is the *draft* supported by the retrieved docs?".
This asks the sharper question the Knowledge Integrity Loop needs: does a piece
of text **contradict** what the KB or the case-history graph already says?

    check(statement, contexts, *, kind="draft") -> {
        relation: "entails" | "neutral" | "contradicts",   # worst-of the claims
        flagged:  bool,      # relation == "contradicts"
        novel:    bool,      # a factual claim nothing in context supports
        verdicts: [ {claim, relation, evidence, confidence}, ... ],
        salient:  [str],     # the claims that drove `relation`
        backend:  "groq" | "heuristic" | "none",
    }

`contexts` is a list of `{"text": str, "ref"?: str, "kind"?: "kb"|"resolution"}`.

Two backends, one shape — a Groq NLI judge when a key is set, a conservative
deterministic heuristic otherwise (it only flags a contradiction on a clear
negation mismatch over shared terms; when unsure it stays `neutral`, because a
false flag costs a manager's attention and a missed one is caught post-send).
Never raises; empty inputs -> a safe neutral result.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import llm

_REL = ("entails", "neutral", "contradicts")
_RANK = {"entails": 0, "neutral": 1, "contradicts": 2}
_MIN_CONF = 0.55

_WORD = re.compile(r"[a-z0-9][a-z0-9-]{2,}")
_NEG = re.compile(
    r"\b(not|no|never|cannot|can't|isn't|aren't|won't|don't|doesn't|"
    r"unsupported|unavailable|disabled|blocked|prohibited|except|only)\b"
)
_STOP = {
    "the", "and", "for", "you", "your", "with", "this", "that", "from", "are",
    "was", "has", "have", "can", "will", "our", "their", "there", "into", "out",
    "about", "when", "then", "than", "which", "who", "how", "why", "what",
    "please", "thanks", "hi", "hello", "team", "support", "reply", "issue",
}


def _content(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def _heuristic_relation(statement: str, ctx_text: str) -> tuple[str, str]:
    """Deterministic, conservative. Returns (relation, evidence-snippet)."""
    s_words, c_words = _content(statement), _content(ctx_text)
    shared = s_words & c_words
    if len(shared) < 2:
        return "neutral", ""
    s_neg = bool(_NEG.search(statement.lower()))
    c_neg = bool(_NEG.search(ctx_text.lower()))
    snip = " ".join(sorted(shared)[:6])
    if s_neg != c_neg and len(shared) >= 3:
        # same subject matter, opposite polarity -> likely contradiction
        return "contradicts", snip
    if len(shared) >= 4 and s_neg == c_neg:
        return "entails", snip
    return "neutral", snip


def _cap_contexts(contexts: list[dict[str, Any]], *, budget: int = 7000) -> str:
    parts, used = [], 0
    for i, c in enumerate(contexts[:6]):
        t = (c.get("text") or "")[:1800]
        if not t or used + len(t) > budget:
            break
        ref = c.get("ref") or f"ctx{i + 1}"
        parts.append(f"[{ref}] {t}")
        used += len(t)
    return "\n\n---\n\n".join(parts) or "(none)"


def _judge_groq(statement: str, contexts: list[dict[str, Any]],
                *, model: str | None, tenant_id: str | None = None) -> dict[str, Any]:
    raw = llm.complete(
        system=(
            "You are a fact-consistency checker for a support knowledge base. "
            "Given a STATEMENT and CONTEXT passages, list each checkable factual "
            "claim in the STATEMENT, and for each decide whether the CONTEXT "
            "ENTAILS it, CONTRADICTS it, or is NEUTRAL (context neither confirms "
            "nor denies). Judge only against the context, not world knowledge. "
            'Return JSON {"claims": [{"claim": string, "relation": '
            '"entails"|"neutral"|"contradicts", "evidence": string, '
            '"confidence": number 0..1}]}.'
        ),
        user=f"# STATEMENT\n{statement}\n\n# CONTEXT\n{_cap_contexts(contexts)}",
        model=model or llm.FAST_MODEL,
        json_object=True,
        max_tokens=500,
        tenant_id=tenant_id,
    )
    try:
        claims = json.loads(raw).get("claims", [])
    except (json.JSONDecodeError, TypeError, AttributeError):
        return _judge_heuristic(statement, contexts)
    verdicts = []
    for c in claims[:12]:
        rel = str(c.get("relation", "neutral")).lower()
        if rel not in _REL:
            rel = "neutral"
        try:
            conf = max(0.0, min(1.0, float(c.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        verdicts.append({"claim": str(c.get("claim", ""))[:300], "relation": rel,
                         "evidence": str(c.get("evidence", ""))[:300], "confidence": conf})
    return _summarize(verdicts, backend="groq")


def _judge_heuristic(statement: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = []
    for i, ctx in enumerate(contexts[:6]):
        rel, ev = _heuristic_relation(statement, ctx.get("text") or "")
        if rel == "neutral":
            continue
        verdicts.append({"claim": statement[:300], "relation": rel,
                         "evidence": (ctx.get("ref") or f"ctx{i + 1}") + ": " + ev,
                         "confidence": 0.6})
    return _summarize(verdicts, backend="heuristic")


def _summarize(verdicts: list[dict[str, Any]], *, backend: str) -> dict[str, Any]:
    strong = [v for v in verdicts if v["confidence"] >= _MIN_CONF]
    # worst-of: any strong `contradicts` wins; else any `neutral`; else (all
    # strong verdicts agree it's supported) `entails`; nothing strong -> neutral.
    ranks = [_RANK[v["relation"]] for v in strong]
    relation = _REL[max(ranks)] if ranks else "neutral"
    salient = [v["claim"] for v in strong
               if v["relation"] == relation and relation != "neutral"]
    return {"relation": relation, "verdicts": verdicts, "salient": salient[:5],
            "backend": backend}


def check(statement: str, contexts: list[dict[str, Any]] | None = None,
          *, kind: str = "draft", model: str | None = None,
          tenant_id: str | None = None) -> dict[str, Any]:
    """See module docstring. `kind` ∈ {draft, inbound, human_reply} — only
    affects `novel` (a still-unsupported factual claim matters on a human reply
    or a bot draft, less so on inbound customer text)."""
    empty = {"relation": "neutral", "flagged": False, "novel": False,
             "verdicts": [], "salient": [], "backend": "none"}
    if not (statement or "").strip() or not contexts:
        return empty
    res = (_judge_groq(statement, contexts, model=model, tenant_id=tenant_id) if llm.available(tenant_id=tenant_id)
           else _judge_heuristic(statement, contexts))
    res["flagged"] = res["relation"] == "contradicts"
    res["novel"] = (kind in ("draft", "human_reply")
                    and res["relation"] == "neutral"
                    and any(v["relation"] != "entails" for v in res["verdicts"]))
    return res


def contexts_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Assemble KB + case-history passages already in the flow state as
    `check()` contexts — retrieved docs, internal KB matches, prior resolutions."""
    out: list[dict[str, Any]] = []
    for p in (state.get("prior_resolutions") or [])[:3]:
        if p.get("resolution_text"):
            out.append({"text": p["resolution_text"], "kind": "resolution",
                        "ref": f"case {p.get('case_number') or '?'}"})
    internal = (state.get("internal_kb") or {}).get("matches") or []
    for m in internal[:3]:
        if m.get("chunk_text"):
            out.append({"text": m["chunk_text"], "kind": "kb",
                        "ref": m.get("doc_url") or "internal-kb"})
    for r in (state.get("retrieval") or [])[:4]:
        if r.get("chunk_text"):
            out.append({"text": r["chunk_text"], "kind": "kb",
                        "ref": r.get("doc_url") or "kb"})
    return out
