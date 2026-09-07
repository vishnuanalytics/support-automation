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
from interpreter.builder import build_graph, initial_state  # noqa: E402
from interpreter.loader import load_flow  # noqa: E402
from interpreter.runs import record_run  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api.worker")


def _run_flow(payload: dict, sb) -> dict:
    flow_id = payload["flow_id"]
    case = payload.get("case") or {}
    context = payload.get("context") or {}      # P5b — generic (webhook/trigger) run
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
    final = build_graph(flow).invoke(initial_state(flow, case=case, context=context))
    # nodes like `sf_case` mutate the case in-flight (add sf_id, refresh the
    # account tier) — persist / act on that, not the pre-run input.
    run_case = final.get("case") or case
    run_id = record_run(flow, final, case=run_case, source="worker", sb=sb,
                        idempotency_key=key)
    out = {"run_id": run_id, "outcome": (final.get("outcome") or {}).get("action")}
    if run_case.get("channel") == "email":
        out["email"] = _email_post_run(final, run_case, flow, sb)
    elif run_case.get("channel") == "freshchat":
        out["freshchat"] = _freshchat_post_run(final, run_case, flow, sb)
    elif run_case.get("channel") == "zendesk":
        out["zendesk"] = _zendesk_post_run(final, run_case, flow, sb)
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
            # log_email_message() resolves this tenant's own creds itself
            # (client_for, not the env-only available()) -- gating on
            # available() here used to skip it for a self-serve tenant with
            # no env creds even though the call underneath would work.
            if r.get("sent") and sf_id:
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


def _freshchat_post_run(final: dict, case: dict, flow: dict, sb) -> dict:
    """Multi-provider connectors step 3 — mirrors `_email_post_run`'s guard
    (reuses `emailer.decide`, which is genuinely channel-agnostic: it only
    reads `outcome`/`cfg.auto_send_enabled`/`clarification`), delivering
    into the Freshchat conversation instead of over SMTP. Also keeps
    `channel_threads` (migration 085) fresh regardless of delivery outcome,
    so the next message on this conversation still attaches to the same
    case even on a run that didn't send anything (e.g. `ask_human`). Never
    raises — a delivery failure must not fail/retry the flow run."""
    from interpreter import channel_threads, emailer, freshchat

    try:
        cfg = freshchat.load_channel(flow["tenant_id"], sb)
        if not cfg:
            return {"skipped": "no freshchat channel"}
        conv_id = case.get("conversation_id")
        sf_id = case.get("sf_id") or case.get("id")
        if conv_id and sf_id:
            channel_threads.link(flow["tenant_id"], freshchat.KIND, conv_id,
                                 case_ref=sf_id, case_number=case.get("case_number"), sb=sb)
        if not conv_id:
            return {"decision": "noop", "reason": "no conversation_id on case"}

        outcome = final.get("outcome") or {}
        kind, meta = emailer.decide(outcome, cfg, final.get("clarification"))
        if kind == "send_reply":
            return {"decision": kind, "delivery": freshchat.send_message(cfg, conv_id, meta["body"])}
        if kind == "send_questions":
            numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(meta["questions"]))
            body = f"To help you with this, could you share:\n\n{numbered}\n\nOnce we have that we'll follow up."
            return {"decision": kind, "delivery": freshchat.send_message(cfg, conv_id, body)}
        return {"decision": kind, "reason": meta.get("reason")}
    except Exception as e:  # noqa: BLE001
        log.warning("freshchat post-run failed: %s", e)
        return {"error": str(e)}


