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
  local embeddings via `sentence-transformers` (`all-MiniLM-L6-v2`, 384-dim),
  not a paid embedding API.
- For LLM calls in code (draft generation, classification), **default to
  Groq** (`llama-3.3-70b-versatile` or `llama-3.1-8b-instant`) over
  Anthropic/OpenAI APIs, unless a step specifically needs a capability Groq
  doesn't have.
- Supabase (Postgres + pgvector) for relational + vector data. Neo4j for
  graph relations between docs/entities that pgvector can't express well.
- No paid Salesforce/HubSpot tier — using a personal Salesforce Developer
  Edition org.

## Phase plan and status

| Phase | Scope | Status |
|---|---|---|
| 0 | Flow-definition schema (`flows`/`flow_nodes`/`flow_edges`), RLS, tenant isolation | **Complete** — migrated, seeded, verified in Supabase |
| 1 | Zapier docs RAG ingestion: sitemap scrape → content-hash diff → chunk → embed → Supabase + Neo4j, daily via cron | **In progress** — scraper + schema written, not yet run against live Supabase; Neo4j sync not yet written |
| 2 | Config-driven LangGraph interpreter: reads a flow row from Supabase, builds a real `StateGraph`, single hand-seeded flow | Not started |
| 3 | Salesforce field write-back (case module/region/account/contact) from the classify node's output; Chatter-mention as the "ask human" mechanism | Not started |
| 4 | Multi-tenant/multi-flow: prove several different flow configs run correctly | Not started (schema already supports it — `tenant_id`/`flow_id` built in from Phase 0) |
| 5 | React Flow UI reading/writing the same flow schema — drag nodes, edit thresholds, toggle auto-send, pause per team/condition | Not started |
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

## Phase 1 — Zapier docs RAG ingestion (in progress)

Goal: scrape `docs.zapier.com`, detect new/changed/deleted pages daily, keep
Supabase (content + vectors) and Neo4j (relations) in sync.

Tables (separate migration, `004_docs_ingestion_schema.sql`):
- **`zapier_docs`**(url pk, title, content_hash, raw_text, last_seen_at,
  last_changed_at, status[`active`|`stale`|`deleted`], missed_runs int)
- **`doc_chunks`**(chunk_id pk, doc_url fk, chunk_index, chunk_text,
  embedding vector(384))

Diff logic (already written in `scraper.py`):
1. Pull `https://docs.zapier.com/sitemap.xml` for the current URL list.
2. Per URL: fetch, strip nav/script/style, hash the cleaned text
   (SHA-256). New hash vs. stored hash decides insert / re-embed / no-op.
3. A URL that's in the DB but missing from today's sitemap is **not**
   deleted immediately — `missed_runs` increments, and only after 3
   consecutive misses does it flip to `status = 'deleted'`. This protects
   against a transient scrape/sitemap failure wiping content.
4. Chunking is naive fixed-size with overlap (1200 chars, 150 overlap) —
   fine for a PoC, revisit if retrieval quality suffers.

**Not yet done in Phase 1:**
- Running `004_docs_ingestion_schema.sql` against the live Supabase project.
- Running `scraper.py` end-to-end for the first time (untested against the
  live site — network access wasn't available to test this from the
  design session).
- Scheduling it via cron for actual daily execution.
- **Neo4j sync is not written yet.** Needs: a node per doc, edges for
  doc→doc relations (e.g. hyperlinks within a doc's content, shared
  category/breadcrumb), keyed on the same `url` so Supabase and Neo4j stay
  joinable. This is the next concrete piece of Phase 1 work.

Files already delivered: `004_docs_ingestion_schema.sql`, `scraper.py`,
`requirements.txt`.

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

Finish Phase 1: run `004_docs_ingestion_schema.sql`, run `scraper.py`
against the live Supabase project and fix whatever breaks (it hasn't been
tested against the real site yet), then write the Neo4j sync piece, then set
up the cron schedule. After that, Phase 2 (the LangGraph interpreter) is
next.
