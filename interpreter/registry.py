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
import re
from typing import Any, Callable

from . import groundedness, llm, policy, salesforce
from .retrieval import hybrid_retrieve
from .state import CaseState

log = logging.getLogger("interpreter.registry")

Handler = Callable[[CaseState, dict], dict]

_REGISTRY: dict[str, Handler] = {}

# A single doc chunk can be ~27k chars; five of them concatenated blow past
# a hosted model's per-request token ceiling (Groq free tier: 8k TPM ->
# hard 413). Cap what actually goes into a prompt.
CTX_PER_CHUNK = 1800
CTX_TOTAL = 7000


def _context_block(retrieval: list[dict], *, max_chunks: int = 5,
                   per_chunk: int = CTX_PER_CHUNK, total: int = CTX_TOTAL,
                   with_urls: bool = True) -> str:
    parts: list[str] = []
    used = 0
    for i, r in enumerate(retrieval[:max_chunks]):
        text = (r.get("chunk_text") or "")[:per_chunk]
        if used + len(text) > total:
            text = text[: max(0, total - used)]
        if not text:
            break
        parts.append(f"[{i+1}] {r.get('doc_url','')}\n{text}" if with_urls else text)
        used += len(text)
    return "\n\n---\n\n".join(parts)


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


def _tier_known(raw: Any) -> bool:
    """True when `raw` maps to one of the three canonical tiers."""
    return str(raw or "").strip().lower() in _TIER_ALIASES


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
    # When the CRM gives no *recognisable* tier for this sender — missing, or a
    # value like the SF standard `Account.Type` ("Customer") that isn't one of
    # basic/premium/enterprise — `_norm_tier` fails *closed* to `enterprise`
    # (always-handover). A flow can opt into a gentler fallback with
    # `config.default_tier`; a real, mappable tier on the account still wins.
    tier_defaulted = bool(config.get("default_tier")) and not _tier_known(tier_raw)
    tier = _norm_tier(config["default_tier"] if tier_defaulted else tier_raw)

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
        "tier_defaulted": tier_defaulted,
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


@register("sf_case")
def h_sf_case(state: CaseState, config: dict) -> dict:
    """Phase 20e — resolve an inbound message (email / chat) to a real
    Salesforce Case, so every downstream SF node (`sf_writeback`,
    `ask_human`, `handover`) has an `sf_id` to act on. Reuses the sender's
    open Case when the message is a thread reply; otherwise creates the
    Contact (and, for a business domain, the Account) and the Case.
    Pass-through — merges `sf_id` and the Account tier / region back into
    `state.case`. Dry-run (nothing created) with no Salesforce creds.

    config: {origin="Email", status="New", create_contact=true,
             create_account=true, reuse_open_days=14}
    """
    nid = config["_node_id"]
    case = dict(state.get("case") or {})
    info = salesforce.ensure_case(
        case, state.get("sender") or {},
        origin=config.get("origin", "Email"),
        status=config.get("status", "New"),
        create_contact=bool(config.get("create_contact", True)),
        create_account=bool(config.get("create_account", True)),
        reuse_open_days=int(config.get("reuse_open_days", 14)),
        tenant_id=state.get("tenant_id"),
    )

    if info.get("sf_id"):
        case["sf_id"] = info["sf_id"]
    acct = dict(case.get("account") or {})
    for k, v in (info.get("account") or {}).items():
        if v:
            acct[k] = v
    if info.get("account_name") and not acct.get("name"):
        acct["name"] = info["account_name"]
    if acct:
        case["account"] = acct
    con = dict(case.get("contact") or {})
    if info.get("contact_id") and not con.get("email"):
        con["email"] = case.get("from") or ""
    if con:
        case["contact"] = con

    if info.get("dry_run"):
        summary = "dry-run — no Salesforce Case created"
    else:
        base = (f"reused open Case {info.get('case_number') or info['sf_id']}"
                if info.get("reused") else
                f"created Case {info['sf_id']}" if info.get("created") else
                f"Case {info.get('sf_id')}")
        extra = [x for x, on in (("new Account", info.get("account_created")),
                                 ("new Contact", info.get("contact_created"))) if on]
        summary = base + (f" ({', '.join(extra)})" if extra else "")

    return {"case": case, "sf_case": info, **_trace(nid, "sf_case", summary, info)}


