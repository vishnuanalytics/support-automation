# CLAUDE.md

Instructions for Claude Code working in this repository. Read this
alongside `docs/PROJECT_SCOPE.md` before making changes —
`docs/PROJECT_SCOPE.md` has the full architecture and phase history; this
file is the operating rules for how to work in it.

## Repository layout

```
docs/            PROJECT_SCOPE.md (the real memory), SALESFORCE_SETUP.md
db/migrations/   001_*.sql .. 010_*.sql   (sequential, single-concern)
ingestion/       Phase 1 — scraper.py, neo4j_sync.py, eval/
interpreter/     Phase 2-4 — the config-driven LangGraph interpreter
  flows/         validate_flow.py, flow_support_example.json
  cases/         sample support cases
api/             Phase 5 — FastAPI backend (reuses interpreter/)
web/             Phase 5 — React Flow editor
scripts/         one-off ops helpers (SF field setup, seed data, RLS check)
tests/           test_interpreter.py (offline), test_multiflow.py (integration)
```
Run modules from the repo root: `python -m ingestion.scraper`,
`python -m interpreter.run …`, `python -m ingestion.eval.run_eval`.

## Don't build ahead

This project is built in explicit, sequenced phases (see
`docs/PROJECT_SCOPE.md` for the phase table). **All phases 0–16 are
built** (0–6 the MVP; 7–13 the hardening roadmap; 14 self-serve internal
KB; 15 Google Docs connector; 16 structured policy rules + Slack-approved
GitHub actions). No open phase.

- Remaining work is **live verification of the external connectors**
  (need creds: Phase 15 `GOOGLE_CLIENT_ID`/`SECRET`; Phase 16 a Slack app
  + `GITHUB_TOKEN`) and polish (Phase 16 rule form builder; Phase 14 file
  upload). See "Immediate next step" in `PROJECT_SCOPE.md`.
- The hardening roadmap (7–13) was built in order **7 → 9 → 8 → 10 → 11 →
  12 → 13**; 14 → 15 → 16 followed. New work still lands one verifiable
  chunk at a time.
- Don't expand scope mid-phase. Each roadmap phase is a small, verifiable
  chunk — land it and its verification before moving on.
- If a task surfaces something that belongs to a later phase, note it in
  your response and stop there — don't implement it preemptively.

## Before making changes

1. Read `docs/PROJECT_SCOPE.md` in full.
2. State your understanding of the current phase and what's already
   done, and wait for confirmation before proceeding, if this is the
   start of a new session.
3. Check whether a migration, schema change, or file you're about to
   create already exists — don't duplicate `001`–`004` style migrations.

## Working conventions

- **Migrations are sequential and single-concern.** New migration =
  next number (`005_...sql`, etc.), scoped to one thing, matching the
  pattern of `db/migrations/001_flow_schema.sql` through `004_docs_ingestion_schema.sql`.
- **Migrations are applied by hand only** — via the Supabase MCP
  `apply_migration` (or the SQL editor), never the Supabase CLI. The
  `supabase_migrations` history table is **not** kept in sync with
  `db/migrations/*.sql`; the `.sql` files are the source of truth. Don't run
  `supabase db push` / `supabase migration` against this project.
- **Multi-tenancy is not optional.** Any new table holding tenant-scoped
  data needs RLS via `tenant_members`, following
  `db/migrations/002_rls_and_constraints.sql`'s pattern. Don't defer this "for later."
- **Node types in the flow schema are generic strings, not an enum.**
  Behavior comes from `config` jsonb plus a type-registry lookup in the
  interpreter (once it exists). Don't add a fixed `CHECK` constraint or
  enum type restricting `flow_nodes.type`.
- **Prefer soft-delete over hard-delete** for anything ingested from an
  external source (see `zapier_docs.status` / `missed_runs` pattern) —
  external sources can fail transiently; don't let one bad scrape wipe
  content.
- **Free/local tooling by default.** Local embeddings via `fastembed`
  (quantised ONNX `bge-small-en-v1.5`, CPU-only, no torch), not a paid
  embedding API. For LLM calls in code the **default provider is Groq**
  (`openai/gpt-oss-120b` for `draft`, `openai/gpt-oss-20b` for
  `classify` / judges — the `llama-3.x` names Groq retired in 2026 are
  kept in the roster only so old flow configs don't `KeyError`).
  `interpreter/llm.py`
  also supports **Anthropic** (`claude-opus-5` / `claude-sonnet-5` /
  `claude-haiku-4-5`) as an opt-in — routed by the model id in a node's
  `config.model`, or flipped wholesale with `LLM_DEFAULT_MODEL` /
  `LLM_FAST_MODEL` in `.env`. Don't change the seed flows' models or the
  Groq default without a reason.
- **Validate before you trust a flow JSON.** Reuse/extend
  `interpreter/flows/validate_flow.py` (referential integrity + cycle
  detection) rather than writing a second validator.

## Keep PROJECT_SCOPE.md current — this is the project's real memory

Chat sessions and even this file can be lost between environments (this
has already happened once). `PROJECT_SCOPE.md`'s phase status table is
the single source of truth for what's actually done. Every time you
finish a meaningful chunk of work:

1. Update the relevant row in `PROJECT_SCOPE.md`'s phase table (status,
   what's done, what's still open).
2. Add or update the "Immediate next step" section at the bottom so a
   completely fresh session (new machine, new chat, no memory of this
   one) can read the file and know exactly where to resume.
3. Do this as part of the same commit as the code change, not as an
   afterthought — treat an out-of-date `PROJECT_SCOPE.md` as a bug.

## When you're unsure

If a decision isn't covered in `PROJECT_SCOPE.md` or here, don't
silently pick an approach and run with it across multiple files — flag
the open question and propose one option, then wait rather than
committing the whole codebase to an assumption.
