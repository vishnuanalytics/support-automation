"""What an aggregator case still needs before anyone can act on it. Pure.

Each kind of issue needs its own details. Fields are checked directly. An order
number or item name only appears in the case text, so those are found by pattern,
and a false "missing" costs one question to the agent.
"""

from __future__ import annotations

import re
from datetime import datetime

from .verdict import parse_dt

LABELS = {"business": "business name", "order": "order ID", "location": "location or platform ID",
          "item": "item name", "platform": "platform name"}
# "Order ID: 123456", "Order #1234567890", "order ABCD1234567890"
_ORDER = re.compile(r"\border(?:\s*(?:id|no|number|ref))?\s*[:#\-]*\s*[A-Z0-9-]*\d{5,}", re.I)
# The same, keeping the ID itself
_ORDER_ID = re.compile(r"\border(?:\s*(?:id|no|number|ref))?\s*[:#\-]*\s*([A-Z0-9-]*\d{5,}[A-Z0-9-]*)", re.I)
# "Item Name - Kunafa Dream Cake" or "items: Latte", anywhere including emails
_ITEM_LABEL = re.compile(r"\bitems?(?:\s*(?:name|id|title))\s*[:\-]\s*\S|\bitems?\s*:\s*\S", re.I)
# In the case itself only, since emails are full of quotes and "for Urban Piper":
# a "quoted name", or "not showing for Russian Honey Cake"
_ITEM_NAMED = re.compile(r"[\"“][^\"”]{3,}[\"”]|\bfor\s+[A-Z][\w&'-]+\s+[A-Z][\w&'-]+")


def _text(case: dict, evidence: list[dict]) -> str:
    """Agents often put the order number in the email rather than the case."""
    mails = [m.get("text") or "" for ev in evidence for m in ev.get("messages") or []]
    return "\n".join([case.get("subject") or "", case.get("description") or "", *mails])


def order_ids(case: dict, evidence: list[dict]) -> list[str]:
    """Order IDs named in the case, then its emails, first seen first."""
    found = [m.strip("-") for m in _ORDER_ID.findall(_text(case, evidence))]
    return list(dict.fromkeys(f for f in found if f))


def _has(case: dict, need: str, text: str) -> bool:
    if need == "business":
        return bool(case.get("business_name"))
    if need == "order":
        return bool(_ORDER.search(text))
    if need == "location":
        return bool(case.get("location_id") or case.get("platform_id"))
    if need == "item":
        own = f"{case.get('subject') or ''}\n{case.get('description') or ''}"
        return bool(_ITEM_LABEL.search(text) or _ITEM_NAMED.search(own))
    if need == "platform":
        return bool(case.get("third_parties"))
    return True


def rule_for(case: dict, rules: list[dict]) -> dict | None:
    for r in rules:
        if case.get("sub_module") in (r.get("sub_modules") or []) or case.get("module") in (r.get("modules") or []):
            return r
    return None


def missing(case: dict, rules: list[dict], evidence: list[dict] | None = None) -> list[str]:
    rule = rule_for(case, rules)
    needs = ["business", *(rule.get("needs") or [] if rule else [])]
    text = _text(case, evidence or [])
    return [LABELS[n] for n in needs if not _has(case, n, text)]


def build(items: list[tuple], cfg: dict, now: datetime) -> dict | None:
    """The Today card's data for aggregator cases raised under the new process.

    items: (case, verdict, evidence)."""
    start = parse_dt(cfg.get("sla_start"))
    rows = []
    for c, _, evidence in items:
        raised = parse_dt(c.get("created_at"))
        if start is None or raised is None or raised < start or now < raised:
            continue
        gaps = missing(c, cfg.get("required_details") or [], evidence)
        if gaps:
            rows.append({"case_id": c["id"], "case_number": c.get("case_number"), "subject": c.get("subject"),
                         "owner": c.get("owner") or "Unassigned", "missing": gaps, "raised_at": raised.isoformat()})
    if not rows:
        return None
    rows.sort(key=lambda r: r["raised_at"], reverse=True)
    lines = ["Hi team, please add these details so the cases can move:", ""]
    lines += [f"- {r['case_number']} ({r['owner']}): {', '.join(r['missing'])}" for r in rows]
    return {"rows": rows, "draft": "\n".join(lines)}
