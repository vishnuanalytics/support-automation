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

from . import connectors, groundedness, integrity, llm, policy, salesforce
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


def _with_ocr(state: "CaseState", body: str) -> str:
    """Fold any OCR'd text from image attachments (Phase 25) into the message
    a classify / draft prompt sees — free, no vision model."""
    at = (state.get("attachment_text") or "").strip()
    if not at:
        return body
    return f"{body}\n\n--- text read from attached image(s) ---\n{at[:4000]}"


def _run_text(state: "CaseState") -> tuple[str, str]:
    """(subject, body) for the retrieve / classify / draft prompts. The Case
    fields when there is a Case; otherwise the generic run payload (P5c) —
    `context.subject`|`title`, then `context.body`|`text`|`query`|`message`,
    then a stringified dump of the non-`_` context fields."""
    case = state.get("case") or {}
    if case.get("subject") or case.get("body"):
        return case.get("subject", "") or "", case.get("body", "") or ""
    ctx = state.get("context") or {}
    subject = ctx.get("subject") or ctx.get("title") or ""
    body = (ctx.get("body") or ctx.get("text") or ctx.get("query")
            or ctx.get("message") or ctx.get("question") or "")
    if not (subject or body) and ctx:
        body = "\n".join(f"{k}: {v}" for k, v in ctx.items() if not str(k).startswith("_"))
    return subject, body


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
# Phase 27c — the case-control-plane writes: Status / Routed_Team__c /
# Next_Action_Due__c on the Salesforce Case, plus one `case_events` audit row.
# --------------------------------------------------------------------------
# `sf_writeback` won't downgrade a Case a human has already moved on.
_ADVANCED_STATUS = {"in progress", "escalated", "resolved", "closed"}
_ACK_MINUTES = 30      # design decision: ack SLA is 30 min across the board


