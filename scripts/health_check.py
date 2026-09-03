"""
Phase 23 — the dead-man's switch. Reads `system_health` + the recent `jobs`
failure rate and alerts if the pipeline is unhealthy. Run it from cron /
cron-job.org / a uptime monitor every few minutes.

    python -m scripts.health_check                    # print + exit 1 if unhealthy
    python -m scripts.health_check --slack <webhook>  # also POST to Slack
    SLACK_ALERT_WEBHOOK=... python -m scripts.health_check

Unhealthy =
  * a component's heartbeat is older than HEALTH_STALE_MIN (default 15), or
  * >HEALTH_FAIL_RATE (default 0.5) of run_flow jobs in the last hour failed, or
  * >HEALTH_STUB_RATE (default 0.3) of runs in the last hour fell back to the
    deterministic LLM stub (every provider down -> generic drafts -> the
    human queues flood), or
  * a `neo4j` heartbeat exists but is stale (graph enrichment silently off), or
  * a `slackbot` heartbeat exists but is stale > HEALTH_SLACKBOT_STALE_MIN
    (default 20) -- the Socket-Mode bot dropped, so no approval / reasoning
    replies land, or
  * >HEALTH_SLA_BREACH_MAX (default 3) Case SLA breaches in the last hour
    (the sweep is escalating but nobody is picking cases up), or
  * KIL (the knowledge-integrity loop) is off the rails:
      - flag precision over the last HEALTH_KIL_WINDOW_DAYS (default 7) is
        below HEALTH_KIL_PRECISION_MIN (default 0.5) with at least
        HEALTH_KIL_MIN_SAMPLE (default 8) resolved reviews -- the judge is
        crying wolf, managers will start rubber-stamping, or
      - more than HEALTH_KIL_OPEN_MAX (default 12) review tasks have been
        open longer than HEALTH_KIL_OPEN_AGE_H (default 48) -- the queue is
        not being worked.

Exit 0 = healthy, 1 = unhealthy, 2 = could not check.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

STALE_MIN = float(os.environ.get("HEALTH_STALE_MIN", "15"))
FAIL_RATE = float(os.environ.get("HEALTH_FAIL_RATE", "0.5"))
STUB_RATE = float(os.environ.get("HEALTH_STUB_RATE", "0.3"))
NEO4J_STALE_MIN = float(os.environ.get("HEALTH_NEO4J_STALE_MIN", "1560"))  # ~26h
SLACKBOT_STALE_MIN = float(os.environ.get("HEALTH_SLACKBOT_STALE_MIN", "20"))
SLA_BREACH_MAX = int(os.environ.get("HEALTH_SLA_BREACH_MAX", "3"))         # per hour
KIL_WINDOW_DAYS = int(os.environ.get("HEALTH_KIL_WINDOW_DAYS", "7"))
KIL_PRECISION_MIN = float(os.environ.get("HEALTH_KIL_PRECISION_MIN", "0.5"))
KIL_MIN_SAMPLE = int(os.environ.get("HEALTH_KIL_MIN_SAMPLE", "8"))
KIL_OPEN_MAX = int(os.environ.get("HEALTH_KIL_OPEN_MAX", "12"))
KIL_OPEN_AGE_H = float(os.environ.get("HEALTH_KIL_OPEN_AGE_H", "48"))
EXPECT = os.environ.get("HEALTH_COMPONENTS", "worker").split(",")


def _is_stub_run(run: dict) -> bool:
    for e in run.get("trace") or []:
        d = e.get("data") or {}
        if d.get("stub") is True or "stub" in str(e.get("summary", "")).lower():
            return True
        if (d.get("groundedness") or {}).get("backend") == "stub":
            return True
    return bool((run.get("groundedness") or {}).get("backend") == "stub")


def _age(ts, now: datetime) -> timedelta | None:
    try:
        return now - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _kil_problems(sb, now: datetime) -> list[str]:
    """KIL (knowledge-integrity loop) health: is the contradiction judge
    still trustworthy, and is the review queue being worked? Best-effort —
    a missing `review_tasks` table (old DB) yields no problems."""
    out: list[str] = []
    try:
        since = (now - timedelta(days=KIL_WINDOW_DAYS)).isoformat()
        rows = (sb.table("review_tasks").select("status, created_at")
                .gte("created_at", since).limit(4000).execute().data or [])
    except Exception:  # noqa: BLE001
        return out

    n_correct = sum(1 for r in rows if r.get("status") == "correct")
    n_wrong = sum(1 for r in rows if r.get("status") == "wrong")
    real = n_correct + n_wrong
    if real >= KIL_MIN_SAMPLE:
        prec = n_correct / real
        if prec < KIL_PRECISION_MIN:
            out.append(f"kil: flag precision {prec:.0%} over {real} resolved reviews in "
                       f"{KIL_WINDOW_DAYS}d (< {KIL_PRECISION_MIN:.0%}) — the judge is crying wolf")

    stale_open = sum(1 for r in rows if r.get("status") == "open"
                     and (_age(r.get("created_at"), now) or timedelta())
                     > timedelta(hours=KIL_OPEN_AGE_H))
    if stale_open > KIL_OPEN_MAX:
        out.append(f"kil: {stale_open} review tasks open > {KIL_OPEN_AGE_H:.0f}h "
                   f"(> {KIL_OPEN_MAX}) — the queue is not being worked")
    return out


def _check() -> tuple[bool, list[str]]:
    from ingestion.scraper import get_supabase

    sb = get_supabase()
    now = datetime.now(timezone.utc)
    problems: list[str] = []

    rows = sb.table("system_health").select("component,last_healthy_at,detail").execute().data or []
    seen = {r["component"]: r for r in rows}
    for comp in (c.strip() for c in EXPECT if c.strip()):
        r = seen.get(comp)
        if not r:
            problems.append(f"{comp}: no heartbeat ever")
            continue
        try:
            age = (now - datetime.fromisoformat(str(r["last_healthy_at"]).replace("Z", "+00:00")))
        except Exception:  # noqa: BLE001
            problems.append(f"{comp}: unparseable heartbeat")
            continue
        if age > timedelta(minutes=STALE_MIN):
            problems.append(f"{comp}: silent for {age.total_seconds() / 60:.0f} min")

    # a `neo4j` heartbeat that has gone stale = graph sync is silently failing
    nr = seen.get("neo4j")
    if nr and nr.get("last_healthy_at"):
        nage = _age(nr["last_healthy_at"], now)
        if nage and nage > timedelta(minutes=NEO4J_STALE_MIN):
            problems.append(f"neo4j: last sync {nage.total_seconds() / 3600:.0f}h ago")

    # a `slackbot` heartbeat that has gone stale = the Socket-Mode bot dropped,
    # so approval / reasoning-thread replies from Slack no longer reach us.
    sr = seen.get("slackbot")
    if sr and sr.get("last_healthy_at"):
        sage = _age(sr["last_healthy_at"], now)
        if sage and sage > timedelta(minutes=SLACKBOT_STALE_MIN):
            problems.append(f"slackbot: silent for {sage.total_seconds() / 60:.0f} min "
                            "— Slack approvals / replies are not landing")

    since = (now - timedelta(hours=1)).isoformat()
    jobs = (sb.table("jobs").select("status,kind")
            .eq("kind", "run_flow").gte("created_at", since).execute().data or [])
    if jobs:
        failed = sum(1 for j in jobs if j["status"] == "failed")
        rate = failed / len(jobs)
        if rate > FAIL_RATE:
            problems.append(f"jobs: {failed}/{len(jobs)} run_flow failed in the last hour ({rate:.0%})")

    runs = (sb.table("runs").select("trace,groundedness")
            .gte("created_at", since).limit(500).execute().data or [])
    if len(runs) >= 5:
        stub = sum(1 for r in runs if _is_stub_run(r))
        srate = stub / len(runs)
        if srate > STUB_RATE:
            problems.append(f"llm: {stub}/{len(runs)} runs hit the offline stub in the last "
                            f"hour ({srate:.0%}) — every provider is down / rate-limited")

    # Phase 27d — a spike of SLA breaches means the sweep is doing its job but
    # cases aren't getting picked up (Omni offline / no agents / routing broken).
    try:
        br = (sb.table("case_events").select("case_number")
              .eq("action", "breach").gte("ts", since).limit(200).execute().data or [])
        if len(br) > SLA_BREACH_MAX:
            problems.append(f"sla: {len(br)} Case SLA breaches in the last hour "
                            f"(> {SLA_BREACH_MAX}) — cases are not being picked up")
    except Exception:  # noqa: BLE001 — case_events may not exist on an old DB
        pass

    problems += _kil_problems(sb, now)

    return (not problems), problems


def _slack(webhook: str, text: str) -> None:
    import httpx

    try:
        httpx.post(webhook, json={"text": text}, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"slack post failed: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.health_check")
    ap.add_argument("--slack", default=os.environ.get("SLACK_ALERT_WEBHOOK"))
    args = ap.parse_args(argv)

    try:
        ok, problems = _check()
    except Exception as e:  # noqa: BLE001
        print(f"health check failed to run: {e}", file=sys.stderr)
        return 2

    if ok:
        print("healthy")
        return 0

    msg = "🔴 support-automation UNHEALTHY:\n" + "\n".join(f"  • {p}" for p in problems)
    print(msg, file=sys.stderr)
    if args.slack:
        _slack(args.slack, msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
