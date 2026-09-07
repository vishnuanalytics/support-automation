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
    document's stable identity *within its connection* (a crawl keys on the
    page URL, a sheet on row number, a gdoc on doc id)."""
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
    # {external_id: {body_md, gdoc_modified, ...}} for the connection's current
    # active entries — lets a `sync()` skip re-fetching an item it can tell is
    # unchanged (incremental). The generic driver still byte-compares bodies,
    # so reusing a stored body here just avoids the fetch/API call.
    existing: dict[str, dict[str, Any]] = field(default_factory=dict)


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
# A "connect a source" crawl is meant to pull a whole docs/help site, going
# as deep as the site is — not a 20-page skim (that's the onboarding wizard's
# sample). Wide ceilings; the site's own extent is the real bound, and the
# worker gives `crawl_site` a long per-job budget (see api/worker.py).
_CRAWL_MAX_PAGES_DEFAULT = 500
_CRAWL_MAX_PAGES_CEILING = 5000
_CRAWL_MAX_DEPTH_DEFAULT = 8
_CRAWL_MAX_DEPTH_CEILING = 20


def _norm_public_url(raw: dict[str, Any]) -> dict[str, Any]:
    url = (raw.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    return {
        "url": url,
        "max_pages": max(1, min(int(raw.get("max_pages") or _CRAWL_MAX_PAGES_DEFAULT),
                                _CRAWL_MAX_PAGES_CEILING)),
        "max_depth": max(1, min(int(raw.get("max_depth") or _CRAWL_MAX_DEPTH_DEFAULT),
                                _CRAWL_MAX_DEPTH_CEILING)),
    }


def _sync_public_url(config: dict, watermark: "dict | None", ctx: SyncCtx) -> KBSyncResult:
    from ingestion.webcrawl import crawl

    max_pages = int(config.get("max_pages", _CRAWL_MAX_PAGES_DEFAULT))
    max_depth = int(config.get("max_depth", _CRAWL_MAX_DEPTH_DEFAULT))
    pages = crawl(config["url"], max_pages=max_pages, max_depth=max_depth)
    docs = [
        KBDocument(
            # the URL is a page's stable identity — NOT its <title>. Many doc
            # systems (GitBook, Docusaurus, …) render a section-scoped or
            # templated <title> that repeats across pages, which would
            # collapse distinct pages onto one entry (last write wins) and
            # then archive the rest as "missing".
            external_id=pg["url"], title=pg["title"],
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
         "placeholder": str(_CRAWL_MAX_PAGES_DEFAULT),
         "help": f"blank = {_CRAWL_MAX_PAGES_DEFAULT}; up to {_CRAWL_MAX_PAGES_CEILING}. "
                 "The crawler stops early once it has followed everything under the start path."},
        {"key": "max_depth", "label": "Crawl depth", "type": "number", "required": False,
         "placeholder": str(_CRAWL_MAX_DEPTH_DEFAULT),
         "help": f"how many links deep to follow from the start URL (blank = "
                 f"{_CRAWL_MAX_DEPTH_DEFAULT}). The sitemap is always followed regardless."},
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


def _clamp_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


# A per-connector cap on how many items one sync pulls — a huge Drive folder
# or Linear workspace would otherwise blow JOB_TIMEOUT and enqueue hundreds
# of embed jobs in one go. When a sync is capped it reports `exhaustive=False`
# so the driver won't archive the items it didn't see (same guard the crawl's
# `max_pages` uses).
_MAX_ITEMS_FIELD = {
    "key": "max_items", "label": "Max items per sync", "type": "number",
    "required": False, "placeholder": "300",
    "help": "Cap on documents/issues/posts pulled in one run.",
}


def _norm_gdocs(raw: dict[str, Any]) -> dict[str, Any]:
    from interpreter import gdrive

    doc_url = (raw.get("doc_url") or "").strip()
    src = doc_url or raw.get("doc_id") or ""
    cfg: dict[str, Any] = {"doc_url": doc_url}

    folder_id = gdrive.parse_folder_id(src)   # a Drive-folder URL -> sync every Doc in it
    if folder_id:
        cfg["folder_id"] = folder_id
        cfg["recursive"] = _as_yes(raw.get("recursive"), default=False)
    else:
        cfg["doc_id"] = gdrive.parse_doc_id(src)

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
    if on_corr != "off" and cfg.get("folder_id"):
        raise ValueError("correction write-back needs a single linked Doc, not a whole folder "
                         "— set correction handling to 'off' for a folder connection")

    cfg["index"] = index
    cfg["on_correction"] = on_corr
    if on_corr != "off":
        repo = (raw.get("github_repo") or "").strip()
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise ValueError("github_repo must be 'owner/name' when the bot opens review issues")
        cfg["github_repo"] = repo
    if cfg.get("folder_id"):
        cfg["max_items"] = _clamp_int(raw.get("max_items"), 300, 1, 2000)
    return cfg


def _gdoc_kbdoc(fetched: dict, doc_id: str, doc_url: str | None) -> "KBDocument":
    return KBDocument(
        external_id=doc_id, title=fetched["title"], body_md=fetched["markdown"],
        origin="gdoc", updated_at=fetched.get("modified_time"),
        extra={"gdoc_id": doc_id,
               "gdoc_url": doc_url or f"https://docs.google.com/document/d/{doc_id}/edit",
               "gdoc_modified": fetched.get("modified_time"),
               "synced_at": _now_iso(), "sync_error": None},
    )


def _sync_gdocs(config: dict, watermark: "dict | None", ctx: SyncCtx) -> KBSyncResult:
    from interpreter import gdrive

    # `index = no` — the doc is connected (so a future correction *could* be
    # aimed at it once turned on) but not read for answering: produce nothing,
    # which lets the generic driver archive any prior entry.
    if config.get("index") is False:
        return KBSyncResult(documents=[], exhaustive=True)

    # Folder scope — one KBDocument per Doc in the folder (like gsheets is one
    # per row). `exhaustive` so the driver archives a Doc removed from the
    # folder. (Fetches every Doc each run; `changes.list` incremental is a
    # later optimization — same note the design doc makes.)
    if config.get("folder_id"):
        cap = int(config.get("max_items") or 300)
        found = gdrive.list_folder_docs(ctx.tenant_id, config["folder_id"], ctx.sb,
                                        recursive=bool(config.get("recursive")))
        docs = found[:cap]
        out, reused = [], 0
        for d in docs:
            prev = ctx.existing.get(d["id"])
            # incremental: the folder listing is cheap; the per-Doc fetch is
            # not. If Drive's modifiedTime matches what we stored, reuse the
            # stored body and skip the fetch entirely.
            if prev and prev.get("gdoc_modified") and prev["gdoc_modified"] == d.get("modified_time"):
                out.append(_gdoc_kbdoc(
                    {"title": d.get("name") or d["id"],
                     "markdown": prev.get("body_md") or "",
                     "modified_time": d.get("modified_time")},
                    d["id"], None))
                reused += 1
            else:
                out.append(_gdoc_kbdoc(gdrive.fetch_doc(ctx.tenant_id, d["id"], ctx.sb), d["id"], None))
        latest = max((d.get("modified_time") or "" for d in docs), default=None)
        return KBSyncResult(documents=out, exhaustive=len(found) <= cap,
                            watermark={"modified_time": latest, "count": len(out), "reused": reused})

    doc_id = config["doc_id"]
    fetched = gdrive.fetch_doc(ctx.tenant_id, doc_id, ctx.sb)
    return KBSyncResult(documents=[_gdoc_kbdoc(fetched, doc_id, config.get("doc_url"))],
                        exhaustive=True,
                        watermark={"modified_time": fetched.get("modified_time")})


def _kb_doc(external_id: str, title: str, body_md: str, *, origin: str, url: str = "",
           updated_at: str | None = None, quality: str = "unverified") -> "KBDocument":
    body = f"<!-- {url} -->\n\n{body_md}" if url else body_md
    return KBDocument(external_id=external_id, title=title or origin, body_md=body,
                      origin=origin, updated_at=updated_at, quality=quality)


# ---- 4. linear — Documents + resolved issues (interpreter/linear.py) -----
def _linear_available(t: "str | None", s: Any) -> "tuple[bool, str | None]":
    from interpreter import linear
    return linear.available(t, s)


def _nolt_available(t: "str | None", s: Any) -> "tuple[bool, str | None]":
    from interpreter import nolt
    return nolt.available(t, s)


def _discourse_available(t: "str | None", s: Any) -> "tuple[bool, str | None]":
    from interpreter import discourse
    return discourse.available(t, s)


def _norm_linear(raw: dict[str, Any]) -> dict[str, Any]:
    inc = (raw.get("include") or "both").strip()
    if inc not in ("documents", "issues", "both"):
        raise ValueError("include must be 'documents', 'issues' or 'both'")
    cfg: dict[str, Any] = {"include": inc}
    tk = (raw.get("team_key") or "").strip()
    if tk:
        cfg["team_key"] = tk
    cfg["max_items"] = _clamp_int(raw.get("max_items"), 300, 1, 2000)
    return cfg


def _sync_linear(config: dict, watermark: "dict | None", ctx: SyncCtx) -> KBSyncResult:
    from interpreter import linear

    inc = config.get("include", "both")
    team_key = (config.get("team_key") or "").strip() or None
    cap = int(config.get("max_items") or 300)
    since = (watermark or {}).get("since")   # incremental after the first run
    newest = since or ""
    docs: list[KBDocument] = []

    if inc in ("documents", "both"):
        for d in linear.fetch_documents(ctx.tenant_id, ctx.sb, limit=cap, updated_after=since):
            newest = max(newest, d.get("updatedAt") or "")
            body = (d.get("content") or "").strip()
            if not body:
                continue
            docs.append(_kb_doc(f"doc:{d['id']}", d.get("title") or "Linear document",
                                body, origin="linear", url=d.get("url", ""),
                                updated_at=d.get("updatedAt"), quality="official"))

    if inc in ("issues", "both"):
        for it in linear.fetch_resolved_issues(ctx.tenant_id, ctx.sb, team_key=team_key,
                                               limit=cap, updated_after=since):
            newest = max(newest, it.get("updatedAt") or "")
            desc = (it.get("description") or "").strip()
            comments = [c for c in ((it.get("comments") or {}).get("nodes") or [])
                        if (c.get("body") or "").strip()]
            if not desc and not comments:
                continue                       # a bare "done" issue is not knowledge
            head = f"{it.get('identifier', '')} {it.get('title', '')}".strip()
            parts = [f"# {head}"] + ([desc] if desc else [])
            for c in comments:
                who = (c.get("user") or {}).get("name") or "someone"
                parts.append(f"**{who}:** {c['body'].strip()}")
            docs.append(_kb_doc(f"issue:{it['id']}", head, "\n\n".join(parts),
                                origin="linear", url=it.get("url", ""),
                                updated_at=it.get("updatedAt"), quality="community_resolved"))

    # An incremental run only sees *changed* items, so it must NOT archive
    # everything else. A first (full) run still archives + can truncate.
    return KBSyncResult(
        documents=docs,
        exhaustive=(since is None) and len(docs) < cap,
        watermark={"since": newest or None, "count": len(docs)},
    )


register(KBConnectorSpec(
    slug="linear", label="Linear", auth="apikey",
    config_fields=[
        {"key": "api_key", "label": "Linear API key", "type": "string", "required": False,
         "secret": True, "placeholder": "lin_api_…",
         "help": "Settings → API → Personal API keys. Leave blank to reuse a saved key."},
        {"key": "include", "label": "What to pull", "type": "select", "required": False,
         "options": ["both", "documents", "issues"],
         "option_labels": {"both": "Documents + resolved issues",
                           "documents": "Documents only", "issues": "Resolved issues only"}},
        {"key": "team_key", "label": "Team key (optional — limits issues to one team)",
         "type": "string", "required": False, "placeholder": "ENG"},
        _MAX_ITEMS_FIELD,
    ],
    normalize=_norm_linear, available=_linear_available,
    sync=_sync_linear,
))


# ---- 5. nolt — resolved feedback-board posts (interpreter/nolt.py) ------
def _norm_nolt(raw: dict[str, Any]) -> dict[str, Any]:
    bid = (raw.get("board_id") or "").strip()
    if not bid:
        raise ValueError("board_id is required (Nolt board admin → API)")
    return {"board_id": bid, "max_items": _clamp_int(raw.get("max_items"), 500, 1, 2000)}


def _sync_nolt(config: dict, watermark: "dict | None", ctx: SyncCtx) -> KBSyncResult:
    from interpreter import nolt

    cap = int(config.get("max_items") or 500)
    docs: list[KBDocument] = []
    for p in nolt.fetch_resolved_posts(ctx.tenant_id, ctx.sb, config["board_id"], max_posts=cap):
        parts = [f"# {p.get('title', '')}".strip()]
        if (p.get("description") or "").strip():
            parts.append(p["description"].strip())
        for c in (p.get("_comments") or []):
            who = (c.get("author") or {}).get("name") or (c.get("user") or {}).get("name") or "someone"
            body = (c.get("body") or c.get("content") or "").strip()
            if body:
                parts.append(f"**{who}:** {body}")
        docs.append(_kb_doc(f"post:{p['id']}", p.get("title") or "Nolt post",
                            "\n\n".join(parts), origin="nolt", url=p.get("url", ""),
                            updated_at=p.get("updatedAt") or p.get("updated_at"),
                            quality="community_resolved"))
    return KBSyncResult(documents=docs, exhaustive=len(docs) < cap,
                        watermark={"count": len(docs)})


# ---- 5b. discourse — resolved forum threads (interpreter/discourse.py) ----
def _norm_discourse(raw: dict[str, Any]) -> dict[str, Any]:
    base = (raw.get("base_url") or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("base_url must be the forum's http(s) URL")
    cfg: dict[str, Any] = {
        "base_url": base,
        "resolved_only": _as_yes(raw.get("resolved_only"), default=True),
        "max_items": _clamp_int(raw.get("max_items"), 300, 1, 2000),
    }
    cat = (raw.get("category") or "").strip()
    if cat:
        cfg["category"] = cat
    user = (raw.get("api_username") or "").strip()
    if user:
        cfg["api_username"] = user
    return cfg


def _sync_discourse(config: dict, watermark: "dict | None", ctx: SyncCtx) -> KBSyncResult:
    from interpreter import discourse

    cap = int(config.get("max_items") or 300)
    resolved_only = config.get("resolved_only", True)
    topics = discourse.fetch_topics(
        ctx.tenant_id, ctx.sb, config["base_url"],
        category=config.get("category"), resolved_only=resolved_only, limit=cap,
        api_username=config.get("api_username"))

    docs: list[KBDocument] = []
    for t in topics:
        posts = t.get("posts") or []
        parts = [f"# {t.get('title', '')}".strip()]
        if posts:
            parts.append(f"**Question ({posts[0].get('username') or 'a user'}):** "
                         f"{(posts[0].get('text') or '').strip()}")
        for p in posts[1:]:
            if p.get("accepted"):
                parts.append(f"**Accepted answer ({p.get('username') or 'a user'}):** "
                             f"{(p.get('text') or '').strip()}")
        # if nothing was marked accepted, keep the next 2 replies as context
        if not any(p.get("accepted") for p in posts):
            for p in posts[1:3]:
                if (p.get("text") or "").strip():
                    parts.append(f"**{p.get('username') or 'a user'}:** {p['text'].strip()}")
        docs.append(_kb_doc(
            f"topic:{t['id']}", t.get("title") or "Forum thread", "\n\n".join(parts),
            origin="discourse", url=t.get("url", ""),
            quality="community_resolved" if t.get("solved") else "unverified"))

    # `/latest` (and even a category listing) is a window, not the whole
    # forum — never archive a thread just because it dropped off the page.
    return KBSyncResult(documents=docs, exhaustive=False,
                        watermark={"count": len(docs)})


register(KBConnectorSpec(
    slug="discourse", label="Discourse forum", auth="apikey",
    config_fields=[
        {"key": "base_url", "label": "Forum URL", "type": "string", "required": True,
         "placeholder": "https://forum.acme.com"},
        {"key": "resolved_only", "label": "Only threads with an accepted answer",
         "type": "select", "required": False, "options": ["yes", "no"]},
        {"key": "category", "label": "Category slug/id (optional — blank = latest)",
         "type": "string", "required": False, "placeholder": "support"},
        {"key": "api_key", "label": "API key (only for a private forum)", "type": "string",
         "required": False, "secret": True,
         "help": "Public forums need none. A gated one: Admin → API → new key."},
        {"key": "api_username", "label": "API username (with the key)", "type": "string",
         "required": False, "placeholder": "system"},
        _MAX_ITEMS_FIELD,
    ],
    normalize=_norm_discourse, available=_discourse_available,
    sync=_sync_discourse,
))


register(KBConnectorSpec(
    slug="nolt", label="Nolt (feedback board)", auth="apikey",
    config_fields=[
        {"key": "api_key", "label": "Nolt board API key", "type": "string", "required": False,
         "secret": True,
         "help": "Nolt board admin → API. Leave blank to reuse a saved key."},
        {"key": "board_id", "label": "Board id", "type": "string", "required": True,
         "help": "Also from the board admin → API panel (not the board URL slug)."},
        _MAX_ITEMS_FIELD,
    ],
    normalize=_norm_nolt, available=_nolt_available,
    sync=_sync_nolt,
))


register(KBConnectorSpec(
    slug="gdocs", label="Google Doc or Drive folder", auth="oauth2", writable=True,
    config_fields=[
        {"key": "doc_url", "label": "Google Doc or Drive folder URL", "type": "string",
         "required": True, "placeholder": "https://docs.google.com/document/d/…  or  /drive/folders/…"},
        {"key": "recursive", "label": "If a folder: include subfolders",
         "type": "select", "required": False, "options": ["no", "yes"]},
        {"key": "max_items", "label": "If a folder: max Docs per sync", "type": "number",
         "required": False, "placeholder": "300",
         "help": "Ignored for a single Doc."},
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
