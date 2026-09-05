"""
Per-tenant case-taxonomy config: the classifier `topic` slug / Account
country -> Salesforce-shaped Case picklists (Module__c / SubModule__c /
Region__c / Type). Was a single hardcoded global dict inside
`salesforce.py` (and, separately, a *second* hardcoded copy of the valid
picklist *values* in `scripts/sf_support_setup.py` -- two sources of truth
that migration 079 already hit once: a rule produced a value the picklist
didn't have). Moved here so:

  * a tenant whose support taxonomy differs from the built-in
    Zapier-shaped defaults (billing/SSO/webhooks/zaps/...) can override
    it via `case_taxonomy` (migration 086), through
    `PUT /api/tenants/case-taxonomy` -- no code change needed;
  * `sf_support_setup.py --tenant-id <id>` can sync a Salesforce org's
    actual picklist values from the *same* rules this module uses to
    write them, instead of drifting.

A tenant with no row (the common case, including every tenant that
existed before this) gets `DEFAULT_TAXONOMY` unchanged -- a tenant's
stored `config` only needs to carry the keys it wants to override, not a
full copy of the defaults.
"""

from __future__ import annotations

import copy
import logging
import os
import time
from typing import Any

log = logging.getLogger("interpreter.case_taxonomy")

# tenant config barely changes and this runs on every classify/writeback
# pass -- same TTL-cache shape as interpreter/routing.py's notify-target cache.
_TTL_S = float(os.environ.get("CASE_TAXONOMY_TTL_S", "300"))
_cache: dict[str, tuple[float, dict]] = {}

ALLOWED_KEYS = {"module_rules", "submodule_rules", "region_by_country", "case_type_rules"}

# ── the original hardcoded rules, unchanged, now the fallback default ──────
DEFAULT_TAXONOMY: dict[str, Any] = {
    "module_rules": [
        {"keywords": ["billing", "refund", "charge", "invoice", "plan", "pricing", "payment",
                      "subscription", "proration", "chargeback"], "module": "Billing & Plans"},
        {"keywords": ["sso", "saml", "login", "password", "2fa", "mfa", "two-factor",
                      "account", "member", "role", "seat", "signin", "sign-in"], "module": "Account & Login"},
        {"keywords": ["webhook", "api", "rest", "rate-limit", "ratelimit", "endpoint", "token",
                      "429"], "module": "API & Webhooks"},
        {"keywords": ["export", "gdpr", "retention", "deletion", "dump"], "module": "Data & Export"},
        {"keywords": ["zap", "trigger", "action", "filter", "path", "schedul"], "module": "Zaps"},
        {"keywords": ["integration", "connector"], "module": "Integrations & Apps"},
    ],
    "submodule_rules": {
        "Billing & Plans": [
            {"keywords": ["refund", "chargeback"], "submodule": "Refunds"},
            {"keywords": ["invoice", "receipt"], "submodule": "Invoices"},
            {"keywords": ["plan", "upgrade", "downgrade", "proration"], "submodule": "Plan Change"},
            {"keywords": ["charge", "billed", "double", "duplicate", "payment"], "submodule": "Charges"},
        ],
        "Account & Login": [
            {"keywords": ["sso", "saml", "okta"], "submodule": "SSO"},
            {"keywords": ["password", "reset"], "submodule": "Password"},
            {"keywords": ["2fa", "mfa", "two-factor"], "submodule": "Two-Factor"},
            {"keywords": ["member", "role", "seat", "invite"], "submodule": "Members & Roles"},
        ],
        "API & Webhooks": [
            {"keywords": ["webhook"], "submodule": "Webhooks"},
            {"keywords": ["rate", "limit", "429"], "submodule": "Rate Limits"},
            {"keywords": ["api", "rest", "endpoint", "token"], "submodule": "REST API"},
        ],
        "Data & Export": [
            {"keywords": ["gdpr", "deletion", "delete"], "submodule": "Deletion / GDPR"},
            {"keywords": ["retention"], "submodule": "Retention"},
            {"keywords": ["export", "dump"], "submodule": "Export"},
        ],
        "Zaps": [
            {"keywords": ["trigger"], "submodule": "Triggers"},
            {"keywords": ["action"], "submodule": "Actions"},
            {"keywords": ["filter"], "submodule": "Filters"},
            {"keywords": ["path"], "submodule": "Paths"},
            {"keywords": ["schedul"], "submodule": "Scheduling"},
        ],
        "Integrations & Apps": [
            {"keywords": ["auth"], "submodule": "Authentication"},
            {"keywords": ["error"], "submodule": "App Errors"},
            {"keywords": ["new-app", "new app", "request"], "submodule": "New App Request"},
        ],
    },
    "region_by_country": {c: r for r, cs in {
        "NA": ("united states", "usa", "us", "u.s.", "canada"),
        "EMEA": ("united kingdom", "uk", "gb", "ireland", "germany", "france", "spain",
                 "italy", "netherlands", "sweden", "poland", "switzerland", "belgium",
                 "austria", "norway", "denmark", "finland", "portugal", "greece",
                 "czechia", "czech republic", "romania", "israel",
                 "united arab emirates", "uae", "saudi arabia", "south africa",
                 "nigeria", "kenya", "egypt", "turkey"),
        "APAC": ("india", "singapore", "australia", "japan", "china", "hong kong",
                 "indonesia", "malaysia", "philippines", "thailand", "vietnam",
                 "south korea", "korea", "new zealand", "taiwan", "bangladesh", "pakistan"),
        "LATAM": ("brazil", "mexico", "argentina", "chile", "colombia", "peru", "uruguay"),
    }.items() for c in cs},
    "case_type_rules": [
        {"keywords": ["refund", "chargeback", "charge", "billed", "invoice", "receipt", "billing",
                      "payment", "pricing", "proration", "subscription", "coupon", "plan change"],
         "case_type": "Billing"},
        {"keywords": ["sso", "saml", "okta", "login", "log in", "log-in", "signin", "sign in",
                      "sign-in", "password", "2fa", "mfa", "two-factor", "locked out", "lockout",
                      "can't access my account", "cannot access my account", "account access"],
         "case_type": "Account / Login"},
        {"keywords": ["bug", "error", "broken", "not working", "isn't working", "stopped working",
                      "fails", "failing", "failure", "exception", "500 error", "crash", "crashing",
                      "regression", "unexpected"],
         "case_type": "Problem / Bug"},
        {"keywords": ["feature request", "feature-request", "would be nice", "please add",
                      "can you add", "roadmap", "suggestion", "enhancement", "wishlist"],
         "case_type": "Feature Request"},
        {"keywords": ["how do i", "how do we", "how can i", "how to", "how-to", "step by step",
                      "step-by-step", "walk me through", "tutorial", "is it possible to"],
         "case_type": "How-to"},
    ],
}


