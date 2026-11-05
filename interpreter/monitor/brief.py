"""The ten-second case brief, built only from fields and evidence. Pure.

Every line carries a source; a field with no value is left out, not guessed.
"""

from __future__ import annotations

from typing import Any

from .timeline import US
from .verdict import AI, CUSTOMER, ENGINEERING, THIRD_PARTY, parse_dt

_BALL_TEXT = {US: "Our turn", ENGINEERING: "Waiting on Engineering", AI: "AI agent working",
              CUSTOMER: "Waiting on the customer", "unknown": "Turn unknown"}


def _sf(cfg: dict, key: str, url: str | None) -> dict:
    return {"label": f"Salesforce: {cfg['fields'].get(key) or key}", "url": url}


def waiting_since(case: dict, raw: dict | None, cfg: dict) -> str | None:
    """When the waiting field last moved to its current value."""
    field = cfg["fields"].get("waiting_on")
    changes = [h for h in (raw or {}).get("history") or []
               if h.get("field") == field and h.get("new") == case.get("waiting_raw")]
    dated = [h["at"] for h in changes if parse_dt(h.get("at"))]
    return max(dated, key=parse_dt) if dated else case.get("created_at")


def build(case: dict, verdict: dict, raw: dict | None, timeline: list[dict],
          cfg: dict) -> dict[str, Any]:
    url = case.get("url")
    facts = []
    for key, label, field in (("brand", "Brand", "brand"), ("biz_id", "BizID", "biz_id"),
                              ("business_id", "Business ID", "business_id"),
                              ("product", "Product", "product")):
        if case.get(key):
            facts.append({"label": label, "value": case[key], "source": _sf(cfg, field, url)})
    if case.get("owner"):
        facts.append({"label": "Owner", "value": case["owner"],
                      "source": {"label": "Salesforce: Owner", "url": url}})
    if case.get("third_parties"):
        facts.append({"label": cfg.get("third_party_label", "Third party"),
                      "value": ", ".join(case["third_parties"]),
                      "source": _sf(cfg, "third_party", url)})

    done = [i for i in timeline if i["party"] == US][-3:]
    since = waiting_since(case, raw, cfg)
    label = (cfg.get("third_party_label", "Third party") if verdict["ball"] == THIRD_PARTY
             else None)
    ball_text = f"Waiting on {label}" if label else _BALL_TEXT[verdict["ball"]]
    if verdict["ball"] == CUSTOMER:
        ball_text = f"Waiting on the {cfg.get('customer_label', 'customer').lower()}"
    waiting = None
    if case.get("waiting_raw"):
        waiting = {"text": f"Marked waiting on {case['waiting_raw']}",
                   "since": since, "source": _sf(cfg, "waiting_on", url)}

    return {
        "problem": {"text": case.get("subject") or "(no subject)",
                    "source": {"label": "Salesforce: Subject", "url": url}},
        "facts": facts,
        "done_so_far": [{"text": i["text"], "at": i["at"], "who": i["who"],
                         "source": {"label": i["source"], "url": i["url"]}} for i in done],
        "ball": {"text": ball_text, "mismatch": verdict["mismatch"]},
        "waiting": waiting,
        "next_action": verdict["next_action"],
    }
