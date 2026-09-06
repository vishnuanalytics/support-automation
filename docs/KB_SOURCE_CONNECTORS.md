# Knowledge source connectors: public URLs, Google Docs/Sheets, Linear, forums

Decision record, not a build plan — same convention as
`MULTI_SYSTEM_ARCHITECTURE.md`: captures the design so a later session
doesn't re-derive it from scratch. Nothing here is built except what it
explicitly says already exists.

**Built so far (2026-09-06):** the `KBConnectorSpec` registry
(`interpreter/kb_connectors.py`), a first-class `kb_source_connections` table
(migration 091), one generic sync driver
(`api/worker.py::_sync_kb_connection`), the `/api/kb/connectors` +
`/api/kb/collections/{sid}/connections` + `/api/kb/connections/{cid}` routes,
and a unified **"Connected sources"** panel in `KnowledgeView.tsx` (add /
re-sync / pause / disconnect, one registry-driven form for every connector).
Three connectors are registered behind it: `public_url` (§1), `gsheets` (§3),
`gdocs` (§2). Linear (§4), forums (§5) and Nolt (§6) are **not built** — each
is now just "register a `KBConnectorSpec` + a `sync()`", no new
worker/endpoint/UI code.

## The actual insight: this is not five problems

Every existing ingestion path (`webcrawl.py`, `fileimport.py`, `gdrive.py`,
the manual `KnowledgeView` editor) already converges on the exact same
narrow contract, proven by `api/worker.py::_embed_kb_entry` /
`_crawl_site`:

    produce (title, body_md) pairs -> one `kb_entries` row each
      -> `_kb_embed()` chunks + embeds into the shared `doc_chunks`
      -> `resolve_sources()` scopes retrieval to shared + this tenant

