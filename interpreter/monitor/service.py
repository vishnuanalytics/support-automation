"""What the API and worker call. Assembles stored rows into queue and case views."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import (agent_quality, brief, coaching, config, demo, drafts, dsp_sla, escalations, followups, help_requests,
               log_expiry, progress, readers, redact, similar, sync, tech_pending, timeline, today, verdict, weekly)
from .store import SupabaseStore
from .verdict import parse_dt

log = logging.getLogger("interpreter.monitor.service")

SLACK_USER_PREFIX = "slack_user:"
_QUEUE_COLUMNS = "case_id, source, case_data, verdict, synced_at"


def load_config(sb: Any, tenant_id: str) -> dict:
    rows = (sb.table("monitor_config").select("config").eq("tenant_id", tenant_id)
            .execute().data or [])
    return config.merge(rows[0]["config"] if rows else None)


def _cfg_for(cfg: dict, source: str | None) -> dict:
    return demo.CONFIG if source == "demo" else cfg


def is_hidden(review: dict | None, v: dict, now: datetime) -> bool:
    """Reviewed or snoozed cases stay hidden until something new arrives."""
    if not review:
        return False
    latest = parse_dt(v.get("latest_activity_at"))
    reviewed = parse_dt(review.get("reviewed_at"))
    if reviewed and (latest is None or latest <= reviewed):
        return True
    until = parse_dt(review.get("snooze_until"))
    snoozed = parse_dt(review.get("snoozed_at"))
    return bool(until and until > now and (latest is None or snoozed is None or latest <= snoozed))


def queue(sb: Any, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cases = (sb.table("monitor_cases").select(_QUEUE_COLUMNS).eq("tenant_id", tenant_id)
             .eq("is_open", True).execute().data or [])
    reviews = {r["case_id"]: r for r in (sb.table("monitor_reviews").select("*")
                                         .eq("tenant_id", tenant_id).execute().data or [])}
    cfg_all = load_config(sb, tenant_id)
    case_rows = [r["case_data"] for r in cases]
    dupes = similar.group_duplicates(case_rows)
    dup_of: dict[str, str] = {}
    for key, members in dupes.items():
        for m in members:
            dup_of[m["id"]] = key

    items = []
    for r in cases:
        c, v = r["case_data"], r["verdict"]
        review = reviews.get(r["case_id"])
        items.append({
            "id": r["case_id"], "source": r["source"], "case_number": c.get("case_number"),
            "subject": c.get("subject"), "priority": c.get("priority"), "owner": c.get("owner"),
            "record_type": c.get("record_type"), "waiting_raw": c.get("waiting_raw"),
            "sub_status": c.get("sub_status"), "bu": c.get("bu"), "module": c.get("module"),
            "sub_module": c.get("sub_module"),
            "link_kinds": sorted({x["kind"] for x in c.get("links") or []}
                                 | ({"email"} if c.get("email_evidence") else set())),
            "link_from_post": any(x.get("via") == "post" for x in c.get("links") or []),
            "partner_channel": (c.get("partner") or {}).get("channel"),
            "not_raised": bool(v.get("not_raised")),
            "stage": verdict.stage(v), "signal": v.get("signal"), "latest": v.get("latest"),
            "stale": bool(v.get("stale")),
            "overdue_hours": v.get("overdue_hours") or 0,
            "mine": bool(cfg_all["lead_owner_name"])
            and c.get("owner") == cfg_all["lead_owner_name"],
            "links": [{"kind": x["kind"], "url": x["url"]} for x in c.get("links") or []],
            "related": [{k: x.get(k) for k in ("id", "case_number", "record_type", "status", "url")}
                        for x in [*(p for p in [c.get("parent")] if p), *(c.get("children") or [])]],
            "waiting_party": c.get("waiting_party"), "third_parties": c.get("third_parties") or [],
            "url": c.get("url"), "ball": v["ball"], "mismatch": v["mismatch"],
            "score": v["score"], "level": v["level"], "next_action": v["next_action"],
            "missing_link": v["missing_link"], "overdue": v["overdue"],
            "stale_hours": v["stale_hours"], "duplicate_of": dup_of.get(r["case_id"]),
            "ai_state": (c.get("ai") or {}).get("state"),
            "hidden": is_hidden(review, v, now),
            "snooze_until": (review or {}).get("snooze_until"),
        })
    items.sort(key=lambda i: i["score"], reverse=True)

    history = (sb.table("monitor_syncs").select("status, trigger, started_at, finished_at, "
                                                "counts, errors")
               .eq("tenant_id", tenant_id).order("started_at", desc=True).limit(30)
               .execute().data or [])
    done = [s for s in history if s["status"] != "running"]
    cfg = _cfg_for(load_config(sb, tenant_id), "demo" if any(
        r["source"] == "demo" for r in cases) else None)
    slack_read = (sb.table("monitor_evidence").select("data").eq("tenant_id", tenant_id)
                  .ilike("url", "%slack.com%").execute().data or [])
    read_times = [r["data"]["imported_at"] for r in slack_read if r["data"].get("imported_at")]
    return {"cases": items,
            "slack_via_claude": bool(cfg_all["slack_via_claude"]),
            "slack_read_at": max(read_times, default=None),
            "labels": {"third_party": cfg["third_party_label"],
                       "customer": cfg["customer_label"]},
            "has_lead": bool(cfg_all["lead_owner_name"]),
            "focus_partners": cfg["focus_partners"], "tiles": sync.counts([{"verdict": r["verdict"]} for r in cases]),
            "trend": [{"at": s["started_at"], "my_turn": (s["counts"] or {}).get("my_turn", 0)}
                      for s in reversed(done) if s["status"] != "failed"],
            "last_sync": done[0] if done else None,
            "running": any(s["status"] == "running" for s in history),
            "duplicate_groups": {k: [m["id"] for m in v] for k, v in dupes.items()}}


def _evidence(sb: Any, tenant_id: str, case: dict) -> list[dict]:
    urls = [x["url"] for x in case.get("links") or []]
    extra = [case["email_evidence"]] if case.get("email_evidence") else []
    if not urls:
        return extra
    rows = (sb.table("monitor_evidence").select("url, data").eq("tenant_id", tenant_id)
            .in_("url", urls).execute().data or [])
    found = {r["url"]: r["data"] for r in rows}
    return [found[u] for u in urls if u in found] + extra


def case_detail(sb: Any, tenant_id: str, case_id: str,
                now: datetime | None = None) -> dict[str, Any] | None:
    rows = (sb.table("monitor_cases").select("*").eq("tenant_id", tenant_id)
            .eq("case_id", case_id).execute().data or [])
    if not rows:
        return None
    row = rows[0]
    case, v = row["case_data"], row["verdict"]
    cfg = _cfg_for(load_config(sb, tenant_id), row["source"])
    evidence = _evidence(sb, tenant_id, case)
    raw = row.get("timeline")
    items = timeline.merge(case, raw, evidence, cfg)

    module = case.get("module")
    pool = []
    if module:
        pool = (sb.table("monitor_resolved").select("*").eq("tenant_id", tenant_id)
                .eq("module", module).order("closed_at", desc=True).limit(200).execute().data
                or [])
        pool = [{**p, "id": p["case_id"]} for p in pool]

    others = (sb.table("monitor_cases").select("case_id, case_data").eq("tenant_id", tenant_id)
              .eq("is_open", True).execute().data or [])
    groups = similar.group_duplicates([o["case_data"] for o in others])
    dupes = {m["id"]: m for key, members in groups.items()
             if any(x["id"] == case_id for x in members)
             for m in members if m["id"] != case_id}

    return {
        "case": case, "verdict": v, "source": row["source"],
        "brief": brief.build(case, v, raw, items, cfg),
        "timeline": items, "timeline_at": row.get("timeline_at"), "evidence": evidence,
        "drafts": drafts.build(case, v, evidence, cfg),
        "similar": similar.rank(case, pool),
        "duplicates": [{"id": d["id"], "case_number": d.get("case_number"),
                        "subject": d.get("subject"), "owner": d.get("owner")}
                       for d in dupes.values()],
        "slack_threads": [{"url": x["url"], "channel": x["channel"], "thread_ts": x["thread_ts"]}
                          for x in case.get("links") or []
                          if x.get("kind") == "slack" and x.get("channel")],
        "slack_team_mention": cfg.get("slack_team_mention") or "",
        "slack_via_claude": bool(cfg.get("slack_via_claude")),
        "partner": case.get("partner"),
        "labels": {"third_party": cfg["third_party_label"], "customer": cfg["customer_label"]},
        "llm_enabled": bool(cfg.get("llm_enabled")),
    }


def digest(sb: Any, tenant_id: str, limit: int = 300) -> dict[str, Any]:
    last = (sb.table("monitor_syncs").select("id, started_at").eq("tenant_id", tenant_id)
            .neq("status", "running").neq("status", "failed").order("started_at", desc=True)
            .limit(1).execute().data or [])
    events = []
    if last:
        events = (sb.table("monitor_events").select("*").eq("tenant_id", tenant_id)
                  .eq("sync_id", last[0]["id"]).order("created_at", desc=True).limit(limit)
                  .execute().data or [])
    return {"since": last[0]["started_at"] if last else None, "events": events}


def follow_up_list(sb: Any, tenant_id: str) -> dict[str, Any]:
    return followups.build(queue(sb, tenant_id)["cases"])


def _evidence_by_url(sb: Any, tenant_id: str, urls: list[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for start in range(0, len(urls), 50):
        rows = (sb.table("monitor_evidence").select("url, data").eq("tenant_id", tenant_id)
                .in_("url", urls[start:start + 50]).execute().data or [])
        found.update({r["url"]: r["data"] for r in rows})
    return found


def _cases_with_evidence(sb: Any, tenant_id: str, cfg: dict) -> list[tuple[dict, dict, list[dict]]]:
    rows = (sb.table("monitor_cases").select("source, case_data, verdict, timeline")
            .eq("tenant_id", tenant_id).eq("is_open", True).execute().data or [])
    urls = sorted({x["url"] for r in rows for x in r["case_data"].get("links") or []})
    found = _evidence_by_url(sb, tenant_id, urls)
    items = []
    for r in rows:
        case = r["case_data"]
        evidence = [found[x["url"]] for x in case.get("links") or [] if x["url"] in found]
        if case.get("email_evidence"):
            evidence.append(case["email_evidence"])
        # The DSP feed keeps only the newest few emails; the timeline has the rest.
        evidence.append(sync.case_emails(case, r["timeline"], _cfg_for(cfg, r["source"])))
        evidence.append({"kind": "email", "ok": True, "messages": case.get("email_log") or []})
        items.append((case, r["verdict"], evidence))
    return items


def escalation_list(sb: Any, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cfg = load_config(sb, tenant_id)
    return escalations.build(_cases_with_evidence(sb, tenant_id, cfg), cfg["escalation_days"], now,
                             cfg["dependency_record_type"])


def _dsp_items(items: list[tuple], cfg: dict) -> list[tuple]:
    return [i for i in items if i[0].get("record_type") == cfg["dependency_record_type"]]


def today_view(sb: Any, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cfg = load_config(sb, tenant_id)
    items = _cases_with_evidence(sb, tenant_id, cfg)
    sla = dsp_sla.build(_dsp_items(items, cfg), now, cfg)
    closed = _closed_issues(sb, tenant_id)
    reviews = {r["case_id"]: r for r in (sb.table("monitor_reviews").select("case_id, reviewed_at")
                                         .eq("tenant_id", tenant_id).execute().data or [])}
    since = (now - timedelta(days=help_requests.STATS_DAYS)).isoformat()
    asks = (sb.table("monitor_help_requests").select("*").eq("tenant_id", tenant_id)
            .gte("raised_at", since).order("raised_at").execute().data or [])
    numbers = {c.get("case_number"): c["id"] for c, _, _ in items}
    view = today.build(tech_pending_list(sb, tenant_id, now), sla, coaching.build(closed, cfg, now),
                       follow_up_list(sb, tenant_id), len(items), reviews, cfg, now,
                       help_requests.build(asks, cfg["assist_hours"], numbers, now),
                       log_expiry.build([c for c, _, _ in items], cfg, now),
                       today.reply_cards(items, cfg, now))
    return {**view, "cards": _with_last_response(view["cards"], items)}


def _with_last_response(cards: list[dict], items: list[tuple]) -> list[dict]:
    """Each card lists its cases and where the last response on each came from."""
    known = {c["id"]: {"case_id": c["id"], "case_number": c.get("case_number"), "subject": c.get("subject"),
                       "owner": c.get("owner"), "latest": v.get("latest")} for c, v, _ in items}
    return [{**card, "cases": [{**known[i], "step": (card.get("steps") or {}).get(i)}
                               for i in card.get("case_ids") or [] if i in known]} for card in cards]


def _closed_issues(sb: Any, tenant_id: str) -> list[dict]:
    return (sb.table("monitor_closed_issues").select("url, title, reporter, labels, closed_at")
            .eq("tenant_id", tenant_id).execute().data or [])


def weekly_view(sb: Any, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cfg = load_config(sb, tenant_id)
    sla = dsp_sla.build(_dsp_items(_cases_with_evidence(sb, tenant_id, cfg), cfg), now, cfg)
    return weekly.build(tech_pending_list(sb, tenant_id, now), sla, now, cfg)


def agents_view(sb: Any, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cfg = load_config(sb, tenant_id)
    rows = (sb.table("monitor_location_facts").select("location_id, platform, platform_id, biz_id")
            .eq("tenant_id", tenant_id).execute().data or [])
    facts: dict[str, dict] = {}
    for r in rows:
        facts.setdefault(r["location_id"], {})[r["platform"].lower()] = r
    items = _dsp_items(_cases_with_evidence(sb, tenant_id, cfg), cfg)
    return agent_quality.build(items, facts, coaching.build(_closed_issues(sb, tenant_id), cfg, now), cfg, now)


def sla_report(sb: Any, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cfg = load_config(sb, tenant_id)
    sla = dsp_sla.build(_dsp_items(_cases_with_evidence(sb, tenant_id, cfg), cfg), now, cfg)
    since = (now - timedelta(days=7)).isoformat()
    resolved = (sb.table("monitor_resolved").select("subject, module, sub_module, third_parties, "
                                                    "opened_at, closed_at")
                .eq("tenant_id", tenant_id).eq("record_type", cfg["dependency_record_type"])
                .gte("closed_at", since).execute().data or [])
    days = (sb.table("monitor_daily_stats").select("day, stats").eq("tenant_id", tenant_id)
            .order("day").execute().data or [])
    return {**dsp_sla.weekly_report(sla, resolved, now, cfg), "rows": sla["rows"],
            "progress": progress.summary(days)}


def record_progress(sb: Any, tenant_id: str, now: datetime | None = None) -> None:
    """Write today's row of workload numbers."""
    now = now or datetime.now(timezone.utc)
    cfg = load_config(sb, tenant_id)
    day = progress.local_day(now, cfg)
    sla = dsp_sla.build(_dsp_items(_cases_with_evidence(sb, tenant_id, cfg), cfg), now, cfg)
    closed = (sb.table("monitor_closed_issues").select("opened_at, closed_at")
              .eq("tenant_id", tenant_id).execute().data or [])
    reviews = (sb.table("monitor_reviews").select("case_id, reviewed_at")
               .eq("tenant_id", tenant_id).execute().data or [])
    before = (sb.table("monitor_daily_stats").select("stats").eq("tenant_id", tenant_id)
              .eq("day", day).execute().data or [])
    stats = progress.snapshot(today_view(sb, tenant_id, now), sla, closed, reviews,
                              before[0]["stats"] if before else None, now, cfg)
    sb.table("monitor_daily_stats").upsert({"tenant_id": tenant_id, "day": day, "stats": stats,
                                            "updated_at": now.isoformat()}).execute()


