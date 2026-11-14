"""Per-tenant Case Monitor settings.

Same shape as `case_taxonomy`: a tenant row only carries overrides of
`DEFAULT_CONFIG`. A tenant's custom fields, waiting values and thresholds all
live here, so nothing in the code is specific to one customer.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from . import auto_checks
from .verdict import parse_dt

SOQL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")
USERNAME = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")
GH_USER = re.compile(r"^[A-Za-z0-9-]+(\[bot\])?$")
PARTIES = ("engineering", "third_party")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "case_connector": "salesforce",
    "org_label": None,
    "owner_username": "",
    "lead_owner_name": "",            # the case-system name shown as "Mine" in the queue
    "lightning_url": "",
    # logical name -> Salesforce API field; "" means the org doesn't have it
    "fields": {
        "waiting_on": "", "waiting_sub_status": "", "link": "", "third_party": "",
        "target_at": "", "ageing": "", "last_reply_from": "", "brand": "",
        "biz_id": "", "business_id": "", "business_name": "Account.Name", "location_id": "", "platform_id": "", "product": "", "module": "", "sub_module": "", "bu": "",
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
    # shown with a stale third-party case, e.g. who to escalate to
    "third_party_escalation": "",
    "eng_nudge_hours": 8,
    "third_party_nudge_hours": 24,
    "third_party_overrides": {},      # {"PartnerA": 12}
    "escalation_days": 5,             # no update for this long puts a case on the Escalations page
    "tech_pending_days": 7,           # the Engineering page's age filter
    "github_support_label": "support",  # the Engineering page lists open issues with this label
    "github_reporter_field": "",      # the bold field in an issue's body naming the agent who raised it
    "wrong_raise_labels": [],         # closing labels meaning an issue didn't need an engineer
    "issue_chase_hours": 4,           # an issue with no tech reply for this long goes on Today
    "issue_quiet_hours": 24,          # so does an issue nobody has touched for this long
    # [{"name", "modules", "sub_modules", "keywords", "escalate_hours", "resolve_hours"}], first match wins
    "dsp_sla_rules": [],
    # [{"modules", "sub_modules", "needs"}], first match wins; needs: order, location, item, platform
    "required_details": [],
    # [{"name", "modules", "sub_modules", "checks"}], first match wins; checks are kinds in
    # auto_checks.KINDS (order, order_status, order_items, item_stock, menu_publish, store_actions)
    "auto_checks": [],
    "auto_checks_dsp": True,          # run them on new aggregator cases
    "auto_checks_engineering": False,  # and on new cases raised to engineering
    # check kind -> the saved question that does it: {"order": {"card_id": "123", "name": "..."}}
    "check_questions": {},
    "needs_reply_hours": 24,          # an outside reply left this long goes on Needs Me
    "week_start_weekday": 2,          # the Wednesday report's week; Monday is 0
    "platform_aliases": {},           # {"PartnerA": "catalogue-platform-name"} when the names differ
    "log_retention_days": {},         # {"PartnerA": 7}, for aggregators that delete logs early
    "log_topics": [],                 # case modules or sub-modules that depend on those logs
    "log_warn_days": 3,               # warn when a case has this many days of logs left
    "sla_start": "",                  # cases raised before this ISO time are the old backlog
    "dsp_escalate_share": 0.5,        # with no rule, escalate this far through the target time
    "assist_hours": {},               # {"start": 11, "end": 20, "tz": "Area/City"}, the lead's own hours
    "github_squad_suffix": "-squad",  # the issue label ending in this names its squad
    "third_party_label": "Third party",
    "customer_label": "Customer",
    "support_github_users": [],
    "github_bot_users": [],           # automation accounts GitHub doesn't mark as bots
    "internal_slack_domain": "",
    "internal_email_domains": [],     # senders on these domains are our own team
    "email_footer_markers": [],       # text after one of these is a signature or footer
    "focus_partners": [],             # third parties shown as cards above the queue
    "slack_team_mention": "",         # e.g. "<!subteam^S123>", used by Tag team
    "slack_via_claude": False,        # threads are imported from a Claude session, with no app login
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
    for key in ("eng_nudge_hours", "third_party_nudge_hours", "escalation_days",
                "tech_pending_days", "needs_reply_hours"):
        if key in config and not (isinstance(config[key], (int, float)) and config[key] > 0):
            errors.append(f"{key} must be a positive number")
    for rule in config.get("dsp_sla_rules", []):
        if not (rule.get("name") and all(isinstance(rule.get(k), (int, float)) and rule[k] > 0
                                         for k in ("escalate_hours", "resolve_hours"))):
            errors.append("each dsp_sla_rules entry needs a name, escalate_hours and resolve_hours")
    if not isinstance(config.get("log_retention_days", {}), dict) or any(
            not (isinstance(v, (int, float)) and v > 0) for v in (config.get("log_retention_days") or {}).values()):
        errors.append("log_retention_days must map each aggregator to a positive number")
    errors += auto_checks.validate(config)
    for key in ("auto_checks_dsp", "auto_checks_engineering"):
        if key in config and not isinstance(config[key], bool):
            errors.append(f"{key} must be true or false")
    if config.get("week_start_weekday", 0) not in range(7):
        errors.append("week_start_weekday must be 0 (Monday) to 6 (Sunday)")
    if config.get("sla_start") and parse_dt(config["sla_start"]) is None:
        errors.append("sla_start must be an ISO date and time")
    if not str(config.get("github_support_label", "support")).strip():
        errors.append("github_support_label can't be empty")
    for key in ("support_github_users", "github_bot_users"):
        if any(not GH_USER.match(str(u)) for u in config.get(key, [])):
            errors.append(f"{key} has an invalid username")
    return errors
