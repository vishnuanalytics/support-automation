"""What the API and worker call. Assembles stored rows into queue and case views."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import brief, config, demo, drafts, readers, redact, similar, sync, timeline, verdict
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
    return {"cases": items, "labels": {"third_party": cfg["third_party_label"]}, "tiles": sync.counts([{"verdict": r["verdict"]} for r in cases]),
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


def set_review(sb: Any, tenant_id: str, case_id: str, user_id: str, *, reviewed: bool = False,
               snooze_until: str | None = None, clear: bool = False) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    row: dict[str, Any] = {"tenant_id": tenant_id, "case_id": case_id, "reviewed_by": user_id,
                           "reviewed_at": None, "snooze_until": None, "snoozed_at": None}
    if not clear:
        if reviewed:
            row["reviewed_at"] = now
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
    return {"github": readers.GithubReader(token, cfg["support_github_users"]),
            "slack": readers.SlackReader(_slack_tokens(sb, tenant_id),
                                         cfg["internal_slack_domain"])}


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
