"""Per-tenant Case Monitor settings.

Same shape as `case_taxonomy`: a tenant row only carries overrides of
`DEFAULT_CONFIG`. A tenant's custom fields, waiting values and thresholds all
live here, so nothing in the code is specific to one customer.
"""

from __future__ import annotations

import copy
import re
from typing import Any

SOQL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")
USERNAME = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")
GH_USER = re.compile(r"^[A-Za-z0-9-]+(\[bot\])?$")
PARTIES = ("engineering", "third_party")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "case_connector": "salesforce",
    "org_label": None,
    "owner_username": "",
    "lightning_url": "",
    # logical name -> Salesforce API field; "" means the org doesn't have it
    "fields": {
        "waiting_on": "", "waiting_sub_status": "", "link": "", "third_party": "",
        "target_at": "", "ageing": "", "last_reply_from": "", "brand": "",
        "biz_id": "", "business_id": "", "product": "", "module": "", "sub_module": "",
        "case_type": "Type", "escalation_level": "", "escalated": "",
    },
    "waiting_values": {},             # {"Engineer": "engineering", ...}
    "customer_reply_values": [],      # values of last_reply_from meaning customer spoke last
    # waiting_sub_status values: a solution was given, the customer must confirm
    "customer_wait_sub_statuses": [],
    # waiting_sub_status values: the other team answered, so it's our turn
    "team_replied_sub_statuses": [],
    # record type used to raise a case with a third party; its emails are follow-up evidence
    "dependency_record_type": "",
    "eng_nudge_hours": 8,
    "third_party_nudge_hours": 24,
    "third_party_overrides": {},      # {"PartnerA": 12}
    "third_party_label": "Third party",
    "customer_label": "Customer",
    "support_github_users": [],
    "internal_slack_domain": "",
    "slack_team_mention": "",         # e.g. "<!subteam^S123>", used by Tag team
    "sync_every_min": 60,             # 0 disables interval syncs
    "sync_hour": 9,
    "timezone": "Asia/Kolkata",
    "resolved_lookback_days": 180,
    "llm_enabled": False,
}


def merge(override: dict | None) -> dict:
    out = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in (override or {}).items():
        if key == "fields":
            out["fields"].update(value or {})
        elif key in out:
            out[key] = value
    return out


def validate(config: Any) -> list[str]:
    if not isinstance(config, dict):
        return ["config must be an object"]
    errors = [f"unknown key: {k}" for k in config if k not in DEFAULT_CONFIG]
    fields = config.get("fields", {})
    if not isinstance(fields, dict):
        return errors + ["fields must be an object"]
    for key, name in fields.items():
        if key not in DEFAULT_CONFIG["fields"]:
            errors.append(f"unknown field: {key}")
        elif name and not SOQL_NAME.match(str(name)):
            errors.append(f"invalid field name: {key}")
    owner = config.get("owner_username")
    if owner and not USERNAME.match(str(owner)):
        errors.append("owner_username is not a valid username")
    waiting = config.get("waiting_values", {})
    if not isinstance(waiting, dict) or any(v not in PARTIES for v in waiting.values()):
        errors.append(f"waiting_values must map to one of {PARTIES}")
    for key in ("eng_nudge_hours", "third_party_nudge_hours"):
        if key in config and not (isinstance(config[key], (int, float)) and config[key] > 0):
            errors.append(f"{key} must be a positive number")
    if any(not GH_USER.match(str(u)) for u in config.get("support_github_users", [])):
        errors.append("support_github_users has an invalid username")
    return errors
