"""Merge every source into one chronological case timeline. Pure."""

from __future__ import annotations

from typing import Any

from .verdict import parse_dt

US, CUSTOMER, ENGINEERING, THIRD_PARTY, SYSTEM = (
    "us", "customer", "engineering", "third_party", "system")


def party_label(party: str, cfg: dict) -> str:
    return {US: cfg.get("our_label") or "Our team",
            CUSTOMER: cfg.get("customer_label") or "Customer",
            ENGINEERING: "Engineering",
            THIRD_PARTY: cfg.get("third_party_label") or "Third party",
            SYSTEM: "System"}[party]


def _item(at: Any, source: str, party: str, who: str | None, text: str,
          url: str | None, cfg: dict) -> dict:
    return {"at": at, "source": source, "party": party, "party_label": party_label(party, cfg),
            "who": who, "text": text, "url": url}


def merge(case: dict, raw: dict | None, evidence: list[dict], cfg: dict) -> list[dict]:
    raw = raw or {}
    url = case.get("url")
    tracked = {cfg["fields"].get("waiting_on"), cfg["fields"].get("waiting_sub_status"),
               "Owner", "ownerAssignment", "Status"}
    items: list[dict] = []
    for e in raw.get("emails") or []:
        inbound = THIRD_PARTY if case.get("record_type") == cfg.get("dependency_record_type") \
            and cfg.get("dependency_record_type") else CUSTOMER
        party = inbound if e.get("incoming") else US
        text = f"{e.get('subject') or ''}\n{e.get('text') or ''}".strip()
        items.append(_item(e.get("at"), "Email", party, e.get("from"), text, url, cfg))
    for c in raw.get("comments") or []:
        items.append(_item(c.get("at"), "Case comment", US, c.get("by"), c.get("text") or "",
                           url, cfg))
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
                               m.get("who"), m.get("text") or "", m.get("url") or ev.get("url"),
                               cfg))
    # Undated items sort last rather than breaking the merge.
    items.sort(key=lambda i: (parse_dt(i["at"]) is None,
                              parse_dt(i["at"]).timestamp() if parse_dt(i["at"]) else 0))
    return items