def _render_template(tmpl: str, state: CaseState) -> str:
    """Tiny `{{ dotted.path }}` substitution over state (Phase 14 kb_lookup
    query). Unknown paths -> empty string. No expressions, no code."""
    def sub(m: "re.Match[str]") -> str:
        v = _dig(state, m.group(1).strip())
        return "" if v is None else str(v)
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", sub, tmpl).strip()


@register("kb_lookup")
def h_kb_lookup(state: CaseState, config: dict) -> dict:
    """Consult one or more internal KB collections *at this point in the
    flow*. Only runs if the graph routes here — otherwise the internal
    knowledge is never touched. Result lands in `state[out_key]` for a
    downstream `draft` node to treat as authoritative."""
    case = state.get("case", {})
    collections = config.get("collections") or []
    out_key = config.get("out_key", "internal_kb")

    tmpl = config.get("query")
    if tmpl:
        query = _render_template(tmpl, state)
    else:
        query = " ".join(
            p for p in (case.get("subject", ""), case.get("body", "")) if p
        ).strip()

    results, score = hybrid_retrieve(
        query,
        top_k=int(config.get("top_k", 4)),
        use_sparse=config.get("use_sparse", True),
        use_graph=False,                       # internal KB isn't in the graph
        use_rerank=config.get("use_rerank", True),
        kb_sources=collections or None,
        tenant_id=state.get("tenant_id"),
    )
    min_score = float(config.get("min_score", 0.0))
    hit = bool(results) and score >= min_score
    payload = {
        "checked": True,
        "collections": collections,
        "score": round(score, 4),
        "matches": results if hit else [],
    }
    top = results[0]["doc_url"] if (hit and results) else None
    return {
        out_key: payload,
        **_trace(
            config["_node_id"], "kb_lookup",
            f"{len(payload['matches'])} internal hit(s) from {collections or 'any'}, "
            f"score={score:.3f}, top={top}",
            {"score": score, "collections": collections, "hits": len(payload["matches"]),
             "query": query[:200]},
        ),
    }


@register("extract")
def h_extract(state: CaseState, config: dict) -> dict:
    """Pull named fields out of the case into `state["entities"]` so policy
    rules can key on them (e.g. a fiscal year the customer mentioned).
    `config.fields` = {name: "what it means"}."""
    fields: dict[str, str] = config.get("fields") or {}
    if not fields:
        return {"entities": {}, **_trace(config["_node_id"], "extract", "no fields configured", {})}

    case = state.get("case", {})
    body = f"{case.get('subject','')}\n\n{case.get('body','')}".strip()
    spec = "\n".join(f"- {k}: {v}" for k, v in fields.items())
    raw = llm.complete(
        system=("Extract the requested fields from the support message. Return a "
                "JSON object with exactly these keys; use null when the message "
                "doesn't say. Numbers as numbers, not strings."),
        user=f"# Fields\n{spec}\n\n# Message\n{body or '(empty)'}",
        model=config.get("model", llm.FAST_MODEL),
        json_object=True,
        max_tokens=int(config.get("max_tokens", 300)),
    )
    parsed = _safe_json(raw)
    entities = {k: parsed.get(k) for k in fields}
    return {
        "entities": entities,
        **_trace(config["_node_id"], "extract",
                 f"extracted {', '.join(f'{k}={v!r}' for k, v in entities.items())}",
                 {"entities": entities}),
    }


@register("policy_gate")
def h_policy_gate(state: CaseState, config: dict) -> dict:
    """Evaluate the tenant/team's `policy_rules` against the whole state.
    First match wins (by ascending priority). `then.type=='route'` sets a
    routing override; `then.type=='task'` stashes a task for a downstream
    `task_dispatch` node. No match -> pass through, `policy.matched=None`."""
    from ingestion.scraper import get_supabase

    tenant_id, team = state.get("tenant_id"), state.get("team")
    rules: list[dict] = []
    if tenant_id and team:
        try:
            sb = config.get("_sb") or get_supabase()
            rules = (sb.table("policy_rules").select("*")
                     .eq("tenant_id", tenant_id).eq("team", team)
                     .eq("status", "active").execute().data or [])
        except Exception as e:  # noqa: BLE001
            log.warning("policy_gate: could not load rules: %s", e)

    rule = policy.first_match(rules, dict(state))
    then = (rule or {}).get("then") or {}
    result = {
        "matched": (rule or {}).get("name"),
        "action": then.get("action") if then.get("type") == "route" else None,
        "task": then if then.get("type") == "task" else None,
        "rules_evaluated": len(rules),
    }
    summary = (f"rule '{result['matched']}' -> {then.get('type')}"
               if rule else f"no match ({len(rules)} rule(s))")
    return {
        "policy": result,
        **_trace(config["_node_id"], "policy_gate", summary, result),
    }