def tech_pending_list(sb: Any, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    rows = (sb.table("monitor_cases").select("case_data").eq("tenant_id", tenant_id)
            .eq("is_open", True).execute().data or [])
    cases = [r["case_data"] for r in rows]
    cfg = load_config(sb, tenant_id)
    issues = (sb.table("monitor_issues").select("url, status, waiting_on, reason")
              .eq("tenant_id", tenant_id).eq("is_open", True).execute().data or [])
    evidence = _evidence_by_url(sb, tenant_id, [i["url"] for i in issues])
    return tech_pending.build(issues, cases, evidence, cfg["tech_pending_days"], now, cfg)


ALERT_LIMIT = 100


def cleared_by_review(event: dict, reviewed_at: str | None) -> bool:
    """Reviewing a case deals with its alerts so far; later ones still count as new."""
    reviewed, created = parse_dt(reviewed_at), parse_dt(event.get("created_at"))
    return bool(reviewed and created and created <= reviewed)


def alerts(sb: Any, tenant_id: str, user_id: str) -> dict[str, Any]:
    rows = (sb.table("monitor_events").select("*").eq("tenant_id", tenant_id)
            .neq("kind", "new_case").order("created_at", desc=True).limit(ALERT_LIMIT)
            .execute().data or [])
    seen = (sb.table("monitor_alert_seen").select("seen_event_id").eq("tenant_id", tenant_id)
            .eq("user_id", user_id).execute().data or [])
    last = seen[0]["seen_event_id"] if seen else 0
    reviews = (sb.table("monitor_reviews").select("case_id, reviewed_at")
               .eq("tenant_id", tenant_id).execute().data or [])
    reviewed = {r["case_id"]: r["reviewed_at"] for r in reviews}
    for r in rows:
        r["unread"] = r["id"] > last and not cleared_by_review(r, reviewed.get(r["case_id"]))
    return {"alerts": rows, "unread": sum(r["unread"] for r in rows)}


def start_alerts(sb: Any, tenant_id: str, user_id: str) -> None:
    """A person's first visit starts from now, so history doesn't arrive as new."""
    seen = (sb.table("monitor_alert_seen").select("user_id").eq("tenant_id", tenant_id)
            .eq("user_id", user_id).execute().data or [])
    if not seen:
        mark_alerts_seen(sb, tenant_id, user_id)


def mark_alerts_seen(sb: Any, tenant_id: str, user_id: str) -> None:
    newest = (sb.table("monitor_events").select("id").eq("tenant_id", tenant_id)
              .order("id", desc=True).limit(1).execute().data or [])
    sb.table("monitor_alert_seen").upsert({
        "tenant_id": tenant_id, "user_id": user_id,
        "seen_event_id": newest[0]["id"] if newest else 0}).execute()


def _review_stamp(sb: Any, tenant_id: str, case_id: str, now: str) -> str:
    """Events carry the database's clock, so a review can't simply use ours to cover them."""
    newest = (sb.table("monitor_events").select("created_at").eq("tenant_id", tenant_id)
              .eq("case_id", case_id).order("created_at", desc=True).limit(1).execute().data or [])
    return max(now, newest[0]["created_at"], key=parse_dt) if newest else now


def set_review(sb: Any, tenant_id: str, case_id: str, user_id: str, *, reviewed: bool = False,
               snooze_until: str | None = None, clear: bool = False) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    row: dict[str, Any] = {"tenant_id": tenant_id, "case_id": case_id, "reviewed_by": user_id,
                           "reviewed_at": None, "snooze_until": None, "snoozed_at": None}
    if not clear:
        if reviewed:
            row["reviewed_at"] = _review_stamp(sb, tenant_id, case_id, now)
        if snooze_until:
            row.update(snooze_until=snooze_until, snoozed_at=now)
    return sb.table("monitor_reviews").upsert(row).execute().data[0]


def slack_user_rows(sb: Any, tenant_id: str) -> list[dict]:
    return (sb.table("tenant_integrations").select("kind, secret").eq("tenant_id", tenant_id)
            .like("kind", f"{SLACK_USER_PREFIX}%").execute().data or [])


def slack_user_token(sb: Any, tenant_id: str, user_id: str) -> str | None:
    from interpreter import vault_secrets
    return vault_secrets.get(tenant_id, f"{SLACK_USER_PREFIX}{user_id}", sb=sb).get("token")


def _slack_tokens(sb: Any, tenant_id: str) -> list[str]:
    from interpreter import vault_secrets
    tokens = [vault_secrets.get(tenant_id, r["kind"], sb=sb).get("token")
              for r in slack_user_rows(sb, tenant_id)]
    return [t for t in tokens if t]


def _evidence_readers(sb: Any, tenant_id: str, cfg: dict) -> dict[str, Any]:
    from interpreter import github
    try:
        token = github.token_for(tenant_id, sb)
    except Exception:  # noqa: BLE001
        token = None
    return {"github": readers.GithubReader(token, cfg["support_github_users"],
                                           cfg["github_bot_users"]),
            "slack": readers.SlackReader(_slack_tokens(sb, tenant_id), cfg["internal_slack_domain"],
                                         cfg["slack_via_claude"])}


def _case_reader(sb: Any, tenant_id: str, cfg: dict) -> readers.SalesforceCaseReader:
    """Build a fresh client from this workspace's own credentials.

    This skips `salesforce.client_for` on purpose: that falls back to the
    platform's org when a login fails, which would put another org's cases in
    this queue. Building fresh also means an expired session can't carry over
    from an earlier sync.
    """
    from interpreter import salesforce, vault_secrets

    org = cfg.get("org_label") or "default"
    creds = vault_secrets.get(tenant_id, f"salesforce:{org}", sb=sb)
    if not creds:
        raise ValueError(f"Connect the workspace's Salesforce org ({org}) first")
    try:
        client = salesforce._build_client(creds)
    except Exception as e:  # noqa: BLE001
        log.warning("monitor salesforce login failed: %s", type(e).__name__)
        raise ValueError(f"Salesforce login for {org} failed: reconnect it") from e
    return readers.SalesforceCaseReader(client, cfg)


def run_sync(sb: Any, tenant_id: str, trigger: str = "schedule") -> dict:
    cfg = load_config(sb, tenant_id)
    store = SupabaseStore(sb)
    try:
        reader = _case_reader(sb, tenant_id, cfg)
    except ValueError as e:
        sync_id = store.start_sync(tenant_id, trigger)
        if sync_id:
            store.finish_sync(sync_id, "failed", {}, [{"error": str(e)}])
        return {"status": "failed", "error": str(e)}
    return sync.run(tenant_id, store, reader, _evidence_readers(sb, tenant_id, cfg), cfg,
                    trigger=trigger)


def refresh_case(sb: Any, tenant_id: str, case_id: str) -> bool:
    """Re-read one case's timeline, for on-demand freshness on the case page."""
    rows = (sb.table("monitor_cases").select("source").eq("tenant_id", tenant_id)
            .eq("case_id", case_id).execute().data or [])
    if not rows or rows[0]["source"] != "salesforce":
        return False
    cfg = load_config(sb, tenant_id)
    data = _case_reader(sb, tenant_id, cfg).timeline(case_id)
    SupabaseStore(sb).save_timeline(tenant_id, case_id, data, datetime.now(timezone.utc))
    return True


def sync_due(cfg: dict, last_started: datetime | None, now: datetime) -> bool:
    if last_started is None:
        return True
    every = int(cfg.get("sync_every_min") or 0)
    if every and (now - last_started).total_seconds() >= every * 60:
        return True
    tz = ZoneInfo(cfg.get("timezone") or "UTC")
    local = now.astimezone(tz)
    slot = local.replace(hour=int(cfg["sync_hour"]), minute=0, second=0, microsecond=0)
    return local >= slot and last_started.astimezone(tz) < slot


def tenants_due(sb: Any, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    due = []
    rows = sb.table("monitor_config").select("tenant_id, config").execute().data or []
    for r in rows:
        cfg = config.merge(r["config"])
        if not cfg["enabled"]:
            continue
        last = (sb.table("monitor_syncs").select("started_at").eq("tenant_id", r["tenant_id"])
                .order("started_at", desc=True).limit(1).execute().data or [])
        if sync_due(cfg, parse_dt(last[0]["started_at"]) if last else None, now):
            due.append(r["tenant_id"])
    return due


def load_demo(sb: Any, tenant_id: str, now: datetime | None = None) -> dict:
    """Run the demo scenarios through the same verdict path as real data."""
    now = now or datetime.now(timezone.utc)
    data = demo.build(now)
    store = SupabaseStore(sb)
    rows = []
    for case in data["cases"]:
        evidence = [data["evidence"][x["url"]] for x in case["links"]]
        rows.append({"case": case, "verdict": verdict.compute(case, evidence, demo.CONFIG, now),
                     "timeline": data["timeline"]})
    store.save_cases(tenant_id, rows, now)
    store.save_evidence(tenant_id, list(data["evidence"].values()), now)
    store.save_resolved(tenant_id, data["resolved"])
    return {"loaded": len(rows), **sync.counts(rows)}


def clear_demo(sb: Any, tenant_id: str) -> None:
    data = demo.build()
    sb.table("monitor_cases").delete().eq("tenant_id", tenant_id).eq("source", "demo").execute()
    (sb.table("monitor_evidence").delete().eq("tenant_id", tenant_id)
     .in_("url", list(data["evidence"])).execute())
    (sb.table("monitor_resolved").delete().eq("tenant_id", tenant_id)
     .in_("case_id", [r["id"] for r in data["resolved"]]).execute())


def slack_links(sb: Any, tenant_id: str) -> list[dict]:
    """Every Slack thread linked from an open case, for a person to import."""
    rows = (sb.table("monitor_cases").select("case_data").eq("tenant_id", tenant_id)
            .eq("is_open", True).execute().data or [])
    out: dict[str, dict] = {}
    for r in rows:
        case = r["case_data"]
        for link in case.get("links") or []:
            if link.get("kind") == "slack" and link.get("channel"):
                out.setdefault(link["url"], {
                    "url": link["url"], "channel": link["channel"],
                    "thread_ts": link["thread_ts"], "case_number": case.get("case_number")})
    return list(out.values())


def import_slack_threads(sb: Any, tenant_id: str, threads: list[dict],
                         now: datetime | None = None) -> int:
    """Store threads read through a person's own Slack access. Each entry is
    `{url, messages: [{who, ours, at, text, url?}], internal?}`, or `{url, error}`
    when that person can't see the thread. `internal` marks a thread with no
    external party, which shows staleness but can't show a third party's turn.
    Text is cleaned like any other evidence."""
    now = now or datetime.now(timezone.utc)
    known = {x["url"] for x in slack_links(sb, tenant_id)}
    rows = []
    for thread in threads:
        url = thread["url"]
        if url not in known:
            raise ValueError("that Slack thread isn't linked to an open case")
        base = {"kind": "slack", "url": url, "label": "Slack thread",
                "imported_at": now.isoformat(),
                "note": f"Read through Claude on {now.date().isoformat()}; not updated by syncs"}
        if thread.get("error"):
            rows.append({**base, "ok": False, "error": redact.clean(thread["error"])})
            continue
        rows.append({**base, "ok": True, "internal": bool(thread.get("internal")), "messages": [
            {"who": m.get("who"), "ours": bool(m.get("ours")), "at": m["at"],
             "text": redact.clean(m.get("text")), "url": m.get("url") or url}
            for m in thread.get("messages") or []]})
    SupabaseStore(sb).save_evidence(tenant_id, rows, now)
    return len(rows)


def ai_draft(sb: Any, tenant_id: str, case_id: str, audience: str) -> dict[str, Any] | None:
    """A context-based draft for the merchant or the aggregator. Never sent."""
    from . import knowledge

    detail = case_detail(sb, tenant_id, case_id)
    if detail is None:
        return None
    if not detail["llm_enabled"]:
        return {"enabled": False}
    try:
        return {"enabled": True, **knowledge.generate_draft(
            tenant_id, detail["case"], audience, detail["timeline"], sb, detail["partner"])}
    except Exception as e:  # noqa: BLE001 -- usually the AI provider's daily limit
        log.warning("ai draft failed: %s", type(e).__name__)
        return {"enabled": True, "error": "The AI service is busy or over its daily limit. "
                                          "Try again later, or use a copy draft below."}


SUMMARY_SYSTEM = (
    "You summarise a support case for a shift lead. Use only the numbered sources given. "
    "Cite each statement like [1]. If something isn't in the sources, write \"not stated\". "
    "Never say whose turn it is. Answer in at most five short lines."
)


def summarize(sb: Any, tenant_id: str, case_id: str) -> dict[str, Any] | None:
    """Optional AI summary. Text is redacted first; the verdict stays rule-based."""
    from interpreter import llm

    detail = case_detail(sb, tenant_id, case_id)
    if detail is None:
        return None
    if not detail["llm_enabled"]:
        return {"enabled": False}
    sources = [{"n": i + 1, "source": t["source"], "url": t["url"], "at": t["at"],
                "text": redact.redact_pii(t["text"])}
               for i, t in enumerate(detail["timeline"][-30:])]
    if not sources:
        return {"enabled": True, "text": "not stated", "sources": []}
    body = "\n".join(f"[{s['n']}] {s['source']} {s['at']}: {s['text']}" for s in sources)
    text = llm.complete(SUMMARY_SYSTEM, body, max_tokens=300, tenant_id=tenant_id)
    return {"enabled": True, "text": text,
            "sources": [{k: s[k] for k in ("n", "source", "url")} for s in sources]}
