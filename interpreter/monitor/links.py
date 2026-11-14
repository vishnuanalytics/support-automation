"""Parse free-text external-reference fields into classified links."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .redact import strip_signed_url

_URL = re.compile(r"https?://[^\s<>\"'|]+")
_TRAILING = ".,;:!?)]}>'\""
_GH = re.compile(r"^/([^/]+)/([^/]+)/(issues|pull)/(\d+)")
_SLACK = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")


def extract_urls(text: str | None) -> list[str]:
    seen: dict[str, None] = {}
    for raw in _URL.findall(text or ""):
        seen.setdefault(strip_signed_url(raw.rstrip(_TRAILING)))
    return list(seen)


def slack_ts(digits10: str, digits6: str) -> str:
    return f"{digits10}.{digits6}"


def classify(url: str) -> dict[str, Any]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in ("github.com", "www.github.com"):
        m = _GH.match(parts.path)
        if m:
            return {"url": url, "kind": "github", "owner": m[1], "repo": m[2],
                    "type": m[3], "number": int(m[4])}
    if host == "slack.com" or host.endswith(".slack.com"):
        m = _SLACK.search(parts.path)
        if m:
            ts = slack_ts(m[2], m[3])
            parent = (parse_qs(parts.query).get("thread_ts") or [None])[0]
            return {"url": url, "kind": "slack", "channel": m[1], "ts": ts,
                    "thread_ts": parent or ts}
        return {"url": url, "kind": "slack"}
    if host.endswith(".atlassian.net"):
        return {"url": url, "kind": "jira"}
    if host == "freshchat.com" or host.endswith(".freshchat.com"):
        return {"url": url, "kind": "freshchat"}
    return {"url": url, "kind": "other"}


def parse(text: str | None) -> list[dict[str, Any]]:
    return [classify(u) for u in extract_urls(text)]


def github_key(link: dict[str, Any]) -> str | None:
    """One issue's identity, however its URL was written."""
    if link.get("kind") != "github" or not link.get("number"):
        return None
    return f"{link['owner']}/{link['repo']}#{link['number']}".lower()
