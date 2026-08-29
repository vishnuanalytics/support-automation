# Project Scope: AI Support Automation Platform (multi-tenant, LangGraph)

This is a handoff/scope document. Feed this to any coding model (Ox Alpha,
GLM-5.2, Claude, etc.) as system/project context before asking it to continue
the build — it should not need anything beyond this file plus the repo itself
to pick up where the last session left off.

## What this project is

A multi-tenant AI support automation platform: agents read incoming support
cases, draft replies from a knowledge base, and either auto-send, ask a human,
or fully hand over — gated by a confidence score that is stricter for
higher-value customers (basic / premium / enterprise). The end goal is a
no-code UI (React Flow) where users design their own agent flow — which
node does what, which thresholds apply, which Google Sheet/Slack/Docs source
feeds the knowledge base — without touching code.

This is portfolio work aimed at an AI/Platform engineering job transition, and
secondarily something that could be resold as a real product. Treat both
"impresses a technical interviewer" and "actually correct" as real
requirements, not just "makes a demo work."

Reference domain for the demo build: Zapier's public developer/support docs
(`docs.zapier.com`), modeled on a Zapier-style tiered support structure
(basic / premium / enterprise).

## Non-negotiable architectural decision

**The LangGraph agent must be built as data, not hardcoded Python.** Node
types, edges, and per-node config live in Supabase tables and are interpreted
into a real LangGraph `StateGraph` at runtime. This is what lets the eventual
UI (Phase 5) be additive — a canvas that reads/writes the same JSON/DB shape —
instead of a rewrite. Do not hardcode a `StateGraph` by hand for the "real"
flow; the hand-written example flow in Phase 0 exists only to validate the
schema, not as production logic.

## Tooling / cost constraints

- Prefer **free/local tooling** wherever it doesn't compromise the design:
  local embeddings, not a paid embedding API.
  Model: **`BAAI/bge-small-en-v1.5`** (384-dim, 512-token window). Changed
  from `all-MiniLM-L6-v2` on 2026-08-28 — MiniLM truncates input at ~256
  word-pieces, so the ~400-token chunks were half-embedded. Same 384-dim, so
  no schema change; reversible by swapping `EMBED_MODEL` in `scraper.py` and
  re-embedding.
- Embeddings run via **`fastembed`** (quantised ONNX, CPU-only, no torch) —
  swapped in from `sentence-transformers` on 2026-08-28 to keep an old
  laptop usable: ~15× smaller install, 2–4× faster on CPU. Same model, same
  384-dim, L2-normalised output; verified cosine ~1.0 vs the
  `sentence-transformers` build, so the swap needed **no re-embed and no
  migration** — the ~3.5k vectors already in `doc_chunks` stay valid.
- The recurring ingestion is meant to run on **GitHub Actions**
  (`.github/workflows/daily-sync.yml`), not a local machine — see cron note
  in Phase 1. Incremental runs only re-embed changed pages.
- For LLM calls in code (draft generation, classification), **default to
  Groq** (`llama-3.3-70b-versatile` or `llama-3.1-8b-instant`) over
  Anthropic/OpenAI APIs, unless a step specifically needs a capability Groq
  doesn't have.
- Supabase (Postgres + pgvector) for relational + vector data. Neo4j for
  graph relations between docs/entities that pgvector can't express well.
- No paid Salesforce/HubSpot tier — using a personal Salesforce Developer
  Edition org.

## Repository layout

Reorganised 2026-08-29 (Phase 5). Modules run from the repo root:

```
docs/            this file, SALESFORCE_SETUP.md
db/migrations/   001_*.sql .. 009_*.sql   (was repo root)
ingestion/       scraper.py, neo4j_sync.py, eval/         → python -m ingestion.scraper
interpreter/     builder/loader/registry/conditions/retrieval/llm/salesforce/run
  flows/         validate_flow.py, flow_support_example.json
  cases/         *.json sample cases
api/             FastAPI backend (Phase 5)                 → uvicorn api.main:app
web/             React + React Flow editor (Phase 5)       → cd web && npm run dev
scripts/         sf_create_fields.py, sf_seed_cases.py, rls_check.sql
tests/           test_interpreter.py (offline), test_multiflow.py (integration)
```

