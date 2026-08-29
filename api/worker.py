"""
Job worker — claims `jobs` rows and executes them off the request thread.

    python -m api.worker            # loop forever
    python -m api.worker --once     # drain the queue and exit (tests / cron)
    python -m api.worker --once --max 5

Only kind handled today is `run_flow`: {flow_id, case, idempotency_key?} ->
load the published snapshot, invoke, record the run. Idempotent — a run with
the same (flow_id, idempotency_key) already recorded is a no-op success.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from ingestion.scraper import get_supabase  # noqa: E402
from interpreter import feedback, jobs, salesforce  # noqa: E402
from interpreter.builder import build_graph  # noqa: E402
from interpreter.loader import load_flow  # noqa: E402
from interpreter.runs import record_run  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api.worker")


def _run_flow(payload: dict, sb) -> dict:
    flow_id = payload["flow_id"]
    case = payload["case"]
    key = payload.get("idempotency_key")

    if key:
        dup = sb.table("runs").select("run_id").eq("flow_id", flow_id) \
            .eq("idempotency_key", key).execute().data
        if dup:
            return {"run_id": dup[0]["run_id"], "idempotent_skip": True}

    flow = load_flow(flow_id=flow_id, sb=sb, status="published", validate=True)
    final = build_graph(flow).invoke({"case": case, "trace": []})
    run_id = record_run(flow, final, case=case, source="worker", sb=sb,
                        idempotency_key=key)
    return {"run_id": run_id, "outcome": (final.get("outcome") or {}).get("action")}


def _check_resolution(payload: dict, sb) -> dict:
    """Phase 11 — what did the human do with the draft?"""
    run_id = payload["run_id"]
    rows = sb.table("runs").select("case_payload, draft").eq("run_id", run_id).execute().data
    if not rows:
        return {"run_id": run_id, "skipped": "run gone"}
    case = rows[0].get("case_payload") or {}
    case_id = case.get("sf_id") or case.get("id")
    draft = rows[0].get("draft") or ""

    reply = None
    if case_id and salesforce.available():
        reply = feedback.fetch_human_reply(salesforce._client(), case_id)
    action, dist = feedback.classify_edit(draft, reply or "")
    if reply is None:
        dist = None

    sb.table("runs").update({
        "human_action": action,
        "human_reply": (reply or "")[:8000] or None,
        "edit_distance": dist,
        "feedback_checked_at": "now()",
    }).eq("run_id", run_id).execute()
    return {"run_id": run_id, "human_action": action, "edit_distance": dist}


HANDLERS = {"run_flow": _run_flow, "check_resolution": _check_resolution}


def process_one(sb) -> bool:
    job = jobs.claim(sb=sb)
    if not job:
        return False
    jid, kind = job["job_id"], job["kind"]
    try:
        handler = HANDLERS.get(kind)
        if not handler:
            raise ValueError(f"no handler for job kind {kind!r}")
        result = handler(job["payload"], sb)
        jobs.complete(jid, result, sb=sb)
        log.info("job %s (%s) done: %s", jid, kind, result)
    except Exception as e:  # noqa: BLE001
        jobs.fail(jid, f"{type(e).__name__}: {e}", sb=sb)
        log.warning("job %s (%s) failed: %s", jid, kind, e)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="api.worker")
    ap.add_argument("--once", action="store_true", help="drain the queue and exit")
    ap.add_argument("--max", type=int, default=1000, help="max jobs when --once")
    ap.add_argument("--idle-sleep", type=float, default=2.0)
    args = ap.parse_args(argv)

    sb = get_supabase()
    if args.once:
        n = 0
        while n < args.max and process_one(sb):
            n += 1
        log.info("drained %d job(s)", n)
        return 0

    log.info("worker started; polling every %.1fs", args.idle_sleep)
    while True:
        if not process_one(sb):
            time.sleep(args.idle_sleep)


if __name__ == "__main__":
    sys.exit(main())
