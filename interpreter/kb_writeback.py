"""
KIL-d — the KB write-back loop.

A manager marks a `review_tasks` row **Correct** (the human's KB-contradicting
statement was actually right). This module:

  draft_change(task_row)      -> {op, title, body_md, rationale, supersedes_*}
                                 an LLM (or a deterministic fallback) proposes
                                 a create / supersede against `kb_entries`.
  raise_kb_change(...)        -> an action_requests row (kind='kb_change') +
                                 a Slack approval card; links review_tasks.
  apply_kb_change(...)        -> on approval: write the entry `provisional`,
                                 supersede the old one (chunks pulled from
                                 retrieval), enqueue the embed, MERGE the
                                 (:KBArticle)-[:SUPERSEDES]-> edge.
  promote_provisional(sb)     -> flip aged `provisional` entries to `active`.

Everything degrades: no LLM -> the fallback draft; no Neo4j -> the graph edge
is skipped; a missing collection -> a `kb-corrections` source is created.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_uuid(s: Any) -> bool:
    return isinstance(s, str) and bool(_UUID_RE.match(s))

from . import llm

log = logging.getLogger("interpreter.kb_writeback")

_PROVISIONAL_DAYS = int(os.environ.get("KB_PROVISIONAL_DAYS", "7"))
_CORRECTIONS_NAME = "kb-corrections"


# ── draft ────────────────────────────────────────────────────────────────
def _kb_ref(contexts: list[dict]) -> tuple[str | None, str | None]:
    """(entry_id, source_id) of the internal-KB passage this correction
    contradicts, if one is in the review task's contexts."""
    for c in contexts or []:
        ref = c.get("ref") or ""
        if ref.startswith("kb://"):
            rest = ref[len("kb://"):].split("/")
            if len(rest) == 2 and _is_uuid(rest[1]):
                return rest[1], (rest[0] if _is_uuid(rest[0]) else None)
    return None, None


def _fallback_change(statement: str, target_entry_id: str | None) -> dict:
    first = (statement or "").strip().splitlines()[0][:90] or "Knowledge correction"
    return {
        "op": "supersede" if target_entry_id else "create",
        "title": first,
        "body_md": (statement or "").strip(),
        "rationale": "Manager-confirmed correction from a support reply.",
        "supersedes_entry_id": target_entry_id,
    }


def draft_change(task_row: dict, *, model: str | None = None) -> dict:
    """Propose a `kb_entries` change from a confirmed-correct review task."""
    statement = task_row.get("statement") or ""
    contexts = task_row.get("contexts") or []
    verdict = task_row.get("verdict") or {}
    target_entry_id, _sid = _kb_ref(contexts)
    if not llm.available():
        return _fallback_change(statement, target_entry_id)

    ctx_txt = "\n\n".join(f"[{c.get('ref')}] {c.get('text', '')}" for c in contexts[:4]) or "(none)"
    salient = "; ".join(verdict.get("salient") or []) or "(the statement itself)"
    raw = llm.complete(
        system=(
            "You maintain a support knowledge base. A support agent's reply was "
            "confirmed correct by a manager but it CONTRADICTS existing KB text. "
            "Write the KB so it now reflects the correct information. If an "
            "existing passage is wrong, rewrite it in full (op 'supersede'); "
            "otherwise add a new short entry (op 'create'). Be concise, factual, "
            "no greeting. Return JSON {\"op\": \"create\"|\"supersede\", "
            "\"title\": string, \"body_md\": string, \"rationale\": string}."
        ),
        user=(f"# Confirmed-correct statement\n{statement}\n\n"
              f"# Claim at issue\n{salient}\n\n"
              f"# Existing KB / context it contradicts\n{ctx_txt}"),
        model=model or llm.FAST_MODEL,
        json_object=True,
        max_tokens=600,
    )
    try:
        p = json.loads(raw)
        op = p.get("op") if p.get("op") in ("create", "supersede") else None
        op = op or ("supersede" if target_entry_id else "create")
        if op == "supersede" and not target_entry_id:
            op = "create"          # the model asked to replace, but no KB entry was in scope
        return {
            "op": op,
            "title": (p.get("title") or statement[:90]).strip()[:200],
            "body_md": (p.get("body_md") or statement).strip(),
            "rationale": (p.get("rationale") or "Manager-confirmed correction.").strip()[:500],
            "supersedes_entry_id": target_entry_id if op == "supersede" else None,
        }
    except (json.JSONDecodeError, TypeError, AttributeError):
        return _fallback_change(statement, target_entry_id)