@register("task_dispatch")
def h_task_dispatch(state: CaseState, config: dict) -> dict:
    """If `policy.task` is set, raise an `action_requests` row and post a
    Slack Approve/Reject message. The external effect (a GitHub issue) is
    NOT done here — it waits on the Slack callback. Terminal-ish."""
    from ingestion.scraper import get_supabase
    from interpreter import slack as slackmod

    task = (state.get("policy") or {}).get("task")
    nid = config["_node_id"]
    if not task:
        info = {"dispatched": False, "reason": "no policy task"}
        return {"outcome": {"action": "task_skipped", **info},
                **_trace(nid, "task_dispatch", "no policy task — nothing to dispatch", info)}

    tenant_id = state.get("tenant_id")
    tmpl_ctx = dict(state)
    payload = {
        "repo": task.get("repo", ""),
        "title": _render_template(task.get("title_tmpl", "Support action: {{case.subject}}"), tmpl_ctx),
        "body": _render_template(task.get("body_tmpl", "{{case.body}}"), tmpl_ctx),
        "labels": task.get("labels", []),
        "assignees": task.get("assignees", []),
    }
    approval = task.get("approval") or {}
    channel = approval.get("slack_channel") or approval.get("slack_user") or config.get("slack_channel")

    sb = config.get("_sb") or get_supabase()
    row = {
        "tenant_id": tenant_id,
        "rule_name": (state.get("policy") or {}).get("matched"),
        "kind": task.get("task", "github_issue"), "payload": payload,
        "slack_channel": channel, "status": "pending",
    }
    # run_id is stamped later by runs.record_run (the run doesn't exist yet).
    ar = sb.table("action_requests").insert(row).execute().data[0]

    posted = None
    if channel and slackmod.available():
        try:
            posted = slackmod.post_approval(
                tenant_id, channel,
                summary=f"*{payload['title']}*\nopen `{payload['repo']}` issue?  "
                        f"(rule: {row['rule_name']})",
                action_id=ar["id"], sb=sb,
            )
            sb.table("action_requests").update(
                {"slack_ts": posted.get("ts")}
            ).eq("id", ar["id"]).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("task_dispatch: Slack post failed: %s", e)

    info = {"dispatched": True, "action_request_id": ar["id"], "kind": row["kind"],
            "repo": payload["repo"], "slack_posted": bool(posted), "channel": channel}
    return {
        # top-level so runs.record_run can link the row even if a later
        # terminal node (handover / auto_reply) overwrites `outcome`
        "action_request_id": ar["id"],
        "outcome": {"action": "task_dispatched", **info},
        **_trace(nid, "task_dispatch",
                 f"raised {row['kind']} for {payload['repo']} -> "
                 f"{'Slack approval requested' if posted else 'pending (no Slack)'}",
                 info),
    }


@register("draft")
def h_draft(state: CaseState, config: dict) -> dict:
    case = state.get("case", {})
    retrieval = state.get("retrieval", [])
    context = _context_block(retrieval) or "(no retrieved context)"
    body = f"Subject: {case.get('subject','')}\n\n{case.get('body','')}".strip()

    # Phase 14: internal-runbook context from a kb_lookup node upstream wins
    # over the public docs.
    internal = state.get(config.get("internal_kb_key", "internal_kb")) or {}
    internal_matches = internal.get("matches") or []
    if internal_matches:
        user = (
            f"# Case\n{body}\n\n"
            f"# Internal runbook (authoritative — follow this over the public docs)\n"
            f"{_context_block(internal_matches)}\n\n"
            f"# Public documentation context\n{context}"
        )
    else:
        user = f"# Case\n{body}\n\n# Documentation context\n{context}"

    raw = llm.complete(
        system=(
            "You are a support agent. Using ONLY the provided context, write a "
            "concise, friendly reply that resolves the customer's issue. When an "
            "internal runbook is provided it is authoritative — prefer it over "
            "the public documentation. "
            "Return a JSON object: {\"reply\": string, \"confidence\": number 0..1} "
            "where confidence reflects how well the context actually answers the case."
        ),
        user=user,
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

    grounded = groundedness.check(reply, (internal_matches[:5] + retrieval[:5]))

    return {
        "draft": reply,
        "draft_confidence": round(model_conf, 4),
        "groundedness": grounded,
        **_trace(
            config["_node_id"], "draft",
            f"{len(reply)} chars, model_confidence={model_conf:.2f}, "
            f"groundedness={grounded['score']:.2f} ({grounded['backend']}), "
            f"sources={len(retrieval[:5])}+{len(internal_matches[:5])} internal",
            {"draft_confidence": model_conf, "chars": len(reply),
             "groundedness": grounded, "tokens": tokens,
             "used_internal_kb": bool(internal_matches)},
        ),
    }


def _slug_tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t}


