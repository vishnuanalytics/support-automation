"""
KIL-c — the human-reply review queue.

After a human's reply reaches the customer, `judge_human_reply()` runs the
KIL-b contradiction judge on it against the KB + case history that the run
already retrieved. A `contradicts` / `novel` verdict — or a random sample of
the clean ones (`REVIEW_SAMPLE_RATE`, default 5%) — opens a `review_tasks`
row and posts a card to the routed-team manager usergroup in Slack.

The manager's Correct / Wrong / Dismiss lives in `slack_socket.dispatch_action`
(and the web Review tab). `correct` is where KIL-d picks up to draft a KB
change. Everything here is best-effort — it never breaks the feedback loop.
"""

from __future__ import annotations

import logging
import os
import random
from typing import Any

from . import integrity

log = logging.getLogger("interpreter.review")

_SAMPLE_RATE = float(os.environ.get("REVIEW_SAMPLE_RATE", "0.05"))
_ACTIONS = {"correct", "wrong", "dismissed"}


# ── context assembly ─────────────────────────────────────────────────────
def _prior_from_trace(trace: list[dict] | None) -> list[dict]:
    for step in (trace or []):
        if step.get("type") == "draft":
            pr = (step.get("data") or {}).get("prior_resolutions")
            if pr:
                return pr
    return []


def assemble_contexts(run_row: dict) -> list[dict[str, Any]]:
    """The KB + case-history passages the run judged against — reused so the
    human reply is checked against exactly what the bot saw."""
    return integrity.contexts_from_state({
        "retrieval": run_row.get("retrieval") or [],
        "prior_resolutions": _prior_from_trace(run_row.get("trace")),
        "internal_kb": {"matches": (run_row.get("internal_kb") or {}).get("matches") or []},
    })


# ── queue ────────────────────────────────────────────────────────────────
def should_sample(rate: float | None = None) -> bool:
    r = _SAMPLE_RATE if rate is None else rate
    return r > 0 and random.random() < r


def open_task(sb, *, tenant_id: str, run_id: str | None, kind: str, trigger: str,
              statement: str, verdict: dict, contexts: list[dict],
              case_sf_id: str | None = None, case_number: str | None = None,
              slack: dict | None = None) -> dict | None:
    """Insert one review task (idempotent on (run_id, kind)). Returns the row
    or None if it already existed / the write failed."""
    payload = {
        "tenant_id": str(tenant_id),
        "run_id": run_id,
        "kind": kind,
        "trigger": trigger,
        "statement": integrity_redact(statement),
        "verdict": verdict or {},
        "contexts": [{"ref": c.get("ref"), "kind": c.get("kind"),
                      "text": (c.get("text") or "")[:600]} for c in (contexts or [])],
        "case_sf_id": case_sf_id,
        "case_number": case_number,
        "slack_channel": (slack or {}).get("channel"),
        "slack_ts": (slack or {}).get("ts"),
    }
    try:
        res = (sb.table("review_tasks")
               .upsert(payload, on_conflict="run_id,kind", ignore_duplicates=True)
               .execute())
        return (res.data or [None])[0]
    except Exception as e:  # noqa: BLE001
        log.warning("review.open_task: %s", e)
        return None


def resolve(sb, task_id: str, *, status: str, reviewer_id: str | None = None) -> dict | None:
    if status not in _ACTIONS:
        raise ValueError(f"status must be one of {_ACTIONS}")
    try:
        res = (sb.table("review_tasks").update({
            "status": status, "reviewer_id": reviewer_id, "reviewed_at": "now()",
        }).eq("id", task_id).eq("status", "open").execute())
        return (res.data or [None])[0]
    except Exception as e:  # noqa: BLE001
        log.warning("review.resolve(%s): %s", task_id, e)
        return None


def integrity_redact(text: str | None) -> str:
    from .case_memory import redact
    return redact(text or "", limit=4000)


