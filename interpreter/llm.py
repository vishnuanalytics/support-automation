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
    # OpenRouter free tier (opt in with OPENROUTER_API_KEY) — the fallback
    # when Groq's daily token quota is spent. `:free` variants are $0.
    "meta-llama/llama-3.3-70b-instruct:free": "openrouter",
    "deepseek/deepseek-chat-v3-0324:free": "openrouter",
    "google/gemini-2.0-flash-exp:free": "openrouter",
    "qwen/qwen-2.5-72b-instruct:free": "openrouter",
    "mistralai/mistral-small-3.1-24b-instruct:free": "openrouter",
}
FREE_MODELS = MODELS   # back-compat alias

DEFAULT_MODEL = os.environ.get("LLM_DEFAULT_MODEL", "openai/gpt-oss-120b")
FAST_MODEL = os.environ.get("LLM_FAST_MODEL", "openai/gpt-oss-20b")
# used when the chosen model's provider rate-limits / errors
FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL",
                                "meta-llama/llama-3.3-70b-instruct:free")

# Vision (Phase 25 — the `ai_prompt` node with images). Free-first, then paid:
# OpenRouter :free vision models, falling back to Anthropic Haiku if a key is
# set. Groq gpt-oss is text-only and never in this chain. Override the free
# list with LLM_VISION_MODELS="a,b" and the paid tail with LLM_VISION_PAID.
VISION_MODELS = [m.strip() for m in os.environ.get(
    "LLM_VISION_MODELS",
    "meta-llama/llama-3.2-11b-vision-instruct:free,"
    "qwen/qwen2.5-vl-32b-instruct:free,"
    "google/gemini-2.0-flash-exp:free",
).split(",") if m.strip()]
VISION_PAID = os.environ.get("LLM_VISION_PAID", "claude-haiku-4-5").strip()

_groq_client = None
_anthropic_client = None