Import rule after the move: `ingestion` and `interpreter` are packages;
intra-repo imports use the package path (`from ingestion.scraper import …`,
`from interpreter.flows.validate_flow import …`). `.github/workflows/
daily-sync.yml` now calls `python -m ingestion.scraper` / `.neo4j_sync`.

## Phase plan and status

| Phase | Scope | Status |
|---|---|---|
| 0 | Flow-definition schema (`flows`/`flow_nodes`/`flow_edges`), RLS, tenant isolation | **Complete** — migrated, seeded, verified in Supabase |
| 1 | Zapier docs RAG ingestion: sitemap scrape → content-hash diff → chunk → embed → Supabase + Neo4j, daily via cron | **Complete (2026-08-28)** — `004`+`005` schema live. `scraper.py`: 401 docs / 3568 chunks / 920 links in Supabase, `fastembed` embeddings. `neo4j_sync.py` against Aura: 401 Doc + 25 stub nodes, 63 Section, 396 IN_SECTION / 56 SUBSECTION_OF / 920 LINKS_TO. Retrieval eval (`eval/`, 48 Q): dense baseline hit@3 1.00 / MRR@10 0.94. Committed + pushed (`6d56f42`); `.github/workflows/daily-sync.yml` cron **live and verified green** (run 33195499360 — remote incremental no-op: scrape `skipped: 401`, Neo4j idempotent). |
| 2 | Config-driven LangGraph interpreter: reads a flow row from Supabase, builds a real `StateGraph`, single hand-seeded flow | **Complete (2026-08-29)** — `interpreter/` package: `loader` (Supabase→dict + `validate_flow.check_flow` reuse), `builder` (dict→compiled `StateGraph`, conditional routing), `registry` (7 node handlers), `conditions` (safe AST eval of edge `if`), `retrieval` (hybrid dense+sparse→RRF→Neo4j expand→cross-encoder rerank), `llm` (Groq free-model roster + offline stub). Runs the Phase 0 seed flow end-to-end on all 3 sample cases → correct branch each (auto_reply / ask_human / handover). `006` (tenant_members RLS policy) + `007` (`match_doc_chunks` / `_fts` / `_hybrid` SQL fns) applied. 8/8 offline unit tests green. Eval: `run_eval.py --strategy all` — dense 0.944 / sparse 0.613 / hybrid 0.861 / hybrid+rerank 0.941 MRR@10 (dense at ceiling on this corpus; rerank matches it, degrades gracefully on harder ones — see `eval/README.md`). Optional follow-up: a real-Groq smoke run once a key is in `.env` (stub mode is by design). |
| 3 | Salesforce field write-back (case module/region/account/contact) from the classify node's output; Chatter-mention as the "ask human" mechanism | **Complete + live-verified (2026-08-29)** — `interpreter/salesforce.py`: 3 auth modes (JWT bearer / OAuth username-password / legacy SOAP), tried by which env vars are set; real when creds present, else dry-run. New `sf_writeback` node: config-driven `field_map` (`urgency`→`Priority` w/ value-map, `topic`→`Module__c`, `region`→`Region__c`, `summary` appended to `Description`), tolerant of missing fields. `ask_human` + `channel: salesforce_chatter` posts a real Chatter FeedItem (Connect API, FeedItem fallback). Migration `008` inserts `sf_writeback` (`classify → sf_writeback → draft`). `run.py --sf-case <Id>` pulls a live Case. `scripts/sf_create_fields.py` (Metadata API — creates `Case.Module__c` / `Case.Region__c` / `Account.Tier__c` + FLS) and `scripts/sf_seed_cases.py` (3 test Cases). 12/12 offline tests green. **Verified against a real Developer Edition org via JWT**: 3 seeded Cases ran end-to-end, 4/4 fields written each, Chatter FeedItem posted on the premium (ask_human) case. |
| 4 | Multi-tenant/multi-flow: prove several different flow configs run correctly | **Complete (2026-08-29)** — migration `009` seeds 3 published flows across 2 tenants: **Acme/support** (`1111…`, lenient per-tier gate, full SF map — now `published`), **Globex/support** (`a2a2…`, NEW — strict gate `{basic .9…enterprise .99}`, minimal SF map, no graph, 8B model; same team name as Acme, different tenant → allowed by `uq_one_published_flow_per_team`), **Acme/offboarding** (`c3c3…`, NEW — different topology `retrieve→classify→draft→handover`, no gate/sf_writeback). `loader.load_flow` now validates with `require_expected_types=False` (a CSM/offboarding flow needn't have a `confidence_gate`); `list_flows()` + `run.py --list` added. `tests/test_multiflow.py`: same `basic_howto.json` case through each flow → **auto_reply / ask_human / handover** — three behaviours, zero code differences. **RLS verified** (`scripts/rls_check.sql`) with simulated JWTs: Acme user sees 2 flows, Globex user sees 1 (+ only its 8 nodes), unknown user sees 0, service role sees 3. 13/13 offline tests green. |
| 5 | React Flow UI reading/writing the same flow schema — drag nodes, edit thresholds, toggle auto-send, pause per team/condition | **Complete (2026-08-29)** — repo reorganised (`db/migrations/`, `docs/`, `ingestion/`, `interpreter/flows/`+`cases/`, `api/`, `web/`; imports + CI updated; `README.md` + `.env.example` added). **`api/`** — thin FastAPI over `interpreter/`: `GET/POST /flows`, `GET/PUT/DELETE /flows/{id}`, `POST /flows/{id}/{validate,run}`, `GET /node-types`. Every request carries the caller's Supabase token; flow reads/writes go through an RLS-scoped client, service role only for the interpreter's own machinery. `loader.load_flow` gains `validate=False`. **`web/`** — Vite + React + `@xyflow/react` + Supabase Auth: flow list per tenant, dagre-laid-out canvas, node palette (per registered type), drag-connect / delete, inspector (label + `config` JSON + friendly per-tier threshold form for `confidence_gate`, edge `condition.if`), Validate (shows refs/orphan/cycle errors), Save (422 on invalid), draft⇄published toggle, and a Run panel (trace + outcome + retrieval). `npm run build` + `tsc` clean; API verified end-to-end (RLS list/get/create/save/invalid-422/validate/run/cross-tenant-404) against the live project. Phase 4's bare synthetic user replaced with a real GoTrue account (`globex-owner@example.test` / `57c26330…`). |
| 6 | Observability: manager reporting on low-confidence cases, per-case "why did the bot respond this way" chat, conflicting-SOP detection across teams | Not started |

## Phase 0 — schema (complete)

Tables (Supabase/Postgres):

- **`flows`**(flow_id uuid pk, tenant_id uuid, team text, name text, version
  int, status text[`draft`|`published`|`archived`], created_at, updated_at)
- **`flow_nodes`**(node_id uuid pk, flow_id fk, type text — free string, no
  fixed enum, label text, position_x/y int [for Phase 5 canvas, nullable
  until then], config jsonb)
- **`flow_edges`**(edge_id uuid pk, flow_id fk, source_node_id fk,
  target_node_id fk, condition jsonb — `{}` = unconditional)
- **`tenant_members`**(user_id uuid fk auth.users, tenant_id uuid, role text)
  — maps a Supabase auth user to a tenant for RLS purposes

Key design choices:
- Node `type` is a free string; behavior comes from `config` jsonb plus a
  registry in the Phase 2 interpreter mapping `type` → handler function.
  Adding a new node type later = new registry entry, no migration.
- Confidence thresholds are **per-node with per-tier overrides**, e.g.
  `{"default_threshold": 0.35, "tier_overrides": {"basic": 0.35, "premium":
  0.45, "enterprise": 0.6}}` — enterprise customers require a stricter
  confidence bar before anything auto-sends.
- RLS is enforced on all three flow tables via `tenant_members`; a unique
  partial index guarantees exactly one `published` flow per (tenant, team).
- Cycle detection and referential-integrity checks live in `validate_flow.py`
  (DFS-based) — reuse/extend this rather than writing a new validator.

Files already delivered: `001_flow_schema.sql`, `002_rls_and_constraints.sql`,
`003_seed_example_flow.sql`, `flow_support_example.json` (7-node/6-edge
Support-team reference flow), `validate_flow.py`.

## Phase 1 — Zapier docs RAG ingestion (complete, verified 2026-08-28)

Goal: scrape `docs.zapier.com`, detect new/changed/deleted pages daily, keep
Supabase (content + vectors) and Neo4j (relations) in sync.

Schema:
- `004_docs_ingestion_schema.sql` — **`zapier_docs`**(url pk, title,
  content_hash, raw_text [now stores Markdown], last_seen_at, last_changed_at,
  status[`active`|`stale`|`deleted`], missed_runs) and **`doc_chunks`**
  (chunk_id pk, doc_url fk, chunk_index, chunk_text, embedding vector(384)).
- `005_docs_rag_metadata.sql` — adds `doc_chunks.heading_path` /
  `chunk_type` / `token_count` / `section` / `fts` (generated tsvector +
  GIN, for the lexical half of hybrid retrieval); an HNSW index on
  `embedding`; the **`doc_links`**(source_url fk, target_url, anchor_text,
  first_seen_at, last_seen_at, missed_runs, status) table with the same
  soft-delete pattern; and explicit `select`-for-`authenticated` RLS
  policies on all three docs tables (they hold public Zapier docs, not
  tenant data — writes stay service-role only).
- Both applied and verified against project `mjohgmivnxfwkqmlojqs` on
  2026-08-28. (Applied via the SQL editor / MCP — `supabase_migrations` is
  empty, so there is no CLI migration baseline.)

Ingestion logic (`scraper.py`, rewritten 2026-08-28):
1. Pull `sitemap.xml` → `{url: lastmod}`.
2. Fetch `<url>.md` (docs.zapier.com is Mintlify — every page has a clean
   Markdown twin), but only when the URL is new or its `lastmod` is newer
   than the stored `last_changed_at`. HTML + `trafilatura` is the fallback.
3. Hash the normalised Markdown (SHA-256): new / changed → re-chunk +
   re-embed + re-capture links; unchanged → bump `last_seen_at`.
4. **Structure-aware chunking**: split on Markdown headings, keep fenced
   code and tables whole, prepend a `> {breadcrumb} — {H1 / H2 / H3}`
   context line to every chunk. ~400-token target. Embed with
   `bge-small-en-v1.5` via `fastembed` (quantised ONNX, CPU, no torch),
   L2-normalised.
5. Same-host links → `doc_links` (feeds Neo4j `LINKS_TO`).
6. Soft-delete unchanged: a URL or link missing from the sitemap/page gets
   `missed_runs += 1`, `deleted` after 3 misses.

Neo4j (`neo4j_sync.py`): reads `zapier_docs` + `doc_links` from Supabase and
builds, all keyed on `url` (same PK as Supabase, so the stores stay
joinable):
- `(:Doc {url, title, status, content_hash, last_changed_at, synced_at})`,
  soft-delete status mirrored.
- `(:Section {path})` from URL path prefixes; `(:Doc)-[:IN_SECTION]->`
  deepest section; `(:Section)-[:SUBSECTION_OF]->` parent.
- `(:Doc)-[:LINKS_TO]->(:Doc)` from `doc_links` (rebuilt each run;
  not-yet-ingested targets become stub Doc nodes).

**Done in Phase 1 (2026-08-28):**
- `.env` created (gitignored) with `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`.
- First live `scraper.py` run against `docs.zapier.com`: sitemap 401 URLs
  → 401 `zapier_docs` (all `active`), 3568 `doc_chunks` (0 null embeddings,
  avg ~297 tok, types prose/code/table), 920 `doc_links`. 0 failures.
- Embeddings swapped `sentence-transformers` → `fastembed` (see tooling
  note). No re-embed / migration.
- Neo4j: user provisioned an **Aura Free** instance (creds in `.env`;
  non-standard — username *and* DB name are the instance id, not `neo4j`).
  `neo4j_sync.py` patched to read `NEO4J_DATABASE` from env (was hardcoded
  `"neo4j"`) and to split the two-pattern `MATCH`es (cartesian-product
  notice). First run on 2026-08-28, idempotent on re-run: **401 Doc + 25
  stub** nodes, **63 Section**, **396 IN_SECTION / 56 SUBSECTION_OF / 920
  LINKS_TO**. (Most-linked target is `partner-solutions/workflow-api/intro`
  @52 — a stub; it's linked heavily but not in the sitemap.)
- `.github/workflows/daily-sync.yml` — scrape + Neo4j steps both wired;
  needs 6 repo secrets (SUPABASE_URL/SERVICE_KEY, NEO4J_URI/USERNAME/
  PASSWORD/DATABASE). User reports the secrets are added.
- Retrieval eval set: `eval/qrels.jsonl` (48 hand-written questions → gold
  doc URLs, spanning every section) + `eval/run_eval.py` (dense pgvector
  ranking in numpy, no DB function needed). **Dense-only baseline
  (2026-08-28):** hit@1 0.896, hit@3 1.000, hit@5 1.000, MRR@10 0.944.
  Sparse / RRF / graph / rerank strategies are Phase 2 — `run_eval.py` has
  the extension point noted.

**Phase 1 closed.** Committed `6d56f42`, pushed to `main`. The
`Daily docs sync` workflow (id 344797351) is active — daily `0 3 * * *`
plus manual dispatch — and its first cloud run (33195499360) went green
after a corrected `NEO4J_PASSWORD` secret: scrape `skipped: 401`
(incremental no-op), Neo4j re-synced idempotently. Six repo secrets set:
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `NEO4J_URI`, `NEO4J_USERNAME`,
`NEO4J_PASSWORD`, `NEO4J_DATABASE`.

Known minor debt (not blocking):
- `scraper.py` builds the `.md` URL as `url.rstrip("/") + ".md"`, which for
  the bare `https://docs.zapier.com` root resolves the host
  `docs.zapier.com.md` (one warning per run, recovers via HTML fallback).
- Neo4j has 25 stub Doc nodes (link targets outside the sitemap, e.g.
  `partner-solutions/workflow-api/intro` @52 inbound links). Expected;
  they fill in if those URLs ever enter the sitemap.
- `.mcp.json` hardcodes `NEO4J_DATABASE: "neo4j"` (wrong for this Aura
  instance) and the `neo4j` MCP server needs `uvx` on PATH — the local MCP
  server doesn't connect. Doesn't affect the pipeline.

Files delivered: `004_docs_ingestion_schema.sql`, `005_docs_rag_metadata.sql`,
`scraper.py`, `neo4j_sync.py`, `.mcp.json` (Neo4j Cypher MCP config),
`requirements.txt`, `.github/workflows/daily-sync.yml`, `.env` (gitignored),
`eval/qrels.jsonl`, `eval/run_eval.py`, `eval/README.md`.

## Working conventions for whichever model picks this up

- Don't build ahead into later phases prematurely (there's a `CLAUDE.md`
  in the repo with this guardrail already — read it).
- When adding a migration, number it sequentially (`005_...sql`, etc.) and
  keep each migration scoped to one concern, matching the existing
  001–004 pattern.
- Any new node type added to a flow must be reflected in
  `validate_flow.py`'s `EXPECTED_TYPES` set if it should be considered a
  "complete" flow, and the interpreter's type registry (once Phase 2
  exists).
