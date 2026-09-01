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
  * a `neo4j` heartbeat exists but is stale (graph enrichment silently off).

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
EXPECT = os.environ.get("HEALTH_COMPONENTS", "worker").split(",")


def _is_stub_run(run: dict) -> bool:
    for e in run.get("trace") or []:
        d = e.get("data") or {}
        if d.get("stub") is True or "stub" in str(e.get("summary", "")).lower():
            return True
        if (d.get("groundedness") or {}).get("backend") == "stub":
            return True
    return bool((run.get("groundedness") or {}).get("backend") == "stub")


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
        try:
            nage = now - datetime.fromisoformat(str(nr["last_healthy_at"]).replace("Z", "+00:00"))
            if nage > timedelta(minutes=NEO4J_STALE_MIN):
                problems.append(f"neo4j: last sync {nage.total_seconds() / 3600:.0f}h ago")
        except Exception:  # noqa: BLE001
            pass

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