def _zendesk_post_run(final: dict, case: dict, flow: dict, sb) -> dict:
    """Phase 31 chunk 2 — deliver a `channel=zendesk` run's outcome as a
    public ticket comment, mirroring `_freshchat_post_run`. `emailer.decide`
    is channel-agnostic (reads only `outcome` / `cfg.auto_send_enabled` /
    `clarification`), so an `auto_reply` only sends when the tenant has
    turned auto-send on for the Zendesk connection; otherwise it's flagged
    for a human. Never raises."""
    from interpreter import emailer, zendesk

    try:
        cfg = zendesk.load_channel(flow["tenant_id"], sb)
        if not cfg:
            return {"skipped": "no zendesk connection"}
        ticket_id = case.get("sf_id") or case.get("id")
        if not ticket_id:
            return {"decision": "noop", "reason": "no ticket id on case"}

        outcome = final.get("outcome") or {}
        kind, meta = emailer.decide(outcome, cfg, final.get("clarification"))
        recipient = case.get("from")
        if kind == "send_reply":
            return {"decision": kind, "delivery": zendesk.send_case_reply(
                ticket_id, meta["body"], to_email=recipient,
                tenant_id=flow["tenant_id"], sb=sb)}
        if kind == "send_questions":
            numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(meta["questions"]))
            body = (f"To help you with this, could you share:\n\n{numbered}\n\n"
                    "Once we have that we'll follow up.")
            return {"decision": kind, "delivery": zendesk.send_case_reply(
                ticket_id, body, to_email=recipient, tenant_id=flow["tenant_id"], sb=sb)}
        return {"decision": kind, "reason": meta.get("reason")}
    except Exception as e:  # noqa: BLE001
        log.warning("zendesk post-run failed: %s", e)
        return {"error": str(e)}


_FEEDBACK_POLL_MIN = int(os.environ.get("FEEDBACK_POLL_MIN", "5"))
_FEEDBACK_MAX_CHECKS = int(os.environ.get("FEEDBACK_MAX_CHECKS", "12"))


