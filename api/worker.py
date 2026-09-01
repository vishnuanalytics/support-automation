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
import os
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
    trigger = payload.get("trigger")

    # Cases pushed by the Salesforce trigger arrive as a bare id — hydrate
    # the full record here (raises -> the job retries with backoff).
    if case.get("sf_id") and not case.get("subject"):
        case = {**salesforce.get_case(case["sf_id"]), "channel": case.get("channel", "salesforce")}
        # An email-origin Case (E2C or a customer reply) has an *incoming*
        # EmailMessage. Overlay it so the flow triages what the customer
        # actually said, not the stale Case Description — for BOTH the
        # `case_created` and the `inbound_email` CDC events, so a new
        # Email-to-Case Case still gets a real reply on the first pass.
        if trigger in ("inbound_email", "case_created"):
            m = salesforce.latest_inbound_email(case["sf_id"])
            if m and m.get("text"):
                case.update(body=m["text"], channel="email",
                            subject=m.get("subject") or case.get("subject"))
                if m.get("from_addr"):
                    case["from"] = m["from_addr"]
                if m.get("message_id"):
                    case["message_id"] = m["message_id"]
                # collapse the CaseChangeEvent + EmailMessageChangeEvent for
                # the same mail onto one run: `plan_events` keys the inbound
                # spec on the EmailMessage *record id*, so match that.
                if m.get("id"):
                    key = f"email:{m['id']}"

    if key:
        dup = sb.table("runs").select("run_id").eq("flow_id", flow_id) \
            .eq("idempotency_key", key).execute().data
        if dup:
            return {"run_id": dup[0]["run_id"], "idempotent_skip": True}

    flow = load_flow(flow_id=flow_id, sb=sb, status="published", validate=True)
    final = build_graph(flow).invoke({"case": case, "tenant_id": flow["tenant_id"], "team": flow.get("team"), "trace": []})
    # nodes like `sf_case` mutate the case in-flight (add sf_id, refresh the
    # account tier) — persist / act on that, not the pre-run input.
    run_case = final.get("case") or case
    run_id = record_run(flow, final, case=run_case, source="worker", sb=sb,
                        idempotency_key=key)
    out = {"run_id": run_id, "outcome": (final.get("outcome") or {}).get("action")}
    if run_case.get("channel") == "email":
        out["email"] = _email_post_run(final, run_case, flow, sb)
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
        sf_id = case.get("sf_id") or case.get("id")

        def _deliver(body: str) -> dict:
            # FR-12: the reply goes out over SMTP from the support mailbox —
            # that is what actually lands in the customer's inbox. A
            # Salesforce API send (`emailSimple`) needs org-wide
            # deliverability = "All email" + an Org-Wide Email Address and
            # silently drops the message otherwise (it returned sent=True
            # while nothing was delivered). Salesforce stays the source of
            # truth for the Case; Gmail just carries the message.
            r = emailer.send_reply(cfg, to=to, subject=subject, body=body,
                                   in_reply_to=mid, references=refs)
            # Best-effort: mirror the sent reply onto the Case as an outbound
            # EmailMessage so agents see the full thread in Salesforce.
            # Never let this fail (or retry) the delivery.
            if r.get("sent") and sf_id and salesforce.available():
                try:
                    em = salesforce.log_email_message(
                        sf_id, incoming=False, status=salesforce._EM_SENT,
                        from_addr=cfg.send_from, from_name=cfg.from_name or "",
                        to_addrs=to, subject=emailer._subject_reply(subject),
                        body=body, message_id=r.get("message_id") or "",
                        tenant_id=flow["tenant_id"],
                    )
                    r["case_email"] = em.get("id") or em.get("error") or "logged"
                except Exception as e:  # noqa: BLE001
                    r["case_email"] = f"log failed: {e}"
            return r

        if kind == "send_reply":
            return {"decision": kind, "delivery": _deliver(meta["body"])}
        if kind == "send_questions":
            return {"decision": kind,
                    "delivery": _deliver(emailer._questions_body(meta["questions"]))}
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


_FEEDBACK_POLL_MIN = int(os.environ.get("FEEDBACK_POLL_MIN", "5"))
_FEEDBACK_MAX_CHECKS = int(os.environ.get("FEEDBACK_MAX_CHECKS", "12"))