_PROVIDER_KEY = {"groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                 "openrouter": "OPENROUTER_API_KEY"}


# allow an out-of-roster fallback id (a new OpenRouter free model) without a code change
if FALLBACK_MODEL not in MODELS:
    MODELS[FALLBACK_MODEL] = "openrouter"


def provider(model: str) -> str:
    if model in MODELS:
        return MODELS[model]
    # unknown id: an ":free" / vendor-slash id is OpenRouter, else Groq
    return "openrouter" if (":" in model or model.count("/") == 1) else "groq"


def _roster(capability: str) -> tuple[list[str], list[str]]:
    """(free, premium) model ids from the daily-refreshed `llm_roster` table
    (Phase 26). Every id is OpenRouter-hosted. Empty on any failure."""
    try:
        from interpreter.roster import chain as _rc
        free, premium = _rc(capability)
    except Exception:  # noqa: BLE001
        return [], []
    for m in free + premium:
        MODELS.setdefault(m, "openrouter")
    return free, premium


def _dedup_available(*groups) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for g in groups:
        for m in g:
            if m and m not in seen and available(m):
                seen.add(m)
                out.append(m)
    return out


def _fallback_chain(model: str) -> list[str]:
    """Models `complete()` tries, in order, before the stub: the chosen model,
    then today's free roster (Phase 26), then the env fallbacks, then the
    roster's paid tail. Only providers with a key survive."""
    free, premium = _roster("text")
    return _dedup_available(
        [model], free, [FALLBACK_MODEL, DEFAULT_MODEL, FAST_MODEL], premium)


def _vision_chain(model: str | None = None) -> list[str]:
    """Models for a call with images — the chosen model (if vision-capable),
    today's free vision roster, the env `LLM_VISION_MODELS`, then the paid
    tail (roster premium, then `LLM_VISION_PAID`)."""
    free, premium = _roster("vision")
    head: list[str] = []
    if model and model != VISION_PAID and available(model) \
            and provider(model) in ("anthropic", "openrouter"):
        head = [model]
    for m in VISION_MODELS:
        MODELS.setdefault(m, "openrouter")
    return _dedup_available(head, free, VISION_MODELS, premium, [VISION_PAID])


def _video_chain(model: str | None = None) -> list[str]:
    """Models for a call that includes video (Phase 26). Usually thin — the
    caller falls back to local whisper+ffmpeg when this is empty."""
    free, premium = _roster("video")
    head = [model] if (model and available(model)) else []
    return _dedup_available(head, free, premium, [VISION_PAID])


_RECOVERABLE = ("rate_limit", "ratelimit", "429", "timeout", "timed out",
                "temporarily", "overloaded", "503", "502", "500", "connection",
                # a provider retiring a model (e.g. an OpenRouter :free slug
                # going paid) 404s — skip it and fall to the next in the chain
                "404", "unavailable", "not found", "no endpoints", "decommission")


def _is_recoverable(e: Exception) -> bool:
    s = f"{e.__class__.__name__} {e}".lower()
    return any(t in s for t in _RECOVERABLE)


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


# small in-process cache (kills retry-storms + re-run cost). Opt-in per call.
import hashlib as _hashlib
from collections import OrderedDict as _OrderedDict

_CACHE_ON = os.environ.get("LLM_CACHE", "1") != "0"
_CACHE_MAX = int(os.environ.get("LLM_CACHE_MAX", "512"))
_cache: "_OrderedDict[str, str]" = _OrderedDict()


def _ckey(model: str, system: str, user: str, max_tokens: int) -> str:
    return _hashlib.sha256(
        f"{model}\x00{max_tokens}\x00{system}\x00{user}".encode()
    ).hexdigest()


def _dispatch(model: str, system: str, user: str, max_tokens: int,
              temperature: float, json_object: bool, images=None) -> str:
    prov = provider(model)
    if prov == "anthropic":
        return _anthropic_complete(system, user, model, max_tokens, json_object, images=images)
    if prov == "openrouter":
        return _openrouter_complete(system, user, model, max_tokens, temperature, json_object,
                                    images=images)
    if images:
        raise RuntimeError(f"model {model!r} ({prov}) has no vision support")
    return _groq_complete(system, user, model, max_tokens, temperature, json_object)


def complete(
    system: str,
    user: str,
    *,
    model: str = None,  # type: ignore[assignment]
    max_tokens: int = 512,
    temperature: float = 0.2,
    json_object: bool = False,
    cache: bool = False,
    images: "list[tuple[bytes, str]] | None" = None,
) -> str:
    """
    One-shot completion. Returns the assistant text (a JSON string when
    `json_object=True`).

    Robustness: tries the chosen model, then `LLM_FALLBACK_MODEL` (an
    OpenRouter free model), then the Groq default — skipping any provider
    that rate-limits or errors — and only falls back to the deterministic
    stub when every provider is unavailable. `cache=True` memoises the
    result in-process (used by `classify`).

    `images` = a list of `(bytes, mime)`. When given, the call goes through
    the **vision** chain instead (free OpenRouter vision models → paid
    Anthropic), and `cache` is ignored.
    """
    global last_usage
    model = model or DEFAULT_MODEL
    if model not in MODELS and not images:
        raise ValueError(f"model {model!r} is not in the roster {sorted(MODELS)}")

    if images:
        chain = _vision_chain(model if model in MODELS else None)
        if not chain:
            last_usage = None
            return _stub(system, user + "\n[image omitted — no vision model]",
                         json_object=json_object)
        return _run_chain(chain, system, user, max_tokens, temperature, json_object,
                          images=images)

    if cache and _CACHE_ON:
        ck = _ckey(model, system, user, max_tokens)
        hit = _cache.get(ck)
        if hit is not None:
            _cache.move_to_end(ck)
            last_usage = None
            return hit

    chain = _fallback_chain(model)
    if not chain:
        last_usage = None
        return _stub(system, user, json_object=json_object)

    out = _run_chain(chain, system, user, max_tokens, temperature, json_object)
    if cache and _CACHE_ON:
        _cache[ck] = out
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return out


def _run_chain(chain: list[str], system: str, user: str, max_tokens: int,
               temperature: float, json_object: bool, *, images=None) -> str:
    global last_usage
    last_err: Exception | None = None
    for i, m in enumerate(chain):
        try:
            out = _dispatch(m, system, user, max_tokens, temperature, json_object, images=images)
            if i > 0:
                log.warning("llm: %s failed — served by fallback %s", chain[0], m)
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            if _is_recoverable(e):
                log.warning("llm: %s recoverable error (%s) — trying next", m, e.__class__.__name__)
                continue
            raise
    log.warning("llm: all providers exhausted (%s) — offline stub", last_err)
    last_usage = None
    return _stub(system, user, json_object=json_object)


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


_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_TIMEOUT = float(os.environ.get("OPENROUTER_TIMEOUT_S", "45"))


def _img_data_url(data: bytes, mime: str) -> str:
    import base64
    return f"data:{mime or 'image/png'};base64,{base64.b64encode(data).decode()}"


def _openrouter_complete(system: str, user: str, model: str, max_tokens: int,
                         temperature: float, json_object: bool, images=None) -> str:
    """OpenAI-compatible call to OpenRouter (free `:free` models). Plain httpx
    — no extra SDK. Raises on 429 / 5xx so `complete()` moves to the next
    model in the chain."""
    global last_usage
    import httpx

    user_content: Any = user
    if images:
        user_content = [{"type": "text", "text": user}] + [
            {"type": "image_url", "image_url": {"url": _img_data_url(d, m)}}
            for d, m in images
        ]
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_object:
        body["response_format"] = {"type": "json_object"}
        body["messages"][0]["content"] += "\n\nRespond with ONLY the JSON object."
    r = httpx.post(
        _OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://support-automation.local"),
            "X-Title": "support-automation",
        },
        json=body,
        timeout=_OPENROUTER_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"openrouter {r.status_code}: {r.text[:300]}")
    data = r.json()
    u = data.get("usage") or {}
    last_usage = ({"prompt": u.get("prompt_tokens", 0),
                   "completion": u.get("completion_tokens", 0),
                   "total": u.get("total_tokens", 0)} if u else None)
    choices = data.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content") or ""


def _anthropic_complete(system: str, user: str, model: str, max_tokens: int,
                        json_object: bool, images=None) -> str:
    global last_usage
    import base64

    sys_prompt = system
    if json_object:
        sys_prompt += "\n\nRespond with only the JSON object — no prose, no code fences."
    content: Any = user
    if images:
        content = [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": m or "image/png",
                                         "data": base64.b64encode(d).decode()}}
            for d, m in images
        ] + [{"type": "text", "text": user}]
    # No `temperature` / `thinking` / `effort`: sampling params are rejected on
    # the Claude 5 family; defaults are fine for classify/draft.
    resp = _anthropic().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=sys_prompt,
        messages=[{"role": "user", "content": content}],
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