- Multi-tenancy is not optional or deferred — every new table that holds
  tenant-scoped data needs RLS via `tenant_members`, following the pattern
  in `002_rls_and_constraints.sql`.
- Prefer soft-delete/status-flag patterns (as in `zapier_docs.status`) over
  hard deletes for anything ingested from an external source, since
  external sources can have transient failures.

## Immediate next step

**Phases 0–5 are complete.** Migrations through `009` applied to
`mjohgmivnxfwkqmlojqs`. 13/13 offline tests + `tests/test_multiflow.py`
green. External calls (Groq, Salesforce) run real when creds are in `.env`,
else deterministic dry-run.

Run the whole thing:
```
python -m interpreter.run --list                     # 3 flows / 2 tenants
python -m tests.test_multiflow                        # same case -> 3 outcomes
uvicorn api.main:app --reload                         # editor backend :8000
cd web && npm install && npm run dev                  # editor :5173
```
Editor login: a Supabase account. `gundamvishnu7@gmail.com` → tenant Acme;
`globex-owner@example.test` (pw `editor-test-pw-8891`) → tenant Globex.

**Next: Phase 6 — observability.** Nothing new is built yet. Planned:
- a `runs` table (persist each `interpreter.run` — flow_id, case ref, final
  `trace`, outcome, confidence) so runs are queryable, not just logged.