# ── approval request + Slack card ────────────────────────────────────────
def raise_kb_change(sb, *, tenant_id: str, task_row: dict, change: dict,
                    post: "callable | None" = None) -> dict | None:
    """Insert an action_requests(kind='kb_change') + post an approve/reject
    card; stamp review_tasks.kb_change_id. Returns the action_requests row."""
    try:
        ar = (sb.table("action_requests").insert({
            "tenant_id": str(tenant_id),
            "run_id": task_row.get("run_id"),
            "rule_name": "kb_writeback",
            "kind": "kb_change",
            "payload": {**change, "review_task_id": task_row.get("id"),
                        "case_number": task_row.get("case_number")},
            "status": "pending",
        }).execute().data or [None])[0]
    except Exception as e:  # noqa: BLE001
        log.warning("raise_kb_change insert: %s", e)
        return None
    if not ar:
        return None
    try:
        sb.table("review_tasks").update({"kb_change_id": ar["id"]}) \
          .eq("id", task_row["id"]).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("raise_kb_change link: %s", e)

    _post_card(sb, tenant_id=tenant_id, ar_id=ar["id"], change=change,
               channel=task_row.get("slack_channel"), post=post)
    return ar


def _post_card(sb, *, tenant_id, ar_id, change, channel, post=None) -> None:
    try:
        from . import slack
        verb = "Rewrite an existing entry" if change["op"] == "supersede" else "Add a new entry"
        text = (
            f":books: *KB update proposed* — {verb}\n"
            f"*{change['title']}*\n"
            f"```{change['body_md'][:1500]}```\n"
            f"_why: {change['rationale']}_"
        )
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "block_id": "kb", "elements": [
                {"type": "button", "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve & publish"},
                 "action_id": "kb_approve", "value": ar_id},
                {"type": "button", "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"},
                 "action_id": "kb_reject", "value": ar_id},
            ]},
        ]
        sender = post or slack.post_message
        sender("A KB update needs approval", tenant_id=tenant_id,
               channel=channel or os.environ.get("SLACK_UNROUTED_CHANNEL", "#cx-unrouted"),
               blocks=blocks)
    except Exception as e:  # noqa: BLE001
        log.warning("kb_writeback._post_card: %s", e)


# ── apply ────────────────────────────────────────────────────────────────
def _corrections_source(sb, tenant_id: str) -> str:
    rows = (sb.table("sources").select("source_id")
            .eq("tenant_id", str(tenant_id)).eq("kind", "internal_kb")
            .eq("name", _CORRECTIONS_NAME).limit(1).execute().data or [])
    if rows:
        return rows[0]["source_id"]
    created = (sb.table("sources").insert({
        "tenant_id": str(tenant_id), "kind": "internal_kb", "name": _CORRECTIONS_NAME,
        "config": {"label": "Corrections (from review)", "origin": "kil"},
    }).execute().data or [None])[0]
    return created["source_id"]


def _graph_supersede(new_id: str, old_id: str | None, tenant_id: str, title: str) -> None:
    try:
        from .case_memory import _driver_or_none
        driver = _driver_or_none()
        if driver is None:
            return
        db = os.environ.get("NEO4J_DATABASE", "neo4j")
        cy = ("MERGE (k:KBArticle {entry_id: $new}) "
              "SET k.tenant_id = $tid, k.title = $title, k.status = 'provisional' ")
        params = {"new": new_id, "tid": str(tenant_id), "title": title}
        if old_id:
            cy += ("WITH k MERGE (o:KBArticle {entry_id: $old}) "
                   "SET o.status = 'superseded' MERGE (k)-[:SUPERSEDES]->(o)")
            params["old"] = old_id
        driver.execute_query(cy, database_=db, **params)
        driver.close()
    except Exception as e:  # noqa: BLE001
        log.warning("kb_writeback._graph_supersede: %s", e)