@register("confidence_gate")
def h_confidence_gate(state: CaseState, config: dict) -> dict:
    tier = state.get("tier", "basic")
    overrides = config.get("tier_overrides", {})
    threshold = float(overrides.get(tier, config.get("default_threshold", 0.35)))

    retr = float(state.get("retrieval_score", 0.0))
    drft = float(state.get("draft_confidence", 0.0))
    grnd = float((state.get("groundedness") or {}).get("score", 0.0))

    weights = config.get("weights")
    if weights:
        # explicit 3-way blend (Phase 7 recalibration). The real LLM's
        # self-graded `draft` confidence sits ~0.95 on everything, so it's
        # weighted low here; `retrieval` + `groundedness` carry the score.
        wr = float(weights.get("retrieval", 0.0))
        wd = float(weights.get("draft", 0.0))
        wg = float(weights.get("groundedness", 0.0))
        tot = (wr + wd + wg) or 1.0
        wr, wd, wg = wr / tot, wd / tot, wg / tot
        score = round(wr * retr + wd * drft + wg * grnd, 4)
    else:
        # legacy 2-knob formula (Phase 2 / the 011 calibration)
        w = float(config.get("retrieval_weight", 0.5))
        gw = float(config.get("groundedness_weight", 0.0))
        wr, wd, wg = w * (1 - gw), (1 - w) * (1 - gw), gw
        base = w * retr + (1.0 - w) * drft
        score = round((1.0 - gw) * base + gw * grnd, 4)

    passed = score >= threshold

    # Topic-level escalation: billing / refunds / pricing / legal / account
    # access / data-export requests are never a docs answer — hand to a human
    # no matter how confident the draft or how good the (wrong-topic)
    # retrieval looks. Matched on shared slug tokens, so "refund-request"
    # hits "refund" but "export-step-howto" does not hit "data-export".
    # Static list here = the Phase 7 down payment on Phase 16's rule engine.
    escalate = config.get("escalate_topics", [])
    topic = str((state.get("classification") or {}).get("topic", ""))
    ttok = _slug_tokens(topic)
    forced = next(
        (e for e in escalate
         if _slug_tokens(e) and _slug_tokens(e) <= ttok), None
    )
    if forced:
        passed = False

    gate = {
        "pass": passed,
        "threshold": threshold,
        "score": score,
        "tier": tier,
        "retrieval_score": round(retr, 4),
        "draft_confidence": round(drft, 4),
        "groundedness": round(grnd, 4),
        "weights": {"retrieval": round(wr, 3), "draft": round(wd, 3),
                    "groundedness": round(wg, 3)},
    }
    if forced:
        gate["forced_escalation"] = f"topic '{topic}' ~ '{forced}'"
    reason = (f"forced escalate ({gate['forced_escalation']})" if forced
              else f"score={score:.3f} vs threshold={threshold:.2f} ({tier})")
    return {
        "confidence": score,
        "confidence_gate": gate,
        **_trace(
            config["_node_id"], "confidence_gate",
            f"{reason} -> {'PASS' if passed else 'FAIL'}",
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


@register("identify")
def h_identify(state: CaseState, config: dict) -> dict:
    """Phase 17b — resolve who the sender is before triage: an exact CRM
    contact/lead by email, else the email **domain → an Account** (a
    colleague of an existing customer), else unknown. Writes `state.sender`;
    downstream nodes/edges branch on it. Pass-through (no routing of its
    own). Degrades to `match='none'` with no Salesforce creds.

    config: {email_field="contact.email", domain_match=true,
             free_email_domains?, create_lead_if_missing=false}
    """
    nid = config["_node_id"]
    case = state.get("case", {})
    paths = [config.get("email_field", "contact.email"),
             "from", "supplied_email", "email", "contact.email"]
    email = ""
    for p in paths:
        v = _dig(case, p)
        if isinstance(v, str) and "@" in v:
            email = v
            break

    sender = salesforce.identify_sender(
        email,
        free_domains=config.get("free_email_domains"),
        domain_match=bool(config.get("domain_match", True)),
        create_lead=bool(config.get("create_lead_if_missing", False)),
        tenant_id=state.get("tenant_id"),
    )
    acct = f" / account '{sender['account_name']}'" if sender.get("account_matched") else ""
    summary = f"{email or '(no email)'} → {sender['match']}{acct}"
    return {"sender": sender, **_trace(nid, "identify", summary, sender)}


@register("clarify")
def h_clarify(state: CaseState, config: dict) -> dict:
    """Low-confidence recovery (Phase 17). The knowledge base didn't cover
    the case and the topic isn't a forced human escalation — so instead of
    a blind handoff, produce the *specific* questions whose answers would
    let the bot resolve it on the next round, and surface them (to a human
    to send for now; `auto_send` customer-facing delivery is a later
    chunk). The customer's reply comes back as a new case. Terminal.

    config: {max_questions=3, auto_send=false, channel="email",
             model=FAST_MODEL, max_tokens=350, mention_id?}
    """
    nid = config["_node_id"]
    case = state.get("case", {})
    sf_id = case.get("sf_id") or case.get("id")
    max_q = max(1, int(config.get("max_questions", 3)))
    channel = config.get("channel", "email")

    # Phase 17d: how many times have we already gone back to this customer?
    # Past `max_rounds` (default 2) we stop asking and hand to a human.
    max_rounds = max(1, int(config.get("max_rounds", 2)))
    case_key = case.get("case_id") or sf_id
    prior_rounds = 0
    if case_key:
        try:
            from ingestion.scraper import get_supabase

            sb = config.get("_sb") or get_supabase()
            rows = (sb.table("runs").select("clarify_round")
                    .eq("case_id", str(case_key)).eq("outcome", "need_info")
                    .execute().data or [])
            prior_rounds = max((int(r.get("clarify_round") or 1) for r in rows), default=0)
        except Exception as e:  # noqa: BLE001 — best-effort; treat as round 1
            log.warning("clarify: prior-round lookup failed: %s", e)
    clarify_round = prior_rounds + 1
    exhausted = clarify_round > max_rounds

    # Phase 17b: if an `identify` node ran and the sender isn't a known
    # contact, also ask them to confirm who they are.
    sender = state.get("sender") or {}
    ask_identity = bool(sender) and not sender.get("known") and (
        sender.get("match") in (None, "", "none") or sender.get("account_matched")
    )
    account_hint = sender.get("account_name") if sender.get("account_matched") else None
    if ask_identity and account_hint:
        identity_line = (
            f"\n\nThe sender is not a known contact, but their email domain matches the "
            f"account '{account_hint}'. Also ask them to confirm they're with '{account_hint}' "
            f"and to share an identifying reference (workspace / order / ticket ID)."
        )
    elif ask_identity:
        identity_line = (
            "\n\nThe sender is not in our records. Also ask which company / account "
            "they're with and for an identifying reference (workspace / order / ticket ID)."
        )
    else:
        identity_line = ""

    body = f"Subject: {case.get('subject', '')}\n\n{case.get('body', '')}".strip()
    context = _context_block(state.get("retrieval") or []) or "(nothing relevant retrieved)"
    unsupported = (state.get("groundedness") or {}).get("unsupported") or []

    raw = llm.complete(
        system=(
            "A support bot could not confidently answer a customer's message from its "
            "knowledge base. Write the SHORTEST list of specific questions whose answers "
            "would let it resolve the issue — concrete details only (exact error text, "
            "product / plan, IDs, what they have already tried). Never ask for something "
            "the message already states. "
            f'Return JSON {{"questions": [string], "missing": [string]}} '
            f"with at most {max_q} questions." + identity_line
        ),
        user=(
            f"# Customer message\n{body or '(empty)'}\n\n"
            f"# What the knowledge base had\n{context}"
            + (
                "\n\n# Draft claims we could not ground\n- " + "\n- ".join(unsupported[:5])
                if unsupported else ""
            )
        ),
        model=config.get("model", llm.FAST_MODEL),
        json_object=True,
        max_tokens=int(config.get("max_tokens", 350)),
    )
    parsed = _safe_json(raw)
    questions = [
        q.strip() for q in (parsed.get("questions") or [])
        if isinstance(q, str) and q.strip()
    ][:max_q]
    if not questions:
        questions = [
            "Could you share more detail about what you're trying to do, including "
            "any exact error message and the steps you've already tried?"
        ]
    missing = [m for m in (parsed.get("missing") or []) if isinstance(m, str)]
    numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))

    # exhausted -> stop asking the customer; hand to a human with the gaps.
    auto_send = bool(config.get("auto_send", False)) and not exhausted
    recipient = (
        (sender.get("email") if sender else None)
        or (case.get("contact") or {}).get("email")
        or case.get("from")
    )

    delivery: dict | None = None       # {sent|posted, via, ...}
    if auto_send and sf_id:
        customer_msg = (
            "Thanks for reaching out. To help you with this, could you share:\n\n"
            + numbered + "\n\nOnce we have that we'll follow up."
        )
        delivery = salesforce.send_case_reply(
            sf_id, customer_msg, to_email=recipient,
            subject=config.get("subject", "We need a bit more information"),
            tenant_id=state.get("tenant_id"),
        )
    elif sf_id:
        note = (
            (f"Asked the customer for more detail {prior_rounds}× already and it's "
             f"still unclear — a human should take this over. Outstanding:\n\n"
             if exhausted else
             "Support bot could not answer this from the knowledge base and needs more "
             "information from the customer. Suggested questions to send:\n\n")
            + numbered
        )
        delivery = salesforce.post_chatter(
            sf_id, note, mention_id=config.get("mention_id"),
            tenant_id=state.get("tenant_id"),
        )

    auto_sent = bool(auto_send and delivery and delivery.get("sent"))
    posted_internal = bool(not auto_send and delivery and not delivery.get("dry_run"))
    clarification = {
        "questions": questions,
        "missing": missing,
        "channel": channel,
        "auto_send": auto_send,
        "auto_sent": auto_sent,
        "posted": posted_internal,
        "delivery": delivery,
        "ask_identity": ask_identity,
        "account_hint": account_hint,
        "round": clarify_round,
        "max_rounds": max_rounds,
        "exhausted": exhausted,
    }
    outcome = {
        "action": "ask_human" if exhausted else "need_info",
        "reason": "clarify_exhausted" if exhausted else "kb_insufficient",
        "questions": questions,
        "missing": missing,
        "channel": channel,
        "confidence": state.get("confidence"),
        "sent_to_customer": auto_sent,
        "awaiting_customer": auto_sent,
        "clarify_round": clarify_round,
    }
    if delivery:
        outcome["delivery"] = delivery
    rnd = f"round {clarify_round}/{max_rounds}"
    if exhausted:
        where = (f"{rnd} — exhausted, handing to a human"
                 + (f" (Chatter {'posted' if posted_internal else 'dry-run'} on Case {sf_id})"
                    if sf_id else ""))
    elif not sf_id:
        where = f"{rnd} — no sf_id, questions in trace only"
    elif auto_send:
        via = (delivery or {}).get("via", "?")
        where = f"{rnd} — {'sent to customer' if auto_sent else 'send failed'} via {via} (Case {sf_id})"
    else:
        where = f"{rnd} — Chatter {'posted' if posted_internal else 'dry-run'} on Case {sf_id}"
    return {
        "clarification": clarification,
        "outcome": outcome,
        "clarify_round": clarify_round,
        **_trace(
            nid, "clarify",
            f"{len(questions)} question(s) for the customer — {where}"
            + (" (+ identity)" if ask_identity else ""),
            {"questions": questions, "missing": missing, "channel": channel,
             "auto_sent": auto_sent, "posted": posted_internal, "round": clarify_round,
             "exhausted": exhausted, "ask_identity": ask_identity,
             "account_hint": account_hint},
        ),
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