**A new source type is a new *producer* of `(title, body_md)` pairs.
Nothing downstream of that changes** — not retrieval, not scoping, not
the confidence gate, not groundedness. This is the same lesson FR-47's
`connectors.py` already proved for case-system actions ("connector is
data, not a hardcoded node handler") — apply it here instead of building
five bespoke, one-off ingestion scripts that each reinvent chunking,
scoping, and dedup slightly differently (exactly the two-sources-of-truth
bug class this session already found and fixed twice elsewhere).

## The contract: `KBConnectorSpec`

Mirrors `interpreter/connectors.py`'s `ConnectorSpec`/`ActionSpec` shape
(same file family, same philosophy), but for *pulling content* instead of
*invoking an action*:

```python
@dataclass
class KBDocument:
    external_id: str        # stable id in the source system (issue #, page url, sheet row key)
    title: str
    body_md: str
    updated_at: str | None  # source's own last-modified, for incremental sync
    origin: str             # "crawl" | "gdoc" | "gsheet" | "linear" | "forum"
    quality: str = "unverified"   # see "trust signal" below

@dataclass
class KBConnectorSpec:
    slug: str                 # "public_url" | "gdocs" | "gsheets" | "linear" | "discourse"
    label: str
    auth: str                 # "none" | "oauth2" | "apikey" — same enum connectors.py already uses
    sync: Callable[[dict, str | None], Iterator[KBDocument]]  # (source.config, watermark) -> documents
```

`sources.config` (already free-form jsonb, `sources.kind` already free-text
— migration 015 was built exactly this extensible from day one) holds
whatever the connector needs: a base URL, a Linear team id, a Drive folder
id, a Discourse category slug. `sources.kind` becomes the `KBConnectorSpec`
slug. One sync job, one shared code path, per connector — same
`_kb_embed()` on the other end regardless of which one produced the
document.

## Per-source design

### 1. Public URLs — already built, extend, don't rebuild
`ingestion/webcrawl.py` (P7c) already does BFS same-host+path-prefix,
robots.txt, SSRF-blocked, heading-aware markdown. Real gaps worth closing
before touching anything else:
- **Sitemap-first discovery** (`GET /sitemap.xml`) instead of blind BFS —
  cheaper (fewer wasted fetches on nav/marketing pages) and more complete
  (catches pages with no inbound link from the crawl root).
- **Scheduled re-crawl + content-hash diff**, not "crawl once at setup and
  never again" — a help center changes; today's `kb_entries` row only
  updates when someone manually re-clicks "crawl a site". Same
  `embed_hash`-skip-if-unchanged discipline `_embed_kb_entry` already uses
  for manual edits, applied to a nightly re-crawl job.

### 2. Google Docs — already built (single-doc), extend to folder scope
`interpreter/gdrive.py`'s OAuth (`drive.readonly`) + `fetch_doc()` already
work per-document. Real gap: today a tenant links docs one at a time
(`KnowledgeView`'s "+ Google Doc" button). Extend to **"sync this whole
Shared Drive / folder"**: Drive API's `files.list` (query by parent folder
id) for the initial pull, `changes.list` + a saved `startPageToken` for
incremental sync after — same resumable-watermark discipline
`graph_sync_state` already established for `case_graph_sync.py`, not a new
pattern.

#### Write-back — built 2026-09-06

A gdocs connection has **two independent knobs** in `config` (picked as two
separate dropdowns in the "+ add source" form; `KBConnectorSpec.writable` =
True for gdocs only):

**`index`** — `true` (default) / `false`. `false` = the doc is connected but
not read into the KB (`_sync_gdocs` yields nothing, so the generic driver
archives any prior entry); the bot won't use it to answer. Turning it back
on re-creates the entry.

**`on_correction`** — what to do to the doc when the Knowledge Integrity
Loop's manager review approves a correction for that doc's KB entry:

| value | effect |
|---|---|
| `off` *(default)* | only the internal `kb_entries` mirror is updated — the doc is never touched |
| **`suggest`** *(recommended)* | open a GitHub issue with the old→new diff + a doc link; **the bot does not edit the doc**. A human applies it and closes the issue; the mirror re-syncs on close. Needs `config.github_repo`, **not** the write OAuth scopes. |
| `write_back` | the bot rewrites the passage in place, opens the issue for the human to verify / `/revert`, re-syncs the mirror. Needs `config.github_repo` **and** the read-write `documents`/`drive` scopes. |

The one coupling: `on_correction` ≠ `off` requires `index = true` (there has
to be a KB entry to correct) — `_norm_gdocs` rejects the combination. An
older single `access` value (`read_only`/`suggest`/`write_back`) still
normalizes: it maps to `{index: true, on_correction: <the same, "off" for
read_only>}`.

**Org-level default (built 2026-09-06).** `tenants.kb_doc_defaults` (jsonb,
migration `094`) is a partial `{index?, on_correction?, github_repo?}` blob
— `GET`/`PUT /api/kb/doc-defaults` (editor-gated, `_validate_kb_doc_
defaults` applies the same rules). `_kb_add_connection` merges it *under* an
incoming gdocs config (`{**default, **raw}`, per-doc wins), and the "+ add
source" form pre-fills the two dropdowns + repo from it. So an org sets
"every new doc → suggest into `acme/support-kb`" once. `{}` = system
defaults (`index: true`, `on_correction: off`).

`suggest` and `write_back` share the same `gdoc_writeback` job, the same
`kb_doc_writebacks` tracking row, and the same watch (below). The rest of
this section describes `write_back`; `suggest` is the same minus the
`replace_passage` calls, with `status='suggested'` and no immediate
`kb_sync` (the watch enqueues one when the issue closes).

When the Knowledge Integrity Loop's manager review
approves a correction (`interpreter/kb_writeback.py::apply_kb_change`) for an
entry on a `suggest` / `write_back` connection:

- `_doc_change_blocks()` paragraph-diffs the old vs. new entry markdown into
  `{old, new}` passages;
- a `gdoc_writeback` worker job re-fetches the doc (a `modifiedTime` mismatch
  vs. the last sync ⇒ **conflict**: open the issue, edit nothing), then for
  each block does `documents.batchUpdate` / `replaceAllText`
  (`gdrive.replace_passage`) — 0 replacements ⇒ that block is flagged for a
  manual edit;
- a **GitHub issue** (`label: kb-writeback`, repo from
  `config.github_repo`) carries the per-block old→new diff and which blocks
  applied — the human verifies the doc against it and **closes the issue to
  confirm**;
- a best-effort Drive comment points at the issue; the connection is
  re-`kb_sync`'d so the mirror re-reads the edited doc;
- every edit is a `kb_doc_writebacks` row (migration `092`) with a pre-edit
  markdown snapshot.

The Slack manager review still gates the internal `kb_entries` copy — GitHub
is **in addition**. Needs the read-write `documents` + `drive` scopes
(`gdrive.SCOPES`); a tenant on an older read-only token gets a clean job
failure until they re-consent. Google has no API for tracked "suggestions",
so verification is against the issue diff + the live doc, not Docs suggestion
mode.

**The watch (close the loop).** `interpreter/kb_writeback.py::
watch_doc_writebacks()` polls every open
(`suggested`/`applied`/`partial`/`conflict`) `kb_doc_writebacks` row's
GitHub issue (`github.get_issue` / `list_issue_comments`): issue **closed**
⇒ `status='verified'` + `verified_at` (and, for a `suggested` row, a
`kb_sync` — the human just edited the doc); a **`/revert`** comment (on an
`applied`/`partial` row only) ⇒ `gdrive.replace_passage` reverses each
applied block (`new` → `old`), `status='reverted'`, a confirming issue
comment, connection re-`kb_sync`'d. Runs from
`ingestion/kb_writeback_watch.py` (wired into `daily-sync.yml`), same "no
always-on worker host" pattern as `ingestion/kb_recrawl.py`.

The tenant-wide list is surfaced in `ReviewView.tsx` (a "N doc write-backs
awaiting verification" panel; `GET /api/kb/doc-writebacks?status=open|all`,
rows enriched with the connection label + doc url), and per-collection in
the "Connected sources" panel. **Still not built:** a Slack "send to GitHub
before applying" button, and true index-range structural section
replacement (vs. today's `replaceAllText` find/replace).

### 3. Google Sheets — not built; needs its own chunking model, not prose
A support/FAQ spreadsheet is structured data, not prose — treating a whole
sheet as one `body_md` blob (the naive approach) destroys retrieval
quality, because a 200-row Q&A sheet becomes one giant chunk nothing scores
well against. Correct shape: **one `KBDocument` per row** (or per logical
record — a header-driven column mapping, e.g. `question`/`answer` or
`topic`/`resolution` columns become the row's title/body), same Drive
OAuth (`spreadsheets.readonly` scope addition), `Sheets.values.get` per
sheet/tab. This is the one connector where "reuse the existing crawler"
would be actively wrong — flag it as its own extraction path, not a
`webcrawl.py` variant.

### 4. Linear — not built
Linear's GraphQL API (issues, comments, project **Documents** — Linear's
own wiki-like docs, distinct from issues) is the source. Two content types
worth treating differently:
- **Linear Documents** (long-form, prose) — same shape as a crawled page:
  one `KBDocument` per doc, direct markdown export.
- **Resolved issues + their comment thread** — same "pattern vs proof"
  discipline `case_memory.py` already applies to resolved support cases
  (`resolution_kind`, `generalizable`): a resolved bug's root-cause comment
  is real institutional knowledge; a still-open issue or a one-line "wontfix"
  isn't worth embedding at all. Reuse that filter, don't invent a new one.
  Auth: Linear issues a personal API key (simplest — `apikey` auth, same
  enum as Zendesk's connector) or OAuth for a real per-tenant app; API key
  first, OAuth later mirrors how Zendesk shipped before Freshchat's OAuth
  did.

### 5. Forums — not built; prefer the platform's API over HTML scraping
"Forum" isn't one shape — Discourse, a Zendesk Community, a custom phpBB,
etc. Discourse (the common case) has a real JSON API
(`/c/<category>.json`, `/t/<topic_id>.json`) — **use it, don't screen-scrape
HTML through `webcrawl.py`**, same reasoning as Linear: an API gives
structured author/accepted-answer/timestamp data a generic crawler would
have to guess at from HTML markup, and it's far less fragile. Extraction
mirrors Linear's issue treatment: a thread with a marked "solution" post is
high-trust content; an unresolved or argumentative thread isn't. A forum
with no API (custom-built, gated) falls back to the existing crawler as a
worse-than-ideal but functional default — don't block the whole feature on
building N bespoke forum-software connectors up front.

### 6. Nolt — not built; a feedback/roadmap board, same shape as a forum
Nolt (nolt.io — like Canny/Featurebase) is a hosted feedback board: users post
requests/bugs, others vote and comment, and an admin marks a post's status
(`planned` / `in progress` / `complete` / `declined`). It has a REST API
(`GET /v1/boards/{boardId}/posts`, `/v1/posts/{id}/comments`), so — same
reasoning as Linear (§4) and forums (§5) — **use the API, don't scrape the
public board HTML through `webcrawl.py`**. Extraction mirrors the forum
treatment:
- A post whose status is `complete` (shipped, with the resolution in the
  post body / a pinned admin comment) is real institutional knowledge —
  `quality="community_resolved"`, one `KBDocument` per post (title = the
  post title, body = description + the admin/accepted comment).
- A still-open `under review` / `planned` post, or a `declined` one, isn't
  worth embedding — it's a wish, not an answer. Same "pattern vs proof"
  filter `case_memory.py` and §4 already apply.
- Auth: Nolt issues an API key (`apikey` auth, same enum as Linear /
  Zendesk). One board id per connection (`config: {board_id, api_key_ref}`),
  a tenant can connect several. `watermark` = the last post `updatedAt` seen,
  for incremental re-sync.

This is a sibling of §5, not a new architecture — it lands the same way:
a `KBConnectorSpec(slug="nolt", auth="apikey", …)` whose `sync()` pages the
posts API and yields the resolved ones.

## Cross-cutting decisions (the "good structure" part)

These are what actually make five connectors compose into one coherent
system instead of five one-off scripts — this is the part worth getting
right before writing connector #1, not something to retrofit after #5:

1. **Incremental sync, always.** Every connector's `sync()` takes a
   watermark and returns one to persist (`sources.config` gets a
   `last_synced` field, or a dedicated table mirroring
   `graph_sync_state` if per-connector cursor shape gets complex). A
   from-scratch re-pull on every sync is the compute-cost mistake this
   session already spent effort closing elsewhere (case_events/audit_log
   retention, judge-call caching) — don't reintroduce it here.
2. **Soft-delete on the losing side of an update**, not hard-delete — same
   convention as everywhere else in this codebase (KIL's `superseded`,
   `zapier_docs.status`). A page that's gone from the source (404 on
   re-crawl, a deleted Linear issue) gets `status='archived'` on its
   `kb_entries` row, never a hard delete — an external source failing
   transiently shouldn't silently erase real content.
3. **Content-type-aware chunking is not optional.** Prose (crawled pages,
   Google Docs, Linear Documents, forum posts) chunks by heading/paragraph,
   same as today. Row-shaped data (Google Sheets, and arguably a
   structured FAQ import) chunks by row. Getting this wrong doesn't error
   loudly — it silently produces bad retrieval, the hardest kind of bug to
   notice.
4. **A trust/quality signal per document, not "all ingested text is
   equally authoritative."** An official help article and a random
   unresolved forum reply should not score identically in retrieval. The
   `KBDocument.quality` field above (`"official"` / `"community_resolved"`
   / `"unverified"`) is a cheap, real signal to thread into `groundedness`/
   `confidence_gate`'s existing weighting — this project already has the
   exact same discipline for case_memory (`resolution_kind`,
   `generalizable`); extend it, don't invent a parallel concept.
5. **Auth model matches `connectors.py`'s existing enum** (`none` /
   `apikey` / `oauth2`) — a tenant connecting Linear or a Google Sheet
   should see the same one-form "connect" UX already built for
   Zendesk/Freshchat, not a bespoke flow per source type.
6. **Access boundary, explicitly, per connector.** `sources.tenant_id`
   already draws the tenant/tenant line (Phase 12). *Within* a tenant, some
   connectors need to respect the source's own ACLs (a Drive folder a
   token can only see some files in) rather than assuming "connected =
   everything is shareable" — worth a one-line note per connector's config
   (`respects_source_acl: bool`), not a design problem to solve generically
   up front.

