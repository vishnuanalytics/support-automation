"""
Chunk-2 of the case-graph enrichment: pull the *what* out of a resolved
case's text — the root cause and any external integrations involved — so
the graph can answer questions the module / sub-module tree can't
("cases whose root cause was a Salesforce sync failure").

One bounded LLM call (classify-tier model), JSON out. Integration names
are snapped to a small allow-list so the graph doesn't sprout a node per
spelling. Best-effort: any failure returns the empty result.

NOT wired into the sync hot path by default — `ingestion/case_graph_sync`
calls this only when `CASE_EXTRACT=1`, so a full backfill doesn't
silently cost one LLM call per case.
"""

from __future__ import annotations

import json
import logging
import os

from interpreter import llm

log = logging.getLogger("interpreter.case_extract")

_MODEL = os.environ.get("CASE_EXTRACT_MODEL", "openai/gpt-oss-20b")
_ROOT_CAUSE_MAX = 80
_EMPTY: dict = {"root_cause": None, "integrations": []}

# The integration names the graph will carry. Flat + lowercased for
# matching, Title-cased for the node name. A tenant can extend this later
# through `case_taxonomy` config; kept module-level for now.
_DEFAULT_INTEGRATIONS = [
    "salesforce", "zendesk", "hubspot", "freshdesk", "freshchat", "intercom",
    "slack", "zapier", "stripe", "razorpay", "paypal", "shopify",
    "zomato", "swiggy", "ubereats", "doordash", "deliveroo", "talabat",
    "google", "meta", "whatsapp", "twilio", "sendgrid", "mailgun",
    "aws", "gcp", "azure", "webhook", "rest api", "graphql", "sftp",
    "csv import", "pos",
]


def integration_vocab(tenant_id: str | None = None) -> dict[str, str]:
    """`{lowercase match key: display name}`. Tenant hook is a no-op today."""
    return {n.lower(): (n.upper() if n in ("aws", "gcp", "pos", "sftp", "rest api")
                        else n.title())
            for n in _DEFAULT_INTEGRATIONS}


_SYS = (
    "You read one resolved support case and report two things as JSON:\n"
    '{"root_cause": <string|null>, "integrations": [<string>...]}\n'
    "- root_cause: the underlying fault in a short noun phrase (<=10 words) "
    "— NOT the symptom the customer saw and NOT the fix that was applied. "
    "null if the text doesn't actually say what went wrong.\n"
    "- integrations: external systems/products the case implicates "
    "(e.g. Salesforce, Zomato, Stripe, a webhook). Empty list if none. "
    "Only list one if the case text actually names or clearly implies it."
)


def extract_case_signals(
    subject: str | None,
    body: str | None,
    resolution: str | None,
    *,
    tenant_id: str | None = None,
) -> dict:
    """`{root_cause: str|None, integrations: [str]}` — never raises."""
    text = "\n".join(
        t for t in (
            f"Subject: {subject}" if subject else "",
            f"Customer wrote: {(body or '')[:1500]}" if body else "",
            f"How it was resolved: {(resolution or '')[:1500]}" if resolution else "",
        ) if t
    ).strip()
    if not text:
        return dict(_EMPTY)
    try:
        raw = llm.complete(_SYS, text, model=_MODEL, json_object=True,
                           max_tokens=200, cache=True, tenant_id=tenant_id)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return dict(_EMPTY)
    except Exception as e:  # noqa: BLE001
        log.warning("case_extract(%s): %s", subject, e)
        return dict(_EMPTY)

    rc = data.get("root_cause")
    rc = (str(rc).strip()[:_ROOT_CAUSE_MAX] or None) if rc else None

    vocab = integration_vocab(tenant_id)
    out: list[str] = []
    for raw_name in (data.get("integrations") or [])[:8]:
        key = str(raw_name).strip().lower()
        if not key:
            continue
        hit = vocab.get(key) or next(
            (v for k, v in vocab.items() if k in key or key in k), None)
        if hit and hit not in out:
            out.append(hit)
    return {"root_cause": rc, "integrations": out}