# ── the hook ─────────────────────────────────────────────────────────────
def judge_human_reply(sb, *, run_row: dict, reply_text: str,
                      sample_rate: float | None = None,
                      post: "callable | None" = None) -> dict | None:
    """Run the contradiction judge on a sent human reply; open a review task
    (+ Slack card) when it's flagged, or on a random sample. Best-effort."""
    if not (reply_text or "").strip():
        return None
    tenant_id = run_row.get("tenant_id")
    run_id = run_row.get("run_id")
    case = run_row.get("case_payload") or {}
    case_sf_id = case.get("sf_id") or case.get("id")
    case_number = case.get("case_number")
    team = run_row.get("routed_team") or run_row.get("team") or ""

    contexts = assemble_contexts(run_row)
    verdict = integrity.check(reply_text, contexts, kind="human_reply", tenant_id=tenant_id)

    if verdict.get("flagged"):
        trigger, kind = "contradicts", "human_reply_review"
    elif verdict.get("novel"):
        trigger, kind = "novel", "human_reply_review"
    elif should_sample(sample_rate):
        trigger, kind = "sample", "sample"
    else:
        return None

    task = open_task(sb, tenant_id=tenant_id, run_id=run_id, kind=kind, trigger=trigger,
                     statement=reply_text, verdict=verdict, contexts=contexts,
                     case_sf_id=case_sf_id, case_number=case_number)
    if not task:
        return None
    slack_meta = _post_card(sb, task_id=task["id"], tenant_id=tenant_id, team=team,
                            reply_text=reply_text, verdict=verdict, case_sf_id=case_sf_id,
                            case_number=case_number, trigger=trigger, post=post)
    if slack_meta:
        try:
            sb.table("review_tasks").update({
                "slack_channel": slack_meta.get("channel"), "slack_ts": slack_meta.get("ts"),
            }).eq("id", task["id"]).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("review: could not stitch slack ts: %s", e)
    log.info("review: %s task for run %s (%s)", trigger, run_id, verdict.get("relation"))
    return task


def _post_card(sb, *, task_id, tenant_id, team, reply_text, verdict, case_sf_id,
               case_number, trigger, post=None) -> dict | None:
    try:
        from . import routing, slack
        route = routing.resolve_slack_route(tenant_id, routed_team=team or None)
        channel = route.get("channel")
        who = slack.usergroup_ref(route.get("usergroup"), tenant_id=tenant_id) or ""
        if not channel:
            return None
        salient = verdict.get("salient") or []
        ev = "; ".join(v.get("evidence", "") for v in (verdict.get("verdicts") or [])[:2])
        head = {
            "contradicts": ":rotating_light: *A sent reply contradicts the knowledge base / case history*",
            "novel": ":grey_question: *A sent reply makes a claim nothing in our knowledge supports*",
            "sample": ":mag: *QA sample — sent reply, please spot-check*",
        }.get(trigger, "*Review*")
        text = (
            f"{head}  {who}\n"
            f"Case *{case_number or case_sf_id or '?'}*\n"
            f">>> {reply_text.strip()[:900]}\n"
            + (f"\n*Salient claim:* {salient[0]}" if salient else "")
            + (f"\n*Conflicts with:* {ev}" if ev and trigger != 'sample' else "")
        )
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "block_id": "review", "elements": [
                {"type": "button", "style": "primary",
                 "text": {"type": "plain_text", "text": "Correct → update KB"},
                 "action_id": "review_correct", "value": task_id},
                {"type": "button", "style": "danger",
                 "text": {"type": "plain_text", "text": "Wrong → coach"},
                 "action_id": "review_wrong", "value": task_id},
                {"type": "button",
                 "text": {"type": "plain_text", "text": "Not a conflict"},
                 "action_id": "review_dismiss", "value": task_id},
            ]},
        ]
        sender = post or slack.post_message
        r = sender("A sent reply needs review", tenant_id=tenant_id,
                   channel=channel, blocks=blocks)
        if r and r.get("sent"):
            return {"channel": r.get("channel") or channel, "ts": r.get("ts")}
    except Exception as e:  # noqa: BLE001
        log.warning("review._post_card: %s", e)
    return None
