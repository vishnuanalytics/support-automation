"""Merge every source into one chronological case timeline. Pure."""

from __future__ import annotations

import re
from typing import Any

from .verdict import email_party as verdict_email_party, parse_dt

US, CUSTOMER, ENGINEERING, THIRD_PARTY, SYSTEM = (
    "us", "customer", "engineering", "third_party", "system")


def party_label(party: str, cfg: dict) -> str:
    return {US: cfg.get("our_label") or "Our team",
            CUSTOMER: cfg.get("customer_label") or "Customer",
            ENGINEERING: "Engineering",
            THIRD_PARTY: cfg.get("third_party_label") or "Third party",
            SYSTEM: "System"}[party]


def tidy(text: str | None, cfg: dict) -> str:
    """Make a message readable: no footer, and no gaps from email formatting."""
    body = text or ""
    for marker in cfg.get("email_footer_markers") or []:
        at = body.lower().find(marker.lower())
        if at > 0:
            body = body[:at]
    lines = [line.strip() for line in body.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _item(at: Any, source: str, party: str, who: str | None, text: str,
          url: str | None, cfg: dict, subject: str | None = None) -> dict:
    return {"at": at, "source": source, "party": party, "party_label": party_label(party, cfg),
            "who": who, "text": text, "url": url, "subject": subject}


def _new_subject(subject: str | None, case: dict) -> str | None:
    """A thread's subject only adds anything when it differs from the case's."""
    bare = re.sub(r"^((re|fw|fwd)\s*:\s*)+", "", subject or "", flags=re.I).strip()
    return bare if bare and bare.lower() != (case.get("subject") or "").strip().lower() else None


def merge(case: dict, raw: dict | None, evidence: list[dict], cfg: dict) -> list[dict]:
    raw = raw or {}
    url = case.get("url")
    tracked = {cfg["fields"].get("waiting_on"), cfg["fields"].get("waiting_sub_status"),
               "Owner", "ownerAssignment", "Status"}
    items: list[dict] = []
    for e in raw.get("emails") or []:
        party = verdict_email_party(e.get("from"), bool(e.get("incoming")), case, cfg)
        items.append(_item(e.get("at"), "Email", party, e.get("from"),
                           tidy(e.get("text"), cfg) or (e.get("subject") or ""), url, cfg,
                           _new_subject(e.get("subject"), case)))
    for c in raw.get("comments") or []:
        items.append(_item(c.get("at"), "Case comment", US, c.get("by"),
                           tidy(c.get("text"), cfg), url, cfg))
    for h in raw.get("history") or []:
        if h.get("field") in tracked:
            text = f"{h['field']}: {h.get('old') or '(empty)'} → {h.get('new') or '(empty)'}"
            items.append(_item(h.get("at"), "Field change", SYSTEM, h.get("by"), text, url, cfg))
    for ev in evidence:
        if not ev.get("ok"):
            continue
        if ev["kind"] == "github":
            source, other = "GitHub", ENGINEERING
        elif ev["kind"] == "slack":
            source = "Slack"
            other = THIRD_PARTY if case.get("waiting_party") == THIRD_PARTY else ENGINEERING
        elif ev["kind"] == "email" and ev.get("related"):
            source, other = "DSP email", THIRD_PARTY
        else:
            continue
        for m in ev.get("messages") or []:
            items.append(_item(m.get("at"), source, US if m.get("ours") else other,
                               m.get("who"), tidy(m.get("text"), cfg),
                               m.get("url") or ev.get("url"), cfg))
    # Undated items sort last rather than breaking the merge.
    items.sort(key=lambda i: (parse_dt(i["at"]) is None,
                              parse_dt(i["at"]).timestamp() if parse_dt(i["at"]) else 0))
    return items
