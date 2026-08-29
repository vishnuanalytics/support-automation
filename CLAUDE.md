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

This project is being built in explicit, sequenced phases (see
`PROJECT_SCOPE.md` for the current phase table). **Do not jump ahead into
later-phase work while an earlier phase is still open** — specifically:

- Do not start on the rule engine / LangGraph interpreter (Phase 2) while
  Phase 1 (Zapier docs ingestion) isn't finished and verified.
- Do not start on the visual/no-code workflow designer (Phase 5, React
  Flow UI) under any circumstances until Phases 0–4 are complete and
  verified. This is the most common way this kind of project scope-creeps
  into an unfinished mess — resist it even if it looks like "just a quick
  prototype."
- If a task naturally surfaces something that belongs to a later phase,
  note it in your response and stop there — do not implement it
  preemptively.

## Before making changes

1. Read `PROJECT_SCOPE.md` in full.
2. State your understanding of the current phase and what's already
   done, and wait for confirmation before proceeding, if this is the
   start of a new session.
3. Check whether a migration, schema change, or file you're about to
   create already exists — don't duplicate `001`–`004` style migrations.

## Working conventions

- **Migrations are sequential and single-concern.** New migration =
  next number (`005_...sql`, etc.), scoped to one thing, matching the
  pattern of `db/migrations/001_flow_schema.sql` through `004_docs_ingestion_schema.sql`.
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
  embedding API. For LLM calls in
  code, default to Groq (`llama-3.3-70b-versatile` or
  `llama-3.1-8b-instant`) over Anthropic/OpenAI APIs unless a step
  specifically needs something Groq can't do.
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