def _check_resolution(payload: dict, sb) -> dict:
    """Telemetry-only follow-up on an escalated Case that has **no** Slack
    reasoning session (the normal path is the Slack dialogue — Phase 24). It
    never sends: it just records what the human did.

      * a human left a CaseComment -> logged as context on the run.
      * the agent emailed the customer directly -> score the draft
        (`sent_as_is` / `edited` / `rewrote`).
      * nothing -> re-poll up to FEEDBACK_MAX_CHECKS, then `human_handling`
        (a human engaged) or `no_reply`.
    """
    run_id = payload["run_id"]
    checks = int(payload.get("checks", 0))
    rows = (sb.table("runs")
            .select("run_id, case_payload, draft, created_at, human_action, human_reply, "
                    "tenant_id, flow_id, team, retrieval, trace")
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
    if case_id:
        resp = salesforce.agent_response_since(case_id, since, tenant_id=tenant_id)

    # a human left a note -> record it as context on the run; never send.
    note_seen = False
    if resp.get("guidance"):
        note_seen = True
        prev = row.get("human_reply") or ""
        if resp["guidance"][:1500] not in prev:
            merged = (prev + ("\n---\n" if prev else "") + resp["guidance"])[:8000]
            sb.table("runs").update({
                "human_reply": merged, "feedback_checked_at": "now()",
            }).eq("run_id", run_id).execute()

    # 2. the agent already replied to the customer -> just score the draft
    if resp.get("outbound_email"):
        action, dist = feedback.classify_edit(draft, resp["outbound_email"])
        sb.table("runs").update({
            "human_action": action, "human_reply": resp["outbound_email"][:8000],
            "edit_distance": dist, "feedback_checked_at": "now()",
        }).eq("run_id", run_id).execute()
        # KIL-c — check the reply the agent sent against the KB + case history.
        try:
            from interpreter import review
            review.judge_human_reply(sb, run_row=row, reply_text=resp["outbound_email"])
        except Exception as e:  # noqa: BLE001
            log.warning("review.judge_human_reply for %s failed: %s", run_id, e)
        return {"run_id": run_id, "human_action": action, "edit_distance": dist}

    # 3. nothing actionable yet -> re-poll, or give up
    if checks + 1 < _FEEDBACK_MAX_CHECKS:
        import datetime as _dt
        nxt = (_dt.datetime.now(_dt.timezone.utc)
               + _dt.timedelta(minutes=_FEEDBACK_POLL_MIN)).isoformat()
        jobs.enqueue("check_resolution", {"run_id": run_id, "checks": checks + 1},
                     dedupe_key=f"{run_id}:{checks + 1}", run_after=nxt, sb=sb)
        return {"run_id": run_id, "waiting": True, "checks": checks + 1,
                "note_seen": note_seen}

    # give up: a human clearly engaged (left notes, or owns the Case) ->
    # `human_handling` (not a miss); otherwise a genuine `no_reply`.
    engaged = note_seen or _case_owned_by_user(case_id, tenant_id)
    final_action = "human_handling" if engaged else "no_reply"
    sb.table("runs").update({
        "human_action": final_action, "feedback_checked_at": "now()",
    }).eq("run_id", run_id).execute()

    # Phase 27 — a genuinely dropped escalation: flip SLA_Breach__c + page,
    # so it isn't just a no_reply row nobody watches.
    if final_action == "no_reply" and str(case_id or "").startswith("500"):
        try:
            from interpreter import case_events, sweeps

            salesforce.update_case_fields(case_id, {"SLA_Breach__c": True},
                                          tenant_id=tenant_id)
            sweeps._page(f":rotating_light: *Dropped escalation* — Case {case_id} had no "
                         f"human response after {_FEEDBACK_MAX_CHECKS} checks. SLA_Breach set.",
                         sb=sb)
            case_events.record(sb, tenant_id=tenant_id, case_sf_id=str(case_id),
                               case_number=(case.get("case_number")),
                               actor="system:sweep", action="breach",
                               reason="check_resolution gave up — no human response")
        except Exception as e:  # noqa: BLE001
            log.warning("check_resolution SLA_Breach for %s failed: %s", case_id, e)

    return {"run_id": run_id, "human_action": final_action}


def _case_owned_by_user(case_id: str | None, tenant_id: str | None) -> bool:
    """True when a person (not a queue) owns the Case — they've taken it."""
    if not case_id:
        return False
    sf = salesforce._try_client(tenant_id)
    if sf is None:
        return False
    try:
        owner = sf.Case.get(case_id).get("OwnerId") or ""
        return owner.startswith("005")   # 005 = User, 00G = Queue/Group
    except Exception as e:  # noqa: BLE001
        log.warning("owner check failed for %s: %s", case_id, e)
        return False


def _embed_kb_entry(payload: dict, sb) -> dict:
    """Phase 14 — chunk + embed a large KB entry off the request thread."""
    eid = payload["entry_id"]
    rows = sb.table("kb_entries").select("*").eq("entry_id", eid).execute().data
    if not rows or rows[0]["status"] not in ("active", "provisional"):
        return {"entry_id": eid, "skipped": "entry gone or archived"}
    e = rows[0]
    url = f"kb://{e['source_id']}/{eid}"
    n = _kb_embed(sb, source_id=e["source_id"], url=url, title=e["title"],
                  body_md=e["body_md"] or "", section=payload.get("collection_name", ""),
                  status=e.get("status", "active"))
    sb.table("kb_entries").update({
        "chunk_count": n,
        "embed_hash": hashlib.md5((e["body_md"] or "").encode()).hexdigest(),
        "embedded_at": "now()",
    }).eq("entry_id", eid).execute()
    return {"entry_id": eid, "chunks": n}


def _sync_kb_connection(payload: dict, sb) -> dict:
    """Generic KB-source sync driver (docs/KB_SOURCE_CONNECTORS.md).

    One code path for every connector: ask the `KBConnectorSpec`'s `sync()`
    for a list of `KBDocument`s, then do the diff/upsert/embed/archive dance
    that `_crawl_site` and `_sync_gsheet` each used to hand-roll:

      * skip the update + re-embed for a document whose `body_md` is
        byte-identical to what's stored (a re-sync of unchanged content
        costs only the fetch), same discipline as `llm.complete(cache=True)`;
      * embed off this job (fastembed × N docs would blow JOB_TIMEOUT);
      * archive (soft-delete, never hard-delete) an entry whose document
        vanished from the source — but *only* when `res.exhaustive` (a crawl
        truncated by `max_pages` saw only part of the source, so a missing
        page may just be outside this run's budget);
      * record `status` / `last_synced_at` / `last_result` / `watermark` on
        the connection row for the UI + future incremental sync.

    Adding Linear / Discourse / Nolt is a new `KBConnectorSpec` — this
    handler doesn't change.
    """
    from interpreter.kb_connectors import SyncCtx, get_kb_connector

    cid = payload["connection_id"]
    rows = (sb.table("kb_source_connections").select("*")
            .eq("connection_id", cid).execute().data or [])
    if not rows:
        return {"connection_id": cid, "skipped": "connection gone"}
    conn = rows[0]
    if conn["status"] not in ("active", "error"):
        return {"connection_id": cid, "skipped": f"status={conn['status']}"}

    sid, tid = conn["source_id"], conn["tenant_id"]
    col_name = payload.get("collection_name") or ""
    if not col_name:
        s = sb.table("sources").select("name").eq("source_id", sid).limit(1).execute().data
        col_name = (s[0]["name"] if s else "")

    existing = {
        r["external_id"]: r for r in (
            sb.table("kb_entries").select("entry_id, external_id, body_md, gdoc_modified")
            .eq("connection_id", cid).eq("status", "active").execute().data or [])
        if r.get("external_id") is not None
    }

    try:
        spec = get_kb_connector(conn["connector"])
        ctx = SyncCtx(tenant_id=tid, sb=sb, collection_name=col_name, existing=existing)
        res = spec.sync(conn.get("config") or {}, conn.get("watermark"), ctx)
    except Exception as e:  # noqa: BLE001
        err = str(e)[:500]
        sb.table("kb_source_connections").update({
            "status": "error", "last_synced_at": "now()", "last_result": {"error": err},
        }).eq("connection_id", cid).execute()
        return {"connection_id": cid, "error": err}

    made = skipped = 0
    seen: set[str] = set()
    for doc in res.documents:
        try:
            seen.add(doc.external_id)
            prev = existing.get(doc.external_id)
            if prev and prev["body_md"] == doc.body_md:
                skipped += 1
                continue
            row = {
                "source_id": sid, "tenant_id": tid, "connection_id": cid,
                "external_id": doc.external_id, "title": doc.title, "body_md": doc.body_md,
                "origin": doc.origin, "status": "active", "quality": doc.quality,
                "created_by": conn.get("created_by"), "updated_by": conn.get("created_by"),
                **(doc.extra or {}),
            }
            if prev:
                entry = (sb.table("kb_entries").update(row)
                         .eq("entry_id", prev["entry_id"]).execute().data[0])
            else:
                entry = sb.table("kb_entries").insert(row).execute().data[0]
            jobs.enqueue("embed_kb_entry",
                         {"entry_id": entry["entry_id"], "source_id": sid,
                          "collection_name": col_name},
                         dedupe_key=f"embed:{entry['entry_id']}", sb=sb)
            made += 1
        except Exception as e:  # noqa: BLE001
            log.warning("kb_sync %s doc %s: %s", cid, doc.external_id, e)

    archived = 0
    if res.exhaustive:
        for ext_id, r in existing.items():
            if ext_id in seen:
                continue
            try:
                sb.table("kb_entries").update({"status": "archived"}) \
                    .eq("entry_id", r["entry_id"]).execute()
                archived += 1
            except Exception as e:  # noqa: BLE001
                log.warning("kb_sync %s archive %s: %s", cid, r["entry_id"], e)

    result = {"documents": len(res.documents), "entries": made,
              "unchanged": skipped, "archived": archived}
    sb.table("kb_source_connections").update({
        "status": "active", "last_synced_at": "now()", "last_result": result,
        "watermark": res.watermark if res.watermark is not None else conn.get("watermark"),
    }).eq("connection_id", cid).execute()
    return {"connection_id": cid, **result}


def _find_or_create_kb_connection(sb, *, source_id, tenant_id, connector, config,
                                  label, created_by=None) -> dict:
    """Back-compat glue for the deprecated `crawl_site` / `sync_gsheet` shims:
    resolve the `kb_source_connections` row a legacy payload targets (matched
    on the connector's identity field), creating it if the pre-091 backfill
    didn't already."""
    key = "url" if connector == "public_url" else "sheet_id"
    for r in (sb.table("kb_source_connections").select("*")
              .eq("source_id", source_id).eq("connector", connector)
              .neq("status", "archived").execute().data or []):
        if (r.get("config") or {}).get(key) == config.get(key):
            return r
    return sb.table("kb_source_connections").insert({
        "source_id": source_id, "tenant_id": tenant_id, "connector": connector,
        "config": config, "label": label, "created_by": created_by,
    }).execute().data[0]


def _crawl_site(payload: dict, sb) -> dict:
    """Deprecated shim — `crawl_site` is now `kb_sync` over a `public_url`
    `kb_source_connections` row. Kept so already-queued jobs and any old
    caller keep working; remove a release after 091."""
    conn = _find_or_create_kb_connection(
        sb, source_id=payload["source_id"], tenant_id=payload["tenant_id"],
        connector="public_url", label=payload["url"], created_by=payload.get("created_by"),
        config={"url": payload["url"], "max_pages": int(payload.get("max_pages", 20))})
    return _sync_kb_connection(
        {"connection_id": conn["connection_id"],
         "collection_name": payload.get("collection_name", "")}, sb)


def _sync_gsheet(payload: dict, sb) -> dict:
    """Deprecated shim — see `_crawl_site`. Now `kb_sync` over a `gsheets` row."""
    conn = _find_or_create_kb_connection(
        sb, source_id=payload["source_id"], tenant_id=payload["tenant_id"],
        connector="gsheets", label=payload["sheet_id"], created_by=payload.get("created_by"),
        config={"sheet_id": payload["sheet_id"], "sheet_name": payload.get("sheet_name")})
    return _sync_kb_connection(
        {"connection_id": conn["connection_id"],
         "collection_name": payload.get("collection_name", "")}, sb)


def _writeback_issue_body(doc_url: str, blocks: list[dict], task: str | None,
                          approver: str, conflict: bool, mode: str) -> str:
    suggest = mode == "suggest"
    verb = "correction to apply" if suggest else "write-back"
    lines = [f"KB {verb} for [the Google Doc]({doc_url}).", ""]
    if task:
        lines.append(f"Source review task: `{task}`")
    lines += [f"Approved by: {approver}", ""]
    if conflict and not suggest:
        lines += ["> ⚠️ The doc changed since this correction was drafted — "
                  "**no edit was applied**. Reconcile the blocks below by hand.", ""]
    elif suggest:
        lines += ["The bot did **not** edit the doc. Apply the change below, then "
                  "**close this issue** — the KB mirror re-syncs from the doc on close.", ""]
    for i, b in enumerate(blocks, 1):
        if suggest:
            state = "apply this"
        else:
            state = "applied" if b.get("applied") else "NOT applied — needs a manual edit"
        lines += [f"### Block {i} — {state}", "", "**Was:**", "```",
                  (b.get("old") or "(new paragraph — nothing to match)"), "```",
                  "**Now:**", "```", (b.get("new") or "(removed)"), "```", ""]
    if not suggest:
        lines.append("_Verify the doc, then **close this issue** to confirm. "
                     "Comment `/revert` to request a rollback._")
    return "\n".join(lines)


def _gdoc_writeback(payload: dict, sb) -> dict:
    """KB write-back (docs/KB_SOURCE_CONNECTORS.md §2). A KIL correction was
    approved for an entry on a gdocs connection whose `config.on_correction` is:

      * "suggest"    — open a GitHub issue with the old→new diff + a doc link;
                       the bot does NOT touch the doc, a human applies it and
                       closes the issue (the mirror re-syncs on close). Default
                       for a tenant that wants doc corrections.
      * "write_back" — the bot rewrites the passage in place, opens the issue
                       for the human to verify / `/revert`, re-syncs the mirror.

    All steps best-effort, recorded on a `kb_doc_writebacks` row."""
    from interpreter import gdrive, github as gh

    cid, tid = payload["connection_id"], payload["tenant_id"]
    rows = (sb.table("kb_source_connections").select("*")
            .eq("connection_id", cid).execute().data or [])
    if not rows:
        return {"connection_id": cid, "skipped": "connection gone"}
    cfg = rows[0].get("config") or {}
    mode = payload.get("mode") or cfg.get("on_correction") or cfg.get("access") or "write_back"
    suggest = mode == "suggest"
    doc_id = cfg.get("doc_id")
    doc_url = cfg.get("doc_url") or f"https://docs.google.com/document/d/{doc_id}/edit"
    repo = payload.get("github_repo") or cfg.get("github_repo")
    blocks = list(payload.get("blocks") or [])
    approver = payload.get("approver") or "a manager"
    task = payload.get("review_task_id")

    track: dict = {"tenant_id": tid, "connection_id": cid, "entry_id": payload.get("entry_id"),
                   "review_task_id": task, "github_repo": repo}

    try:
        fetched = gdrive.fetch_doc(tid, doc_id, sb)
    except Exception as e:  # noqa: BLE001
        track.update({"status": "error", "error": str(e)[:500], "blocks": blocks})
        sb.table("kb_doc_writebacks").insert(track).execute()
        return {"connection_id": cid, "error": str(e)[:300]}

    track["pre_edit_markdown"] = fetched.get("markdown")
    conflict = bool(payload.get("old_modified")
                    and fetched.get("modified_time") != payload["old_modified"])

    if suggest:
        applied = [{**b, "applied": False} for b in blocks]
        status = "suggested"
    elif conflict:
        applied = [{**b, "applied": False} for b in blocks]
        status = "conflict"
    else:
        applied = []
        for b in blocks:
            n = 0
            if (b.get("old") or "").strip():
                try:
                    n = gdrive.replace_passage(tid, doc_id, b["old"], b.get("new", ""), sb)
                except Exception as e:  # noqa: BLE001
                    log.warning("gdoc_writeback replace_passage: %s", e)
            applied.append({**b, "applied": bool(n)})
        done = sum(1 for b in applied if b["applied"])
        status = "applied" if done == len(applied) else ("partial" if done else "conflict")

    track.update({"blocks": applied, "status": status})

    issue = None
    if repo:
        try:
            issue = gh.create_issue(
                gh.token_for(tid, sb), repo,
                title=f"KB {'correction to apply' if suggest else 'write-back'}: "
                      f"{fetched.get('title') or doc_id}",
                body=_writeback_issue_body(doc_url, applied, task, approver, conflict, mode),
                labels=["kb-writeback"])
            track["github_issue_number"] = issue["number"]
            track["github_issue_url"] = issue["html_url"]
        except Exception as e:  # noqa: BLE001
            log.warning("gdoc_writeback github issue: %s", e)

    where = issue["html_url"] if issue else "(no GitHub repo configured)"
    if suggest:
        gdrive.comment(tid, doc_id,
                       f"A KB correction is proposed for this doc"
                       f"{f' (review {task})' if task else ''}, approved by {approver} — "
                       f"see {where}. Apply it and close the issue.", sb)
    elif status in ("applied", "partial"):
        gdrive.comment(tid, doc_id,
                       f"Automated KB write-back applied from a support resolution"
                       f"{f' (review {task})' if task else ''}, approved by {approver}. "
                       f"Please verify and close {where} to confirm, or comment /revert.", sb)
        jobs.enqueue("kb_sync", {"connection_id": cid}, dedupe_key=f"kb_sync:{cid}", sb=sb)

    sb.table("kb_doc_writebacks").insert(track).execute()
    return {"connection_id": cid, "status": status, "mode": mode, "blocks": len(applied),
            "issue": issue["html_url"] if issue else None}


def _import_kb_bundle(payload: dict, sb) -> dict:
    """Phase 28 step 6 — bulk-restore entries from an export bundle. Same
    shape as _crawl_site: upsert-by-title, embed off this job (fastembed x N
    entries would blow JOB_TIMEOUT)."""
    sid, tid = payload["source_id"], payload["tenant_id"]
    col_name = payload.get("collection_name", "")
    made = 0
    for entry_in in payload.get("entries", []):
        try:
            existing = (sb.table("kb_entries").select("entry_id")
                        .eq("source_id", sid).eq("title", entry_in["title"])
                        .eq("origin", "import").limit(1).execute().data or [])
            row = {"source_id": sid, "tenant_id": tid, "title": entry_in["title"],
                   "body_md": entry_in["body_md"], "origin": "import",
                   "created_by": payload.get("created_by"), "updated_by": payload.get("created_by")}
            if existing:
                entry = (sb.table("kb_entries").update(row)
                         .eq("entry_id", existing[0]["entry_id"]).execute().data[0])
            else:
                entry = sb.table("kb_entries").insert(row).execute().data[0]
            jobs.enqueue("embed_kb_entry",
                         {"entry_id": entry["entry_id"], "source_id": sid,
                          "collection_name": col_name},
                         dedupe_key=f"embed:{entry['entry_id']}", sb=sb)
            made += 1
        except Exception as e:  # noqa: BLE001
            log.warning("import_kb_bundle entry %r: %s", entry_in.get("title"), e)
    return {"source_id": sid, "requested": len(payload.get("entries", [])), "entries": made}


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


def _apply_kb_change(payload: dict, sb) -> dict:
    """KIL-d — a manager approved a KB update in Slack; write it (provisional)."""
    from interpreter import kb_writeback

    ar_id = payload["action_request_id"]
    rows = sb.table("action_requests").select("*").eq("id", ar_id).execute().data
    if not rows:
        return {"action_request_id": ar_id, "skipped": "gone"}
    ar = rows[0]
    if ar["kind"] != "kb_change":
        return {"action_request_id": ar_id, "skipped": f"kind={ar['kind']}"}
    res = kb_writeback.apply_kb_change(sb, ar)
    try:
        if ar.get("slack_channel") and ar.get("slack_ts") and slackmod.available():
            slackmod.update_message(
                ar["tenant_id"], ar["slack_channel"], ar["slack_ts"],
                f":books: KB updated (provisional) — {ar['payload'].get('title', '')}", sb)
    except Exception as e:  # noqa: BLE001
        log.warning("slack update after kb change failed: %s", e)
    return {"action_request_id": ar_id, **res}


# ── Phase 27d — the case-control-plane safety-net sweeps ──────────────────
_SWEEP_EVERY_MIN = {"queue_sweep": 5, "cdc_reconcile": 60, "reasoning_ttl": 5,
                    "handoff_watch": 5, "kb_promote": 360,
                    "case_graph_sync": 60, "case_memory_sync": 60,
                    "fire_schedules": 1, "kil_digest": 30, "failed_jobs_sweep": 10,
                    "product_analytics_sync": 720, "zendesk_case_graph_sync": 60}


def _reschedule(kind: str, sb) -> None:
    """Re-enqueue a periodic sweep for its next slot. Bucketed dedupe key so a
    worker restart can't pile them up."""
    import datetime as _dt

    mins = _SWEEP_EVERY_MIN[kind]
    nxt = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=mins)
    bucket = int(nxt.timestamp() // (mins * 60))
    jobs.enqueue(kind, {}, dedupe_key=f"{kind}:{bucket}",
                 run_after=nxt.isoformat(), sb=sb)


def _sweep_handler(fn):
    def _run(payload: dict, sb) -> dict:
        from interpreter import sweeps

        try:
            return getattr(sweeps, fn)(sb)
        finally:
            _reschedule(fn, sb)
    return _run


HANDLERS = {"run_flow": _run_flow, "check_resolution": _check_resolution,
            "embed_kb_entry": _embed_kb_entry, "create_github_issue": _create_github_issue,
            "apply_kb_change": _apply_kb_change, "kb_sync": _sync_kb_connection,
            "gdoc_writeback": _gdoc_writeback,
            "crawl_site": _crawl_site, "sync_gsheet": _sync_gsheet,
            "import_kb_bundle": _import_kb_bundle,
            "queue_sweep": _sweep_handler("queue_sweep"),
            "cdc_reconcile": _sweep_handler("cdc_reconcile"),
            "reasoning_ttl": _sweep_handler("reasoning_ttl"),
            "handoff_watch": _sweep_handler("handoff_watch"),
            "kb_promote": _sweep_handler("kb_promote"),
            "case_graph_sync": _sweep_handler("case_graph_sync"),
            "case_memory_sync": _sweep_handler("case_memory_sync"),
            "zendesk_case_graph_sync": _sweep_handler("zendesk_case_graph_sync"),
            "product_analytics_sync": _sweep_handler("product_analytics_sync"),
            "fire_schedules": _sweep_handler("fire_schedules"),
            "kil_digest": _sweep_handler("kil_digest"),
            "failed_jobs_sweep": _sweep_handler("failed_jobs_sweep")}

JOB_TIMEOUT = int(os.environ.get("WORKER_JOB_TIMEOUT", "120"))

# A whole-site crawl (or a re-sync of one) legitimately runs for minutes —
# it's a bounded loop of politely-spaced HTTP GETs, not a runaway. Give it a
# long budget instead of the 120s default that fits an LLM flow run.
JOB_TIMEOUT_BY_KIND = {
    "crawl_site": int(os.environ.get("WORKER_CRAWL_TIMEOUT", "1800")),
    "kb_sync": int(os.environ.get("WORKER_CRAWL_TIMEOUT", "1800")),
}


class _JobTimeout(Exception):
    pass


def _resolve_job_tenant(kind: str, payload: dict, sb) -> str | None:
    """Best-effort tenant attribution for a job whose row has no tenant_id
    (payload didn't carry one). Cheap single lookups keyed on whatever id
    the payload does have. Cross-tenant infra sweeps have no owner -> None."""
    direct = jobs._tenant_from_payload(payload)
    if direct:
        return direct
    try:
        if kind in ("run_flow",) and payload.get("flow_id"):
            r = (sb.table("flows").select("tenant_id")
                 .eq("flow_id", payload["flow_id"]).limit(1).execute().data or [])
            return r[0]["tenant_id"] if r else None
        if kind == "check_resolution" and payload.get("run_id"):
            r = (sb.table("runs").select("tenant_id")
                 .eq("run_id", payload["run_id"]).limit(1).execute().data or [])
            return r[0]["tenant_id"] if r else None
        if kind in ("kb_sync", "gdoc_writeback") and payload.get("connection_id"):
            r = (sb.table("kb_source_connections").select("tenant_id")
                 .eq("connection_id", payload["connection_id"]).limit(1).execute().data or [])
            return r[0]["tenant_id"] if r else None
        if kind == "embed_kb_entry" and payload.get("entry_id"):
            r = (sb.table("kb_entries").select("tenant_id")
                 .eq("entry_id", payload["entry_id"]).limit(1).execute().data or [])
            return r[0]["tenant_id"] if r else None
    except Exception as e:  # noqa: BLE001 -- attribution is never worth failing a job over
        log.warning("_resolve_job_tenant(%s): %s", kind, e)
    return None


def process_one(sb, *, job_id: str | None = None) -> bool:
    import signal

    job = jobs.claim(sb=sb, job_id=job_id)
    if not job:
        return False
    jid, kind = job["job_id"], job["kind"]

    if not job.get("tenant_id"):
        tid = _resolve_job_tenant(kind, job.get("payload") or {}, sb)
        if tid:
            try:
                sb.table("jobs").update({"tenant_id": str(tid)}).eq("job_id", jid).execute()
            except Exception as e:  # noqa: BLE001
                log.warning("stamp job %s tenant: %s", jid, e)

    budget = JOB_TIMEOUT_BY_KIND.get(kind, JOB_TIMEOUT)

    def _alarm(_sig, _frm):
        raise _JobTimeout(f"job exceeded {budget}s")

    have_alarm = hasattr(signal, "SIGALRM")
    if have_alarm:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(budget)
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
    if os.environ.get("SWEEPS_DISABLED") != "1":
        for _k in _SWEEP_EVERY_MIN:          # Phase 27d — seed the periodic sweeps
            try:
                jobs.enqueue(_k, {}, dedupe_key=f"{_k}:boot", sb=sb)
            except Exception as e:  # noqa: BLE001
                log.warning("could not seed sweep %s: %s", _k, e)
    while True:
        did = process_one(sb)
        beat("worker", {"idle": not did}, sb=sb)
        if not did:
            time.sleep(args.idle_sleep)


if __name__ == "__main__":
    sys.exit(main())