def _cache_get(tenant_id: str) -> dict | None:
    hit = _cache.get(tenant_id)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def _cache_put(tenant_id: str, value: dict) -> None:
    _cache[tenant_id] = (time.monotonic() + _TTL_S, value)


def invalidate(tenant_id: str | None) -> None:
    """Drop a tenant's cached config -- call after a `case_taxonomy` write
    so the next case doesn't triage against a stale rule set for up to
    `_TTL_S` seconds."""
    if tenant_id:
        _cache.pop(str(tenant_id), None)


def _default_sb():
    from ingestion.scraper import get_supabase

    return get_supabase()


def _fetch_config(tenant_id: str, sb) -> dict | None:
    if sb is None and "PYTEST_CURRENT_TEST" in os.environ:
        return None  # offline tests monkeypatch load()/pass sb explicitly
    try:
        sb = sb or _default_sb()
        rows = (sb.table("case_taxonomy").select("config")
                .eq("tenant_id", str(tenant_id)).execute().data)
        return rows[0]["config"] if rows else None
    except Exception as e:  # noqa: BLE001
        log.warning("case_taxonomy load failed for tenant %s: %s", tenant_id, e)
        return None


def _merge(default: dict, override: dict | None) -> dict:
    if not override:
        return default
    merged = copy.deepcopy(default)
    for k, v in override.items():
        if k in ALLOWED_KEYS and v:
            merged[k] = v
    return merged


def load(tenant_id: str | None, sb=None) -> dict:
    """The effective taxonomy config for this tenant: `DEFAULT_TAXONOMY`
    with any tenant-configured keys overlaid. No `tenant_id` (a synthetic /
    offline run) -> the pure default, same as before this module existed."""
    if not tenant_id:
        return DEFAULT_TAXONOMY
    tid = str(tenant_id)
    cached = _cache_get(tid)
    if cached is not None:
        return cached
    merged = _merge(DEFAULT_TAXONOMY, _fetch_config(tid, sb))
    _cache_put(tid, merged)
    return merged


def validate_config(config: Any) -> list[str]:
    """Structural validation for a tenant's override before it's stored --
    a wrong-shaped config would otherwise fail silently deep inside
    `map_case_fields`/`map_case_type` at case-triage time instead of at
    save time. Empty `{}` is always valid ("use the defaults"). Returns a
    list of problems, empty if well-formed."""
    if not isinstance(config, dict):
        return ["config must be an object"]
    errs = []
    unknown = set(config) - ALLOWED_KEYS
    if unknown:
        errs.append(f"unknown key(s): {sorted(unknown)}")

    def _is_rule(r: Any, target: str) -> bool:
        return (isinstance(r, dict) and isinstance(r.get("keywords"), list)
                and all(isinstance(k, str) for k in r["keywords"])
                and isinstance(r.get(target), str))

    if "module_rules" in config:
        v = config["module_rules"]
        if not isinstance(v, list) or not all(_is_rule(r, "module") for r in v):
            errs.append("module_rules must be a list of {keywords: [str], module: str}")
    if "submodule_rules" in config:
        v = config["submodule_rules"]
        if not isinstance(v, dict) or not all(
            isinstance(rules, list) and all(_is_rule(r, "submodule") for r in rules)
            for rules in v.values()
        ):
            errs.append("submodule_rules must be {module: [{keywords: [str], submodule: str}]}")
    if "region_by_country" in config:
        v = config["region_by_country"]
        if not isinstance(v, dict) or not all(
            isinstance(k, str) and isinstance(val, str) for k, val in v.items()
        ):
            errs.append("region_by_country must be {country: region}")
    if "case_type_rules" in config:
        v = config["case_type_rules"]
        if not isinstance(v, list) or not all(_is_rule(r, "case_type") for r in v):
            errs.append("case_type_rules must be a list of {keywords: [str], case_type: str}")
    return errs


