"""
Phase 27d — the case-control-plane safety-net jobs.

Run inside the `api.worker` loop; each re-enqueues itself with `run_after`.
They are the backstop for everything the pipeline + Omni-Channel can't catch.

  queue_sweep    every 5 min — overdue / stuck / escalated-unaccepted Cases:
                               nudge + re-route once, then SLA_Breach + page.
  cdc_reconcile  hourly      — Cases with no matching `runs` row -> enqueue
                               (covers CDC's 72h retention cliff / cdc downtime).
  reasoning_ttl  every 5 min — reasoning_sessions stuck open past a ceiling:
                               nudge the Slack thread, then escalate + abandon.

SWEEP_DRY_RUN=1 -> log intended actions, change nothing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("interpreter.sweeps")

STUCK_MIN = int(os.environ.get("SWEEP_STUCK_MIN", "20"))          # New/Triaged with no progress
ACK_MIN = int(os.environ.get("SWEEP_ACK_MIN", "30"))             # escalated, unaccepted
RECONCILE_HOURS = int(os.environ.get("SWEEP_RECONCILE_HOURS", "6"))
SESSION_MAX_MIN = int(os.environ.get("SWEEP_SESSION_MAX_MIN", "120"))
ALERT_CHANNEL = os.environ.get("SWEEP_ALERT_CHANNEL", "#cx-unrouted")
TEAM_QUEUE = {                                                    # routed_team -> SF queue
    "support": "Team_Support", "tier2": "Support_Tier2", "csm": "Team_CSM",
    "sales": "Team_Sales", "offboarding": "Team_Offboarding", "billing": "Billing_Escalations",
}


def _dry() -> bool:
    return os.environ.get("SWEEP_DRY_RUN") == "1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _age_min(ts: str | None, now: datetime) -> float:
    d = _parse(ts)
    return (now - d).total_seconds() / 60 if d else 0.0


def _page(text: str, *, tenant_id=None, channel=None, thread_ts=None, sb=None) -> None:
    try:
        from interpreter import slack

        if slack.available() or os.environ.get("SLACK_ALERT_WEBHOOK"):
            slack.post_message(text, tenant_id=tenant_id, channel=channel or ALERT_CHANNEL,
                               thread_ts=thread_ts, sb=sb)
    except Exception as e:  # noqa: BLE001
        log.warning("sweep slack post failed: %s", e)


def _event(sb, **kw) -> None:
    try:
        from interpreter import case_events

        os.environ.setdefault("CASE_EVENTS_FORCE", "1")   # sweeps run outside pytest
        case_events.record(sb, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("sweep case_events failed: %s", e)


# ── queue_sweep ─────────────────────────────────────────────────────────
def queue_sweep(sb, *, dry_run: bool | None = None) -> dict:
    from interpreter import salesforce

    dry = _dry() if dry_run is None else dry_run
    if not salesforce.available():
        return {"skipped": "no Salesforce creds"}
    sf = salesforce.client_for(None)
    now = _now()
    try:
        rows = sf.query(
            "SELECT Id, CaseNumber, Status, OwnerId, Routed_Team__c, Next_Action_Due__c, "
            "SLA_Breach__c, CreatedDate, LastModifiedDate FROM Case "
            "WHERE IsClosed = false AND Status NOT IN ('Resolved', 'Closed') "
            "ORDER BY CreatedDate ASC LIMIT 500"
        ).get("records", [])
    except Exception as e:  # noqa: BLE001
        log.warning("queue_sweep query failed: %s", e)
        return {"error": str(e)[:200]}

    nudged: list[str] = []
    breached: list[str] = []
    for r in rows:
        if r.get("SLA_Breach__c"):
            continue
        status = r.get("Status") or ""
        owner = r.get("OwnerId") or ""
        is_queue = owner.startswith("00G")
        due = _parse(r.get("Next_Action_Due__c"))
        team = r.get("Routed_Team__c") or "support"
        cn = r.get("CaseNumber")

        reason = hard = None
        if due and due < now:
            over = (now - due).total_seconds() / 60
            reason, hard = "overdue", over > ACK_MIN
        elif status in ("New", "Triaged"):
            age = _age_min(r.get("CreatedDate"), now)
            if age > STUCK_MIN:
                reason, hard = "stuck", age > 2 * STUCK_MIN
        elif status == "Escalated" and is_queue:
            age = _age_min(r.get("LastModifiedDate"), now)
            if age > ACK_MIN:
                reason, hard = "unaccepted", age > 2 * ACK_MIN
        if not reason:
            continue

        if hard:
            breached.append(cn)
            if not dry:
                try:
                    salesforce.update_case_fields(r["Id"], {"SLA_Breach__c": True})
                    salesforce.assign_case(r["Id"], queue="SLA_Breach")
                except Exception as e:  # noqa: BLE001
                    log.warning("queue_sweep breach %s: %s", cn, e)
                _page(f":rotating_light: *SLA breach* — Case *{cn}* ({reason}, team `{team}`) "
                      f"has been unattended past 2× its window. Parked in `SLA_Breach`.", sb=sb)
                _event(sb, tenant_id=None, case_sf_id=r["Id"], case_number=cn,
                       actor="system:sweep", action="breach", from_status=status,
                       to_status=status, reason=reason, routed_team=team)
        else:
            nudged.append(cn)
            if not dry:
                try:
                    # re-route: touch the team queue so the Omni Flow re-fires,
                    # and give it one more ack window.
                    salesforce.assign_case(r["Id"], queue=TEAM_QUEUE.get(team, "Team_Support"))
                    salesforce.update_case_fields(r["Id"], {
                        "Next_Action_Due__c": (now + timedelta(minutes=ACK_MIN)).isoformat(),
                    })
                except Exception as e:  # noqa: BLE001
                    log.warning("queue_sweep nudge %s: %s", cn, e)
                _page(f":hourglass_flowing_sand: Case *{cn}* ({reason}) re-routed to `{team}` — "
                      f"still no owner. Next check in {ACK_MIN} min.", sb=sb)
                _event(sb, tenant_id=None, case_sf_id=r["Id"], case_number=cn,
                       actor="system:sweep", action="reconcile", from_status=status,
                       to_status=status, reason=reason, routed_team=team)

    return {"scanned": len(rows), "nudged": nudged, "breached": breached, "dry_run": dry}


# ── cdc_reconcile ───────────────────────────────────────────────────────
def cdc_reconcile(sb, *, dry_run: bool | None = None) -> dict:
    from interpreter import salesforce
    from interpreter.sf_ingest import enqueue_case_run

    dry = _dry() if dry_run is None else dry_run
    if not salesforce.available():
        return {"skipped": "no Salesforce creds"}
    sf = salesforce.client_for(None)
    since = (_now() - timedelta(hours=RECONCILE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        recs = sf.query(
            "SELECT Id, CaseNumber FROM Case WHERE IsClosed = false "
            f"AND (CreatedDate >= {since} OR LastModifiedDate >= {since}) LIMIT 500"
        ).get("records", [])
    except Exception as e:  # noqa: BLE001
        log.warning("cdc_reconcile query failed: %s", e)
        return {"error": str(e)[:200]}

    missing: list[str] = []
    for r in recs:
        sid, cn = r["Id"], r.get("CaseNumber")
        try:
            have = (sb.table("runs").select("run_id")
                    .or_(f"case_payload->>sf_id.eq.{sid},case_id.eq.{cn}")
                    .limit(1).execute().data)
        except Exception:  # noqa: BLE001
            have = (sb.table("runs").select("run_id")
                    .eq("case_payload->>sf_id", sid).limit(1).execute().data)
        if have:
            continue
        missing.append(cn)
        if not dry:
            ik = f"reconcile:{sid}"
            enqueue_case_run(sb, sid, dedupe_key=ik, idempotency_key=ik, trigger="reconcile")
            _event(sb, tenant_id=None, case_sf_id=sid, case_number=cn,
                   actor="system:cdc", action="reconcile", reason="no run — CDC gap backfill")
    return {"scanned": len(recs), "enqueued": missing, "dry_run": dry}


# ── reasoning_ttl ───────────────────────────────────────────────────────
def reasoning_ttl(sb, *, dry_run: bool | None = None) -> dict:
    from interpreter import salesforce

    dry = _dry() if dry_run is None else dry_run
    now = _now()
    cutoff = (now - timedelta(minutes=SESSION_MAX_MIN)).isoformat()
    try:
        rows = (sb.table("reasoning_sessions").select("*")
                .not_.in_("state", ("sent", "abandoned"))
                .lt("updated_at", cutoff).limit(200).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("reasoning_ttl query failed: %s", e)
        return {"error": str(e)[:200]}

    nudged: list[str] = []
    escalated: list[str] = []
    for s in rows:
        age = _age_min(s.get("updated_at"), now)
        sid = s["session_id"]
        cn = s.get("case_number") or s.get("case_id")
        ch, ts = s.get("slack_channel"), s.get("slack_thread_ts")
        if age < 2 * SESSION_MAX_MIN and ch and ts:
            nudged.append(cn)
            if not dry:
                _page(":wave: This reasoning thread has been idle a while — @mention me to "
                      "continue, or it escalates to the team shortly.",
                      tenant_id=s.get("tenant_id"), channel=ch, thread_ts=ts, sb=sb)
            continue
        escalated.append(cn)
        if dry:
            continue
        sf_id = s.get("case_id")
        if sf_id and salesforce.available():
            try:
                salesforce.update_case_fields(sf_id, {"Status": "Escalated"},
                                              tenant_id=s.get("tenant_id"))
                salesforce.assign_case(sf_id, queue="Team_Support", tenant_id=s.get("tenant_id"))
            except Exception as e:  # noqa: BLE001
                log.warning("reasoning_ttl escalate %s: %s", cn, e)
        try:
            sb.table("reasoning_sessions").update({"state": "abandoned"}) \
              .eq("session_id", sid).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("reasoning_ttl abandon %s: %s", sid, e)
        _page(f":warning: Reasoning session for Case *{cn}* went unanswered for "
              f"{age / 60:.0f}h — auto-escalated to the support team.", sb=sb)
        _event(sb, tenant_id=s.get("tenant_id"), case_sf_id=str(sf_id or ""), case_number=cn,
               actor="system:sweep", action="handover", to_status="Escalated",
               reason="reasoning session TTL", slack_ts=ts, slack_channel=ch)
    return {"open": len(rows), "nudged": nudged, "escalated": escalated, "dry_run": dry}
