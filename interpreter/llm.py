"""
LLM provider abstraction — Groq (default, free) and Anthropic (opt-in).

A node's `config.model` picks the model; `MODELS` maps model id -> provider.
`complete()` routes by that: a `claude-*` id goes to the Anthropic SDK
(needs `ANTHROPIC_API_KEY`), anything else to Groq (needs `GROQ_API_KEY`).

Provider default per CLAUDE.md is still Groq. To run everything on Claude
without touching flows, set in `.env`:
    LLM_DEFAULT_MODEL=claude-sonnet-5      # the `draft` node's default
    LLM_FAST_MODEL=claude-haiku-4-5        # the `classify` / judge default

`complete()` works with **no** key for the chosen provider: it returns a
deterministic stub (`"_stub": true` in JSON) so the graph runs offline in
CI / eval / demos. The stub is heuristic, not smart.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger("interpreter.llm")

# model id -> provider
MODELS: dict[str, str] = {
    # Groq (free tier — https://console.groq.com; llama-3.x names were retired)
    "openai/gpt-oss-120b": "groq",
    "openai/gpt-oss-20b": "groq",
    "qwen/qwen3.8-27b": "groq",
    "qwen/qwen3.6-27b": "groq",
    "groq/compound": "groq",
    "groq/compound-mini": "groq",
    "llama-3.3-70b-versatile": "groq",   # legacy — kept so old flow configs don't KeyError
    "llama-3.1-8b-instant": "groq",
    # Anthropic (paid — opt in with ANTHROPIC_API_KEY)
    "claude-opus-5": "anthropic",
    "claude-sonnet-5": "anthropic",
    "claude-haiku-4-5": "anthropic",
}
FREE_MODELS = MODELS   # back-compat alias

DEFAULT_MODEL = os.environ.get("LLM_DEFAULT_MODEL", "openai/gpt-oss-120b")
FAST_MODEL = os.environ.get("LLM_FAST_MODEL", "openai/gpt-oss-20b")

_groq_client = None
_anthropic_client = None

_PROVIDER_KEY = {"groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def provider(model: str) -> str:
    return MODELS.get(model, "groq")


def available(model: str | None = None) -> bool:
    """With `model`: is that model's provider configured? Without: is *any*
    provider configured (i.e. will real calls happen anywhere)?"""
    if model is not None:
        return bool(os.environ.get(_PROVIDER_KEY[provider(model)]))
    return any(os.environ.get(k) for k in _PROVIDER_KEY.values())


# usage of the most recent complete() call: {"prompt", "completion", "total"}
# or None for a stub call. Handlers read this straight after calling complete().
last_usage: dict[str, int] | None = None


def _groq():
    global _groq_client
    if _groq_client is None:
        from groq import Groq

        _groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _groq_client


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        _anthropic_client = anthropic.Anthropic()   # resolves ANTHROPIC_API_KEY / ant profile
    return _anthropic_client


def complete(
    system: str,
    user: str,
    *,
    model: str = None,  # type: ignore[assignment]
    max_tokens: int = 512,
    temperature: float = 0.2,
    json_object: bool = False,
) -> str:
    """
    One-shot completion. Returns the assistant text (a JSON string when
    `json_object=True`). Falls back to a deterministic stub when the chosen
    model's provider has no key.
    """
    global last_usage
    model = model or DEFAULT_MODEL
    if model not in MODELS:
        raise ValueError(f"model {model!r} is not in the roster {sorted(MODELS)}")
    prov = provider(model)

    if not available(model):
        last_usage = None
        return _stub(system, user, json_object=json_object)

    if prov == "anthropic":
        return _anthropic_complete(system, user, model, max_tokens, json_object)

    try:
        return _groq_complete(system, user, model, max_tokens, temperature, json_object)
    except Exception as e:  # noqa: BLE001
        # Groq free-tier daily token quota (TPD) exhausted, or a transient
        # 429 that outlived the retry budget: degrade to the deterministic
        # stub instead of failing the whole Case. Routing / escalation still
        # work; only draft quality suffers (and the confidence gate catches a
        # weak stub draft). Any other error is a real bug -> re-raise.
        if "rate_limit" in str(e).lower() or e.__class__.__name__ == "RateLimitError":
            log.warning("Groq rate-limited (%s) — falling back to the offline stub", model)
            last_usage = None
            return _stub(system, user, json_object=json_object)
        raise


# cap on how long we'll sit blocked on a single rate-limited Groq call
GROQ_MAX_BACKOFF_S = float(os.environ.get("GROQ_MAX_BACKOFF_S", "35"))


def _retry_after_seconds(err: Any) -> float:
    """Pull the wait hint out of a Groq 429 ('try again in 11.25s'), capped."""
    m = re.search(r"try again in ([\d.]+)s", str(err))
    secs = float(m.group(1)) + 0.5 if m else 2.0
    return min(secs, GROQ_MAX_BACKOFF_S)


def _groq_call(model: str, system: str, user: str, max_tokens: int,
               temperature: float, *, response_format: bool) -> Any:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # gpt-oss reasoning models burn completion budget on hidden reasoning
    # tokens; drafting / triage don't need deep reasoning and the extra
    # tokens are what push JSON replies past max_tokens into a truncated
    # (invalid) object. Keep it low.
    if model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "low"
    if response_format:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        from groq import RateLimitError
    except Exception:  # noqa: BLE001
        RateLimitError = ()  # type: ignore[assignment]

    import time as _time
    for attempt in range(3):
        try:
            return _groq().chat.completions.create(**kwargs)
        except RateLimitError as e:  # type: ignore[misc]
            if attempt == 2:
                raise
            _time.sleep(_retry_after_seconds(e))


def _groq_complete(system: str, user: str, model: str, max_tokens: int,
                   temperature: float, json_object: bool) -> str:
    global last_usage
    try:
        from groq import BadRequestError
    except Exception:  # noqa: BLE001
        BadRequestError = ()  # type: ignore[assignment]

    def _record(resp: Any) -> str:
        global last_usage
        u = getattr(resp, "usage", None)
        last_usage = (
            {"prompt": u.prompt_tokens, "completion": u.completion_tokens,
             "total": u.total_tokens}
            if u else None
        )
        return resp.choices[0].message.content or ""

    try:
        return _record(_groq_call(model, system, user, max_tokens, temperature,
                                  response_format=json_object))
    except BadRequestError as e:  # type: ignore[misc]
        code = getattr(e, "code", None) or ""
        body = getattr(e, "body", None) or {}
        if not json_object or ("json_validate_failed" not in str(code)
                               and "json_validate_failed" not in str(body)):
            raise
        # Groq's server-side JSON grammar rejected a (usually truncated)
        # object. Salvage the partial if it parses, else retry once free-form
        # with more headroom — a truncated free-form reply still comes back
        # 200 and _safe_json can recover the leading object.
        partial = ""
        try:
            partial = body.get("error", {}).get("failed_generation", "") or ""
        except AttributeError:
            pass
        if partial:
            salvaged = _coerce_json(partial)
            if salvaged is not None:
                last_usage = None
                return salvaged
        resp = _groq_call(
            model,
            system + "\n\nReturn ONLY the JSON object — no prose, no code fences.",
            user,
            max(max_tokens, 1024),
            temperature,
            response_format=False,
        )
        return _record(resp)


def _coerce_json(s: str) -> str | None:
    """Return `s` (or its leading {...} span) if it parses as JSON, else None."""
    import json as _json
    import re as _re

    span = _re.search(r"\{.*\}", s or "", _re.S)
    for candidate in (s, span.group(0) if span else None):
        if not candidate:
            continue
        try:
            _json.loads(candidate)
            return candidate
        except Exception:  # noqa: BLE001
            continue
    return None


def _anthropic_complete(system: str, user: str, model: str, max_tokens: int,
                        json_object: bool) -> str:
    global last_usage
    sys_prompt = system
    if json_object:
        sys_prompt += "\n\nRespond with only the JSON object — no prose, no code fences."
    # No `temperature` / `thinking` / `effort`: sampling params are rejected on
    # the Claude 5 family; defaults are fine for classify/draft.
    resp = _anthropic().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=sys_prompt,
        messages=[{"role": "user", "content": user}],
    )
    u = getattr(resp, "usage", None)
    last_usage = (
        {"prompt": u.input_tokens, "completion": u.output_tokens,
         "total": u.input_tokens + u.output_tokens}
        if u else None
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


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


_STUB_FLOW = {
    "name": "Stub support flow",
    "nodes": [
        {"key": "retrieve", "type": "retrieve", "label": "Retrieve"},
        {"key": "classify", "type": "classify", "label": "Classify"},
        {"key": "draft", "type": "draft", "label": "Draft reply"},
        {"key": "gate", "type": "confidence_gate", "label": "Confidence gate"},
        {"key": "send", "type": "auto_reply", "label": "Auto reply"},
        {"key": "human", "type": "ask_human", "label": "Ask a human"},
    ],
    "edges": [
        {"source": "retrieve", "target": "classify", "if": None},
        {"source": "classify", "target": "draft", "if": None},
        {"source": "draft", "target": "gate", "if": None},
        {"source": "gate", "target": "send", "if": "confidence_gate.pass"},
        {"source": "gate", "target": "human", "if": None},
    ],
}


def _stub_fields(system: str, user: str) -> dict[str, Any]:
    """Heuristic values for the keys classify / draft ask for."""
    sys_l = system.lower()
    # Phase 19b — `assist_generate`: a whole flow graph from a description.
    if "you design flows for a support-automation platform" in sys_l:
        return {"_stub": True, **json.loads(json.dumps(_STUB_FLOW))}
    # Phase 19c — `assist_edit`: echo the current graph back unchanged (a
    # valid no-op edit; the deterministic stub can't follow an instruction).
    if "you edit an existing support-automation flow" in sys_l:
        m = re.search(r"\{.*\}", user or "", re.S)
        try:
            cur = json.loads(m.group(0)) if m else {}
        except (json.JSONDecodeError, AttributeError):
            cur = {}
        return {"_stub": True, "summary": "stub: no change",
                "nodes": cur.get("nodes", []), "edges": cur.get("edges", [])}
    # Phase 17 `clarify` node — asks for {questions, missing}.
    if "questions whose answers" in system:
        return {
            "_stub": True,
            "questions": [
                "Which product or plan is this about, and what exactly are you trying to do?",
                "What is the exact error message or unexpected behaviour you see?",
                "What have you already tried?",
            ],
            "missing": ["product/plan", "exact error", "steps tried"],
        }
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