# ── the actual matching logic (pure, given a resolved taxonomy) ────────────
def map_case_fields(topic: str | None, country: str | None, *,
                     tenant_id: str | None = None, sb=None) -> dict[str, str]:
    """Classifier `topic` slug + Account country -> the restricted Case
    picklists (`Module__c` / `SubModule__c` / `Region__c`). `Topic__c`
    always gets the raw slug -- the safety net; the rest are best-effort
    and omitted when nothing matches."""
    tax = load(tenant_id, sb)
    slug = (topic or "").strip().lower()
    out: dict[str, str] = {}
    if slug:
        out["Topic__c"] = str(topic)
    module = next((r["module"] for r in tax["module_rules"]
                   if any(k in slug for k in r["keywords"])), "Other" if slug else "")
    if module:
        out["Module__c"] = module
        sub = next((r["submodule"] for r in tax["submodule_rules"].get(module, [])
                    if any(k in slug for k in r["keywords"])), "")
        if sub:
            out["SubModule__c"] = sub
    region = tax["region_by_country"].get((country or "").strip().lower())
    if region:
        out["Region__c"] = region
    return out


def map_case_type(topic: str | None, text: str | None = None, *,
                   tenant_id: str | None = None, sb=None) -> str:
    """Best-effort `Case.Type` from the classifier `topic` slug (+ optional
    raw case text). Returns `"Question"` for any non-empty input that
    matches no rule, `""` for empty."""
    tax = load(tenant_id, sb)
    hay = " ".join(x for x in ((topic or ""), (text or "")) if x).strip().lower()
    if not hay:
        return ""
    for r in tax["case_type_rules"]:
        if any(k in hay for k in r["keywords"]):
            return r["case_type"]
    return "Question"


def normalize_case_type(value: str | None, *, tenant_id: str | None = None, sb=None) -> str:
    """Coerce a free-form / LLM `type` string to an exact `Case.Type`
    picklist value, or `""` if it doesn't map. The canonical list matched
    against is the tenant's own (`valid_values()["case_types"]`); the
    keyword-heuristic fallback below is a fixed safety net, not
    per-tenant-configurable (case_type_rules already covers keyword-based
    per-tenant matching for `map_case_type`)."""
    if not value:
        return ""
    s = str(value).strip().lower().replace("_", " ").replace("-", " ")
    s = " ".join(s.split())
    for canon in valid_values(tenant_id, sb)["case_types"]:
        c = canon.lower().replace(" / ", " ").replace("-", " ")
        if s in (canon.lower(), c) or s.replace(" ", "") == c.replace(" ", ""):
            return canon
    if "bill" in s or "refund" in s or "invoice" in s:
        return "Billing"
    if "login" in s or "log in" in s or "auth" in s or ("account" in s and "access" in s):
        return "Account / Login"
    if "bug" in s or "problem" in s or "error" in s or "broken" in s:
        return "Problem / Bug"
    if "feature" in s:
        return "Feature Request"
    if s.startswith("how"):
        return "How-to"
    if "question" in s:
        return "Question"
    return ""


def valid_values(tenant_id: str | None = None, sb=None) -> dict[str, Any]:
    """The distinct target values for each picklist, derived from the
    tenant's *actual* rules -- what `sf_support_setup.py` should sync into
    Salesforce, so the picklist and the rules that write to it can never
    drift apart (the migration-079 bug class)."""
    tax = load(tenant_id, sb)
    modules = list(dict.fromkeys([r["module"] for r in tax["module_rules"]] + ["Other"]))
    submodule_by_module = {
        m: list(dict.fromkeys(r["submodule"] for r in rules))
        for m, rules in tax["submodule_rules"].items()
    }
    regions = list(dict.fromkeys(list(tax["region_by_country"].values()) + ["Other"]))
    case_types = list(dict.fromkeys(
        [r["case_type"] for r in tax["case_type_rules"]] + ["Question", "Other"]
    ))
    return {"modules": modules, "submodule_by_module": submodule_by_module,
            "regions": regions, "case_types": case_types}
