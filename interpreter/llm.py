"""
LLM provider abstraction.

Per CLAUDE.md, code LLM calls default to Groq's free models. A node's
`config.model` picks one; `FREE_MODELS` is the allowed roster (all free on
Groq's current tier -- classify can use a small fast one, draft a larger).

`complete()` works with no API key: if `GROQ_API_KEY` is unset it returns a
deterministic stub so the whole graph runs offline (CI, eval, demos without
a key). The stub is clearly marked (`"_stub": true` in JSON output) and is
heuristic, not smart -- it exists so the interpreter is exercisable, not so
it produces good support replies.

Set GROQ_API_KEY in .env to switch every call to the real API. Nothing else
changes.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# model id -> provider. All currently free on Groq (https://console.groq.com).
FREE_MODELS: dict[str, str] = {
    "llama-3.3-70b-versatile": "groq",
    "llama-3.1-8b-instant": "groq",
    "openai/gpt-oss-20b": "groq",
    "openai/gpt-oss-120b": "groq",
    "gemma2-9b-it": "groq",
    "qwen/qwen3-32b": "groq",
}
DEFAULT_MODEL = "llama-3.3-70b-versatile"
FAST_MODEL = "llama-3.1-8b-instant"

_groq_client = None


def available() -> bool:
    """True when real API calls will be made (a key is present)."""
    return bool(os.environ.get("GROQ_API_KEY"))


# usage of the most recent complete() call: {"prompt", "completion", "total"}
# or None for a stub call. Handlers read this straight after calling complete()
# so the run's trace/record can total tokens (Phase 7).
last_usage: dict[str, int] | None = None


def _client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq

        _groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _groq_client


def complete(
    system: str,
    user: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 512,
    temperature: float = 0.2,
    json_object: bool = False,
) -> str:
    """
    One-shot completion. Returns the assistant text (a JSON string when
    `json_object=True`). Falls back to a deterministic stub with no key.
    """
    global last_usage
    if model not in FREE_MODELS:
        raise ValueError(
            f"model {model!r} is not in the free roster {sorted(FREE_MODELS)}"
        )

    if not available():
        last_usage = None
        return _stub(system, user, json_object=json_object)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_object:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client().chat.completions.create(**kwargs)
    u = getattr(resp, "usage", None)
    last_usage = (
        {"prompt": u.prompt_tokens, "completion": u.completion_tokens, "total": u.total_tokens}
        if u else None
    )
    return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------
# Deterministic offline stub
# --------------------------------------------------------------------------
_URGENT = re.compile(
    r"\b(urgent\w*|asap|immediat\w*|down|outage|broke\w*|can'?t|cannot|fail\w*|"
    r"error\w*|blocked|500|503|critical|impact\w*|production)\b",
    re.I,
)
_BILLING = re.compile(r"\b(bill|billing|invoice|charge|refund|payment|plan|pricing|upgrade|subscription)\b", re.I)
_AUTH = re.compile(r"\b(login|log in|password|sign in|2fa|mfa|sso|auth|authenticat)\b", re.I)


def _stub(system: str, user: str, *, json_object: bool) -> str:
    if json_object:
        return json.dumps(_stub_fields(system, user))
    # free-text: a template "draft" grounded in whatever context block we were given
    first_line = next((ln for ln in user.splitlines() if ln.strip()), "your request")
    return (
        "Thanks for getting in touch. Based on our documentation, here are the "
        "steps that should resolve this:\n\n"
        "1. Review the linked guide for the exact configuration.\n"
        "2. Apply the change described there.\n"
        "3. Reply here if the issue persists and we'll escalate.\n\n"
        f"(stub draft; re: {first_line.strip()[:120]})"
    )


def _stub_fields(system: str, user: str) -> dict[str, Any]:
    """Heuristic values for the keys classify / draft ask for."""
    urgency = "high" if _URGENT.search(user) else "normal"
    if _BILLING.search(user):
        topic = "billing"
    elif _AUTH.search(user):
        topic = "authentication"
    else:
        topic = "product-usage"
    summary = " ".join(user.split())[:160]
    # crude confidence: longer, keyword-rich context -> a bit more confident
    conf = 0.55 + 0.1 * bool(_BILLING.search(user) or _AUTH.search(user))
    return {
        "_stub": True,
        "topic": topic,
        "urgency": urgency,
        "summary": summary,
        "reply": _stub(system, user, json_object=False),
        "confidence": round(min(conf, 0.9), 2),
    }
