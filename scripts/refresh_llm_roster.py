"""
Phase 26 — refresh `llm_roster` from OpenRouter's live catalog.

OpenRouter's free tier churns hard — model *names* go stale in weeks. So we
don't hardcode names; we score whatever is $0 *today* by heuristics that
don't age:

  * vendor reputation  (google / anthropic / meta / deepseek / qwen / …)
  * context window     (bigger = better, roughly)
  * parameter hint     (`-70b` in the id, tiebreak)
  * modality           (text / +image / +video)

and take the top few per capability. The premium tail = the cheapest capable
paid models from the major vendors (also self-healing).

    python -m scripts.refresh_llm_roster            # write llm_roster
    python -m scripts.refresh_llm_roster --dry-run  # print only

Runs daily from .github/workflows/daily-sync.yml (and the VM cron).
"""

from __future__ import annotations

import argparse
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

_CATALOG = "https://openrouter.ai/api/v1/models"
_MAX_FREE = 6
_MAX_PREMIUM = 4

# lower = more trusted. Prefix match on the vendor part of the id.
_VENDOR_RANK = {
    "google": 0, "anthropic": 1, "openai": 1, "meta-llama": 2, "deepseek": 2,
    "qwen": 3, "mistralai": 3, "x-ai": 3, "z-ai": 4, "nvidia": 4, "microsoft": 4,
    "cohere": 5, "minimax": 5, "amazon": 5, "perplexity": 5, "liquid": 7,
}
# ids containing any of these are not chat models / not sync — skip.
_SKIP = ("embed", "rerank", "reranker", "guard", "moderation", "content-safety",
         "safety", "lyria", "whisper", "tts", "vui", "clip-preview",
         "-code", "coder", "note-preview", ":batch", ":extended")


def _vscore(mid: str) -> int:
    vendor = mid.split("/", 1)[0].lower()
    return _VENDOR_RANK.get(vendor, 6)


def _params_b(mid: str) -> float:
    m = re.search(r"[-(](\d+(?:\.\d+)?)\s*b\b", mid.lower())
    return float(m.group(1)) if m else 0.0


def _price(m: dict) -> float:
    p = m.get("pricing") or {}
    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    return f(p.get("prompt")) + f(p.get("completion"))


def _is_free(m: dict) -> bool:
    p = m.get("pricing") or {}
    def z(v):
        try:
            return float(v) == 0.0
        except (TypeError, ValueError):
            return False
    return z(p.get("prompt")) and z(p.get("completion"))


def _modalities(m: dict) -> tuple[list[str], list[str]]:
    a = m.get("architecture") or {}
    inp = [x.lower() for x in (a.get("input_modalities") or [])]
    out = [x.lower() for x in (a.get("output_modalities") or [])]
    if not inp or not out:                       # older entries: parse `modality`
        mod = str(a.get("modality", "text->text")).lower()
        lhs, _, rhs = mod.partition("->")
        inp = inp or re.split(r"[+\s]+", lhs.strip()) or ["text"]
        out = out or re.split(r"[+\s]+", rhs.strip()) or ["text"]
    return inp, out


def _chat_ok(mid: str, out: list[str]) -> bool:
    low = mid.lower()
    if low.startswith("openrouter/") or any(s in low for s in _SKIP):
        return False
    return "text" in out


def _fetch() -> list[dict]:
    headers = {}
    if os.environ.get("OPENROUTER_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['OPENROUTER_API_KEY']}"
    r = httpx.get(_CATALOG, headers=headers, timeout=30)
    r.raise_for_status()
    return [m for m in (r.json().get("data") or []) if m.get("id")]


def _rank(models: list[dict], *, need_image=False, need_video=False) -> list[str]:
    scored = []
    for m in models:
        inp, out = _modalities(m)
        if not _chat_ok(m["id"], out):
            continue
        if need_image and "image" not in inp:
            continue
        if need_video:
            explicit = [x.lower() for x in ((m.get("architecture") or {}).get("input_modalities") or [])]
            if "video" not in explicit:               # never guess video support
                continue
        scored.append((
            _vscore(m["id"]),
            -int(m.get("context_length") or 0),
            -_params_b(m["id"]),
            _price(m),
            m["id"],
        ))
    scored.sort()
    return [s[4] for s in scored]


def build(catalog: list[dict]) -> dict[str, dict]:
    free = [m for m in catalog if _is_free(m)]
    paid = [m for m in catalog if not _is_free(m) and _price(m) > 0]

    out: dict[str, dict] = {}
    for cap, kw in (("text", {}), ("vision", {"need_image": True}),
                    ("video", {"need_video": True})):
        out[cap] = {
            "models": _rank(free, **kw)[:_MAX_FREE],
            "premium": _rank(paid, **kw)[:_MAX_PREMIUM],
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.refresh_llm_roster")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    try:
        catalog = _fetch()
    except Exception as e:  # noqa: BLE001
        print(f"openrouter catalog fetch failed: {e}")
        return 1
    plan = build(catalog)
    for cap, row in plan.items():
        print(f"[{cap}] free: {', '.join(row['models']) or '(none)'}")
        print(f"        premium: {', '.join(row['premium']) or '(none)'}")

    if a.dry_run:
        return 0
    from ingestion.scraper import get_supabase
    from interpreter import roster
    sb = get_supabase()
    for cap, row in plan.items():
        if not row["models"] and not row["premium"]:
            print(f"skip {cap}: nothing resolved (keeping the old row)")
            continue
        roster.write(sb, cap, row["models"], row["premium"])
    print("llm_roster updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