- manager view: low-confidence / ask_human / handover cases over time.
- per-case "why did the bot do this" — render the stored `trace` (each node
  already emits `{summary, data}`), plus the retrieved chunks + gate math.
- conflicting-SOP detection: flag docs that different teams' flows retrieve
  with contradictory guidance (needs an LLM-judge pass over `doc_chunks`).

Default to Groq for any LLM calls (classification, draft generation).

## Known issues / debt

- ~~`tenant_members` RLS enabled with no policy~~ — **fixed** in `006`
  (`self_membership_read`) and **verified in Phase 4** (`scripts/rls_check.sql`):
  simulated JWTs see only their tenant's flows. The interpreter itself still
  runs as service-role (`scraper.get_supabase`); a real auth'd client is a
  Phase 5 concern.
- Phase 4 added a **synthetic auth user** `57c26330-cb98-475a-875f-8f8a925672fd`
  (`globex-owner@example.test`) directly in `auth.users` via the SQL editor,
  so `009` could seed its `tenant_members` row (FK to `auth.users`). Not a
  real login. Same out-of-band pattern as `4ddf2413` (created via signup).
- `009` seeds `Account.Tier__c` values as `basic/premium/enterprise`; the
  Globex flow's `sf_writeback` only maps `Priority` + `Description` (no
  custom fields) — a deliberate "different tenant, different SF schema"
  contrast, not a bug.
