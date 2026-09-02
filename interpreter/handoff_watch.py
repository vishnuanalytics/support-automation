"""
KIL-e — the post-handover watcher.

Handover means the bot writes nothing to the customer. But it keeps watching:
on each sweep pass for an escalated Case, `watch_case()` runs the KIL-b
contradiction judge on any **new** message (customer follow-up or agent reply)
and checks the pointer bank for still-unanswered *critical* questions on a
big-issue Case. A hit posts one flag to the manager thread — never the
customer — rate-limited (`HANDOFF_MAX_FLAGS`, default 3) and deduped by
signature so the same conflict isn't re-raised every pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from . import integrity, llm

log = logging.getLogger("interpreter.handoff_watch")

_MAX_FLAGS = int(os.environ.get("HANDOFF_MAX_FLAGS", "3"))
_WS = re.compile(r"\s+")


def _sig(kind: str, text: str) -> str:
    norm = _WS.sub(" ", (text or "").strip().lower())[:400]
    return kind[:4] + ":" + hashlib.sha1(norm.encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── inputs ──────────────────────────────────────────────────────────────
def _new_messages(sf, case_id: str, since_iso: str | None) -> list[dict]:
    """CaseComments + outbound/inbound EmailMessages created after `since`."""
    from . import salesforce
    since = since_iso or "1970-01-01T00:00:00Z"
    lit = salesforce._soql_lit(since)
    out: list[dict] = []
    try:
        for rec in sf.query(
            "SELECT Id, Incoming, TextBody, MessageDate FROM EmailMessage "
            f"WHERE ParentId = '{salesforce._soql_lit(case_id)}' AND MessageDate > {lit} "
            "ORDER BY MessageDate ASC LIMIT 30"
        ).get("records", []):
            kind = "inbound" if rec.get("Incoming") else "agent_reply"
            out.append({"id": rec["Id"], "role": kind,
                        "author_kind": "customer" if rec.get("Incoming") else "agent",
                        "text": rec.get("TextBody") or "", "ts": rec.get("MessageDate")})
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_watch email query %s: %s", case_id, e)
    try:
        for rec in sf.query(
            "SELECT Id, CommentBody, CreatedDate FROM CaseComment "
            f"WHERE ParentId = '{salesforce._soql_lit(case_id)}' AND CreatedDate > {lit} "
            "ORDER BY CreatedDate ASC LIMIT 30"
        ).get("records", []):
            body = rec.get("CommentBody") or ""
            low = body.lower().lstrip()
            role = "draft" if low.startswith(("[bot draft", "[draft")) else "agent_note"
            out.append({"id": rec["Id"], "role": role,
                        "author_kind": "bot" if role == "draft" else "agent",
                        "text": body, "ts": rec.get("CreatedDate")})
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_watch comment query %s: %s", case_id, e)
    out.sort(key=lambda m: m.get("ts") or "")
    return out


def _context_for(sb, case_id: str, case_number: str | None) -> list[dict]:
    """The KB + case-history the run already retrieved (via review.assemble_contexts)."""
    from . import review
    try:
        for col, val in (("case_payload->>sf_id", case_id), ("case_id", case_number)):
            if not val:
                continue
            rows = (sb.table("runs").select("retrieval, trace")
                    .eq(col, val).order("created_at", desc=True).limit(1).execute().data or [])
            if rows:
                return review.assemble_contexts(rows[0])
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_watch._context_for %s: %s", case_id, e)
    return []


_POINTER_SYS = (
    "You review an escalated support thread. Given the CRITICAL questions a "
    "responder must answer and the THREAD so far, list the critical questions "
    "that are still UNANSWERED. Return a JSON array of strings (max 3), [] if none."
)


def _missed_pointers(sb, case_type: str | None, thread_text: str) -> list[str]:
    if not (llm.available() and thread_text.strip()):
        return []
    try:
        rows = (sb.table("pointer_bank").select("pointers")
                .eq("case_type", case_type or "Other").limit(1).execute().data
                or sb.table("pointer_bank").select("pointers")
                .eq("case_type", "Other").limit(1).execute().data or [])
        pts = rows[0]["pointers"] if rows else []
        if isinstance(pts, str):
            pts = json.loads(pts)
        crit = [p["q"] for p in pts
                if isinstance(p, dict) and p.get("critical") and p.get("q")]
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_watch pointer_bank: %s", e)
        return []
    if not crit:
        return []
    raw = llm.complete(
        system=_POINTER_SYS,
        user="# CRITICAL QUESTIONS\n- " + "\n- ".join(crit)
             + f"\n\n# THREAD\n{thread_text[:6000]}",
        model=llm.FAST_MODEL, json_object=True, max_tokens=250,
    )
    try:
        arr = json.loads(raw)
        return [str(x)[:200] for x in arr][:3] if isinstance(arr, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ── the watcher ─────────────────────────────────────────────────────────
def _target(sb, tenant_id: str, routed_team: str, case_id: str) -> tuple[str | None, str | None]:
    """(channel, thread_ts) for the manager flag — the open reasoning thread
    if there is one, else the routed-team channel."""
    try:
        rows = (sb.table("reasoning_sessions")
                .select("slack_channel, slack_thread_ts")
                .eq("case_id", case_id).not_.in_("state", ("sent", "abandoned"))
                .limit(1).execute().data or [])
        if rows and rows[0].get("slack_channel"):
            return rows[0]["slack_channel"], rows[0].get("slack_thread_ts")
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_watch._target session: %s", e)
    try:
        from . import routing
        r = routing.resolve_slack_route(tenant_id, routed_team=routed_team or None)
        return r.get("channel"), None
    except Exception:  # noqa: BLE001
        return None, None


def _flag(post, channel, thread_ts, text) -> bool:
    try:
        r = post(text, channel=channel, thread_ts=thread_ts)
        return bool((r or {}).get("sent"))
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_watch._flag: %s", e)
        return False


def _tenant_for_case(sb, case_id: str, case_number: str | None) -> str:
    """The tenant that owns this Case — from its latest `runs` row; falls back
    to `DEFAULT_TENANT_ID` (one SF org == one tenant in this deployment)."""
    default = os.environ.get("DEFAULT_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    try:
        for col, val in (("case_payload->>sf_id", case_id), ("case_id", case_number)):
            if not val:
                continue
            rows = (sb.table("runs").select("tenant_id")
                    .eq(col, val).order("created_at", desc=True).limit(1).execute().data or [])
            if rows and rows[0].get("tenant_id"):
                return str(rows[0]["tenant_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_watch._tenant_for_case %s: %s", case_id, e)
    return default


def watch_case(sb, case: dict, *, tenant_id: str | None = None, sf=None,
               post=None, dry: bool = False) -> dict:
    """Run one watch pass for one escalated Case. `case` is a Salesforce record
    dict (Id, CaseNumber, Routed_Team__c, CreatedDate, …)."""
    from . import salesforce, slack
    case_id = case["Id"]
    cn = case.get("CaseNumber")
    tenant_id = tenant_id or _tenant_for_case(sb, case_id, cn)
    team = case.get("Routed_Team__c") or "support"

    st = {}
    try:
        rows = (sb.table("handoff_watch_state").select("*")
                .eq("case_sf_id", case_id).limit(1).execute().data or [])
        st = rows[0] if rows else {}
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_watch state read %s: %s", case_id, e)
    flags_sent = int(st.get("flags_sent") or 0)
    seen = set(st.get("seen_sigs") or [])
    since = st.get("last_seen_ts") or case.get("CreatedDate")

    sf = sf or (salesforce.client_for(None) if salesforce.available() else None)
    if sf is None:
        return {"case": cn, "skipped": "no SF"}
    msgs = _new_messages(sf, case_id, since)
    if not msgs and flags_sent:
        return {"case": cn, "new_messages": 0}

    contexts = _context_for(sb, case_id, cn)
    poster = post or slack.post_message
    channel, thread_ts = _target(sb, tenant_id, team, case_id)
    fired: list[str] = []

    def _room() -> bool:
        return (flags_sent + len(fired)) < _MAX_FLAGS

    # 1. contradictions / novel claims on new turns
    for m in msgs:
        if m["role"] not in ("inbound", "agent_reply", "agent_note") or not contexts:
            continue
        if not _room():
            break
        kind = "human_reply" if m["author_kind"] == "agent" else "inbound"
        res = integrity.check(m["text"], contexts, kind=kind)
        if not (res.get("flagged") or res.get("novel")):
            continue
        sig = _sig("contra", (res.get("salient") or [m["text"]])[0])
        if sig in seen:
            continue
        claim = (res.get("salient") or [""])[0] or m["text"][:200]
        text = (f":rotating_light: *Post-handover check — a {'reply' if kind=='human_reply' else 'new message'} "
                f"on Case {cn} {res['relation']}s the KB / case history*\n>>> {claim}")
        if dry or _flag(poster, channel, thread_ts, text):
            seen.add(sig)
            fired.append(sig)

    # 2. still-unanswered critical questions (LLM-gated)
    if _room():
        thread_text = "\n".join(f"[{m['role']}] {m['text']}" for m in msgs)
        for q in _missed_pointers(sb, case.get("Type"), thread_text):
            if not _room():
                break
            sig = _sig("point", q)
            if sig in seen:
                continue
            text = f":grey_question: *Post-handover check — Case {cn}: still unestablished* — {q}"
            if dry or _flag(poster, channel, thread_ts, text):
                seen.add(sig)
                fired.append(sig)

    last_ts = max((m["ts"] for m in msgs if m.get("ts")), default=since) or _now_iso()
    if not dry:
        try:
            sb.table("handoff_watch_state").upsert({
                "case_sf_id": case_id, "tenant_id": tenant_id, "last_seen_ts": last_ts,
                "flags_sent": flags_sent + len(fired), "seen_sigs": sorted(seen),
                "updated_at": _now_iso(),
            }, on_conflict="case_sf_id").execute()
        except Exception as e:  # noqa: BLE001
            log.warning("handoff_watch state write %s: %s", case_id, e)
    return {"case": cn, "new_messages": len(msgs), "flags": len(fired),
            "flags_total": flags_sent + len(fired)}
