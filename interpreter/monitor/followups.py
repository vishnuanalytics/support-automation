"""The daily follow-up list: what each agent owes today. Pure."""

from __future__ import annotations

REPLY, RAISE, CHASE = "reply", "raise", "chase"
_WAITING = ("engineering", "third_party", "customer")
_ORDER = {REPLY: 0, RAISE: 1, CHASE: 2}


def action_for(item: dict) -> str | None:
    """Why this case is on today's list, or None if it needs nothing today."""
    if item["hidden"]:
        return None
    if item["stage"] == "act":
        return REPLY
    if item["stage"] == "raise":
        return RAISE
    if item["stage"] in _WAITING and item.get("stale"):
        return CHASE
    return None


def _message(owner: str, cases: list[dict]) -> str:
    first = owner.split()[0]
    lines = [f"Hi {first}, please check these cases today:"]
    for c in cases:
        partner = ", ".join(c["third_parties"][:1])
        name = f"{c['case_number']} ({partner})" if partner else str(c["case_number"])
        lines.append(f"- {name}: {c['next_action']}")
    return "\n".join(lines)


def build(items: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for item in items:
        action = action_for(item)
        if action:
            groups.setdefault(item.get("owner") or "Unassigned", []).append(
                {"action": action, **{k: item.get(k) for k in (
                    "id", "case_number", "subject", "next_action", "third_parties", "overdue",
                    "overdue_hours", "record_type", "score", "latest", "mine")}})
    owners = []
    for owner, cases in groups.items():
        cases.sort(key=lambda c: (_ORDER[c["action"]], -(c["score"] or 0)))
        counts = {a: sum(c["action"] == a for c in cases) for a in _ORDER}
        owners.append({"owner": owner, "mine": any(c["mine"] for c in cases), "counts": counts,
                       "total": len(cases), "overdue": sum(bool(c["overdue"]) for c in cases),
                       "cases": cases, "message": _message(owner, cases)})
    owners.sort(key=lambda o: (not o["mine"], -o["counts"][REPLY], -o["total"]))
    return {"owners": owners, "total": sum(o["total"] for o in owners),
            "counts": {a: sum(o["counts"][a] for o in owners) for a in _ORDER}}