def apply_kb_change(sb, ar_row: dict, *, enqueue=True) -> dict:
    """A manager approved the change — write it. `ar_row` is the
    action_requests row (kind='kb_change', status='approved')."""
    if ar_row.get("status") != "approved":
        return {"skipped": f"status={ar_row.get('status')}"}
    if ar_row.get("result"):
        return {"idempotent_skip": True, **ar_row["result"]}
    tenant_id = ar_row["tenant_id"]
    p = ar_row["payload"]
    old_id = p.get("supersedes_entry_id")
    if not _is_uuid(old_id):
        old_id = None                       # a create, not a supersede
    approver = ar_row.get("decided_by")

    src = _corrections_source(sb, tenant_id)
    if old_id:
        try:
            src_rows = (sb.table("kb_entries").select("source_id")
                        .eq("entry_id", old_id).limit(1).execute().data or [])
            if src_rows:
                src = src_rows[0]["source_id"]
            sb.table("kb_entries").update({
                "status": "superseded", "approved_by": approver, "updated_at": "now()",
            }).eq("entry_id", old_id).execute()
            old_url = f"kb://{src}/{old_id}"
            # Flag first (retrieval already excludes 'superseded'), then hard-
            # delete. If delete_entry throws, the stale chunks are still out of
            # retrieval instead of silently reading as 'active'.
            sb.table("doc_chunks").update({"entry_status": "superseded"}) \
              .eq("doc_url", old_url).execute()
            from ingestion.sources.kb_common import delete_entry
            delete_entry(sb, url=old_url)
        except Exception as e:  # noqa: BLE001
            log.warning("apply_kb_change supersede %s: %s", old_id, e)

    until = (datetime.now(timezone.utc) + timedelta(days=_PROVISIONAL_DAYS)).isoformat()
    entry = (sb.table("kb_entries").insert({
        "source_id": src, "tenant_id": str(tenant_id),
        "title": p["title"], "body_md": p["body_md"],
        "status": "provisional", "origin": "review_writeback",
        "approved_by": approver, "source_review_task": p.get("review_task_id"),
        "supersedes_entry_id": old_id, "provisional_until": until,
    }).execute().data or [None])[0]
    eid = entry["entry_id"]

    if enqueue:
        try:
            from interpreter import jobs
            jobs.enqueue("embed_kb_entry", {"entry_id": eid, "collection_name": _CORRECTIONS_NAME},
                         dedupe_key=f"embed:{eid}")
        except Exception as e:  # noqa: BLE001
            log.warning("apply_kb_change enqueue embed: %s", e)

    _graph_supersede(eid, old_id, tenant_id, p["title"])
    result = {"entry_id": eid, "op": p["op"], "superseded": old_id, "status": "provisional"}
    try:
        sb.table("action_requests").update({"status": "done", "result": result}) \
          .eq("id", ar_row["id"]).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("apply_kb_change finalize: %s", e)
    return result


# ── promotion (the poisoning guard) ─────────────────────────────────────
def _has_fresh_contradiction(sb, entry: dict) -> bool:
    """An open `human_reply_review` task raised *after* this entry whose judged
    contexts reference it — the entry is disputed, don't promote it yet."""
    eid = entry["entry_id"]
    try:
        rows = (sb.table("review_tasks")
                .select("contexts, created_at")
                .eq("tenant_id", str(entry.get("tenant_id")))
                .eq("kind", "human_reply_review").eq("status", "open")
                .gte("created_at", entry.get("created_at") or "1970-01-01")
                .limit(500).execute().data or [])
    except Exception as e:  # noqa: BLE001
        log.warning("_has_fresh_contradiction: %s", e)
        return False
    for t in rows:
        if any(eid in (c.get("ref") or "") for c in (t.get("contexts") or [])):
            return True
    return False


def promote_provisional(sb, *, dry_run: bool = False) -> int:
    """Flip `provisional` entries whose `provisional_until` has passed to
    `active` — unless an open contradiction still references the entry."""
    now = datetime.now(timezone.utc).isoformat()
    rows = (sb.table("kb_entries")
            .select("entry_id, source_id, tenant_id, title, created_at")
            .eq("status", "provisional").lt("provisional_until", now)
            .execute().data or [])
    ready = [r for r in rows if not _has_fresh_contradiction(sb, r)]
    held = len(rows) - len(ready)
    if dry_run or not ready:
        if held:
            log.info("promote_provisional: %d held (disputed)", held)
        return len(ready)
    for r in ready:
        sb.table("kb_entries").update({"status": "active", "updated_at": "now()"}) \
          .eq("entry_id", r["entry_id"]).execute()
        # P1b — the entry's chunks are now trusted context.
        if r.get("source_id"):
            sb.table("doc_chunks").update({"entry_status": "active"}) \
              .eq("doc_url", f"kb://{r['source_id']}/{r['entry_id']}").execute()
    log.info("promoted %d provisional KB entr(y/ies) to active (%d held)", len(ready), held)
    return len(ready)
