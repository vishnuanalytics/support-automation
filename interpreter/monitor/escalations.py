"""Cases where the third party we're waiting on has gone quiet. Pure.

Reads every channel a case has. An update only counts if it comes from the third
party and says something: an acknowledgement doesn't, and neither does a colleague
on an internal thread.
"""

from __future__ import annotations

from datetime import datetime

from . import replies, verdict
from .verdict import parse_dt

CHANNELS = {"email": "Email", "slack": "Slack", "github": "GitHub", "jira": "Jira",
            "freshchat": "Freshchat"}
UNSET = ("", "na", "n/a", "none")


def _party(ev: dict, m: dict) -> str:
    if m.get("party"):
        return m["party"]
    if m.get("ours"):
        return verdict.US
    return verdict.ENGINEERING if ev["kind"] == "github" or ev.get("internal") else verdict.THIRD_PARTY


def _channel(ev: dict) -> str:
    return CHANNELS.get(ev["kind"], ev["kind"])


def _messages(evidence: list[dict]) -> list[dict]:
    """Every dated message once; a case's timeline repeats some of its DSP emails."""
    seen: dict[tuple, dict] = {}
    for ev in evidence:
        if not ev.get("ok"):
            continue
        for m in ev.get("messages") or []:
            at = parse_dt(m.get("at"))
            if at:
                seen.setdefault((at, m.get("who")), {
                    **m, "at_dt": at, "party": _party(ev, m), "channel": _channel(ev)})
    return sorted(seen.values(), key=lambda m: m["at_dt"])


def _is_ack(m: dict, case: dict, v: dict) -> bool:
    """Rules read every message; the LLM's label only exists for the latest one."""
    stored = (case.get("reply") or {}).get("kind") in replies.NOISE
    latest = parse_dt((v.get("latest") or {}).get("at"))
    if stored and latest == m["at_dt"]:
        return True
    return m["ack"] if "ack" in m else replies.rule_kind(m.get("text")) is not None


def _waiting_on(case: dict) -> list[str]:
    names = [n for n in case.get("third_parties") or [] if n.lower() not in UNSET]
    return names or ["Not set"]


def row(case: dict, v: dict, evidence: list[dict], days: float, now: datetime) -> dict | None:
    """The escalation for this case, or None if it isn't one."""
    if verdict.stage(v) != verdict.THIRD_PARTY:
        return None  # our turn, or never raised, so there's nothing to chase them for

    messages = _messages(evidence)
    theirs = [m for m in messages if m["party"] == verdict.THIRD_PARTY]
    updates = [m for m in theirs if not _is_ack(m, case, v)]
    last = updates[-1] if updates else None
    ours = [m for m in messages if m["party"] == verdict.US]
    since = last["at_dt"] if last else ours[0]["at_dt"] if ours else parse_dt(case.get("created_at"))
    if since is None or (now - since).total_seconds() < days * 86400:
        return None

    acks = [m for m in theirs if m not in updates]
    follow_ups = [m for m in ours if m["at_dt"] > since] if last else ours
    return {
        "id": case["id"], "case_number": case.get("case_number"), "url": case.get("url"),
        "subject": case.get("subject"), "record_type": case.get("record_type"),
        "owner": case.get("owner"), "priority": case.get("priority"),
        "waiting_on": _waiting_on(case),
        "days_silent": int((now - since).total_seconds() // 86400),
        "silent_since": since.isoformat(),
        "last_update_at": last["at_dt"].isoformat() if last else None,
        "last_update_via": last["channel"] if last else None,
        "last_ack_at": acks[-1]["at_dt"].isoformat() if acks else None,
        "our_last_at": ours[-1]["at_dt"].isoformat() if ours else None,
        "our_follow_ups": len(follow_ups),
        "checked": sorted({_channel(e) for e in evidence if e.get("ok")}),
        "not_read": sorted({_channel(e) for e in evidence if not e.get("ok")}),
        "support_cases": [],
        "_related": _related_ids(case),
    }


def _related_ids(case: dict) -> list[str]:
    return [x["id"] for x in [*(p for p in [case.get("parent")] if p), *(case.get("children") or [])]
            if x.get("id")]


def _linked_replies(case: dict, by_id: dict[str, list[dict]]) -> dict:
    """A reply can land on either side of a support case and its DSP case."""
    messages = [m for i in _related_ids(case) for ev in by_id.get(i, [])
                if ev["kind"] == "email" and ev.get("ok") for m in ev.get("messages") or []
                if m.get("party") == verdict.THIRD_PARTY]
    return {"kind": "email", "ok": True, "messages": messages}


def _merge_mirrors(rows: list[dict], dependency_type: str) -> list[dict]:
    """A support case mirrors its DSP case, so one chase is listed once."""
    by_id = {r["id"]: r for r in rows}
    kept = []
    for r in rows:
        twin = next((by_id[i] for i in r["_related"] if i in by_id
                     and by_id[i]["record_type"] == dependency_type), None)
        if twin and r["record_type"] != dependency_type:
            twin["support_cases"].append(r["case_number"])
        else:
            kept.append(r)
    return kept


def build(items: list[tuple[dict, dict, list[dict]]], days: float, now: datetime,
          dependency_type: str = "") -> dict:
    by_id = {c["id"]: ev for c, _, ev in items}
    found = [r for c, v, ev in items
             if (r := row(c, v, ev + [_linked_replies(c, by_id)], days, now))]
    rows = _merge_mirrors(found, dependency_type)
    for r in rows:
        del r["_related"]
    rows.sort(key=lambda r: -r["days_silent"])
    groups: dict[str, list[dict]] = {}
    for r in rows:
        for name in r["waiting_on"]:
            groups.setdefault(name, []).append(r)
    summary = [{"name": name, "cases": len(g), "longest_days": g[0]["days_silent"]}
               for name, g in groups.items()]
    summary.sort(key=lambda s: -s["cases"])
    return {"days": days, "total": len(rows), "summary": summary, "cases": rows}