def _check_resolution(payload: dict, sb) -> dict:
    """After an `ask_human` / `handover` on a real Case, poll for what the
    human did (Phase 11) — and *act on it* (Phase 20m):

      * an agent left a CaseComment  -> treat it as the answer: polish it
        into a customer reply and send it (`interpreter.agent_reply`), mark
        the run `guided_resume`, record the resume as its own run.
      * the agent emailed the customer directly -> just score the draft
        (`sent_as_is` / `edited` / `rewrote`).
      * nothing yet -> re-poll every FEEDBACK_POLL_MIN up to
        FEEDBACK_MAX_CHECKS, then give up as `no_reply`.
    """
    run_id = payload["run_id"]
    checks = int(payload.get("checks", 0))
    rows = (sb.table("runs")
            .select("case_payload, draft, created_at, human_action, tenant_id, flow_id, team")
            .eq("run_id", run_id).execute().data)
    if not rows:
        return {"run_id": run_id, "skipped": "run gone"}
    row = rows[0]
    if row.get("human_action") not in (None, "pending"):
        return {"run_id": run_id, "skipped": f"already {row['human_action']}"}

    case = row.get("case_payload") or {}
    case_id = case.get("sf_id") or case.get("id")
    draft = row.get("draft") or ""
    tenant_id = row.get("tenant_id")
    since = row.get("created_at")

    resp = {"guidance": None, "guidance_at": None, "outbound_email": None}
    if case_id and salesforce.available():
        resp = salesforce.agent_response_since(case_id, since, tenant_id=tenant_id)

    # 1. an agent left internal guidance -> the bot composes + sends the reply
    if resp.get("guidance"):
        from interpreter import agent_reply, mailbox

        cfg = mailbox.load_channel(tenant_id, sb) if tenant_id else None
        out = agent_reply.resume_from_guidance(case, resp["guidance"], cfg=cfg,
                                               tenant_id=tenant_id, draft=draft)
        try:
            sb.table("runs").insert({
                "flow_id": row["flow_id"], "tenant_id": tenant_id, "team": row["team"],
                "source": "agent_resume", "case_id": (str(case_id)[:200] if case_id else None),
                "subject": (str(case.get("subject") or "")[:500] or None),
                "outcome": "auto_reply" if out.get("auto_sent") else "draft",
                "draft": out.get("reply"), "case_payload": case,
            }).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("could not record agent_resume run for %s: %s", run_id, e)
        sb.table("runs").update({
            "human_action": "guided_resume",
            "human_reply": resp["guidance"][:8000],
            "feedback_checked_at": "now()",
        }).eq("run_id", run_id).execute()
        return {"run_id": run_id, "human_action": "guided_resume", "resume": out}

    # 2. the agent already replied to the customer -> just score the draft
    if resp.get("outbound_email"):
        action, dist = feedback.classify_edit(draft, resp["outbound_email"])
        sb.table("runs").update({
            "human_action": action, "human_reply": resp["outbound_email"][:8000],
            "edit_distance": dist, "feedback_checked_at": "now()",
        }).eq("run_id", run_id).execute()
        return {"run_id": run_id, "human_action": action, "edit_distance": dist}

    # 3. nothing yet -> re-poll, or give up
    if checks + 1 < _FEEDBACK_MAX_CHECKS:
        import datetime as _dt
        nxt = (_dt.datetime.now(_dt.timezone.utc)
               + _dt.timedelta(minutes=_FEEDBACK_POLL_MIN)).isoformat()
        jobs.enqueue("check_resolution", {"run_id": run_id, "checks": checks + 1},
                     dedupe_key=f"{run_id}:{checks + 1}", run_after=nxt, sb=sb)
        return {"run_id": run_id, "waiting": True, "checks": checks + 1}

    sb.table("runs").update({
        "human_action": "no_reply", "feedback_checked_at": "now()",
    }).eq("run_id", run_id).execute()
    return {"run_id": run_id, "human_action": "no_reply"}


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

JOB_TIMEOUT = int(os.environ.get("WORKER_JOB_TIMEOUT", "120"))


class _JobTimeout(Exception):
    pass


def process_one(sb) -> bool:
    import signal

    job = jobs.claim(sb=sb)
    if not job:
        return False
    jid, kind = job["job_id"], job["kind"]

    def _alarm(_sig, _frm):
        raise _JobTimeout(f"job exceeded {JOB_TIMEOUT}s")

    have_alarm = hasattr(signal, "SIGALRM")
    if have_alarm:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(JOB_TIMEOUT)
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
    finally:
        if have_alarm:
            signal.alarm(0)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="api.worker")
    ap.add_argument("--once", action="store_true", help="drain the queue and exit")
    ap.add_argument("--max", type=int, default=1000, help="max jobs when --once")
    ap.add_argument("--idle-sleep", type=float, default=2.0)
    args = ap.parse_args(argv)

    from interpreter.config import validate_env
    validate_env()

    sb = get_supabase()
    if args.once:
        n = 0
        while n < args.max and process_one(sb):
            n += 1
        log.info("drained %d job(s)", n)
        return 0

    from interpreter.health import beat

    log.info("worker started; polling every %.1fs", args.idle_sleep)
    beat("worker", {"pid": os.getpid()}, sb=sb, force=True)
    while True:
        did = process_one(sb)
        beat("worker", {"idle": not did}, sb=sb)
        if not did:
            time.sleep(args.idle_sleep)


if __name__ == "__main__":
    sys.exit(main())