## Onboarding tie-in (answers the "easy user journey" question directly)

The real fix isn't a new architecture, it's a missing step in
`OnboardingWizard.tsx`: today's 4 steps (Salesforce, Slack, model, first
flow) skip KB entirely — a tenant discovers `KnowledgeView` after
onboarding, empty. Add a step: pick connectors to enable (checkboxes,
each showing its `auth` requirement), enter their config (a URL, a Linear
API key, a Sheet id), and kick off `sync()` as background jobs the tenant
sees progress on ("crawling your help center... 40 pages found") instead
of landing in a flow with a KB no one's populated yet. This is additive to
the wizard, not a rebuild of it.

## What this does NOT solve

The prior conversation's question — "can we crawl internal sites gated by
Google SSO" — stays open. None of the five connectors above touch that;
Google OAuth here only ever means the *structured Drive/Sheets API*, never
a general session-passthrough into arbitrary third-party tools. A gated
internal wiki/Confluence/Notion needs its own bespoke connector (its own
API, its own auth) if and when it's worth building — not something this
design makes any easier or harder.

## Suggested build order, if/when this gets picked up

Not committed to, just the honest ranking: (1) ~~public-URL sitemap +
re-crawl~~ **built 2026-09-05** (cheapest, extends what already works, no new
auth model) → (2) ~~Google Sheets~~ **built 2026-09-05** (reuses existing
Drive OAuth, high value for FAQ-shaped tenants) → (2.5) ~~the
`KBConnectorSpec` registry + `kb_source_connections` table + generic sync
driver + unified "Connected sources" UI~~ **built 2026-09-06, see
PROJECT_SCOPE.md** (the "get the structure right before connector #3" step —
existing crawl/gsheet/gdoc paths migrated onto it) → (3) Linear
(`apikey` auth, clean GraphQL API) → (4) forums / **Nolt (§6)** (API-per-
platform, `apikey`; Nolt first since a real tenant asked) → (5) the
onboarding-wizard "pick your sources" step, now that the registry + panel
exist to make it meaningful.
