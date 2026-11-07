"""Who raises cases wrongly, why, and what to tell them. Pure.

Every check here is a rule, so each finding can be shown with its reason. A "wrong"
raise has something provably missing or mismatched. A "please check" is a gap in our
own records, so it's shown but never counted against the agent.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta

from . import case_details, escalations
from .verdict import parse_dt

DAYS = 30
MISSING, IDS, PAYLOAD, ENGINEER = ("Missing details", "IDs don't match", "Wrong store's payload",
                                   "Didn't need engineering")
# "1111111-222222-PARTNERA-2026-01-02_120000" or ".../locations/222222/platforms/..."
_PAYLOADS = (re.compile(r"\b\d{6,}-(\d{3,})-[A-Z]{2,}-\d{4}-\d{2}-\d{2}"), re.compile(r"/locations/(\d{3,})/platforms/"))
_SIGNED = re.compile(r"(?:Regards|At Your Service),?\s*\n+\s*([^|\n]{2,40}?)\s*\|", re.I)


def _split(value: object) -> list[str]:
    return [s.strip() for s in str(value or "").split(",") if s.strip()]


def same_person(a: str, b: str) -> bool:
    """'Asha Kumar', 'asha.k', 'Asha' and 'ashakumar' are one person."""
    x, y = sorted((a.lower().strip(), b.lower().strip()), key=len)
    if re.sub(r"\W", "", x) == re.sub(r"\W", "", y):
        return True
    head, *tail = re.split(r"[.\s]+", x)
    first, *rest = re.split(r"[.\s]+", y)
    if head != first:
        return False
    return not tail or not rest or rest[0].startswith(tail[0]) or tail[0].startswith(rest[0])


def raiser(case: dict, evidence: list[dict]) -> str:
    """Who signed the first email out, since a case can change owner later.

    Only the first counts: a later signature is whoever chased it. With no signature
    there, the owner is the best guess."""
    owner = case.get("owner") or "Unassigned"
    ours = [m for ev in evidence if ev.get("kind") == "email" for m in ev.get("messages") or []
            if m.get("ours") and parse_dt(m.get("at"))]
    if not ours:
        return owner
    found = _SIGNED.search(min(ours, key=lambda m: parse_dt(m["at"])).get("text") or "")
    name = found[1].strip() if found else owner
    return owner if same_person(name, owner) else name


def payload_problem(case: dict, evidence: list[dict]) -> str | None:
    """A payload from another store, when none of the attached ones is this store's."""
    locations = _split(case.get("location_id"))
    text = "\n".join(m.get("text") or "" for ev in evidence if ev.get("kind") == "email"
                     for m in ev.get("messages") or [] if m.get("ours"))
    found = {x for p in _PAYLOADS for x in p.findall(text)}
    if not locations or not found or found & set(locations):
        return None
    return f"Payload is for location {', '.join(sorted(found))}, but the case is {', '.join(locations)}"


def id_problems(case: dict, facts: dict[str, dict], aliases: dict) -> tuple[list[str], list[str]]:
    """(wrong, please check) from comparing the case's IDs with the catalogue."""
    wrong, check = [], []
    names = case.get("third_parties") or []
    partner = names[0] if names else ""
    platform = str(aliases.get(partner) or partner).lower()
    locations, ids = _split(case.get("location_id")), _split(case.get("platform_id"))
    biz = str(escalations.business(case)["biz_id"] or "")
    for i, location in enumerate(locations):
        rows = facts.get(location)
        if not rows:
            continue  # not copied from the catalogue yet
        owner = next((r["biz_id"] for r in rows.values() if r.get("biz_id")), None)
        if biz and owner and str(owner) != biz:
            wrong.append(f"Location {location} belongs to business {owner}, not {biz}")
        given = ids[i] if i < len(ids) else None
        expected = (rows.get(platform) or {}).get("platform_id")
        if expected and given and given != expected:
            wrong.append(f"Location {location} has {partner} ID {expected}, but the case says {given}")
        elif given and not expected:
            check.append(f"Our records have no {partner} ID for location {location}, so {given} can't be confirmed")
    return wrong, check


def check_case(case: dict, evidence: list[dict], facts: dict[str, dict], cfg: dict) -> dict:
    found = case_details.missing(case, cfg.get("required_details") or [], evidence)
    # Item names are written too many ways to find reliably, so a miss isn't proof.
    gaps = [g for g in found if g != case_details.LABELS["item"]]
    wrong_ids, check = id_problems(case, facts, cfg.get("platform_aliases") or {})
    if len(gaps) < len(found):
        check = [*check, "No item name found in the case or emails"]
    payload = payload_problem(case, evidence)
    problems = ([{"kind": MISSING, "text": f"Missing {', '.join(gaps)}"}] if gaps else [])
    problems += [{"kind": IDS, "text": t} for t in wrong_ids]
    problems += [{"kind": PAYLOAD, "text": payload}] if payload else []
    return {"problems": problems, "check": check}


def _message(agent: str, cases: list[dict], github: list[dict]) -> str:
    lines = [f"Hi {agent}, please look at these before raising the next similar case:"]
    lines += [f"- {c['case_number']}: {'; '.join(p['text'] for p in c['problems'])}" for c in cases]
    lines += [f"- {g['title']}: didn't need an engineer ({', '.join(g['reasons'])}) {g['url']}" for g in github[:5]]
    return "\n".join(lines)


def build(items: list[tuple], facts: dict[str, dict], coaching: dict, cfg: dict, now: datetime) -> dict:
    """items: (case, verdict, evidence) for open aggregator cases."""
    since = now - timedelta(days=DAYS)
    people: dict[str, dict] = {}

    def person(name: str) -> dict:
        for key, p in people.items():
            if same_person(key, name):
                return p
        return people.setdefault(name, {"agent": name, "raised": 0, "cases": [], "check": [], "github": []})

    for case, _, evidence in items:
        raised = parse_dt(case.get("created_at"))
        if raised is None or raised < since:
            continue
        p = person(raiser(case, evidence))
        p["raised"] += 1
        found = check_case(case, evidence, facts, cfg)
        row = {"case_id": case["id"], "case_number": case.get("case_number"), "subject": case.get("subject"),
               "raised_at": raised.isoformat(), **found}
        if found["problems"]:
            p["cases"].append(row)
        elif found["check"]:
            p["check"].append(row)
    for a in coaching.get("agents") or []:
        p = person(a["agent"])
        p["raised"] += a["raised"]
        p["github"] = a["issues"]

    agents = []
    for p in people.values():
        wrong = len(p["cases"]) + len(p["github"])
        mistakes = Counter(x["kind"] for c in p["cases"] for x in c["problems"])
        mistakes[ENGINEER] += len(p["github"])
        top = mistakes.most_common(1)
        agents.append({**p, "wrong": wrong, "main_mistake": top[0][0] if top and top[0][1] else None,
                       "message": _message(p["agent"], p["cases"], p["github"]) if wrong else None})
    agents.sort(key=lambda a: (-a["wrong"], -len(a["check"]), a["agent"]))
    raised, wrong = sum(a["raised"] for a in agents), sum(a["wrong"] for a in agents)
    return {"days": DAYS, "raised": raised, "wrong": wrong, "share": round(100 * wrong / raised) if raised else 0,
            "agents": agents}
