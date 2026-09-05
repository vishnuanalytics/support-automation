"""
Declarative connector registry (FR-47).

A "connector" is either a **builtin** (`salesforce`, `slack` — thin wrappers
around the existing, unmodified `interpreter/salesforce.py` /
`interpreter/slack.py` modules) or one of a tenant's own named HTTP
`connections` (`interpreter/connections.py`), each carrying its own saved
`connection_actions` rows. Either way, the generic `connector_action` flow
node (`registry.py`) talks to both kinds through the exact same interface:

    list_connectors(tenant_id)                       -> [ConnectorSpec, ...]
    get_action(tenant_id, connector, action)          -> (ConnectorSpec, ActionSpec)
    ActionSpec.impl(tenant_id, org_label, params)     -> dict

This is the "connector is data, not a hardcoded node handler" layer FR-47
called for: adding a new connector or action for a tenant's own HTTP API
never touches `registry.py` — it's a row in `connections`/`connection_actions`,
managed entirely from the web UI. The two builtins below are the exception
(Salesforce/Slack need real SDK/API-shaped logic, not raw HTTP), registered
once here as thin wrappers, not reimplemented.

2026-09-04 — the 9 originally SF/Slack-hardwired node handlers in
`registry.py` (`sf_writeback`, `sf_case`, `notify`, `ask_human`, `handover`,
`identify`, `clarify`, plus `alert.alert_human` behind `notify_human`) were
migrated to call their Salesforce/Slack side effects through `invoke()`
below instead of importing `salesforce`/`slack` and calling a verb directly
— proving the *existing* production behavior can be expressed the same way
a brand-new connector now can, with zero change to any node's config shape,
output shape, or the seeded flows that use them. `sf_context` is a
deliberate exception (see its own module) — it's a bespoke multi-object
SOQL read, not a single named write action, and doesn't fit this shape
without forcing it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("interpreter.connectors")

ActionImpl = Callable[[str | None, str | None, dict[str, Any]], dict[str, Any]]


@dataclass
class ActionSpec:
    name: str
    description: str
    params: list[dict[str, Any]]  # [{key, label, type: "string"|"template"|"json"|"select", required, options?}]
    impl: ActionImpl


@dataclass
class ConnectorSpec:
    slug: str
    label: str
    auth: str  # "builtin" | "apikey" | "oauth2" | "none"
    actions: dict[str, ActionSpec] = field(default_factory=dict)


_BUILTINS: dict[str, ConnectorSpec] = {}


def register_builtin(spec: ConnectorSpec) -> None:
    _BUILTINS[spec.slug] = spec


# --------------------------------------------------------------------------
# Multi-provider connectors, step 1 (2026-09-05) — which connector is "the
# case system" for a tenant is now data (migration 084: tenants.case_connector),
# not a literal `"salesforce"` hardcoded at 15+ call sites in registry.py /
# alert.py. `salesforce` is still the only real implementation; this is the
# seam a future Zendesk/HubSpot connector plugs into by registering a
# builtin with these exact action names — the contract every case-touching
# node handler (sf_case/sf_writeback/notify/ask_human/handover/identify/
# clarify/notify_human) actually calls.
# --------------------------------------------------------------------------
CASE_ACTIONS = (
    "update_fields", "post_note", "add_comment", "assign_owner",
    "ensure_case", "log_email_message", "identify_sender", "send_case_reply",
)


def _sb():
    from ingestion.scraper import get_supabase
    return get_supabase()


def resolve_case_connector(tenant_id: str | None, config: dict[str, Any] | None,
                          *, sb=None) -> str:
    """Which connector slug a case-touching node handler should invoke this
    call — resolved fresh each time (no caching), matching this module's
    existing per-call-read style (`connections.resolve`, `vault_secrets.get`).

    Precedence: an explicit per-node `config["connector"]` override (the
    same field the generic `connector_action` node already uses) >
    `tenants.case_connector` (this tenant's default) > `"salesforce"` — so
    a flow/tenant with neither set behaves exactly as before this existed.
    """
    override = (config or {}).get("connector")
    if override:
        return str(override)
    if not tenant_id:
        return "salesforce"
    if sb is None and "PYTEST_CURRENT_TEST" in os.environ:
        return "salesforce"           # offline tests monkeypatch this or pass sb (matches routing.py)
    try:
        rows = ((sb or _sb()).table("tenants").select("case_connector")
                .eq("tenant_id", tenant_id).execute().data or [])
        return (rows[0].get("case_connector") if rows else None) or "salesforce"
    except Exception as e:  # noqa: BLE001
        log.warning("resolve_case_connector(%s): %s", tenant_id, e)
        return "salesforce"


_ORG_PARAM = {"key": "org", "label": "Salesforce org (blank = default)",
              "type": "string", "required": False}


def _as_bool(v: Any, default: bool) -> bool:
    """A param may arrive as a real Python bool (an internal `invoke()` call
    passing a real dict) or as the string "true"/"false" (the generic
    connector_action UI's `select` param type) — normalize either."""
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "")
    return bool(v)


def _sf_update_fields(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.update_case_fields(
        params["case_id"], dict(params.get("fields") or {}),
        append=dict(params.get("append") or {}),
        tenant_id=tenant_id, org_label=org_label or params.get("org"),
    )


def _sf_post_note(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.post_chatter(
        params["case_id"], params.get("body", ""), mention_id=params.get("mention_id"),
        tenant_id=tenant_id, org_label=org_label or params.get("org"),
    )


def _sf_add_comment(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.add_case_comment(
        params["case_id"], params.get("body", ""), published=bool(params.get("published", False)),
        tenant_id=tenant_id, org_label=org_label or params.get("org"),
    )


def _sf_assign_owner(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.assign_case(
        params["case_id"], queue=params.get("queue"), user_id=params.get("user_id"),
        tenant_id=tenant_id, org_label=org_label or params.get("org"),
    )


def _sf_ensure_case(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.ensure_case(
        dict(params.get("case") or {}), dict(params.get("sender") or {}),
        origin=params.get("origin", "Email"), status=params.get("status", "New"),
        create_contact=_as_bool(params.get("create_contact"), True),
        create_account=_as_bool(params.get("create_account"), True),
        reuse=str(params.get("reuse", "thread")),
        tenant_id=tenant_id, org_label=org_label or params.get("org"),
    )


def _sf_log_email_message(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.log_email_message(
        params["case_id"], incoming=_as_bool(params.get("incoming"), True),
        from_addr=params.get("from_addr", ""), from_name=params.get("from_name", ""),
        to_addrs=params.get("to_addrs", ""), subject=params.get("subject", ""),
        body=params.get("body", ""), message_id=params.get("message_id", ""),
        status=params.get("status"),
        tenant_id=tenant_id, org_label=org_label or params.get("org"),
    )


def _sf_identify_sender(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.identify_sender(
        params.get("email", ""), free_domains=params.get("free_domains"),
        domain_match=_as_bool(params.get("domain_match"), True),
        create_lead=_as_bool(params.get("create_lead"), False),
        tenant_id=tenant_id, org_label=org_label or params.get("org"),
    )


def _sf_send_case_reply(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.send_case_reply(
        params["case_id"], params.get("body", ""),
        to_email=params.get("to_email"), subject=params.get("subject"),
        tenant_id=tenant_id, org_label=org_label or params.get("org"),
    )


def _slack_post_message(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import slack
    return slack.post_message(
        params.get("text", ""), tenant_id=tenant_id, channel=params.get("channel"),
        thread_ts=params.get("thread_ts"), webhook=params.get("webhook"),
        blocks=params.get("blocks"),
    )


register_builtin(ConnectorSpec(
    slug="salesforce", label="Salesforce", auth="builtin",
    actions={
        "update_fields": ActionSpec(
            "update_fields", "Update fields on a Case",
            params=[{"key": "case_id", "label": "Case Id", "type": "template", "required": True},
                    {"key": "fields", "label": "Fields (JSON)", "type": "json", "required": True},
                    {"key": "append", "label": "Append to fields (JSON: field -> text)",
                     "type": "json", "required": False},
                    _ORG_PARAM],
            impl=_sf_update_fields),
        "post_note": ActionSpec(
            "post_note", "Post a Chatter note on a Case",
            params=[{"key": "case_id", "label": "Case Id", "type": "template", "required": True},
                    {"key": "body", "label": "Note", "type": "template", "required": True},
                    {"key": "mention_id", "label": "@mention User/Group Id", "type": "template", "required": False},
                    _ORG_PARAM],
            impl=_sf_post_note),
        "add_comment": ActionSpec(
            "add_comment", "Add an internal CaseComment",
            params=[{"key": "case_id", "label": "Case Id", "type": "template", "required": True},
                    {"key": "body", "label": "Comment", "type": "template", "required": True},
                    _ORG_PARAM],
            impl=_sf_add_comment),
        "assign_owner": ActionSpec(
            "assign_owner", "Route a Case to a queue or user",
            params=[{"key": "case_id", "label": "Case Id", "type": "template", "required": True},
                    {"key": "queue", "label": "Queue name", "type": "string", "required": False},
                    {"key": "user_id", "label": "User Id", "type": "string", "required": False},
                    _ORG_PARAM],
            impl=_sf_assign_owner),
        "ensure_case": ActionSpec(
            "ensure_case", "Resolve/create a Case (+ Contact/Account) for an inbound message",
            params=[{"key": "case", "label": "Case (JSON)", "type": "json", "required": True},
                    {"key": "sender", "label": "Sender (JSON)", "type": "json", "required": False},
                    {"key": "origin", "label": "Origin", "type": "string", "required": False},
                    {"key": "status", "label": "Status", "type": "string", "required": False},
                    {"key": "reuse", "label": "Reuse", "type": "select", "required": False,
                     "options": ["thread", "never"]},
                    _ORG_PARAM],
            impl=_sf_ensure_case),
        "log_email_message": ActionSpec(
            "log_email_message", "Add an EmailMessage to a Case",
            params=[{"key": "case_id", "label": "Case Id", "type": "template", "required": True},
                    {"key": "incoming", "label": "Incoming", "type": "select", "required": False,
                     "options": ["true", "false"]},
                    {"key": "from_addr", "label": "From", "type": "template", "required": False},
                    {"key": "to_addrs", "label": "To", "type": "template", "required": False},
                    {"key": "subject", "label": "Subject", "type": "template", "required": False},
                    {"key": "body", "label": "Body", "type": "template", "required": False},
                    {"key": "message_id", "label": "Message-Id", "type": "template", "required": False},
                    _ORG_PARAM],
            impl=_sf_log_email_message),
        "identify_sender": ActionSpec(
            "identify_sender", "Resolve a sender email to a Contact/Lead/Account",
            params=[{"key": "email", "label": "Email", "type": "template", "required": True},
                    {"key": "domain_match", "label": "Domain match", "type": "select", "required": False,
                     "options": ["true", "false"]},
                    {"key": "create_lead", "label": "Create lead if missing", "type": "select",
                     "required": False, "options": ["true", "false"]},
                    _ORG_PARAM],
            impl=_sf_identify_sender),
        "send_case_reply": ActionSpec(
            "send_case_reply", "Send a customer-facing reply on a Case",
            params=[{"key": "case_id", "label": "Case Id", "type": "template", "required": True},
                    {"key": "body", "label": "Body", "type": "template", "required": True},
                    {"key": "to_email", "label": "To", "type": "template", "required": False},
                    {"key": "subject", "label": "Subject", "type": "template", "required": False},
                    _ORG_PARAM],
            impl=_sf_send_case_reply),
    },
))

register_builtin(ConnectorSpec(
    slug="slack", label="Slack", auth="builtin",
    actions={
        "post_message": ActionSpec(
            "post_message", "Post a Slack message",
            params=[{"key": "text", "label": "Text", "type": "template", "required": True},
                    {"key": "channel", "label": "Channel", "type": "string", "required": True},
                    {"key": "thread_ts", "label": "Thread ts", "type": "template", "required": False},
                    {"key": "webhook", "label": "Webhook URL override", "type": "string", "required": False},
                    {"key": "blocks", "label": "Block Kit blocks (JSON)", "type": "json", "required": False}],
            impl=_slack_post_message),
    },
))


def list_connectors(tenant_id: str | None, *, sb=None) -> list[ConnectorSpec]:
    """Builtins + this tenant's own HTTP connections, each exposed as a
    connector whose actions are its saved `connection_actions` rows."""
    specs = list(_BUILTINS.values())
    if tenant_id:
        from . import connections
        specs.extend(connections.as_connectors(tenant_id, sb=sb))
    return specs


def get_action(tenant_id: str | None, connector_slug: str | None,
                action_name: str | None, *, sb=None) -> tuple[ConnectorSpec, ActionSpec]:
    if not (connector_slug and action_name):
        raise KeyError(f"connector={connector_slug!r} action={action_name!r} required")
    spec = _BUILTINS.get(connector_slug)
    if spec is None:
        from . import connections
        spec = connections.as_connector(tenant_id, connector_slug, sb=sb)
    if spec is None:
        raise KeyError(f"unknown connector {connector_slug!r}")
    action = spec.actions.get(action_name)
    if action is None:
        raise KeyError(f"unknown action {action_name!r} on connector {connector_slug!r}")
    return spec, action


def invoke(tenant_id: str | None, connector_slug: str, action_name: str,
           params: dict[str, Any], *, org_label: str | None = None, sb=None) -> dict[str, Any]:
    """Convenience for an internal caller (a migrated node handler) that
    already has real Python values in hand — skips the `connector_action`
    node's own param-templating, which is only needed when values come from
    a flow's own JSON config. Raises `KeyError` for an unknown connector/
    action; whatever the action's `impl` raises otherwise propagates too —
    callers keep whatever try/except semantics they already had."""
    _spec, action = get_action(tenant_id, connector_slug, action_name, sb=sb)
    return action.impl(tenant_id, org_label, params)