- `vector` extension lives in `public` (advisor `extension_in_public`).
  Cosmetic for now; move to an `extensions` schema if it's ever a concern.
- **Local dev env:** this box has no `python3.12-venv` package and system
  Python is PEP-668 externally-managed. `venv/` was bootstrapped with
  `python -m venv --system-site-packages --without-pip` + `get-pip.py`
  (Phase 1 deps resolve from `~/.local`; `venv/` adds `langgraph`, `groq`).
  If `venv/` is ever rebuilt, do the same, or `apt install python3.12-venv`
  first. GitHub Actions is unaffected (fresh `pip install -r
  requirements.txt`).
- Remote migration history also has a `007b_retrieval_functions_search_path`
  row — a hotfix already folded into `007_retrieval_functions.sql` (the repo
  file is canonical). `001`–`004` were never recorded in
  `supabase_migrations` (applied via SQL editor pre-CLI-baseline); `005`
  onward are.
- `classify` reads `tier` straight from the case's `account.customer_type`
  (mapped via `_TIER_ALIASES`); the LLM only fills `topic`/`urgency`/
  `summary`. From Salesforce (`--sf-case`) that value is `Account.Tier__c`
  if the org has it, else the standard `Account.Type` picklist — whose
  values aren't `basic/premium/enterprise`, so tier falls back to `basic`.
  Add a `Tier__c` picklist on Account for a faithful demo (`SALESFORCE_SETUP.md`).
