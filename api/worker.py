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

import hashlib  # noqa: E402

from ingestion.scraper import get_supabase  # noqa: E402
from ingestion.sources.kb_common import embed_entry as _kb_embed  # noqa: E402
from interpreter import feedback, github as githubmod, jobs, salesforce, slack as slackmod  # noqa: E402
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
    final = build_graph(flow).invoke({"case": case, "tenant_id": flow["tenant_id"], "team": flow.get("team"), "trace": []})
    run_id = record_run(flow, final, case=case, source="worker", sb=sb,
                        idempotency_key=key)
    out = {"run_id": run_id, "outcome": (final.get("outcome") or {}).get("action")}
    if case.get("channel") == "email":
        out["email"] = _email_post_run(final, case, flow, sb)
    return out


def _email_post_run(final: dict, case: dict, flow: dict, sb) -> dict:
    """Phase 20c — the hard guard. Decide from the flow's outcome whether a
    customer-facing email goes out; otherwise flag the message for a human.
    Never raises (a delivery failure must not fail/retry the flow run)."""
    from interpreter import emailer, mailbox

    try:
        cfg = mailbox.load_channel(flow["tenant_id"], sb)
        if not cfg:
            return {"skipped": "no email channel"}
        outcome = final.get("outcome") or {}
        kind, meta = emailer.decide(outcome, cfg, final.get("clarification"))
        to = case.get("from") or ""
        subject = case.get("subject") or "your request"
        mid = case.get("message_id") or ""
        refs = case.get("references") or []

        if kind == "send_reply":
            d = emailer.send_reply(cfg, to=to, subject=subject, body=meta["body"],
                                   in_reply_to=mid, references=refs)
            return {"decision": kind, "delivery": d}
        if kind == "send_questions":
            d = emailer.send_reply(cfg, to=to, subject=subject,
                                   body=emailer._questions_body(meta["questions"]),
                                   in_reply_to=mid, references=refs)
            return {"decision": kind, "delivery": d}
        if kind == "needs_human":
            try:
                mailbox.mark_needs_human(cfg, mid)
                flagged = True
            except Exception as e:  # noqa: BLE001
                flagged = f"flag failed: {e}"
            return {"decision": kind, "reason": meta.get("reason"), "flagged": flagged}
        return {"decision": "noop", "reason": meta.get("reason")}
    except Exception as e:  # noqa: BLE001
        log.warning("email post-run failed: %s", e)
        return {"error": str(e)}


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


def _embed_kb_entry(payload: dict, sb) -> dict:
    """Phase 14 — chunk + embed a large KB entry off the request thread."""
    eid = payload["entry_id"]
    rows = sb.table("kb_entries").select("*").eq("entry_id", eid).execute().data
    if not rows or rows[0]["status"] != "active":
        return {"entry_id": eid, "skipped": "entry gone or archived"}
    e = rows[0]
    url = f"kb://{e['source_id']}/{eid}"
    n = _kb_embed(sb, source_id=e["source_id"], url=url, title=e["title"],
                  body_md=e["body_md"] or "", section=payload.get("collection_name", ""))
    sb.table("kb_entries").update({
        "chunk_count": n,
        "embed_hash": hashlib.md5((e["body_md"] or "").encode()).hexdigest(),
        "embedded_at": "now()",
    }).eq("entry_id", eid).execute()
    return {"entry_id": eid, "chunks": n}


def _create_github_issue(payload: dict, sb) -> dict:
    """Phase 16 — a human approved a task_dispatch action in Slack."""
    ar_id = payload["action_request_id"]
    rows = sb.table("action_requests").select("*").eq("id", ar_id).execute().data
    if not rows:
        return {"action_request_id": ar_id, "skipped": "gone"}
    ar = rows[0]
    if ar["status"] not in ("approved",):
        return {"action_request_id": ar_id, "skipped": f"status={ar['status']}"}
    if ar.get("result"):
        return {"action_request_id": ar_id, "idempotent_skip": True, **ar["result"]}

    p = ar["payload"]
    try:
        token = githubmod.token_for(ar["tenant_id"], sb)
        issue = githubmod.create_issue(
            token, p["repo"], title=p["title"], body=p.get("body", ""),
            labels=p.get("labels"), assignees=p.get("assignees"),
        )
    except Exception as e:  # noqa: BLE001
        sb.table("action_requests").update({"status": "error", "error": str(e)[:500]}) \
            .eq("id", ar_id).execute()
        raise

    sb.table("action_requests").update({
        "status": "done", "result": issue,
    }).eq("id", ar_id).execute()
    try:
        if ar.get("slack_channel") and ar.get("slack_ts") and slackmod.available():
            slackmod.update_message(
                ar["tenant_id"], ar["slack_channel"], ar["slack_ts"],
                f":white_check_mark: *{p['title']}* — opened <{issue['html_url']}|"
                f"{p['repo']}#{issue['number']}>", sb,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("slack update after issue failed: %s", e)
    return {"action_request_id": ar_id, **issue}


HANDLERS = {"run_flow": _run_flow, "check_resolution": _check_resolution,
            "embed_kb_entry": _embed_kb_entry, "create_github_issue": _create_github_issue}


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
