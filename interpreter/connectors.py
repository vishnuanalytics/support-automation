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
"""

from __future__ import annotations

import logging
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


_ORG_PARAM = {"key": "org", "label": "Salesforce org (blank = default)",
              "type": "string", "required": False}


def _sf_update_fields(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import salesforce
    return salesforce.update_case_fields(
        params["case_id"], dict(params.get("fields") or {}),
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


def _slack_post_message(tenant_id: str | None, org_label: str | None, params: dict) -> dict:
    from . import slack
    return slack.post_message(
        params.get("text", ""), tenant_id=tenant_id, channel=params.get("channel"),
        thread_ts=params.get("thread_ts"),
    )


register_builtin(ConnectorSpec(
    slug="salesforce", label="Salesforce", auth="builtin",
    actions={
        "update_fields": ActionSpec(
            "update_fields", "Update fields on a Case",
            params=[{"key": "case_id", "label": "Case Id", "type": "template", "required": True},
                    {"key": "fields", "label": "Fields (JSON)", "type": "json", "required": True},
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
    },
))

register_builtin(ConnectorSpec(
    slug="slack", label="Slack", auth="builtin",
    actions={
        "post_message": ActionSpec(
            "post_message", "Post a Slack message",
            params=[{"key": "text", "label": "Text", "type": "template", "required": True},
                    {"key": "channel", "label": "Channel", "type": "string", "required": True},
                    {"key": "thread_ts", "label": "Thread ts", "type": "template", "required": False}],
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
