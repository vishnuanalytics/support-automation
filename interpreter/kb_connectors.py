"""
Declarative KB source-connector registry (docs/KB_SOURCE_CONNECTORS.md).

Mirrors `interpreter/connectors.py`'s ConnectorSpec/invoke() philosophy —
"a connector is data, not a hardcoded handler" — but for *pulling content*
into the knowledge base instead of *invoking a case-system action*.

Every KB producer (crawl, Google Sheets, Google Docs, and later Linear /
Discourse / Nolt) converges on one narrow contract already proven by
`api/worker.py::_embed_kb_entry` / `ingestion/sources/kb_common.py`:

    sync()  -> [KBDocument(external_id, title, body_md), ...]
      -> the generic driver (api/worker.py::_sync_kb_connection) upserts one
         `kb_entries` row per document (keyed connection_id + external_id),
         skips unchanged bodies, archives ones that vanished, enqueues embed
      -> `_kb_embed()` chunks + embeds into the shared `doc_chunks`
      -> `resolve_sources()` scopes retrieval to shared + this tenant

Nothing downstream of `sync()` is connector-specific. A new source type is a
new *producer*: register a KBConnectorSpec here, done — no new worker handler,
API endpoint, or UI code.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("interpreter.kb_connectors")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class KBDocument:
    """One logical document a connector produces. `external_id` is this
    document's stable identity *within its connection* (a crawl keys on page
    title — parity with the pre-registry `_crawl_site` — a sheet on row
    number, a gdoc on doc id)."""
    external_id: str
    title: str
    body_md: str
    origin: str = "crawl"          # kb_entries.origin value: crawl | gsheet | gdoc | ...
    updated_at: str | None = None  # source's own last-modified, for the UI / future incremental sync
    quality: str = "unverified"    # "official" | "community_resolved" | "unverified"
    #   ^ carried per the design doc's trust-signal decision; persistence into
    #     groundedness/confidence weighting is a later chunk (no column yet).
    extra: dict[str, Any] = field(default_factory=dict)  # origin-specific kb_entries columns
    #   ^ e.g. {"gsheet_id":..., "gsheet_row":...} / {"gdoc_id":..., "gdoc_url":...}
    #     merged verbatim into the upserted row so the existing data shape is kept.


@dataclass
class KBSyncResult:
    documents: list[KBDocument]
    watermark: dict[str, Any] | None = None
    exhaustive: bool = True
    #   ^ False => this run did NOT see the source's whole current contents
    #     (a crawl truncated by max_pages) -> the driver must NOT archive
    #     entries missing from this run. Same guard `_crawl_site` used.


@dataclass
class SyncCtx:
    tenant_id: str | None
    sb: Any
    collection_name: str = ""


SyncFn = Callable[[dict[str, Any], "dict[str, Any] | None", "SyncCtx"], KBSyncResult]
AvailFn = Callable[["str | None", Any], "tuple[bool, str | None]"]
NormFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class KBConnectorSpec:
    slug: str
    label: str
    auth: str                       # "none" | "oauth2" | "apikey"
    sync: SyncFn
    config_fields: list[dict[str, Any]] = field(default_factory=list)
    #   [{key, label, type: "string"|"number"|"select", required, placeholder?,
    #     options?: [str], show_if?: {key, eq}}] — drives the "+ add source" form
    normalize: NormFn | None = None  # raw form dict -> stored `config` (parse a URL to an id, clamp, validate)
    available: AvailFn | None = None # (tenant_id, sb) -> (ok, reason) — e.g. "Connect Google first"
    writable: bool = False           # does an approved KIL change get written back to the source?

    def is_available(self, tenant_id: "str | None", sb: Any) -> "tuple[bool, str | None]":
        if self.available is None:
            return True, None
        try:
            return self.available(tenant_id, sb)
        except Exception as e:  # noqa: BLE001
            log.warning("kb connector %s availability check failed: %s", self.slug, e)
            return False, str(e)

    def normalize_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        return self.normalize(raw) if self.normalize else dict(raw or {})


_REGISTRY: dict[str, KBConnectorSpec] = {}


def register(spec: KBConnectorSpec) -> None:
    _REGISTRY[spec.slug] = spec


def get_kb_connector(slug: "str | None") -> KBConnectorSpec:
    spec = _REGISTRY.get(slug or "")
    if spec is None:
        raise KeyError(f"unknown KB connector {slug!r}")
    return spec


def list_kb_connectors() -> list[KBConnectorSpec]:
    return list(_REGISTRY.values())


# ==========================================================================
# Built-in connectors — each `sync()` wraps an existing, unmodified fetcher.
# ==========================================================================

# ---- 1. public_url — sitemap-first crawl (ingestion/webcrawl.py, P7c) -----
def _norm_public_url(raw: dict[str, Any]) -> dict[str, Any]:
    url = (raw.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    return {"url": url, "max_pages": max(1, min(int(raw.get("max_pages") or 20), 50))}


def _sync_public_url(config: dict, watermark: "dict | None", ctx: SyncCtx) -> KBSyncResult:
    from ingestion.webcrawl import crawl

    max_pages = int(config.get("max_pages", 20))
    pages = crawl(config["url"], max_pages=max_pages)
    docs = [
        KBDocument(
            external_id=pg["title"], title=pg["title"],
            body_md=f"<!-- {pg['url']} -->\n\n{pg['markdown']}",
            origin="crawl", quality="official",
        )
        for pg in pages
    ]
    # not truncated => this run exhausted everything reachable; safe to archive misses
    return KBSyncResult(documents=docs, exhaustive=len(pages) < max_pages)


register(KBConnectorSpec(
    slug="public_url", label="Public docs site (crawl)", auth="none",
    config_fields=[
        {"key": "url", "label": "Start URL (same host + path prefix)", "type": "string",
         "required": True, "placeholder": "https://docs.example.com/guide"},
        {"key": "max_pages", "label": "Max pages", "type": "number", "required": False,
         "placeholder": "20"},
    ],
    normalize=_norm_public_url,
    sync=_sync_public_url,
))


# ---- Google connection availability (shared by gsheets + gdocs) -----------
def _google_available(tenant_id: "str | None", sb: Any) -> "tuple[bool, str | None]":
    from interpreter import gdrive

    if not gdrive.available():
        return False, "Google is not configured on this server (GOOGLE_CLIENT_ID/SECRET)"
    if not tenant_id or not gdrive.connected(tenant_id, sb):
        return False, "Connect Google for this tenant first"
    return True, None


# ---- 2. gsheets — one KB entry per data row (interpreter/gsheets.py) ------
def _norm_gsheets(raw: dict[str, Any]) -> dict[str, Any]:
    from interpreter import gsheets

    return {"sheet_id": gsheets.parse_sheet_id(raw.get("sheet_url") or raw.get("sheet_id") or ""),
            "sheet_name": (raw.get("sheet_name") or "").strip() or None}


def _sync_gsheets(config: dict, watermark: "dict | None", ctx: SyncCtx) -> KBSyncResult:
    from interpreter import gsheets

    sheet_id = config["sheet_id"]
    fetched = gsheets.fetch_sheet(ctx.tenant_id, sheet_id,
                                  sheet_name=config.get("sheet_name"), sb=ctx.sb)
    docs = [
        KBDocument(
            external_id=str(r["row"]), title=r["title"], body_md=r["body_md"],
            origin="gsheet", updated_at=fetched.get("modified_time"),
            extra={"gsheet_id": sheet_id, "gsheet_range": fetched["tab"],
                   "gsheet_row": r["row"], "gsheet_modified": fetched.get("modified_time")},
        )
        for r in fetched["rows"]
    ]
    # fetch_sheet always reads the sheet's entire current range — no page-budget
    # ambiguity like crawling, so "not in this run" always means "gone".
    return KBSyncResult(
        documents=docs, exhaustive=True,
        watermark={"modified_time": fetched.get("modified_time"), "tab": fetched.get("tab")},
    )


register(KBConnectorSpec(
    slug="gsheets", label="Google Sheet (one entry per row)", auth="oauth2",
    config_fields=[
        {"key": "sheet_url", "label": "Google Sheet URL", "type": "string", "required": True,
         "placeholder": "https://docs.google.com/spreadsheets/d/…"},
        {"key": "sheet_name", "label": "Tab name (blank = first tab)", "type": "string",
         "required": False},
    ],
    normalize=_norm_gsheets, available=_google_available,
    sync=_sync_gsheets,
))


# ---- 3. gdocs — a linked Google Doc (interpreter/gdrive.py, Phase 15) -----
# Two **independent** org-level knobs, not one combined enum:
#   index          — is this doc read into the KB for answering?  (yes / no)
#   on_correction  — what to do to the doc when a support resolution corrects
#                    the KB content: off | suggest (GitHub issue) | write_back
# The one coupling: `suggest`/`write_back` need a KB entry to correct, so they
# require `index = yes`.
def _as_yes(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() not in ("no", "false", "0", "off", "")


def _norm_gdocs(raw: dict[str, Any]) -> dict[str, Any]:
    from interpreter import gdrive

    doc_url = (raw.get("doc_url") or "").strip()
    cfg: dict[str, Any] = {
        "doc_id": gdrive.parse_doc_id(doc_url or raw.get("doc_id") or ""),
        "doc_url": doc_url,
    }

    # back-compat: an older single `access` value maps onto the two knobs
    legacy = raw.get("access")
    index = _as_yes(raw.get("index"))
    if legacy in ("read_only", "suggest", "write_back"):
        index, on_corr = True, ("off" if legacy == "read_only" else legacy)
    else:
        on_corr = (raw.get("on_correction") or "off").strip()

    if on_corr not in ("off", "suggest", "write_back"):
        raise ValueError("on_correction must be 'off', 'suggest' or 'write_back'")
    if on_corr != "off" and not index:
        raise ValueError("'suggest' / 'write_back' need the doc read into the KB — set 'Read' to Yes")

    cfg["index"] = index
    cfg["on_correction"] = on_corr
    if on_corr != "off":
        repo = (raw.get("github_repo") or "").strip()
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise ValueError("github_repo must be 'owner/name' when the bot opens review issues")
        cfg["github_repo"] = repo
    return cfg


def _sync_gdocs(config: dict, watermark: "dict | None", ctx: SyncCtx) -> KBSyncResult:
    from interpreter import gdrive

    # `index = no` — the doc is connected (so a future correction *could* be
    # aimed at it once turned on) but not read for answering: produce nothing,
    # which lets the generic driver archive any prior entry.
    if config.get("index") is False:
        return KBSyncResult(documents=[], exhaustive=True)

    doc_id = config["doc_id"]
    fetched = gdrive.fetch_doc(ctx.tenant_id, doc_id, ctx.sb)
    doc = KBDocument(
        external_id=doc_id, title=fetched["title"], body_md=fetched["markdown"],
        origin="gdoc", updated_at=fetched.get("modified_time"),
        extra={"gdoc_id": doc_id, "gdoc_url": config.get("doc_url"),
               "gdoc_modified": fetched.get("modified_time"),
               "synced_at": _now_iso(), "sync_error": None},
    )
    return KBSyncResult(documents=[doc], exhaustive=True,
                        watermark={"modified_time": fetched.get("modified_time")})


register(KBConnectorSpec(
    slug="gdocs", label="Google Doc", auth="oauth2", writable=True,
    config_fields=[
        {"key": "doc_url", "label": "Google Doc URL", "type": "string", "required": True,
         "placeholder": "https://docs.google.com/document/d/…"},
        {"key": "index", "label": "Read this doc into the knowledge base",
         "type": "select", "required": False, "options": ["yes", "no"],
         "option_labels": {
             "yes": "Yes — the bot can use it to answer",
             "no": "No — connected but not used for answers",
         }},
        {"key": "on_correction", "label": "When a support resolution corrects this content",
         "type": "select", "required": False,
         "options": ["off", "suggest", "write_back"],
         "option_labels": {
             "off": "Do nothing to the doc (default)",
             "suggest": "Open a GitHub issue with the fix — a person applies it (recommended)",
             "write_back": "Let the bot edit the doc — a person verifies",
         },
         "help": "'Suggest' and 'Let the bot edit' both need 'Read' set to Yes."},
        {"key": "github_repo", "label": "GitHub repo for review issues (owner/name)",
         "type": "string", "required": False, "placeholder": "acme/support-kb",
         "help": "Where the correction issue is opened. Needs a GitHub connection for this tenant.",
         "show_if": {"key": "on_correction", "ne": "off"}},
    ],
    normalize=_norm_gdocs, available=_google_available,
    sync=_sync_gdocs,
))
