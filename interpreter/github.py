"""
GitHub connector (Phase 16) — open an issue once a human approves in Slack.

Per-tenant token in `tenant_integrations (kind='github')` (`{token, ...}`),
falling back to a shared `GITHUB_TOKEN` env var. A fine-grained PAT with
"Issues: read & write" on the target repo(s) is enough.
"""

from __future__ import annotations

import os
from typing import Any

_API = "https://api.github.com"


def token_for(tenant_id: str | None, sb) -> str:
    if tenant_id and sb is not None:
        from interpreter import vault_secrets

        token = vault_secrets.get(tenant_id, "github", sb=sb).get("token")
        if token:
            return token
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise RuntimeError(f"no GitHub token for tenant {tenant_id} and GITHUB_TOKEN unset")
    return tok


def available(tenant_id: str | None = None, sb=None) -> bool:
    try:
        return bool(token_for(tenant_id, sb))
    except Exception:  # noqa: BLE001
        return False


def create_issue(token: str, repo: str, *, title: str, body: str = "",
                 labels: list[str] | None = None,
                 assignees: list[str] | None = None) -> dict[str, Any]:
    """repo = 'owner/name'. Returns {html_url, number}."""
    import requests

    if "/" not in (repo or ""):
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    r = requests.post(
        f"{_API}/repos/{repo}/issues", json=payload,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"github issues API {r.status_code}: {r.text[:300]}")
    j = r.json()
    return {"html_url": j["html_url"], "number": j["number"]}


def _get(token: str, path: str) -> Any:
    import requests

    r = requests.get(
        f"{_API}{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"github API {r.status_code} on {path}: {r.text[:300]}")
    return r.json()


def get_issue(token: str, repo: str, number: int) -> dict[str, Any]:
    """repo = 'owner/name'. -> {number, state, html_url, title, labels: [str]}.
    Used by the KB write-back watch (chunk 2) to see when a maintainer has
    closed the verification issue."""
    j = _get(token, f"/repos/{repo}/issues/{number}")
    return {
        "number": j["number"], "state": j["state"], "html_url": j["html_url"],
        "title": j.get("title", ""),
        "labels": [lbl["name"] for lbl in j.get("labels", []) if isinstance(lbl, dict)],
    }


def list_issue_comments(token: str, repo: str, number: int) -> list[dict[str, Any]]:
    """-> [{id, body, user}], oldest first."""
    j = _get(token, f"/repos/{repo}/issues/{number}/comments")
    return [{"id": c["id"], "body": c.get("body", ""),
             "user": (c.get("user") or {}).get("login", "")} for c in j]