def _iso_in(minutes: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _cp_fields(*, status=None, routed_team=None, next_action=None, due_minutes=None,
               escalation_reason=None, confidence=None, slack_ts=None,
               last_run_id=None, stamp_run_at=False) -> dict[str, Any]:
    """Build the Case field dict for a control-plane write. Only non-empty keys."""
    f: dict[str, Any] = {}
    if status:
        f["Status"] = status
    if routed_team:
        f["Routed_Team__c"] = routed_team
    if next_action:
        f["Next_Action__c"] = str(next_action)[:255]
    if due_minutes is not None:
        f["Next_Action_Due__c"] = _iso_in(due_minutes)
    if escalation_reason:
        f["Escalation_Reason__c"] = str(escalation_reason)[:255]
    if confidence is not None:
        try:
            f["AI_Confidence__c"] = round(float(confidence), 2)
        except (TypeError, ValueError):
            pass
    if slack_ts:
        f["Handoff_Slack_Ts__c"] = str(slack_ts)[:64]
    if last_run_id:
        f["Last_Run_Id__c"] = str(last_run_id)[:40]
    if stamp_run_at:
        f["Last_AI_Run_At__c"] = _iso_in(0)
    return f


def _case_conn(state: CaseState, config: dict) -> str:
    """Which connector slug a case-touching node invokes for its Salesforce-
    shaped side effects (update_fields/ensure_case/post_note/add_comment/
    assign_owner/log_email_message/identify_sender/send_case_reply — see
    `connectors.CASE_ACTIONS`). Was a hardcoded `"salesforce"` literal at
    every call site; now resolved once per call via
    `connectors.resolve_case_connector` (config's own `connector` override >
    the tenant's `tenants.case_connector` default, migration 084 >
    `"salesforce"`) — every existing flow/tenant with neither set is
    unaffected."""
    return connectors.resolve_case_connector(state.get("tenant_id"), config, sb=config.get("_sb"))


def _cp_write(state: CaseState, config: dict, *, action: str, actor: str = "ai",
              fields: dict[str, Any] | None = None, reason: str | None = None,
              routed_team: str | None = None, confidence: float | None = None,
              slack_ts: str | None = None, slack_channel: str | None = None) -> dict[str, Any]:
    """Push `fields` to the Case (one API call) and append a `case_events`
    row. Best-effort — never raises, never blocks the node. Returns the
    fields actually attempted."""
    case = state.get("case", {})
    sf_id = case.get("sf_id") or case.get("id")
    fields = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
    prior = str(case.get("status") or "").strip().lower()
    # never move a Case *backwards* in the lifecycle within one run — a later
    # node (e.g. notify_human after ask_human) reads a stale in-state status
    # and must not undo Escalated -> In Progress / Triaged.
    _RANK = {"new": 0, "triaged": 1, "in progress": 2, "waiting on customer": 2,
             "escalated": 3, "resolved": 4, "closed": 5}
    want = str(fields.get("Status") or "").strip().lower()
    if want and prior and _RANK.get(want, 0) < _RANK.get(prior, 0):
        fields.pop("Status")
    if sf_id and fields:
        try:
            connectors.invoke(state.get("tenant_id"), _case_conn(state, config), "update_fields",
                              {"case_id": sf_id, "fields": fields}, org_label=config.get("org"))
        except Exception as e:  # noqa: BLE001
            log.warning("cp_write(%s): %s", sf_id, e)
    # keep the in-run view current so the *next* node doesn't clobber this
    if isinstance(case, dict) and fields.get("Status"):
        case["status"] = fields["Status"]
    try:
        from interpreter import case_events

        case_events.record(
            config.get("_sb"), tenant_id=state.get("tenant_id"),
            case_sf_id=str(sf_id) if sf_id else "",
            case_number=case.get("case_number") or (case.get("case_id") if str(case.get("case_id") or "").isdigit() else None),
            actor=actor, action=action,
            from_status=case.get("status"),
            to_status=fields.get("Status"),
            reason=reason, routed_team=routed_team or fields.get("Routed_Team__c"),
            slack_ts=slack_ts, slack_channel=slack_channel, run_id=state.get("_run_id"),
            confidence=confidence if confidence is not None else fields.get("AI_Confidence__c"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("cp_write case_events(%s): %s", sf_id, e)
    return fields


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------
@register("retrieve")
def h_retrieve(state: CaseState, config: dict) -> dict:
    _subj, _body = _run_text(state)          # P5c — Case fields or `context.*`
    # Phase 29 step 2 — the `agent` node re-invokes this with a reformulated
    # query on a later iteration; every existing caller leaves this unset,
    # so behavior/cost is unchanged for every flow that doesn't opt into `agent`.
    query = config.get("query_override") or (
        " ".join(p for p in (_subj, _body) if p).strip() or _subj or "")
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
    # tier_field / region_field resolve against state first (so an `sf_context`
    # node's `sf_context.account.tier` works), then the case.
    _tf = config.get("tier_field", "account.customer_type")
    _rf = config.get("region_field", "account.region")
    tier_raw = _dig(state, _tf) if _dig(state, _tf) is not None else _dig(case, _tf)
    region = _dig(state, _rf) if _dig(state, _rf) is not None else _dig(case, _rf)
    # When the CRM gives no *recognisable* tier for this sender — missing, or a
    # value like the SF standard `Account.Type` ("Customer") that isn't one of
    # basic/premium/enterprise — `_norm_tier` fails *closed* to `enterprise`
    # (always-handover). A flow can opt into a gentler fallback with
    # `config.default_tier`; a real, mappable tier on the account still wins.
    tier_defaulted = bool(config.get("default_tier")) and not _tier_known(tier_raw)
    tier = _norm_tier(config["default_tier"] if tier_defaulted else tier_raw)

    _subj, _cbody = _run_text(state)          # P5c
    body = _with_ocr(state, f"{_subj}\n\n{_cbody}".strip())
    _model = config.get("model", llm.FAST_MODEL)
    raw = llm.complete(
        system=(
            "You triage inbound support cases. Return a JSON object with keys: "
            "topic (short slug), "
            "type (one of: Question | How-to | Problem / Bug | Billing | "
            "Account / Login | Feature Request | Other), "
            "answer_mode (one of: informational | diagnostic | action | status) "
            "— informational = a how-to / what-is question answerable from docs; "
            "diagnostic = 'why did THIS happen to my data / account' — needs "
            "the customer's own logs or records to answer with proof; "
            "action = a request to DO something (cancel, onboard, change plan, "
            "export) that a person must carry out; "
            "status = 'is this broken right now / known issue'. "
            "urgency (one of low|normal|high|critical), "
            "summary (<=200 chars)."
        ),
        user=body or "(empty case)",
        model=_model,
        json_object=True,
        max_tokens=320,
        tenant_id=state.get("tenant_id"),
        cache=True,   # same case text -> same triage; kills retry/re-run cost
    )
    parsed = _safe_json(raw)
    topic = parsed.get("topic", "unknown")
    # Case.Type — the field queue owners scan by and the key `notify` routes on
    # (Phase 20n). Trust the classifier's `type` when it maps to a real picklist
    # value; else derive deterministically from the topic/body (stub-safe).
    _tid, _sb = state.get("tenant_id"), config.get("_sb")
    case_type = (salesforce.normalize_case_type(parsed.get("type"), tenant_id=_tid, sb=_sb)
                 or salesforce.map_case_type(topic, body, tenant_id=_tid, sb=_sb))
    answer_mode = _norm_answer_mode(parsed.get("answer_mode"), topic, body)
    classification = {
        "topic": topic,
        "case_type": case_type,
        "answer_mode": answer_mode,
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
            f"tier={tier} region={region} urgency={classification['urgency']} "
            f"topic={topic} type={case_type or '-'} mode={answer_mode}",
            {**classification, "tokens": llm.last_usage, "model": _model},
        ),
    }


_ANSWER_MODES = ("informational", "diagnostic", "action", "status")
_ACTION_KW = ("cancel", "close my account", "close our account", "downgrade",
              "upgrade my plan", "change our plan", "onboard", "offboard",
              "delete my account", "delete our account", "export all",
              "gdpr", "right to be forgotten", "terminate our contract")
_DIAGNOSTIC_KW = ("why did", "why is my", "why are my", "did my", "did our",
                  "is my data", "where is my", "where are my", "prove",
                  "what happened to my", "my zap didn't run", "my zap did not run",
                  "not showing my", "missing from my")
_STATUS_KW = ("is this a known issue", "known issue", "is it down", "is the api down",
              "any outage", "status page", "incident")


def _norm_answer_mode(value: str | None, topic: str, body: str) -> str:
    v = (value or "").strip().lower()
    if v in _ANSWER_MODES:
        return v
    hay = f"{topic} {body}".lower()
    if any(k in hay for k in _ACTION_KW):
        return "action"
    if any(k in hay for k in _STATUS_KW):
        return "status"
    if any(k in hay for k in _DIAGNOSTIC_KW):
        return "diagnostic"
    return "informational"


@register("trigger")
def h_trigger(state: CaseState, config: dict) -> dict:
    """P5b — the entry node for a non-Case flow. Shapes the generic
    `state.context` payload a trigger/webhook adapter produced:

    config: {
      map:      {"in_key": "out_key", ...}   # rename incoming fields
      required: ["email", "plan"]            # fields that must be present
      defaults: {"plan": "free"}             # fill when absent
    }

    Passes the (mapped/defaulted) context through; on a missing required field
    it records `context._missing` + a trace note and lets an edge branch on it
    (`input._missing`) rather than raising — the transport already 400s a
    malformed body before enqueue.
    """
    nid = config["_node_id"]
    ctx = dict(state.get("context") or {})
    for k, v in (config.get("defaults") or {}).items():
        ctx.setdefault(k, v)
    for src, dst in (config.get("map") or {}).items():
        if src in ctx:
            ctx[dst] = ctx.pop(src)
    missing = [f for f in (config.get("required") or []) if not ctx.get(f)]
    if missing:
        ctx["_missing"] = missing
    trig = ctx.get("_trigger", "?")
    return {"context": ctx,
            **_trace(nid, "trigger",
                     f"trigger={trig}, fields={sorted(k for k in ctx if not k.startswith('_'))}"
                     + (f", MISSING {missing}" if missing else ""),
                     {"trigger": trig, "missing": missing})}


@register("team_route")
def h_team_route(state: CaseState, config: dict) -> dict:
    """Phase 20i — pick the team that owns this case (the design doc's
    "One team, one flow" routing). Keyword rules over the case
    subject/body/topic; first match wins, else `default`. Writes
    `state.routed_team` ('support' | 'csm' | 'sales' | 'offboarding'); the
    `ask_human` / `handover` nodes resolve their target queue from it, and
    edge conditions can branch on `routed_team`. Pure — no LLM.

    config: {
      rules: [{"team": "csm", "any": ["renewal", "account manage", ...]}, ...],
      default: "support",
    }
    """
    nid = config["_node_id"]
    case = state.get("case", {})
    topic = str((state.get("classification") or {}).get("topic", ""))
    hay = " ".join(str(x) for x in (
        topic, case.get("subject", ""), case.get("body", ""))).lower()

    rules = config.get("rules") or _DEFAULT_ROUTE_RULES
    matched = None
    hit = ""
    for r in rules:
        kws = [k.lower() for k in (r.get("any") or []) if k]
        m = next((k for k in kws if k in hay), None)
        if m:
            matched, hit = r.get("team"), m
            break
    team = matched or config.get("default", "support")
    summary = (f"{team}  (matched '{hit}')" if matched
               else f"{team}  (default)")
    return {"routed_team": team,
            **_trace(nid, "team_route", summary, {"routed_team": team, "matched": hit})}


# Order matters — first match wins. Offboarding (leaving) is unambiguous;
# CSM (existing-customer lifecycle: renew / expand / adopt) is checked
# before Sales (net-new buying intent) so an expansion question from a
# current customer doesn't fall to Sales.
_DEFAULT_ROUTE_RULES = [
    {"team": "offboarding", "any": ["cancel", "close my account", "close account",
        "data export", "export my data", "delete my data", "delete my account",
        "offboard", "gdpr", "right to be forgotten", "terminate our", "downgrade to free"]},
    {"team": "csm", "any": ["renewal", "renew", "renewing", "account management",
        "account manager", "our contract", "contract renewal", "add seats",
        "more seats", "expand our", "expansion", "quarterly review", "qbr",
        "success plan", "adoption", "onboarding help", "true-up"]},
    {"team": "sales", "any": ["pricing", "how much does", "quote", "pre-sales",
        "presales", "plan comparison", "which plan", "compare plans", "discount",
        "upgrade to enterprise", "new subscription", "purchasing", "procurement",
        "evaluate", "trial extension"]},
]


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
    # classifier slug + Account country -> the restricted Case picklists
    # (Module__c / SubModule__c / Region__c) + Topic__c (raw slug). Phase 20g.
    derived = salesforce.map_case_fields(
        classification.get("topic"),
        state.get("region") or _dig(case, "account.region"),
        tenant_id=state.get("tenant_id"), sb=config.get("_sb"),
    )
    # Phase 20n — Case.Type: the classifier's mapped value, else derived from
    # the topic. Written on every pass so it is set at first triage and kept
    # current on each customer-reply re-run while the Case sits in the queue.
    case_type = (classification.get("case_type")
                 or salesforce.map_case_type(classification.get("topic"),
                                              tenant_id=state.get("tenant_id"), sb=config.get("_sb")))
    ctx: dict[str, Any] = {
        "classification": classification,
        "tier": state.get("tier"),
        "region": state.get("region"),
        "urgency": classification.get("urgency"),
        "topic": classification.get("topic"),
        "summary": classification.get("summary"),
        "case_type": case_type,
        "case_topic": derived.get("Topic__c"),
        "case_module": derived.get("Module__c"),
        "case_submodule": derived.get("SubModule__c"),
        "case_region": derived.get("Region__c"),
    }

    field_map = config.get("field_map") or {
        "urgency": "Priority", "case_type": "Type", "case_topic": "Topic__c",
        "case_module": "Module__c", "case_submodule": "SubModule__c",
        "case_region": "Region__c",
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

    # Phase 27c — case-control-plane fields, folded into the same write:
    # Status -> Triaged (unless a human already advanced it), the routed team,
    # and the run linkage. `Next_Action_Due__c` is set by the terminal node.
    prior_status = str(case.get("status") or "").strip().lower()
    if config.get("advance_status", True) and prior_status not in _ADVANCED_STATUS:
        fields.setdefault("Status", "Triaged")
    if state.get("routed_team"):
        fields.setdefault("Routed_Team__c", state["routed_team"])
    fields.update(_cp_fields(stamp_run_at=True, last_run_id=state.get("_run_id")))

    if not sf_id:
        info = {
            "target": None, "written": {}, "skipped": {}, "dry_run": True,
            "planned": fields, "status": "no sf_id on case",
        }
        return {
            "sf_writeback": info,
            **_trace(config["_node_id"], "sf_writeback", "no sf_id — nothing written", info),
        }

    result = connectors.invoke(
        state.get("tenant_id"), _case_conn(state, config), "update_fields",
        {"case_id": sf_id, "fields": fields, "append": append}, org_label=config.get("org"),
    )
    result["target"] = sf_id
    if result["dry_run"]:
        summary = f"Case {sf_id} [dry-run] would write {list(result.get('planned') or {})}"
    else:
        summary = f"Case {sf_id} [live] wrote {list(result['written'])}"
        if result["skipped"]:
            summary += f", skipped {list(result['skipped'])}"
    try:
        from interpreter import case_events

        case_events.record(
            config.get("_sb"), tenant_id=state.get("tenant_id"),
            case_sf_id=str(sf_id),
            case_number=case.get("case_number") or ctx.get("case_number"),
            actor="ai", action="route",
            to_status=fields.get("Status"),
            routed_team=fields.get("Routed_Team__c"),
            run_id=state.get("_run_id"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("sf_writeback case_events(%s): %s", sf_id, e)
    return {
        "sf_writeback": result,
        **_trace(config["_node_id"], "sf_writeback", summary, result),
    }


@register("sf_case")
def h_sf_case(state: CaseState, config: dict) -> dict:
    """Phase 20e/f — resolve an inbound message (email / chat) to a real
    Salesforce Case, so every downstream SF node (`sf_writeback`,
    `ask_human`, `handover`) has an `sf_id` to act on. Attaches to an open
    Case only when the email is a genuine thread reply (`reuse="thread"`);
    otherwise creates the Contact (and, for a business domain, the Account)
    and a new Case. For an email-channel case it also records the inbound
    mail as an `EmailMessage` on the Case (FR-7). Pass-through — merges
    `sf_id` and the Account tier / region back into `state.case`. Dry-run
    (nothing created) with no Salesforce creds.

    config: {origin="Email", status="New", create_contact=true,
             create_account=true, reuse="thread"|"never"}
    """
    nid = config["_node_id"]
    case = dict(state.get("case") or {})
    info = connectors.invoke(
        state.get("tenant_id"), _case_conn(state, config), "ensure_case",
        {"case": case, "sender": state.get("sender") or {},
         "origin": config.get("origin", "Email"), "status": config.get("status", "New"),
         "create_contact": bool(config.get("create_contact", True)),
         "create_account": bool(config.get("create_account", True)),
         "reuse": str(config.get("reuse", "thread"))},
        org_label=config.get("org"),
    )

    if info.get("sf_id"):
        case["sf_id"] = info["sf_id"]
        if info.get("case_number"):
            case["case_number"] = info["case_number"]
        if info.get("status"):
            case["status"] = info["status"]          # Phase 27c — don't downgrade
        if info.get("owner_id"):
            case["owner_id"] = info["owner_id"]
        # FR-7: the customer's email itself, on the Case (not just Description)
        if case.get("channel") == "email" and not info.get("dry_run"):
            info["inbound_email"] = connectors.invoke(
                state.get("tenant_id"), _case_conn(state, config), "log_email_message",
                {"case_id": info["sf_id"], "incoming": True,
                 "from_addr": case.get("from", ""), "from_name": case.get("from_name", ""),
                 "to_addrs": case.get("to") or case.get("supplied_email") or "",
                 "subject": case.get("subject", ""), "body": case.get("body", ""),
                 "message_id": case.get("message_id", "")},
                org_label=config.get("org"),
            )
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


@register("case_lookup")
def h_case_lookup(state: CaseState, config: dict) -> dict:
    """Phase 21 — pull the closest past *resolved* Cases so `draft` can answer
    from real resolutions, not just KB docs. Best-effort: no `case_memory`
    rows / no embedder -> a no-op, `draft` behaves exactly as before.

    Respects `classification.answer_mode`:
      * action     -> skipped entirely (a person carries it out).
      * diagnostic -> `prior_resolutions` is forced empty; matches become
                      `investigation_hints` only (memory must never state a
                      customer-specific fact it cannot prove).
      * informational / status -> full result.

    config: {k=3, pool=10, min_similarity=0.35, min_memories=3,
             use_graph=true, skip_modes=["action"]}
    """
    nid = config["_node_id"]
    from interpreter import case_memory

    case = state.get("case", {})
    cls = state.get("classification") or {}
    mode = cls.get("answer_mode", "informational")
    tenant_id = state.get("tenant_id")
    skip_modes = config.get("skip_modes", ["action"])

    def _out(citable, hints, note):
        return {
            "prior_resolutions": citable,
            "investigation_hints": hints,
            **_trace(nid, "case_lookup", note,
                     {"citable": len(citable), "hints": len(hints), "mode": mode}),
        }

    if mode in skip_modes:
        return _out([], [], f"skipped (answer_mode={mode})")
    if not tenant_id:
        return _out([], [], "skipped (no tenant)")

    try:
        from ingestion.scraper import get_supabase

        sb = config.get("_sb") or get_supabase()
    except Exception as e:  # noqa: BLE001
        return _out([], [], f"skipped (no db: {e})")

    min_mem = int(config.get("min_memories", 3))
    if case_memory.count_for_tenant(sb, tenant_id) < min_mem:
        return _out([], [], f"skipped (<{min_mem} memories for tenant)")

    query = f"{case.get('subject', '')}\n{case.get('body', '')}\n{cls.get('summary', '')}".strip()
    module = ((state.get("sf_writeback") or {}).get("written") or {}).get("Module__c") \
        or salesforce.map_case_fields(cls.get("topic"), None, tenant_id=tenant_id, sb=sb).get("Module__c")
    res = case_memory.lookup(
        sb, query, tenant_id=str(tenant_id),
        case_type=cls.get("case_type"), module=module, tier=state.get("tier"),
        k=int(config.get("k", 3)), pool=int(config.get("pool", 10)),
        min_similarity=float(config.get("min_similarity", 0.35)),
        use_graph=bool(config.get("use_graph", True)),
    )
    citable = [] if mode == "diagnostic" else res["citable"]
    hints = list(res["hints"])
    if mode == "diagnostic":
        # the near-matches are leads for the human / an evidence step, not copy
        hints = [f"{c['subject']}: {summarize_hint(c['resolution_text'])}"
                 for c in res["citable"]] + hints
    note = (f"{len(citable)} citable + {len(hints)} hint(s) from {res['scanned']} scanned"
            if (citable or hints) else f"no match in {res['scanned']} scanned")
    return _out(citable, hints[:6], note)


def summarize_hint(text: str, limit: int = 160) -> str:
    from interpreter import case_memory

    return case_memory.summarize(text, limit=limit)


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
        tenant_id=state.get("tenant_id"),
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
    # P1b (FR-41) — a `provisional` chunk is an unverified review-writeback
    # correction. Keep it available but never let it displace a confirmed
    # passage: confirmed context leads, provisional trails in a labelled block.
    confirmed = [r for r in retrieval if (r.get("entry_status") or "active") != "provisional"]
    provisional = [r for r in retrieval if (r.get("entry_status") or "active") == "provisional"]
    context = _context_block(confirmed) or "(no retrieved context)"
    if provisional:
        context += ("\n\n# UNVERIFIED corrections (pending review — use only if the "
                    "confirmed context above does not answer, and say the reply is "
                    "provisional)\n" + _context_block(provisional, max_chunks=2))
    _subj, _cbody = _run_text(state)          # P5c — Case fields or `context.*`
    body = _with_ocr(state, f"Subject: {_subj}\n\n{_cbody}".strip())

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

    # Phase 21: resolutions that actually closed near-identical past Cases.
    prior = state.get("prior_resolutions") or []
    mode = (state.get("classification") or {}).get("answer_mode", "informational")
    if prior:
        blocks = []
        for p in prior[:3]:
            tag = " (CONFIRMED DUPLICATE — lead with this)" if p.get("duplicate") else ""
            blocks.append(f"[relevance {p.get('relevance', 0):.2f}] Case "
                          f"{p.get('case_number') or '?'} \"{p.get('subject') or ''}\"{tag}\n"
                          f"resolution ({p.get('kind')}): {p.get('resolution_text', '')}")
        user += "\n\n# Prior resolved cases (replies that actually resolved a near-identical issue)\n" \
                + "\n\n".join(blocks)

    grounding_rule = (
        "Ground the reply in the KNOWLEDGE BASE and, when they closely match, the "
        "PRIOR RESOLVED CASES — prefer their wording and steps; if one is a "
        "CONFIRMED DUPLICATE, lead with it. "
        if prior else
        "Using ONLY the provided context, "
    )
    if mode == "diagnostic":
        grounding_rule += (
            "This is a DIAGNOSTIC request about the customer's own data. Do NOT "
            "assert what happened to their specific account/records unless the "
            "context proves it. If it isn't proven, say what you will check and "
            "that a specialist will follow up with findings — never guess. "
        )

    _model = config.get("model", llm.DEFAULT_MODEL)
    raw = llm.complete(
        system=(
            "You are a support agent. " + grounding_rule +
            "When an internal runbook is provided it is authoritative — prefer it "
            "over the public documentation. Invent nothing not in the context. "
            "Return a JSON object: {\"reply\": string, \"confidence\": number 0..1} "
            "where confidence reflects how well the context actually answers the case."
        ),
        user=user,
        model=_model,
        json_object=True,
        max_tokens=int(config.get("max_tokens", 500)),
        tenant_id=state.get("tenant_id"),
    )
    tokens = llm.last_usage
    parsed = _safe_json(raw)
    reply = parsed.get("reply") or parsed.get("draft") or raw
    try:
        model_conf = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        model_conf = 0.5
    model_conf = max(0.0, min(1.0, model_conf))

    prior_as_src = [{"chunk_text": p.get("resolution_text", ""),
                     "doc_url": f"case:{p.get('case_number')}"} for p in prior[:3]]
    # P1b (FR-41) — grounding is measured against CONFIRMED context only, so a
    # reply that only parrots an unverified correction does not score as
    # grounded (which could otherwise push the gate to auto-send).
    grounded = groundedness.check(reply, (internal_matches[:5] + prior_as_src + confirmed[:5]),
                                  tenant_id=state.get("tenant_id"))

    # KIL-b — does the reply, or the customer's own claim, CONTRADICT the KB or
    # a past resolution? A contradicting draft forces the gate to escalate.
    icontexts = integrity.contexts_from_state(
        {"prior_resolutions": prior, "internal_kb": internal, "retrieval": retrieval})
    integ = {
        "draft": integrity.check(reply, icontexts, kind="draft", tenant_id=state.get("tenant_id")),
        "inbound": integrity.check(case.get("body") or "", icontexts, kind="inbound",
                                   tenant_id=state.get("tenant_id")),
    }
    idraft = integ["draft"]

    return {
        "draft": reply,
        "draft_confidence": round(model_conf, 4),
        "groundedness": grounded,
        "integrity": integ,
        **_trace(
            config["_node_id"], "draft",
            f"{len(reply)} chars, model_confidence={model_conf:.2f}, "
            f"groundedness={grounded['score']:.2f} ({grounded['backend']}), "
            f"integrity={idraft['relation']}"
            f"{' FLAGGED' if idraft['flagged'] else ''} ({idraft['backend']}), "
            f"sources={len(retrieval[:5])}+{len(internal_matches[:5])} internal"
            f"+{len(prior_as_src)} prior-case",
            {"draft_confidence": model_conf, "chars": len(reply),
             "groundedness": grounded, "integrity": integ, "tokens": tokens,
             "model": _model,
             "used_internal_kb": bool(internal_matches),
             "prior_cases": [p.get("case_number") for p in prior[:3]],
             "answer_mode": mode},
        ),
    }


# --------------------------------------------------------------------------
# Phase 29 step 2 — a bounded ReAct loop over retrieve+draft, gated on the
# case identified as the highest-ROI target: retrieval that missed on the
# first try. See docs/PROJECT_SCOPE.md's Phase 29 entry for the analysis
# (qrels_hard.jsonl already exists to score multi-hop retrieval against and
# was never wired to anything).
# --------------------------------------------------------------------------
_AGENT_TOOLS = [
    {
        "name": "search_kb",
        "description": ("Search the knowledge base again with a different, more "
                         "specific query, to find documentation that better "
                         "supports the reply."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "the new search query"}},
            "required": ["query"],
        },
    },
    {
        "name": "give_up",
        "description": "Stop — a different query is unlikely to find better-supporting docs.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def _agent_reformulate(question: str, tried: list[str], top_titles: list[str],
                        unsupported: list[str], model: str,
                        tenant_id: str | None = None) -> str | None:
    """One ReAct decision: propose a better search query, or give up. Returns
    the new query, or None to stop looping. The stub path (no API key) never
    proposes a tool call — deterministic, matching every other handler's
    offline behavior — so the loop always exits after one attempt in tests.
    Best-effort: this is the OPTIONAL "try harder" step, on top of an
    already-usable first-pass draft the caller has in hand — unlike
    `complete()`, `complete_with_tools` has no cross-provider fallback and
    raises visibly on a transient error (rate limit, timeout), so any
    exception here must not crash the node; it just means give up and keep
    the best attempt seen so far (same as a live-verified real crash found
    via a Groq 429 during this node's own development)."""
    user = (
        f"Customer's question:\n{question}\n\n"
        f"Search quer{'y' if len(tried) == 1 else 'ies'} already tried: {tried}\n\n"
        f"Top docs found: {top_titles or '(none)'}\n\n"
        f"Reply claims NOT supported by any doc found: {unsupported[:5] or '(none)'}\n\n"
        "Call search_kb with a genuinely different query likely to surface "
        "better-supporting documentation, or give_up if nothing else is "
        "likely to help."
    )
    try:
        result = llm.complete_with_tools(
            messages=[{"role": "user", "content": user}],
            system=("You refine a support-case knowledge-base search that didn't "
                     "find well-supported documentation."),
            tools=_AGENT_TOOLS,
            model=model,
            max_tokens=200,
            tenant_id=tenant_id,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("agent: reformulation call failed (%s) — keeping best attempt so far", e)
        return None
    for tc in result.tool_calls:
        if tc.name == "search_kb":
            q = str(tc.arguments.get("query") or "").strip()
            if q and q not in tried:
                return q
    return None


@register("agent")
def h_agent(state: CaseState, config: dict) -> dict:
    """A bounded ReAct loop wrapping retrieve+draft: reformulate the search
    query and retry when the draft's own groundedness score says the first
    pass wasn't well supported, up to `max_iterations`.

    Selective by design, not "always agentic": iteration 1 is exactly
    h_retrieve + h_draft, same calls, same tokens as wiring those two nodes
    separately. It only spends more when that first pass's groundedness is
    below `groundedness_threshold` — i.e. only on cases a downstream
    confidence_gate would otherwise escalate anyway. Drop-in for a
    retrieve+draft pair: every field a confidence_gate reads
    (retrieval_score / draft_confidence / groundedness) is still produced,
    so nothing downstream needs to change.

    config: retrieve (the retrieve node's own config, e.g. top_k/kb_sources),
    draft (the draft node's own config), max_iterations (default 3),
    groundedness_threshold (default 0.6 — a proxy for "would clear the
    gate", not the gate's own blended score), model (for the reformulation
    decision only, default llm.FAST_MODEL).
    """
    max_iter = max(1, int(config.get("max_iterations", 3)))
    threshold = float(config.get("groundedness_threshold", 0.6))
    retrieve_cfg = dict(config.get("retrieve") or {})
    draft_cfg = dict(config.get("draft") or {})
    _model = config.get("model", llm.FAST_MODEL)

    _subj, _body = _run_text(state)
    question = " ".join(p for p in (_subj, _body) if p).strip() or _subj or ""

    working = dict(state)
    tried: list[str] = []
    query_override: str | None = None
    best: dict[str, Any] | None = None
    best_score = -1.0
    attempts: list[dict[str, Any]] = []
    tokens_total = 0

    for i in range(max_iter):
        r_cfg = {**retrieve_cfg, "_node_id": config["_node_id"]}
        if query_override:
            r_cfg["query_override"] = query_override
        r_out = h_retrieve(working, r_cfg)
        working = {**working, **{k: v for k, v in r_out.items() if k != "trace"}}
        tried.append(r_out.get("query") or question)

        d_cfg = {**draft_cfg, "_node_id": config["_node_id"]}
        d_out = h_draft(working, d_cfg)
        working = {**working, **{k: v for k, v in d_out.items() if k != "trace"}}
        d_tok = ((d_out.get("trace") or [{}])[0].get("data") or {}).get("tokens") or {}
        tokens_total += int(d_tok.get("total") or 0)

        score = float((d_out.get("groundedness") or {}).get("score", 0.0))
        attempts.append({"iteration": i, "query": tried[-1],
                          "retrieval_score": r_out.get("retrieval_score"), "groundedness": score})
        if score > best_score:
            best_score, best = score, dict(working)

        if score >= threshold or i == max_iter - 1:
            break

        top_titles = [c.get("doc_url") for c in (r_out.get("retrieval") or [])[:3] if c.get("doc_url")]
        unsupported = (d_out.get("groundedness") or {}).get("unsupported") or []
        query_override = _agent_reformulate(question, tried, top_titles, unsupported, _model,
                                            tenant_id=state.get("tenant_id"))
        tokens_total += int((llm.last_usage or {}).get("total") or 0)
        if not query_override:
            break

    best = best if best is not None else working
    result_keys = ("retrieval", "retrieval_score", "query", "draft", "draft_confidence",
                   "groundedness", "integrity")
    out = {k: best[k] for k in result_keys if k in best}
    return {
        **out,
        "agent_iterations": len(attempts),
        "agent_attempts": attempts,
        **_trace(
            config["_node_id"], "agent",
            f"{len(attempts)} attempt(s), best groundedness={best_score:.2f} "
            f"(threshold {threshold:.2f})",
            {"attempts": attempts, "threshold": threshold,
             "tokens": {"total": tokens_total} if tokens_total else None,
             "model": draft_cfg.get("model", llm.DEFAULT_MODEL)},
        ),
    }


def _slug_tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t}


def _re_subject(subject: str) -> str:
    s = (subject or "").strip() or "your request"
    return s if s[:3].lower() == "re:" else f"Re: {s}"


_BILLING_REASON = ("billing", "refund", "charge", "invoice", "& plans")


def _route_queue(state: CaseState, config: dict) -> str | None:
    """Pick the human queue for an escalation/handover (Phase 20i).

    * a case the router sent to a specific team (csm / sales / offboarding)
      goes to that team's queue — they own it, billing-adjacent or not.
    * otherwise (routed to support, or unrouted): a **billing-reason** forced
      escalation goes to `escalate_queue` (the billing sub-queue); else the
      routed team's / the static `queue`.
    """
    team = state.get("routed_team") or ""
    by_team = config.get("queue_by_team") or {}
    if team and team != "support" and by_team.get(team):
        return by_team[team]
    reason = str((state.get("confidence_gate") or {}).get("forced_escalation") or "").lower()
    if config.get("escalate_queue") and any(w in reason for w in _BILLING_REASON):
        return config["escalate_queue"]
    return by_team.get(team) or config.get("queue")


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
        (f"topic '{topic}' ~ '{e}'" for e in escalate
         if _slug_tokens(e) and _slug_tokens(e) <= ttok), None
    )
    # Module-family escalation (Phase 20h): billing/plans questions are never a
    # docs answer whatever slug the classifier picked. `escalate_modules`
    # defaults to Billing & Plans; a flow can set `[]` to opt out.
    if not forced:
        esc_mods = config.get("escalate_modules", ["Billing & Plans"])
        mod = salesforce.map_case_fields(topic, None, tenant_id=state.get("tenant_id"),
                                          sb=config.get("_sb")).get("Module__c")
        if mod and mod in esc_mods:
            forced = f"module '{mod}'"
    # Case.Type escalation (Phase 20n): a whole class of request (Billing,
    # Account / Login, …) that is never a docs answer, keyed on the field a
    # human filters by. Opt-in via `escalate_types`.
    if not forced:
        esc_types = config.get("escalate_types", [])
        ctype = ((state.get("classification") or {}).get("case_type")
                 or salesforce.map_case_type(topic, tenant_id=state.get("tenant_id"),
                                              sb=config.get("_sb")))
        if ctype and ctype in esc_types:
            forced = f"type '{ctype}'"
    # answer_mode escalation (Phase 21, opt-in): an `action` request (cancel,
    # onboard, export, plan change) is carried out by a person, never
    # auto-answered. Off by default; a flow sets `escalate_answer_modes`.
    if not forced:
        esc_modes = config.get("escalate_answer_modes", [])
        amode = (state.get("classification") or {}).get("answer_mode")
        if amode and amode in esc_modes:
            forced = f"answer_mode '{amode}'"
    # KIL-b — the draft contradicts the KB or a past resolution. Never
    # auto-send a reply that disagrees with established knowledge; a human
    # decides. On by default; a flow sets `escalate_on_integrity_conflict: false`.
    if not forced and config.get("escalate_on_integrity_conflict", True):
        idraft = (state.get("integrity") or {}).get("draft") or {}
        if idraft.get("flagged"):
            forced = "integrity conflict (draft contradicts KB/history)"
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
        gate["forced_escalation"] = forced
    reason = (f"forced escalate ({gate['forced_escalation']})" if forced
              else f"score={score:.3f} vs threshold={threshold:.2f} ({tier})")
    # Phase 27c — surface the gate score on the Case (no status change here).
    _cp_write(state, config, action="gate",
              fields=_cp_fields(confidence=score),
              reason=reason, confidence=score)
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


def _looks_like_sf_id(v: Any) -> bool:
    """A 15- or 18-char Salesforce record id (safe to pass to a Chatter
    @mention). A configured target that is a plain Name is not."""
    return isinstance(v, str) and len(v) in (15, 18) and v.isalnum()


@register("notify")
def h_notify(state: CaseState, config: dict) -> dict:
    """Ping an internal party on the Case **without changing ownership**
    (Phase 20n). The Case stays in whatever queue it is already in; the
    routed rep gets a Chatter note (an @mention when the target is a real
    User/Group id) plus the suggested reply as a private CaseComment.

    Target is resolved from `Case.Type` first (the field queue owners scan
    by), then `Module__c`. The node's own `target_by_type` / `target_by_module`
    win when they have an entry; otherwise the per-tenant `notify_targets`
    table (`interpreter.routing.resolve_notify_target` — live SF lookup for
    `sf_team_role` / `sf_queue` rows); otherwise `fallback_target`. Terminal —
    Phase 20m's resume poller picks up the rep's CaseComment and continues.

    config: {
      channel: "salesforce_chatter",
      target_by_type:   {"Billing": "<User/Group id or name>", ...},   # optional override
      target_by_module: {"API & Webhooks": "...", ...},                # optional override
      fallback_target:  null,
      use_table:        true,   # consult notify_targets when no override matches (default true)
      note_tmpl?: "... {label} ... {confidence} ... {draft} ...",
    }
    """
    nid = config["_node_id"]
    case = state.get("case", {})
    sf_id = case.get("sf_id") or case.get("id")
    cls = state.get("classification") or {}
    confidence = state.get("confidence")
    draft = state.get("draft", "")

    _tid, _sb = state.get("tenant_id"), config.get("_sb")
    case_type = cls.get("case_type") or salesforce.map_case_type(cls.get("topic"), tenant_id=_tid, sb=_sb)
    written = (state.get("sf_writeback") or {}).get("written") or {}
    module = written.get("Module__c") or salesforce.map_case_fields(
        cls.get("topic"), None, tenant_id=_tid, sb=_sb).get("Module__c")

    by_type = config.get("target_by_type") or {}
    by_module = config.get("target_by_module") or {}
    label = case_type or module or "support"
    target = by_type.get(case_type) or by_module.get(module)
    target_type = None
    resolved_via = "node_config" if target else None

    # no per-flow override matched → the central tenant routing table
    if not target and config.get("use_table", True):
        from interpreter.routing import resolve_notify_target

        row = resolve_notify_target(
            state.get("tenant_id"), case_type, module, sb=config.get("_sb"),
            org_label=config.get("org"),
        )
        if row:
            target, target_type = row.get("id"), row.get("type")
            label = row.get("label") or label
            resolved_via = f"table:{row.get('resolver')}"

    if not target:
        target = config.get("fallback_target")
        resolved_via = resolved_via or ("fallback" if target else "none")

    outcome = {
        "action": "notify",
        "channel": config.get("channel", "salesforce_chatter"),
        "target": target,
        "target_type": target_type,
        "resolved_via": resolved_via,
        "label": label,
        "case_type": case_type,
        "module": module,
        "draft": draft,
        "confidence": confidence,
        "reassigned": False,
    }

    tmpl = config.get("note_tmpl") or (
        "{label} case — support bot needs input from the {label} team "
        "(confidence {confidence}). The Case stays in this queue; suggested "
        "draft below, please review before it goes to the customer.\n\n{draft}"
    )
    body = tmpl.format(label=label, confidence=confidence, draft=draft or "(no draft yet)")
    # `draft_inline: true` -> one feed row (draft in the Chatter note); default
    # keeps the draft as a separate private CaseComment the agent can copy.
    inline = bool(config.get("draft_inline"))

    if sf_id and outcome["channel"] == "salesforce_chatter":
        # @mention a real person. A User/Group id is mentionable directly; a
        # Queue isn't, so mention one of its members; else the fallback id.
        if _looks_like_sf_id(target) and target_type in (None, "user", "group"):
            mention = target
        elif target_type == "queue" and target:
            from interpreter import routing
            mention = routing.queue_member(
                target, state.get("tenant_id"), config.get("org")
            )[0] or config.get("mention_id")
        else:
            mention = config.get("mention_id")
        chatter = connectors.invoke(
            state.get("tenant_id"), _case_conn(state, config), "post_note",
            {"case_id": sf_id, "body": body, "mention_id": mention}, org_label=config.get("org"),
        )
        outcome["chatter"] = chatter
        mode = "dry-run" if chatter.get("dry_run") else "posted"
        summary = f"Chatter {mode} → {label} [{resolved_via}] (no reassign)"
        if draft.strip() and not inline:
            note = connectors.invoke(
                state.get("tenant_id"), _case_conn(state, config), "add_comment",
                {"case_id": sf_id, "body": f"[bot draft — {label}; review before sending]\n\n{draft}"},
                org_label=config.get("org"),
            )
            outcome["draft_comment"] = note
            if note.get("created"):
                summary += " + draft comment"

        # Optional: also set flag fields on the Case (e.g. Bot_Attention__c)
        # so a record-triggered SF Flow can fire an Email Alert / Slack in
        # addition to the @mention. Unknown fields are skipped.
        af = config.get("attention_fields")
        if af:
            rendered = {k: (v.format(label=label, case_type=case_type or "",
                                     module=module or "") if isinstance(v, str) else v)
                        for k, v in af.items()}
            outcome["attention"] = connectors.invoke(
                state.get("tenant_id"), _case_conn(state, config), "update_fields",
                {"case_id": sf_id, "fields": rendered}, org_label=config.get("org"))
    else:
        summary = f"notify {label!r} (no sf_id — not posted)"

    # Phase 27c — someone's been pinged; the Case is being worked but stays
    # where it is (no reassign). Ack clock so the sweep notices if ignored.
    _cp_write(state, config, action="notify",
              fields=_cp_fields(status="In Progress",
                                next_action=f"{label} rep to respond in Chatter",
                                due_minutes=_ACK_MINUTES, confidence=confidence),
              reason=f"notify {label}", confidence=confidence)

    return {"outcome": outcome, **_trace(nid, "notify", summary, outcome)}


@register("notify_human")
def h_notify_human(state: CaseState, config: dict) -> dict:
    """Ping a named person about an escalated Case on **Slack and/or
    Salesforce Chatter** — the flow decides who and where, not hard-coded
    config. Place it after `ask_human` / `handover` (or on any escalation
    edge). Pass-through: it doesn't change `outcome`, so a terminal node's
    action is preserved.

    config: {
      channel: "both" | "slack" | "salesforce_chatter",
      slack_channel: "#support-escalations",
      slack_channel_by_team: {"csm": "#csm", "default": "#support"},
      slack_webhook: "https://hooks.slack.com/…",     # or SLACK_ALERT_WEBHOOK
      mention: {
        slack_user_id: "U123", slack_user_by_team: {...},
        sf_user_id: "005…", sf_team: "Support",       # -> a queue member
        mention_id: "005…"                            # final fallback
      },
      note_tmpl?: "... {cn} {outcome} {conf} {subject} {who} {draft} {link} ..."
    }
    """
    from interpreter import alert

    res = alert.alert_human(dict(state), config)
    legs = []
    for k in ("slack", "chatter"):
        r = res.get(k) or {}
        if r.get("sent") or r.get("posted"):
            legs.append(f"{k}:{r.get('via') or ('mention' if r.get('mention_id') else 'ok')}")
        elif r:
            legs.append(f"{k}:skip")
    summary = "alerted " + (", ".join(legs) or "nobody (no channel configured)")
    if res.get("mention"):
        summary += f"  ·  @slack={res['mention'].get('slack')} @sf={res['mention'].get('sf')}"

    # Phase 27c — stamp the reasoning-thread ts on the Case and record the
    # handoff. If we arrived here straight from the gate (support + PASS, no
    # ask_human) also move the Case to In Progress with an ack clock.
    sl = res.get("slack") or {}
    prior = str(state.get("case", {}).get("status") or "").strip().lower()
    cp = _cp_fields(slack_ts=sl.get("ts"))
    if prior not in _ADVANCED_STATUS and prior != "escalated":
        cp.update(_cp_fields(status="In Progress",
                             next_action="agent to reason through the reply in Slack",
                             due_minutes=_ACK_MINUTES))
    _cp_write(state, config, action="notify_human", fields=cp,
              reason="slack reasoning handoff", slack_ts=sl.get("ts"),
              slack_channel=sl.get("channel"))

    return {"human_alert": res, **_trace(config["_node_id"], "notify_human", summary, res)}


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

    draft = state.get("draft", "")
    # `post_note: false` -> this node only routes the Case; a downstream
    # `notify_human` does the Chatter/Slack alert (avoids a double post).
    post_note = config.get("post_note", True)
    if post_note and channel == "salesforce_chatter" and sf_id:
        body = (
            f"Support bot needs a human on this case (confidence {confidence}). "
            f"Suggested draft below — please review before sending.\n\n{draft}"
        )
        chatter = connectors.invoke(
            state.get("tenant_id"), _case_conn(state, config), "post_note",
            {"case_id": sf_id, "body": body, "mention_id": config.get("mention_id")},
            org_label=config.get("org"),
        )
        outcome["chatter"] = chatter
        mode = "dry-run" if chatter.get("dry_run") else "posted"
        summary = f"Chatter {mode} on Case {sf_id}"

        if draft.strip():
            note = connectors.invoke(
                state.get("tenant_id"), _case_conn(state, config), "add_comment",
                {"case_id": sf_id, "body": f"[bot draft — review before sending]\n\n{draft}"},
                org_label=config.get("org"),
            )
            outcome["draft_comment"] = note
            if note.get("created"):
                summary += " + draft comment"
    else:
        summary = ("routed only (alert deferred to notify_human)" if not post_note
                   else f"handed to human via {channel}")
        if post_note and channel == "salesforce_chatter":
            summary += " (no sf_id — not posted)"

    # Phase 20g/h/i: drop the Case into a human queue. Billing/forced -> the
    # escalation queue; else the routed team's queue; else the static `queue`.
    forced = bool((state.get("confidence_gate") or {}).get("forced_escalation"))
    queue = _route_queue(state, config)
    if sf_id and queue:
        assignment = connectors.invoke(state.get("tenant_id"), _case_conn(state, config), "assign_owner",
                                       {"case_id": sf_id, "queue": queue}, org_label=config.get("org"))
        outcome["assignment"] = assignment
        if assignment.get("assigned"):
            summary += f" → {queue}"
        elif assignment.get("reason"):
            summary += f" (queue: {assignment['reason']})"

    # Phase 27c — Status = Escalated, the routed team (Omni routes on it), and
    # a 30-min ack clock. Escalation_Reason__c carries the gate's why.
    team = state.get("routed_team") or "support"
    esc_reason = ((state.get("confidence_gate") or {}).get("forced_escalation")
                  or outcome.get("reason"))
    _cp_write(state, config, action="ask_human",
              fields=_cp_fields(status="Escalated", routed_team=team,
                                next_action=f"pick up in {team} queue",
                                due_minutes=_ACK_MINUTES, escalation_reason=esc_reason,
                                confidence=confidence),
              reason=esc_reason, routed_team=team, confidence=confidence)

    return {"outcome": outcome, **_trace(config["_node_id"], "ask_human", summary, outcome)}


@register("handover")
def h_handover(state: CaseState, config: dict) -> dict:
    """Full human handover. When `config.queue` (or `config.owner_user_id`) is
    set and the case has an `sf_id`, the Case is reassigned to that queue /
    user so it lands in a human's list (FR-14); otherwise just the outcome."""
    outcome = {
        "action": "handover",
        "reason": config.get("reason", "policy"),
        "draft": state.get("draft", ""),
        "confidence": state.get("confidence"),
        "tier": state.get("tier"),
    }
    summary = f"full handover ({outcome['reason']})"
    case = state.get("case", {})
    sf_id = case.get("sf_id") or case.get("id")
    # enterprise always goes to the enterprise queue; otherwise the routed
    # team's queue (queue_by_team) / the static queue (Phase 20i).
    queue = config.get("queue")
    if config.get("enterprise_queue") and state.get("tier") == "enterprise":
        queue = config["enterprise_queue"]
    else:
        queue = _route_queue(state, config) or queue
    if sf_id and (queue or config.get("owner_user_id")):
        assignment = connectors.invoke(
            state.get("tenant_id"), _case_conn(state, config), "assign_owner",
            {"case_id": sf_id, "queue": queue, "user_id": config.get("owner_user_id")},
            org_label=config.get("org"),
        )
        outcome["assignment"] = assignment
        if assignment.get("assigned"):
            summary += f" → reassigned ({assignment.get('owner_type')})"
        elif assignment.get("reason"):
            summary += f" (not reassigned: {assignment['reason']})"

    # Phase 27c — Status = Escalated + routed team + ack clock (Omni routes
    # on Routed_Team__c; the queue assign above is the interim + fallback).
    team = state.get("routed_team") or "support"
    _cp_write(state, config, action="handover",
              fields=_cp_fields(status="Escalated", routed_team=team,
                                next_action=f"pick up in {team} queue",
                                due_minutes=_ACK_MINUTES,
                                escalation_reason=outcome["reason"],
                                confidence=state.get("confidence")),
              reason=outcome["reason"], routed_team=team,
              confidence=state.get("confidence"))
    return {"outcome": outcome, **_trace(config["_node_id"], "handover", summary, outcome)}


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

    sender = connectors.invoke(
        state.get("tenant_id"), _case_conn(state, config), "identify_sender",
        {"email": email, "free_domains": config.get("free_email_domains"),
         "domain_match": bool(config.get("domain_match", True)),
         "create_lead": bool(config.get("create_lead_if_missing", False))},
        org_label=config.get("org"),
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
    # key on the stable SF record id (`runs.case_payload->>sf_id`), NOT
    # `runs.case_id` — `get_case` maps the CaseNumber into that field, so a
    # synthetic-vs-real mismatch used to silently reset the counter (WF-3).
    case_key = sf_id or case.get("case_id")
    prior_rounds = 0
    if case_key:
        try:
            from ingestion.scraper import get_supabase

            sb = config.get("_sb") or get_supabase()
            rows = (sb.table("runs").select("clarify_round")
                    .eq("case_payload->>sf_id", str(case_key)).eq("outcome", "need_info")
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
        tenant_id=state.get("tenant_id"),
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
        delivery = connectors.invoke(
            state.get("tenant_id"), _case_conn(state, config), "send_case_reply",
            {"case_id": sf_id, "body": customer_msg, "to_email": recipient,
             "subject": config.get("subject", "We need a bit more information")},
            org_label=config.get("org"),
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
        # @mention a real person: a queue member (Chatter can't mention a
        # Queue), the routed team's lead, else the configured fallback id.
        mention = config.get("mention_id")
        try:
            from interpreter import routing
            qref = config.get("mention_queue") or (
                f"Team_{(state.get('routed_team') or '').capitalize()}"
                if config.get("mention_team") else None)
            if qref:
                uid, _ = routing.queue_member(qref, state.get("tenant_id"), config.get("org"))
                mention = uid or mention
        except Exception:  # noqa: BLE001
            pass
        delivery = connectors.invoke(
            state.get("tenant_id"), _case_conn(state, config), "post_note",
            {"case_id": sf_id, "body": note, "mention_id": mention}, org_label=config.get("org"),
        )

    auto_sent = bool(auto_send and delivery and delivery.get("sent"))
    posted_internal = bool(not auto_send and delivery and not delivery.get("dry_run"))

    # Phase 20n — round-cap reached and still unclear: hand the Case to the
    # general support queue (owner change), so it stops being the bot's and
    # a human owns it. The questions we could not get answered ride along in
    # the Chatter note above.
    handover_queue = config.get("handover_queue")
    handover_assignment = None
    if exhausted and sf_id and handover_queue:
        handover_assignment = connectors.invoke(
            state.get("tenant_id"), _case_conn(state, config), "assign_owner",
            {"case_id": sf_id, "queue": handover_queue}, org_label=config.get("org"),
        )

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
        "handover_queue": handover_queue if exhausted else None,
        "handover_assignment": handover_assignment,
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
    if handover_assignment:
        outcome["handover_queue"] = handover_queue
        outcome["handover_assignment"] = handover_assignment
    # Phase 27c — when the ball is with the customer, the Case says so and a
    # 3-business-day clock runs; when exhausted we've escalated, so it's a
    # 30-min ack clock against the routed team instead.
    if exhausted:
        _team = state.get("routed_team") or "support"
        _cp_write(state, config, action="ask_human",
                  fields=_cp_fields(status="Escalated", routed_team=_team,
                                    next_action="clarify exhausted — human to take over",
                                    due_minutes=_ACK_MINUTES,
                                    escalation_reason="clarify_exhausted",
                                    confidence=state.get("confidence")),
                  reason="clarify_exhausted", routed_team=_team)
    else:
        _cp_write(state, config, action="clarify",
                  fields=_cp_fields(status="Waiting on Customer",
                                    next_action=f"awaiting customer reply ({len(questions)} q)",
                                    due_minutes=int(config.get("customer_wait_hours", 72)) * 60),
                  reason="kb_insufficient")

    rnd = f"round {clarify_round}/{max_rounds}"
    if exhausted:
        handed = ""
        if handover_assignment:
            handed = (f" → {handover_queue}" if handover_assignment.get("assigned")
                      else f" (handover: {handover_assignment.get('reason') or handover_assignment.get('error') or 'queued'})")
        where = (f"{rnd} — exhausted, handing to a human{handed}"
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


# ==========================================================================
# Phase 25 — image attachments, Salesforce context, generic AI-prompt node
# ==========================================================================
def _flat(state: CaseState) -> dict:
    """A flat view of the bits a prompt template interpolates: `case.subject`,
    `sf_context.account.tier`, `attachment_text`, `classification.topic`, …"""
    return {
        "case": state.get("case", {}),
        "sf_context": state.get("sf_context", {}),
        "sender": state.get("sender", {}),
        "classification": state.get("classification", {}),
        "attachment_text": state.get("attachment_text", ""),
        "attachments": state.get("attachments", []),
        "ai": state.get("ai", {}),
        "retrieval": state.get("retrieval", {}),
        "draft": state.get("draft", ""),
        "tier": state.get("tier"),
        "region": state.get("region"),
        "routed_team": state.get("routed_team"),
    }


def _render(tmpl: str, flat: dict) -> str:
    """`{case.subject}` / `{sf_context.account.name}` -> value; unknown -> ''."""
    if not tmpl:
        return ""

    def sub(m: "re.Match") -> str:
        v = _dig(flat, m.group(1).strip())
        if v is None:
            return ""
        return v if isinstance(v, str) else __import__("json").dumps(v, default=str)[:4000]

    return re.sub(r"\{([a-zA-Z0-9_.]+)\}", sub, tmpl)


@register("attachments")
def h_attachments(state: CaseState, config: dict) -> dict:
    """Fetch image (and, opt-in, video) attachments on the Case + local
    OCR / transcription (Phase 25). Writes `state.attachments` +
    `state.attachment_text` (folded into classify/draft) +
    `state._attachment_blobs` (image bytes + video keyframes, for `ai_prompt`
    vision; not persisted). Pass-through, best-effort.

    config: {source="salesforce"|"email"|"auto", max_images=5, ocr=true,
             video=false, video_frames=4, video_max_seconds=300}
    """
    from interpreter import attachments as att

    nid = config["_node_id"]
    _sb = None
    if config.get("skip_signatures", True) is not False:
        try:
            from ingestion.scraper import get_supabase
            _sb = get_supabase()
        except Exception:  # noqa: BLE001
            _sb = None
    try:
        out = att.extract(state.get("case", {}), tenant_id=state.get("tenant_id"),
                          limit=int(config.get("max_images", att.MAX_IMAGES)),
                          do_ocr=config.get("ocr", True) is not False,
                          do_video=bool(config.get("video", False)),
                          video_frames_n=int(config.get("video_frames", att.VIDEO_FRAMES)),
                          video_max_seconds=int(config.get("video_max_seconds",
                                                           att.VIDEO_MAX_SECONDS)),
                          skip_signatures=config.get("skip_signatures", True) is not False,
                          sb=_sb,
                          source=config.get("source", "salesforce"),
                          org_label=config.get("org"))
    except Exception as e:  # noqa: BLE001
        log.warning("attachments node failed: %s", e)
        out = {"attachments": [], "attachment_text": "", "_blobs": {}}

    atts = out["attachments"]
    n_vid = sum(1 for a in atts if a.get("kind") == "video")
    n_sig = sum(1 for a in atts if a.get("skipped"))
    n_img = sum(1 for a in atts if a.get("kind", "image") == "image" and not a.get("skipped"))
    chars = len(out["attachment_text"])
    return {
        "attachments": atts,
        "attachment_text": out["attachment_text"],
        "_attachment_blobs": out["_blobs"],
        **_trace(nid, "attachments",
                 f"{n_img} image(s)" + (f" + {n_vid} video(s)" if n_vid else "")
                 + (f", {n_sig} signature(s) skipped" if n_sig else "")
                 + f", {chars} chars extracted",
                 {"images": n_img, "videos": n_vid, "signatures_skipped": n_sig,
                  "chars": chars, "files": [a["filename"] for a in atts]}),
    }


@register("sf_context")
def h_sf_context(state: CaseState, config: dict) -> dict:
    """Load the Salesforce picture around the Case — Account (+ parent =
    organization), Contact + siblings, Lead, Case history, Account team —
    into `state.sf_context` (Phase 25). Put it after `identify`. Pass-through.

    config: {want: ["account","contacts","leads","cases","team"]}
    """
    from interpreter import sf_context as sfc

    nid = config["_node_id"]
    want = config.get("want") or list(sfc.ALL_WANT)
    try:
        ctx = sfc.load(state.get("sender") or {}, want=set(want),
                       tenant_id=state.get("tenant_id"), org_label=config.get("org"))
    except Exception as e:  # noqa: BLE001
        log.warning("sf_context node failed: %s", e)
        ctx = {}
    acc = (ctx.get("account") or {}).get("name")
    cs = ctx.get("cases") or {}
    summary = (f"{acc or 'no account'}"
               + (f" · {cs.get('open')} open / {cs.get('total')} cases" if cs else "")
               + (f" · team {len(ctx.get('account_team') or [])}" if ctx.get("account_team") else ""))
    return {"sf_context": ctx, **_trace(nid, "sf_context", summary,
                                        {"keys": sorted(ctx)})}


@register("ai_prompt")
def h_ai_prompt(state: CaseState, config: dict) -> dict:
    """Run a configurable LLM prompt and write the result to `state[output_key]`
    (Phase 25). Templates interpolate `{case.subject}` /
    `{sf_context.account.tier}` / `{attachment_text}` etc. With `images` set it
    sends image attachments to a vision model (free OpenRouter → paid
    Anthropic). Edges then branch on the structured output — the routing stays
    a plain expression, the intelligence is this (traced, cached) node.

    config: {
      system, user,                 # prompt templates
      model?, temperature=0.2, max_tokens=600,
      output_key="ai_output",
      json_schema?: {...},          # ask for + parse JSON
      images: "none" | "auto" | ["attachments"],   # attachment blobs to send
      cache=true, on_error="passthrough" | "fail"
    }
    """
    nid = config["_node_id"]
    flat = _flat(state)
    system = _render(config.get("system", ""), flat)
    user = _render(config.get("user", ""), flat) or _render("{case.subject}\n{case.body}", flat)
    out_key = config.get("output_key") or "ai_output"
    want_json = bool(config.get("json_schema"))
    if want_json:
        system += ("\n\nReturn ONLY a JSON object matching this schema:\n"
                   + __import__("json").dumps(config["json_schema"])[:2000])

    imgs: list[tuple[bytes, str]] = []
    mode = config.get("images", "none")
    if mode and mode != "none":
        blobs = state.get("_attachment_blobs") or {}
        by_key = {a.get("blob_key"): a for a in (state.get("attachments") or [])}
        for k, data in blobs.items():
            mime = (by_key.get(k) or {}).get("mime", "image/png")
            imgs.append((data, mime))
        imgs = imgs[:4]

    _model = config.get("model") or llm.DEFAULT_MODEL
    try:
        raw = llm.complete(
            system=system or "You are a helpful support assistant.",
            user=user, model=_model,
            temperature=float(config.get("temperature", 0.2)),
            max_tokens=int(config.get("max_tokens", 600)),
            json_object=want_json,
            cache=bool(config.get("cache", True)) and not imgs,
            images=imgs or None,
            tenant_id=state.get("tenant_id"),
        )
    except Exception as e:  # noqa: BLE001
        if config.get("on_error") == "fail":
            raise
        log.warning("ai_prompt %s failed: %s", out_key, e)
        return {"ai": {out_key: None}, **_trace(nid, "ai_prompt", f"error → ai.{out_key}=None",
                                                {"error": str(e)[:300]})}

    value: Any = raw
    if want_json:
        value = _safe_json(raw) or {}
    usage = llm.last_usage or {}
    return {
        "ai": {out_key: value},            # declared channel -> merged, not dropped
        **_trace(nid, "ai_prompt",
                 f"ai.{out_key} ← {len(raw)} chars"
                 + (f", {len(imgs)} image(s)" if imgs else "")
                 + (f", {usage.get('total')} tok" if usage.get("total") else ""),
                 {"output_key": out_key, "value": value, "images": len(imgs),
                  "json": want_json, "tokens": usage or None, "model": _model}),
    }


@register("http_request")
def h_http_request(state: CaseState, config: dict) -> dict:
    """P6c — call an external HTTP API through a named per-tenant connection.

    config: {
      connection: "<slug>",           # -> connections.base_url + auth
      method: "GET",
      path: "/v1/things/{{context.id}}",   # {{ dotted.path }} over state
      query: {"q": "{{context.term}}"},
      headers: {"X-Extra": "..."},
      body: {...} | "{{context.payload}}",   # dict sent as JSON, or a template
      out_key: "http",                # -> state.context[out_key] = {status, ok, json|text}
      timeout: 15,
      on_error: "passthrough" | "fail",
    }

    The connection is the allow-list: `path` is appended to `base_url`, an
    absolute URL in `path` is rejected. No LLM. Best-effort — a failure lands
    `{error}` in `out_key` (unless `on_error="fail"`).
    """
    from . import connections

    nid = config["_node_id"]
    out_key = config.get("out_key", "http")
    slug = config.get("connection")
    conn = connections.resolve(state.get("tenant_id"), slug) if slug else None
    if not conn:
        msg = f"unknown connection {slug!r}"
        if config.get("on_error") == "fail":
            raise RuntimeError(msg)
        return {"context": {out_key: {"error": msg}},
                **_trace(nid, "http_request", msg, {"connection": slug})}

    path = _render_template(str(config.get("path", "")), state)
    method = str(config.get("method", "GET")).upper()
    headers = config.get("headers") or {}
    query = {k: _render_template(str(v), state) for k, v in (config.get("query") or {}).items()}
    raw_body = config.get("body")
    json_body = None
    if isinstance(raw_body, dict):
        json_body = raw_body
    elif isinstance(raw_body, str) and raw_body.strip():
        rendered = _render_template(raw_body, state)
        json_body = _safe_json(rendered) or {"_raw": rendered}

    url = conn["base_url"].rstrip("/") + "/" + path.lstrip("/")  # for the trace only; execute() rebuilds it
    try:
        res = connections.execute(conn, method=method, path=path, query=query,
                                  headers=headers, body=json_body,
                                  timeout=float(config.get("timeout", 15)))
    except ValueError:
        raise  # absolute path -- always a hard error, regardless of on_error
    except Exception as e:  # noqa: BLE001
        if config.get("on_error") == "fail":
            raise
        res = {"error": f"{type(e).__name__}: {e}"[:300]}

    return {
        "context": {out_key: res},         # operator.or_ channel -> survives the merge
        **_trace(nid, "http_request",
                 f"{method} {url} -> "
                 + (str(res.get("status")) if "status" in res else res.get("error", "?")),
                 {"connection": slug, "method": method, "url": url,
                  "status": res.get("status"), "ok": res.get("ok")}),
    }


@register("connector_action")
def h_connector_action(state: CaseState, config: dict) -> dict:
    """FR-47 — call a declared action on any connector: a `salesforce` /
    `slack` builtin, or one of the tenant's own named HTTP connections (each
    carrying its own saved `connection_actions`, see `connections.py`).
    Genuinely data-driven — adding a new connector or action for a tenant's
    own REST API never touches this file; `GET /api/connectors` is the
    catalog the web editor's pickers read from.

    config: {
      connector: "salesforce" | "slack" | "<tenant connection slug>",
      action: "post_note" | "update_fields" | "post_message" | "<saved action name>",
      params: {...},          # values, or "{{ dotted.path }}" templates over
                               # state — see the action's declared param list
      org: "<org label>",     # Salesforce actions only; ignored otherwise
      out_key: "connector_result",
      on_error: "passthrough" | "fail",
    }
    """
    from . import connectors

    nid = config["_node_id"]
    out_key = config.get("out_key", "connector_result")
    connector_slug = config.get("connector")
    action_name = config.get("action")
    tenant_id = state.get("tenant_id")

    try:
        _spec, action = connectors.get_action(tenant_id, connector_slug, action_name)
    except KeyError as e:
        msg = str(e)
        if config.get("on_error") == "fail":
            raise
        return {"context": {out_key: {"error": msg}},
                **_trace(nid, "connector_action", msg,
                         {"connector": connector_slug, "action": action_name})}

    rendered = {k: (_render_template(v, state) if isinstance(v, str) else v)
                for k, v in (config.get("params") or {}).items()}

    try:
        result = action.impl(tenant_id, config.get("org"), rendered)
    except Exception as e:  # noqa: BLE001
        if config.get("on_error") == "fail":
            raise
        result = {"error": f"{type(e).__name__}: {e}"[:300]}

    outcome = (str(result.get("status")) if "status" in result
               else result.get("error") or ("ok" if result else "?"))
    return {
        "context": {out_key: result},
        **_trace(nid, "connector_action", f"{connector_slug}.{action_name} -> {outcome}",
                 {"connector": connector_slug, "action": action_name, "params": sorted(rendered)}),
    }


@register("transform")
def h_transform(state: CaseState, config: dict) -> dict:
    """P6c — reshape `state.context` between nodes, no LLM.

    config: {
      map:  {"out_key": "context.http.json.total", ...}   # copy a value by dotted path
      set:  {"out_key": "{{context.a}}-{{context.b}}", ...}  # templated literal
      drop: ["scratch_key", ...]                          # null out context keys
      into: "context"                                     # target bag (default)
    }

    `map` reads any dotted path over the whole state (`context.*`, `ai.*`,
    `classification.*`, …); `set` renders `{{ dotted.path }}` templates.
    """
    nid = config["_node_id"]
    patch: dict[str, Any] = {}
    for out, src in (config.get("map") or {}).items():
        patch[out] = _dig(state, str(src))
    for out, tmpl in (config.get("set") or {}).items():
        patch[out] = _render_template(str(tmpl), state)
    for k in (config.get("drop") or []):
        patch[k] = None
    into = config.get("into", "context")
    return {into: patch,
            **_trace(nid, "transform",
                     f"{into}: set {sorted(k for k, v in patch.items() if v is not None)}"
                     + (f", dropped {config.get('drop')}" if config.get("drop") else ""),
                     {"keys": list(patch)})}
