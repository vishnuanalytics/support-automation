"""
Response Quality Feedback Loop chunk E (2026-09-10) — turn the confidence
gate's own escalation decisions, cross-referenced with chunk B's judged
corrections, into a threshold-tuning PROPOSAL. Never applied automatically.

Only escalated cases ever get a `human_reply`/correction at all
(`build_row`'s `pending` flag, migration `014`) — nothing today reviews an
auto-replied case. So the one direction this data can actually support is:
a case the gate escalated *purely because the blended score fell under
threshold* (not a forced topic/module/answer_mode/integrity rule) that a
human then barely touched. A cluster of those is real evidence the
threshold is stricter than it needs to be for that tier. This module
therefore only ever proposes LOWERING a threshold, and says so in the
proposal's own rationale rather than pretending to see a direction it has
no evidence for.

Reuses the existing P4/FR-44 "one approvals inbox" instead of building new
UI or a new table: a proposal is one `action_requests` row (kind=
"gate_tuning"), rendered by the same generic card `ReviewView.tsx` already
draws for `kb_change`/`github_issue` (its `title`/`rationale`/`body_md`
fields are exactly what that card looks for). Approving it (`POST
/api/approvals/action-requests/{id}`, existing endpoint) enqueues
`apply_gate_tuning` (`api/worker.py`), which patches the one
`flow_nodes.config.tier_overrides` the evidence came from — nothing else
in that node's config is touched. Rejecting it does nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("interpreter.gate_tuning")

LOW_SEVERITY_CEILING = 0.25       # a correction this small = escalation added no value
UNNECESSARY_RATE_FLOOR = 0.5      # a majority of the bucket must show it
MIN_SAMPLES = 5
MAX_STEP = 0.05                   # never propose more than a 0.05 shift at once
FLOOR_THRESHOLD = 0.2             # never propose below this, whatever the data says
LOOKBACK_DAYS = 90
POOL = 500


def _gate_step(trace: list | None) -> dict | None:
    """The `confidence_gate` trace entry's `data`, plus its own node_id
    (which IS `flow_nodes.node_id` — `interpreter/builder.py` sets
    `cfg["_node_id"] = n["node_id"]` when building the graph)."""
    for step in (trace or []):
        if step.get("type") == "confidence_gate":
            out = dict(step.get("data") or {})
            out["_node_id"] = step.get("node_id")
            return out
    return None


def _unnecessary(category: "str | None", severity: "float | None") -> bool:
    if category == "none":
        return True
    return severity is not None and severity <= LOW_SEVERITY_CEILING


def find_proposals(sb, *, lookback_days: int = LOOKBACK_DAYS, pool: int = POOL,
                   min_samples: int = MIN_SAMPLES, max_step: float = MAX_STEP) -> list[dict]:
    """Scan every tenant's recent judged, escalated runs and return one
    proposal dict per (tenant_id, flow_id, node_id, tier) bucket that
    clears the bar. Read-only — never writes anything. Each proposal:
    `{tenant_id, flow_id, node_id, tier, current_threshold,
    suggested_threshold, sample_size, unnecessary_count,
    evidence_run_ids}`. Best-effort — a query failure returns `[]`."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    try:
        rows = (sb.table("runs")
                .select("run_id, tenant_id, flow_id, trace, correction_analysis, created_at")
                .not_.is_("correction_analysis", "null")
                .gte("created_at", cutoff)
                .order("created_at", desc=True).limit(pool).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("find_proposals query failed: %s", e)
        return []

    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        ca = r.get("correction_analysis") or {}
        if ca.get("category") == "unknown":
            continue   # the judge couldn't classify it -- not evidence either way
        gate = _gate_step(r.get("trace"))
        if not gate or gate.get("pass") or gate.get("forced_escalation"):
            continue   # only a threshold-driven escalation is addressable here
        tenant_id, flow_id, node_id, tier = (r.get("tenant_id"), r.get("flow_id"),
                                             gate.get("_node_id"), gate.get("tier"))
        if not (tenant_id and flow_id and node_id and tier):
            continue
        buckets.setdefault((tenant_id, flow_id, node_id, tier), []).append({
            "run_id": r["run_id"], "category": ca.get("category"),
            "severity": ca.get("severity"), "threshold": gate.get("threshold"),
            "score": gate.get("score"),
        })

    proposals = []
    for (tenant_id, flow_id, node_id, tier), items in buckets.items():
        if len(items) < min_samples:
            continue
        unnecessary = [it for it in items if _unnecessary(it["category"], it["severity"])]
        if len(unnecessary) / len(items) < UNNECESSARY_RATE_FLOOR:
            continue
        current = items[0]["threshold"]
        if current is None:
            continue
        gaps = [current - it["score"] for it in unnecessary if it["score"] is not None]
        step = min(max_step, round(sum(gaps) / len(gaps), 3)) if gaps else max_step
        step = max(step, 0.02)
        suggested = round(max(FLOOR_THRESHOLD, current - step), 3)
        if suggested >= current - 0.009:
            continue
        proposals.append({
            "tenant_id": tenant_id, "flow_id": flow_id, "node_id": node_id, "tier": tier,
            "current_threshold": current, "suggested_threshold": suggested,
            "sample_size": len(items), "unnecessary_count": len(unnecessary),
            "evidence_run_ids": [it["run_id"] for it in unnecessary[:5]],
        })
    return proposals


def raise_proposal(sb, proposal: dict) -> dict | None:
    """Insert one `action_requests(kind='gate_tuning')` for a proposal.
    Skips (returns `None`) if a pending one already exists for the same
    (flow_id, node_id, tier), so a daily sweep doesn't spam a fresh card
    every run while the last one still awaits a decision."""
    try:
        existing = (sb.table("action_requests").select("id, payload")
                    .eq("tenant_id", proposal["tenant_id"]).eq("kind", "gate_tuning")
                    .eq("status", "pending").execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("raise_proposal existence check failed: %s", e)
        return None
    for ar in existing:
        p = ar.get("payload") or {}
        if (p.get("flow_id") == proposal["flow_id"] and p.get("node_id") == proposal["node_id"]
                and p.get("tier") == proposal["tier"]):
            return None

    title = (f"Lower the {proposal['tier']} confidence-gate threshold from "
             f"{proposal['current_threshold']:.2f} to {proposal['suggested_threshold']:.2f}")
    rationale = (
        f"{proposal['unnecessary_count']} of {proposal['sample_size']} escalated "
        f"'{proposal['tier']}' cases in the last {LOOKBACK_DAYS} days needed no "
        f"meaningful correction (a human barely changed the draft) despite the "
        f"gate score falling just under threshold. This only ever proposes "
        f"LOWERING a threshold — nothing today reviews an auto-replied case, so "
        f"there's no signal here to justify raising one."
    )
    body_md = (
        f"- Tier: {proposal['tier']}\n"
        f"- Current threshold: {proposal['current_threshold']:.2f}\n"
        f"- Suggested threshold: {proposal['suggested_threshold']:.2f}\n"
        f"- Sample size: {proposal['sample_size']} escalated, corrected case(s)\n"
        f"- Needed no meaningful correction: {proposal['unnecessary_count']}"
    )
    try:
        return (sb.table("action_requests").insert({
            "tenant_id": proposal["tenant_id"],
            "rule_name": "gate_tuning",
            "kind": "gate_tuning",
            "payload": {**proposal, "title": title, "rationale": rationale, "body_md": body_md},
            "status": "pending",
        }).execute().data or [None])[0]
    except Exception as e:  # noqa: BLE001
        log.warning("raise_proposal insert failed: %s", e)
        return None


def apply_threshold_change(sb, ar_row: dict) -> dict:
    """A human approved the proposal — patch the one `flow_nodes.config`
    it came from. Idempotent (`ar_row["result"]` already set -> no-op) and
    narrow (only ever writes `config.tier_overrides[tier]`, nothing else
    in that node's config)."""
    if ar_row.get("status") != "approved":
        return {"skipped": f"status={ar_row.get('status')}"}
    if ar_row.get("result"):
        return {"idempotent_skip": True, **ar_row["result"]}
    p = ar_row.get("payload") or {}
    node_id, tier, suggested = p.get("node_id"), p.get("tier"), p.get("suggested_threshold")
    try:
        rows = sb.table("flow_nodes").select("config").eq("node_id", node_id).execute().data
    except Exception as e:  # noqa: BLE001
        log.warning("apply_threshold_change lookup for %s: %s", node_id, e)
        return {"error": str(e)[:200]}
    if not rows:
        result = {"error": "node gone"}
    else:
        config = dict(rows[0].get("config") or {})
        overrides = dict(config.get("tier_overrides") or {})
        overrides[tier] = suggested
        config["tier_overrides"] = overrides
        try:
            sb.table("flow_nodes").update({"config": config}).eq("node_id", node_id).execute()
            result = {"applied": True, "node_id": node_id, "tier": tier, "new_threshold": suggested}
        except Exception as e:  # noqa: BLE001
            log.warning("apply_threshold_change write for %s: %s", node_id, e)
            result = {"error": str(e)[:200]}
    try:
        sb.table("action_requests").update({"result": result}).eq("id", ar_row["id"]).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("apply_threshold_change stamp result for %s: %s", ar_row.get("id"), e)
    return result