- Phase 3 SF integration is **live-verified** against a real Developer
  Edition org ("speed", `orgfarm-8f5f468eb6-dev-ed`) via the **JWT bearer
  flow**. That org has SOAP login *and* the OAuth username-password flow
  disabled (both default-off on Agentforce/trial orgs), so JWT is the only
  path that works — keypair in `sf_jwt/` (gitignored), cert uploaded to the
  Connected App, user profile pre-authorized. `.env` has `SF_USERNAME` /
  `SF_CONSUMER_KEY` / `SF_PRIVATE_KEY_FILE` / `SF_DOMAIN`.
- Chatter @mention uses the Connect API with a plain-`FeedItem` fallback.
  In the live run the mention resolved to `None` (fell back to a plain
  post); set `ask_human.config.mention_id` to a real User/Group Id for an
  actual @mention.
- `sf_writeback` appends the `[triage] …` block to `Description` every run,
  so re-running the same Case grows the field. Fine for the demo; a real
  build would use a dedicated field or a dedupe marker.
- Seeded `region` is a country ("United States" / "United Kingdom") not a
  region code — the org has State & Country picklists, so `BillingCountry`
  must be a real country; `get_case` reads it straight back into
  `account.region` and thus `Case.Region__c`. Map country→region in
  `get_case` if AMER/EMEA semantics are wanted.
- `Account.Tier__c` is a `Text(40)` custom field created by
  `scripts/sf_create_fields.py`; `get_case` prefers it over the standard
  `Account.Type` picklist for `classify`'s tier.
