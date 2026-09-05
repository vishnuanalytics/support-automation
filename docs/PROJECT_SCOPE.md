# Project Scope: AI Support Automation Platform (multi-tenant, LangGraph)

This is a handoff/scope document. Feed this to any coding model (Ox Alpha,
GLM-5.2, Claude, etc.) as system/project context before asking it to continue
the build — it should not need anything beyond this file plus the repo itself
to pick up where the last session left off.

**`docs/REQUIREMENTS.md` is the spec** (numbered functional / non-functional
requirements, constraints, open decisions, acceptance scenarios). This file
is the build log — phase status and history. New requirement → write it in
`REQUIREMENTS.md` first, then build and record progress here.

## What this project is

**Primary objective (stated by the project owner, 2026-08-30): have AI
handle Salesforce support Cases end to end.** An inbound support request
becomes — or attaches to — a real Salesforce Case; the config-driven
LangGraph flow triages it, drafts a reply from the knowledge base, and
then auto-responds, asks a human, or fully hands over — always writing the
outcome back onto the Case (fields via `sf_writeback`, human requests as
Chatter on the Case, resolution feedback via the Phase 11 loop). Every
channel (web form, email — Phase 20, Slack, …) is just an input path into
that Salesforce-Case pipeline. Salesforce is the source of truth.

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
  Groq** (`openai/gpt-oss-120b` for drafting, `openai/gpt-oss-20b` for
  classification / judges) over Anthropic/OpenAI APIs, unless a step
  specifically needs a capability Groq doesn't have. Groq retired the
  `llama-3.x` names in 2026; migration `017_llm_model_ids.sql` repointed
  the seeded `draft` nodes and re-snapshotted the published versions.
- Supabase (Postgres + pgvector) for relational + vector data. Neo4j for
  graph relations between docs/entities that pgvector can't express well.
- No paid Salesforce/HubSpot tier — using a personal Salesforce Developer
  Edition org.

## Repository layout

Reorganised 2026-08-29 (Phase 5). Modules run from the repo root:

```
docs/            this file, SALESFORCE_SETUP.md
db/migrations/   001_*.sql .. 016_*.sql   (was repo root)
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
| 6 | Observability: manager reporting on low-confidence cases, per-case "why did the bot respond this way" chat, conflicting-SOP detection across teams | **Complete (2026-08-29)** — migration `010` adds a **`runs`** table (flow_id, tenant, team, source, tier/outcome/confidence, `gate`/`trace`/`retrieval`/`sf_writeback`/`case_payload` jsonb), tenant-scoped RLS like the flow tables. `interpreter/runs.py` `record_run()` (best-effort — never breaks the run; `RUNS_DISABLED=1` to skip) wired into `interpreter.run` (`--no-record` to opt out) and `POST /flows/{id}/run` (returns `run_id`). API: `GET /runs`, `GET /runs/{id}`, `GET /runs/stats`. Web: a **Runs** tab — stat tiles (per outcome / per tier / low-confidence), filterable table, and a per-run detail showing the trace steps + gate math + retrieved docs = the "why". `scripts/sop_conflicts.py` probes each team's `retrieve` config across a fixed topic set and flags where teams surface different top docs (Groq-judged for actual contradiction when a key is present, reported unjudged otherwise). 14/14 offline tests green; runs API verified end-to-end (record from CLI + API, list/stats/detail, RLS-scoped). |

**Phases 0–6 = the MVP (complete). Phases 7–13 = hardening — the roadmap
from the 2026-08-29 self-review; still sequential (do 7 before 10, etc.).**

| Phase | Scope | Status |
|---|---|---|
| 7 | **Evaluation & calibration.** End-to-end action eval; calibrate the gate; report *auto-send / escalation precision*; draft groundedness check; harder qrels; latency+token accounting; fix fail-open tier. | **Complete (2026-08-29)** — `eval/e2e/` (22 hand-labelled cases → gold action) runs the real pipeline and reports auto-send / escalation precision + a threshold sweep. Baseline: acc 0.864, auto-send P **0.769** (3/13 unsafe), escalation P 1.00. `interpreter/groundedness.py` (Groq judge / lexical fallback) → `state["groundedness"]`; `confidence_gate` gains `groundedness_weight` (default 0 = unchanged). `builder._make_node` stamps `elapsed_ms`; `llm.last_usage` + handlers record `tokens`. `registry._norm_tier` unknown → **`enterprise`** (strictest) + warn, was `basic`. **`011_calibrate_gate.sql`**: Acme gate → threshold 0.5 / per-tier {.5,.55,.6} / groundedness_weight 0.2 → **acc 0.909, auto-send P 0.833, escalation P 1.00** (2 residual — SOC2 / Partner-API — need an intent edge, noted). `qrels_hard.jsonl` (10 Q) + `run_eval.py --qrels hard`. 24 offline pytest tests; `test_multiflow` green with the new gate. Migration applied via SQL editor (MCP `apply_migration` timed out); `011_*.sql` is canonical. **Note (2026-08-29):** the 0.909 figure was with the deterministic stub; a re-run with real Groq drafts gave acc **0.636** / auto-send P **0.556** (over-confident `draft_confidence`). **`019_recalibrate_gate.sql`** fixes it — the Acme gate now uses an explicit blend `weights={retrieval .55, draft .1, groundedness .35}` + `escalate_topics` (billing/refund/pricing/legal/account-access/data-export/partner-api/cancellation intents → forced `ask_human`, matched on slug tokens by `registry._slug_tokens`). Real-Groq e2e: **acc 1.000, auto-send P 1.000 (10/10), escalation P 1.000 (12/12)**, coverage 0.455 (= the 10/22 ceiling; 8 gold `ask_human` + 4 gold `handover`). `escalate_topics` is the static precursor to Phase 16's rule engine; `run_e2e.py`'s threshold sweep is now stale (legacy 1-D blend). **`020`** repoints the Globex `classify` node off the retired `llama-3.1-8b-instant` (017 missed non-draft nodes). **`021`** applies the same recalibration to the Globex "human-review-first" gate — non-enterprise tiers now always route to a human (thresholds >1.0), which real Groq drafts had started defeating (caught by `test_multiflow`). 34 offline + `test_multiflow` (4/4 real-Groq) green. |
| 8 | **Flow versioning & safe writes.** Immutable versions, `runs.flow_version`, transactional save, optimistic concurrency. | **Complete (2026-08-29)** — migration `012`: **`flow_versions`** (immutable `nodes`/`edges`/`name`/`definition_hash`/`created_by` snapshot, RLS like the flow tables), `flows.published_version`, `runs.flow_version`; **`replace_flow_graph(flow_id, nodes, edges)`** plpgsql fn — one transactional delete+insert; backfilled v1 for the 3 published flows. `flow_nodes`/`flow_edges` stay the editable **draft**; a **run executes the published snapshot** (`loader.load_flow` reads `flow_versions` for `status="published"`, `flow_nodes/edges` for `status="draft"`) and records `flow_version`. `interpreter/loader.definition_hash()` (order-independent sha256). API: `PUT` → `replace_flow_graph` RPC + **409** if `body.version` ≠ current (bumped every save); `POST /flows/{id}/publish` (snapshot → version), `/rollback` (restore draft + re-publish), `GET /versions`. Web: `published vN` pill, `draft rev` token, **Publish** button, rollback `<select>`, 409 → auto-reload banner. Full lifecycle verified (create→PUT→stale-409→publish→edit→publish v2→run records v2→rollback restores draft). 24 offline + 7 integration pytest green; web `tsc`+vitest+build green. |
| 9 | **Test coverage & CI.** pytest, `test_api.py`, web smoke tests, de-brittle `test_multiflow`, `ci.yml`. | **Complete (2026-08-29)** — `pytest` (`pytest.ini` with an `integration` marker; repo-root `conftest.py` for `sys.path`). **`tests/test_api.py`** — offline: `/health`, `/node-types`, 401 without a bearer token, `_structural_errors` (dangling edge / unknown type / cycle / clean). integration (real Globex token, skipped without `SUPABASE_ANON_KEY`): RLS-scoped list, cross-tenant 404, PUT→422, run→`run_id`. `test_multiflow` rewritten as pytest + de-brittled — asserts a **structural fact per flow** (gate present/absent → routing) + the **cross-tenant invariant** (`a.action != b.action`, `a.threshold < b.threshold`), not exact score-paths. **`web`**: `vitest` on `graph.ts` (RF round-trip, dagre layout, conditional-edge mapping, uuid) — 5 tests. **`.github/workflows/ci.yml`** — job `python`: `pytest -m "not integration"` (**24 pass**, 0.9s); job `web`: `tsc -b` + `vitest run` + `vite build`. On push + PR to `main`. `pytest` + `httpx` added to `requirements.txt`. |
| 10 | **Event-driven pipeline.** SF trigger → task queue → async run → idempotency. | **Complete (2026-08-29)** — migration `013`: **`jobs`** table + **`claim_job()`** (`FOR UPDATE SKIP LOCKED`), a partial-unique `(kind, dedupe_key)` so a redelivered Case never double-enqueues; `runs.idempotency_key` + unique `(flow_id, idempotency_key)`. **`api/worker.py`** — `python -m api.worker [--once]`, dispatches `run_flow` (loads the published snapshot, invokes, records `source="worker"`); a run already recorded for `(flow_id, key)` is a no-op success. `interpreter/jobs.py` (enqueue / claim / complete / fail-with-retry). API: `POST /flows/{id}/enqueue` → `202 {job_id}`, `GET /jobs/{id}`; `POST /run` honours an `Idempotency-Key` header (returns the prior `run_id`). **`ingestion/sf_case_watch.py`** — polls `Case WHERE Status='New' AND LastModifiedDate >= now-Nmin`, enqueues one job per Case keyed on the Case Id (lookback can overlap; job dedupe handles it); `--once` for cron. Kept `POST /run` **synchronous** for the editor. 14 integration tests (incl. `test_queue.py`: dedupe, worker executes + records, no double-run). Persistent worker / trigger cron aren't deployed here — code + `--once` verified. |
| 11 | **Human-in-the-loop feedback.** Capture the human's resolution after ask_human/handover; feed accepted drafts to the eval golden set. | **Complete (2026-08-29)** — migration `014`: `runs` += `draft`, `human_action` (`pending\|sent_as_is\|edited\|rewrote\|no_reply`), `human_reply`, `edit_distance`, `feedback_checked_at`. A run that goes to a human on a real Case is stamped `pending` and `record_run` schedules a delayed **`check_resolution`** job (`FEEDBACK_DELAY_MIN`, default 20). `interpreter/feedback.py`: `fetch_human_reply` (latest outbound `EmailMessage`, else `CaseComment`) + `classify_edit` (`SequenceMatcher` ratio → bucket + `edit_distance`). Worker handles `check_resolution`. API `/runs/stats` gains `draft_acceptance` + `by_human_action`; `/runs` + detail carry `human_action`/`edit_distance`/`human_reply`. Web Runs view: "draft kept %" + "awaiting human" tiles, a `human` column, and a bot-draft-vs-sent diff in the detail. `scripts/harvest_feedback.py` dumps kept-draft runs as candidate `eval/e2e/` cases. **Verified live**: seeded Case → handover (`pending`) → posted an outbound EmailMessage → `check_resolution` scored the run (`edited`, `edit_distance`, `feedback_checked_at`). 26 offline + 15 integration pytest green. |
| 12 | **Real multi-tenancy.** `sources` + per-source ingestion; `tenant_integrations`; a real 2nd KB source; write-path scoping. | **Complete (2026-08-29)** — migration `015`: **`sources`** (tenant_id NULL = shared), `doc_chunks`/`zapier_docs` gain `source_id` (backfilled to a shared `zapier-public` source), the 3 `match_doc_chunks*` fns gain an optional `p_source_ids uuid[]` filter (old signatures dropped to avoid overload ambiguity). **`tenant_integrations`** (per-tenant SF/Slack creds). `interpreter/retrieval.resolve_sources(names, sb, tenant_id)` — a flow only ever reaches **shared + its own tenant's** sources; naming another tenant's source falls back to its legitimate scope (**no cross-tenant KB leak** — verified). `retrieve` node `config.kb_sources`; `hybrid_retrieve(kb_sources, tenant_id)`; `state.tenant_id` threaded from the flow at `invoke`. `interpreter/salesforce.client_for(tenant_id)` resolves creds from `tenant_integrations`, else env. **`ingestion/sources/markdown_source.py`** + a real **`globex-sop`** source (4 SOP docs / 8 chunks, tenant Globex) with *deliberately different* guidance (webhooks Business-only, annual+true-up billing, no self-serve export) — migration `016` points the Globex flow's retrieve at `["globex-sop", "zapier-public"]`. `scripts/sop_conflicts.py` now probes per (tenant, team). 27 offline + 15 integration pytest green. |
| 13 | **Security & hardening.** Verify tokens, rate-limit, `/security-review`, `sop_conflicts` fires. | **Complete (2026-08-29)** — `api`'s `caller` now **verifies** the bearer token via `GET {SUPABASE_URL}/auth/v1/user` (authoritative signature/expiry/revocation check, 60 s cache) instead of a base64 decode of the payload; a tampered token → 401 (tested). Per-user in-process `rate_limit()` — `/run` 20/min, `/enqueue` 120/min → 429. `/security-review` run over the API+web diff → **no HIGH/MEDIUM findings** (the change is a net auth improvement). `scripts/sop_conflicts.py` already **fires** since Phase 12 (5 real Globex-SOP-vs-public divergences). 28 offline + 17 integration pytest green. *Residual:* rate-limit state is per-process (fine for one uvicorn worker; needs Redis behind a load balancer); token cache honours a revoked session for ≤60 s. |

**Phases 14–16 = self-serve knowledge & internal actions (added 2026-08-29
from a scoping conversation). Sequential: 14 → 15 → 16. Phase 14 is
BUILT; 15 and 16 are planned. Do not start 16 before its `policy_gate`
groundwork — the Phase 7 recalibration — which is done (`019`).**

| Phase | Scope | Status |
|---|---|---|
| 14 | **Internal knowledge base (unstructured), self-serve.** Per-team named collections; entries authored in-app (markdown editor); chunked + embedded locally, scoped to the tenant. New `kb_lookup` node the flow author drops at a checkpoint — consulted **only when the run reaches it**, feeds `draft` as authoritative context above the public docs. | **Built (2026-08-29)** — migrations `022` (`kb_entries` + `sources` internal_kb RLS) + `023` (a `kb_lookup` checkpoint on the Globex flow's billing branch). `ingestion/sources/kb_common.embed_entry` (shared with `markdown_source`). `registry.h_kb_lookup` (templated `{{state.path}}` query, tenant-scoped collections, `use_graph=False` → `state.internal_kb`); `h_draft` folds an internal hit in above the public docs + counts it toward groundedness. API: `/api/kb/collections` + `/entries` CRUD (RLS-scoped, `kb_write` rate-limit, inline embed ≤8 KB else an `embed_kb_entry` job); `api/worker` handles it. Web: a **Knowledge** tab (collections → entries → markdown editor) + a `kb_lookup` Inspector form (collection multi-select + `top_k` + query). `scripts/seed_kb_demo.py` seeds `globex-billing-runbook`. 38 offline + KB integration tests green; Globex flow recompiles with the checkpoint. File upload (`.pdf`/`.docx` via `pypdf`/`python-docx`) is a noted follow-on; end-to-end billing-branch run pending a Groq daily-quota reset. |
| 15 | **Google Drive / Docs connector.** Per-tenant Google OAuth; link a Google Doc into a KB collection; a scheduled job re-exports + re-embeds on `modifiedTime` change. | **Built + live-verified (2026-08-29)** — OAuth → Doc export → markdown → chunk → embed, tenant-scoped (a linked doc landed 2 chunks in `doc_chunks`). A linked doc is a `kb_entries` row with `origin='gdoc'` **inside an `internal_kb` collection** (deviation from the "distinct `gdoc` source kind" sketch — keeps one retrieval path, and `kb_lookup` picks up manual + synced entries together). Migration `024` adds `origin`/`gdoc_id`/`gdoc_url`/`gdoc_modified`/`synced_at`/`sync_error` to `kb_entries`. `interpreter/gdrive.py`: OAuth (`authorize_url`/`exchange_code`, offline access), `fetch_doc` (Drive `files.get` + Docs `documents.get`), and a pure `docs_json_to_markdown` (headings/bullets/tables — unit-tested). Refresh token in `tenant_integrations (kind='google')`. API: `/api/integrations/google/{status,authorize,callback}`, `POST /api/kb/collections/{id}/gdoc`, `POST /api/kb/entries/{id}/resync`; gdoc entries reject `body_md` PATCH (409). `ingestion/sources/gdoc_sync.py --once` for the cron. Web: **Connect Google** / **＋ Google Doc** / per-entry **re-sync**, gdoc entries read-only with a 🔗. `docs/GOOGLE_SETUP.md`. 42 offline pytest green. **Live-verified** against a real Google account (project `root-anvil-303306`). |
| 16 | **Structured policy rules + internal task actions.** Per-team rule store (`when → then`); `policy_gate` (routing override) + `task_dispatch` (Slack-approved GitHub issue) nodes. | **Built + live-verified (2026-08-29).** `interpreter/policy.py` — pure JSON predicate evaluator (`all`/`any`/`not` + `{field,op,value}`, ops eq/ne/in/nin/gt/gte/lt/lte/contains/icontains/exists), `first_match` by priority. Migration `025`: `policy_rules` (tenant/team-scoped, RLS) + `action_requests` (the approval queue, unique `(run_id,kind)`). Nodes: **`extract`** (LLM → `state.entities`), **`policy_gate`** (loads rules, first match → `state.policy` = {action|task}), **`task_dispatch`** (raises an `action_requests` row + posts a Slack Approve/Reject). `builder._context` exposes `policy`/`entities` to edge conditions; `runs.record_run` links the `action_requests` row to the run. `interpreter/slack.py` (OAuth, `verify_signature` (pure, tested), `post_approval`/`update_message`) + `interpreter/github.py` (`create_issue`, per-tenant token). API: `/api/rules` CRUD, `/api/action-requests`, `/api/integrations/slack/{status,authorize,callback,interactions}` (signed callback → mark approved → `create_github_issue` job). `api/worker` handler opens the issue + edits the Slack msg. `scripts/expire_approvals.py` (stale `pending` → `expired`). Web: a **Rules** tab — a recursive **`when` form builder** (nested ALL/ANY groups, NOT wrapper, field datalist, per-op value widget) + a `then` form (route action, or task repo/title/body/labels/approver) with a **JSON toggle** as the power-user fallback; plus the approval-queue table + Connect Slack. `extract`/`policy_gate`/`task_dispatch` in the palette + Inspector. Seed `026`: an Acme-offboarding rule ("data older than 2 years → GitHub ops ticket") with `extract → policy_gate → {task_dispatch\|draft}`. `docs/SLACK_SETUP.md`. 52 offline pytest green; web tsc/vitest/build green. **Live-verified end-to-end**: offboarding case → `extract report_age_years=6` → `policy_gate` match → Slack Approve in `#support-leads` → signed callback → worker opened `vishnuanalytics/GH-Alert#2`, Slack message edited with the link. Fixes from that run: `entities`/`policy` added to `CaseState` (LangGraph drops undeclared keys — the policy chain had been inert); `jobs.claim()` now guards an all-NULL `claim_job` row (was a worker crash-loop on an empty queue); `action_request_id` promoted to a top-level state key so `record_run` links it past a later terminal node. |

**Phase 17 = low-confidence recovery (added 2026-08-29 from a scoping
conversation). Chunks: 17a `clarify` node · 17b `identify` node
(sender / email-domain → account match) · 17c web + observability · 17d
cross-run clarify loop. All four built + verified — Phase 17 COMPLETE
(2026-08-29). No open phase.**

| Phase | Scope | Status |
|---|---|---|
| 17a | **`clarify` node.** On a `confidence_gate` FAIL for a *non-escalation* topic, instead of a bare handoff, generate the specific questions whose answers would let the bot resolve the case next round (the customer's reply arrives as a new case). | **Built + live-verified (2026-08-29).** `registry.h_clarify` — from the case + retrieved context + `groundedness.unsupported`, one LLM call (`FAST_MODEL`, JSON) → `state.clarification = {questions[], missing[], channel, auto_send, posted}` and `outcome.action = "need_info"`; posts the question list to Chatter when there's an `sf_id` (dry-run without creds), else trace-only. Empty model output → one generic fallback question; `max_questions` capped. `auto_send` config knob is stored but customer-facing send is deferred to 17c. `CaseState.clarification` + `builder._context` key added (LangGraph drops undeclared keys — see Phase 16). `llm._stub_fields` gains a `clarify` branch for offline runs. **Migration `029`** (applied): adds a `clarify` node to the retrieval-gated flow (`d4d4…`, now published **v3**) and splits its retrieval_gate FAIL edge into `forced_escalation → ask_human` (unchanged) / benign `→ clarify` — the four gate conditions stay mutually exclusive, so routing is edge-order-independent. Portable copy `flow_retrieval_gated.json` updated. **Verify:** 58 offline pytest green (6 new — stub questions, fallback, `max_questions`, Chatter dry-run, `build_row` `need_info` not-pending, the 4-way routing split). **Live e2e (real Groq)**: benign unanswerable case → `retrieve→classify→confidence_gate→clarify` → `need_info` + 3 generated questions, `runs` row `outcome='need_info'` `human_action=null`; refund case → `ask_human` (forced escalation); enterprise tier → `handover`. |
| 17b | **`identify` node.** Resolve the sender: exact Contact/Lead by email → else email **domain → Account** match (skip free-mail domains) → else unknown; optional Lead create. `clarify` reads `state.sender` and also asks who they are when unknown. | **Built + live-verified (2026-08-29).** `salesforce.identify_sender(email, *, free_domains?, domain_match, create_lead, tenant_id)` — SOQL via `client_for()` (`_soql_lit` escaping): exact `Contact` by Email → exact unconverted `Lead` → `Contact WHERE Email LIKE '%@domain'` → `Account.Website LIKE` → else `match='none'`; `FREE_EMAIL_DOMAINS` set skips the domain step; no SF creds → `match='none'` (never raises). `registry.h_identify` (`email_field` default `contact.email`, falls back to `from`/`supplied_email`) → `state.sender = {email, domain, is_free_domain, known, account_matched, match, contact_id, lead_id, name, account_id, account_name}`; pass-through. `CaseState.sender` + `builder._context` key. `h_clarify` gains `ask_identity` (`not sender.known and (match in {none} or account_matched)`) + `account_hint` → prompt line asking the sender to confirm identity / share a reference; both in `clarification` + the trace. **Migration `030`** (applied): splices `identify` into `d4d4…` as `retrieve → identify → classify` (published **v4**). Portable `flow_retrieval_gated.json` updated + a "portable flow compiles" test. **Verify:** 66 offline pytest green (8 new — none/free-mail/no-email, exact-contact, domain→account, free-mail skips domain query, `h_identify` shape, `clarify` ask_identity matrix, portable-flow compile). **Live e2e (real dev-org data)**: `rose@edge.com` → `match=contact` (Edge Communications), clarify `ask_identity=false`; `newhire@edge.com` → `match=domain` (same account), clarify asks "confirm you're with Edge Communications" + a reference ID; unknown / gmail sender → `match=none`, clarify asks which company + reference. |
| 17c | Inspector forms for `clarify`/`identify`; palette + `NodeCard` terminal styling; `need_info` outcome pill + Runs surfacing; `clarify.auto_send` real outbound delivery. | **Built + verified (2026-08-29).** `salesforce.send_case_reply(case_id, body, *, to_email, subject, tenant_id)` — `emailSimple` invocable action when a recipient is known (actually sends), else a public `CaseComment`; dry-run without creds, never raises. `h_clarify` rewrite: `auto_send=true` + `sf_id` → `send_case_reply` (recipient from `sender.email` / `case.contact.email` / `case.from`), sets `clarification.auto_sent` + `outcome.{sent_to_customer,awaiting_customer}`; `auto_send=false` keeps the Chatter-to-an-agent path. `clarify` added to the web `TERMINAL` set (`graph.ts` + `FlowEditor.tsx`). **Inspector** (`Inspector.tsx`): `ClarifyForm` (`max_questions` / `channel` / `auto_send` toggle with an explanation) + `IdentifyForm` (`email_field`, `domain_match`, `create_lead_if_missing`, `free_email_domains` textarea). **Runs** (`RunsView.tsx`): `need_info` in the outcome filter, `.pill.need_info` (accent), and a "waiting on the customer" banner in the detail listing the questions from the `clarify` trace step (+ "identity check" / "sent" vs "for an agent to send"). **Verify:** 68 offline pytest green (+2 — `send_case_reply` dry-run, `h_clarify` auto-send emails the recipient); web tsc + vitest (5) + `vite build` green. Live e2e: unknown-sender benign case → `need_info`, `awaiting_customer=false` (auto_send off), `ask_identity=true`, 3 questions in the trace. |
| 17d | Cross-run clarify loop: correlate runs by Case id, `clarify_round` counter, cap at 2 → then force `ask_human`. | **Built + live-verified (2026-08-29).** Migration `031`: `runs.clarify_round int`. `h_clarify` queries `runs` for prior `need_info` rows on the same `case_id` (`config._sb` injectable; best-effort — a failed lookup = round 1) → `clarify_round = prior_max + 1`; once `clarify_round > config.max_rounds` (default 2) it emits `outcome.action='ask_human'` / reason `clarify_exhausted` with the outstanding questions attached, and `auto_send` is forced off (no point asking again). `clarify_round` is a top-level `CaseState` key; `runs.build_row` persists it. **Verify:** 71 offline pytest green (+3 — round increments from prior runs, exhausted→`ask_human`, `build_row` persists the round). **Live e2e**: same `case_id` run 3× through `d4d4d4d4-…` → `need_info` (round 1) → `need_info` (round 2) → `ask_human` `clarify_exhausted` (round 3); `runs.clarify_round` = 1/2/3. |

**Phase 18 = team access — invitations + roles (view / edit) + real
sign-in (added 2026-08-29). Chunks: 18a one-click New flow · 18b roles
(`owner`/`editor`/`viewer`) enforced in RLS + UI · 18c `tenant_invitations`
(owner invites email+role; claimed on first login) · 18d Google
sign-in button. Build + verify one chunk at a time.**

| Phase | Scope | Status |
|---|---|---|
| 18a | **One-click New flow.** `＋ New flow` no longer prompts for a `tenant_id` — the API infers it from the caller's membership. | **Built + live-verified (2026-08-29).** `FlowCreate.tenant_id` now optional; `create_flow` calls the existing `_caller_tenant(c, None)` (single membership → that tenant; several → 400 asking for one; not-a-member → 403). New `GET /api/tenants` → `[{tenant_id, role}]` for the caller (the UI's tenant picker / prompt-skip). Web: `api.listTenants()`; `FlowList.newFlow()` drops the uuid prompt, keeps team (default `support`) + name. **Verify:** 72 offline pytest green (+1 — `FlowCreate` valid without `tenant_id`, `/tenants` 401 without a token) + 2 integration; web tsc/vitest green. **Live e2e**: `GET /api/tenants` → `[{"00000000-…","owner"}]`; `POST /api/flows {team,name}` (no tenant_id) → 201, flow landed in `00000000-…`. |
| 18b | Roles `owner`/`editor`/`viewer` on `tenant_members` (col exists). Migration `032`: split RLS on `flows`/`flow_nodes`/`flow_edges`/`flow_versions`/`policy_rules`/KB tables — SELECT any member, write only `owner\|editor`. Web hides Save/Publish/New/Delete for `viewer`. | **Built + live-verified (2026-08-29).** **Migration `032`** (applied): `public.is_tenant_member(tid)` / `is_tenant_editor(tid)` SQL helpers; every editable tenant-scoped table (`flows`, `flow_nodes`, `flow_edges`, `flow_versions`, `policy_rules`, `kb_entries`, `sources` internal_kb) drops its single `ALL` policy for a **member SELECT** + **editor `owner\|editor` write** pair (mirrors `002`). API: `_require_editor(c, tenant_id)` pre-check on every flows / rules / KB write endpoint → a clean `403 "your access is view-only"` (RLS + the SECURITY INVOKER `replace_flow_graph` RPC are the real backstop). Web: `App.tsx` reads the caller's role from `GET /api/tenants` (max of memberships) → `canEdit`; `FlowList` hides ＋ New flow, `FlowEditor` hides Save / Publish / Delete / Re-layout / rollback / the node palette and shows a **view-only** pill, for `viewer`. **Verify:** 72 offline pytest green + 3 integration (18a's 2 + `test_viewer_can_read_but_not_write`: Globex owner demoted to `viewer` → PUT / POST / publish / DELETE / rules all `403`, reads still work); web tsc + vitest (5) + build green. |
| 18c | `tenant_invitations` + RLS. API `POST/GET/DELETE /api/invitations` (owner) + `POST /api/invitations/accept` (the web calls it on sign-in — matches the verified email → `tenant_members` row with the invited role). Web: a **Team** panel. No email infra — an invite pre-authorises an address + role. | **Built + live-verified (2026-08-29).** **Migration `033`** (applied): `tenant_invitations (invite_id, tenant_id, email citext, role ['editor'\|'viewer'], status ['pending'\|'accepted'\|'revoked'], invited_by, created_at, accepted_at)`, partial-unique on `(tenant_id,email) where status='pending'`; RLS — a tenant `owner` manages its rows, an invitee `select`s their own pending ones (`lower(email)=auth.jwt()->>'email'`). API: `Caller.email` (from the verified token); `_require_owner`; `GET/POST/DELETE /api/invitations`, `POST /api/invitations/accept` (idempotent, service-role — claims every pending invite for the caller's email → membership with the invited role), `GET /api/members` + `DELETE /api/members/{uid}` (owner-only, can't drop yourself or the last owner; emails via the Auth admin API). Web: `App.tsx` calls `acceptInvitations()` before `listTenants()`, shows a **Team** nav (owners) + a "no workspace — ask an owner to invite <email>" screen when `memberships==0`. `team/TeamView.tsx` — invite form (email + can-view/can-edit), members list (remove), pending invites (revoke). **Verify:** 73 offline pytest + 8 integration (18a×2, 18b×1, 18c×5 — invite/list/revoke, `accept` no-op, bad role → 400, non-owner → 403, members lists caller); web tsc/vitest/build green. **Live e2e (two real users)**: globex-owner invites `gundamvishnu7@gmail.com` `viewer` → gundamvishnu7 `POST /accept` → `{accepted:1}` → now a member of tenant `2222` as `viewer` → `POST /flows` there → `403`. |
| 18d | **Continue with Google** button on `Login.tsx` (`supabase.auth.signInWithOAuth`). New-user-no-invite screen. Needs the Supabase dashboard Google provider + a Google OAuth web client redirect (`…supabase.co/auth/v1/callback`) — operator steps, not code. | **Built (2026-08-29).** `Login.tsx` gains a **Continue with Google** button → `supabase.auth.signInWithOAuth({ provider: 'google', options: { redirectTo: window.location.origin } })`; an OAuth error shows inline. Nothing else changes — a Google user's pending invite is claimed by the `acceptInvitations()` call `App.tsx` already makes; no invite → the 18c "no workspace" screen. `docs/GOOGLE_SETUP.md` gains a "Google sign-in for the editor" section (the 3 dashboard/console steps: OAuth web client redirect `…supabase.co/auth/v1/callback` + JS origin `localhost:5173`; Supabase → Providers → Google enable + paste ID/secret; Supabase → URL Configuration). web tsc/vitest/build green. **Live sign-in needs the dashboard steps done** (Supabase Google provider is not yet enabled for this project — until then the button returns a provider error, which is displayed). |

**Phase 19 = assisted flow authoring — you no longer have to hand-draw the
graph (added 2026-08-30 from a scoping conversation). Chunks: 19a Mermaid
import (deterministic, no LLM) · 19b AI generate-from-description · 19c AI
edit-an-existing-flow · 19d docs/polish. Sequential. All four built +
verified — Phase 19 COMPLETE (2026-08-30). No open phase. No migration —
every path is stateless: a candidate graph loads onto the editor canvas as
unsaved state and Save/Publish go through the existing validated
`replace_flow_graph` path.**

| Phase | Scope | Status |
|---|---|---|
| 19a | **Mermaid import.** Parse a `flowchart` diagram → candidate flow graph; label→type match against the registry; a node that doesn't map is kept and set to `draft` (flagged); edge labels are surfaced as warnings, not turned into `condition.if`. | **Built + live-verified (2026-08-30).** `interpreter/flows/flow_candidate.py` `assemble_candidate(raw_nodes, raw_edges, defaults)` — the shared assembler (uuid per new key; a uuid key passes through so an AI *edit* keeps identity; unknown type → `draft` + warning; per-type default config merged *under* supplied; `check_flow` + the builder's single-entry-point rule → `errors` vs `warnings`). `interpreter/flows/mermaid_import.py` `mermaid_to_flow(text, defaults)` — pure/offline: node shapes `[] () {} ([]) [[]] [()] {{}} >]`, links `--> --- ==> -.->` with `|label|` or inline `-- label -->`, chains + `&` fan, `subgraph`/`end` flattened, `%%` comments, `--- title: … ---` front-matter → `name`; a `_SYNONYMS` map (`"check confidence"`→`confidence_gate`, `"escalate to human"`→`ask_human`, …). API `POST /api/flows/import/mermaid` (editor-only, rate-limited, persists nothing). Web: `graph.candidateToCanvas()`; `FlowEditor` **Import Mermaid** button (overlay w/ paste box → replaces the canvas as an unsaved draft, warnings/errors in the banner); `FlowList` **⬇ From Mermaid** (creates an empty flow, opens the editor with the overlay). **Verify:** 12 offline pytest (`test_mermaid_import.py` — linear/chained/fan, label flagging, inline syntax, unknown→draft, shapes/comments/subgraph, front-matter, self-loop=cycle, multi-root=warning, empty input, result compiles via `build_graph`, uuid-key passthrough) + 1 API integration; web tsc/vitest (6)/build green. **Live e2e**: `POST /api/flows/import/mermaid` with a 5-node diagram → `[retrieve, classify, draft, confidence_gate, auto_reply]`, 4 edges, `errors: []`. |
| 19b | **AI generate-from-description.** Plain-English → a whole candidate flow graph. | **Built + live-verified (2026-08-30).** `interpreter/flows/assist.py` `assist_generate(prompt, defaults, model?)` — one `llm.complete(json_object=True)` call (Groq default per CLAUDE.md; system prompt enumerates `known_types()` + one-liners + the `if`-expression names + shape rules), `assemble_candidate`, then **one repair round-trip** if the graph is structurally broken (kept only if it has fewer errors). `llm._stub_fields` gains an `assist` branch (system-prompt marker → a fixed valid `retrieve→classify→draft→confidence_gate→{auto_reply|ask_human}` graph) so it runs offline/CI. API `POST /api/flows/assist` (editor-only, rate-limit 12/min, `502` on model failure). Web: `FlowList` **✨ From prompt** → `assistNewFlow()` → empty flow + `sessionStorage` handoff → `FlowEditor` loads it as an unsaved draft. **Verify:** offline pytest (`test_assist.py` — `stub_llm` fixture clears the local key; generate → compilable flow w/ a real conditional edge; `assemble_candidate` coercion / dangling-edge / default-merge / multi-root / cycle) + 1 module integration (real Groq → `build_graph`) + 1 API integration. **Live e2e (real Groq)**: `POST /api/flows/assist` "retrieve, classify, draft, gate, auto-reply else ask human" → `errors: []`, graph compiles. |
| 19c | **AI edit-an-existing-flow.** An instruction + the working draft → a rewritten candidate + a diff to review on the canvas. | **Built + live-verified (2026-08-30).** `assist.assist_edit(current, instruction, defaults, model?)` — sends the current graph (keys = real `node_id`s, so kept nodes retain identity) + the instruction; the model returns the COMPLETE new graph; `assemble_candidate` + `interpreter/flows/flow_diff.py` `diff_graphs(before, after)` → `{added_nodes, removed_nodes, changed_nodes (labels), added_edges, removed_edges (counts)}`. `llm` stub edit branch echoes the current graph back (a valid no-op — the deterministic stub can't follow an instruction). API `POST /api/flows/{id}/assist` (loads the draft snapshot, editor-only, persists nothing). Web: `FlowEditor` **✨ AI edit** button (overlay → instruction → `assistEditFlow()` → canvas replaced as unsaved, diff summary in the banner). **Verify:** offline pytest (`test_assist.py` — edit returns a valid graph + a diff, node identity preserved on a no-op; `diff_graphs` add/remove/change) + 1 API integration (`/api/flows/{globex}/assist` → a diff). **Live e2e (real Groq)**: `POST /api/flows/{GLOBEX_FLOW}/assist` "add a handover branch for the enterprise tier" → 200, well-formed diff, graph non-empty. |
| 19d | Docs + polish. | **Done (2026-08-30).** `docs/FLOW_AUTHORING.md` (the three entry points, how types/labels/conditions map, the "review then Save" model). `EXPECTED_TYPES` untouched (import/AI don't change what a "complete" flow is). Note: for a multi-tenant caller the assist/import endpoints need a `tenant_id` (same 400 as `＋ New flow`); the diff can cosmetically over-report a bare node as "changed" when defaults get merged in (advisory only — the user reviews on the canvas). |
| 19e | **AI-copilot output quality** (found + fixed 2026-09-03, no scope change — same endpoints/UI). Live-testing 19b/19c with 10 varied prompts found `_TYPE_DOC` documented only 14 of 25 registered node types, causing the model to: pick `notify` (Chatter-only) instead of `notify_human` for an explicit "post to Slack" request (silently not doing what was asked); omit `trigger` entirely on a "start from a webhook payload" request; use generic `transform`+`extract` instead of the dedicated `attachments` OCR node on a screenshot request; and reinvent `team_route`'s config-driven routing as duplicate near-identical `if`/`else` edges instead of using `team_route.config.rules` (leaving `state.routed_team` computed but unused). Separately, a hallucinated `sf_writeback` node referenced in two edges but never declared was silently dropped with only a warning — `assist_generate`'s repair round-trip only re-prompted on `errors`, never `warnings`. | **Fixed (2026-09-03).** `interpreter/flows/assist.py`: all 11 missing `_TYPE_DOC` one-liners added (`trigger`/`sf_case`/`sf_context`/`case_lookup`/`team_route`/`attachments`/`ai_prompt`/`http_request`/`transform`/`notify`/`notify_human`), each naming its config shape and, where it mattered, explicitly steering away from the observed mistake (e.g. "use notify_human for Slack", "don't re-implement the routing as duplicate if/else edges"); `_CONDITION_NAMES` gains `routed_team` (promised by `team_route`'s own docstring but missing from the prompt). New `_hard_warnings()` (the two warning shapes that mean the model made a mistake — a coerced-to-`draft` unknown type, or a dangling edge — vs. an advisory like "several possible start nodes") now also triggers the repair round-trip, which is shared via a new `_repair_round()` helper between `assist_generate` **and** `assist_edit` (the edit path previously had no repair at all) and now carries the full original request/current-graph context into the retry, not just the bare error list. `_SYSTEM_EDIT` also nudges the model to always fill the optional `summary` field (it came back `None` in testing). **Verify:** `tests/test_assist.py` 8/8 unchanged (offline/stub path untouched — the stub-detection markers at the start of `_SYSTEM_GENERATE`/`_SYSTEM_EDIT` weren't touched). **Live re-test (real Groq, same 10 prompts):** the Slack-ping, webhook-trigger, OCR-node, and dropped-`sf_writeback` cases all now come back correct with zero warnings; `team_route` no longer generates duplicate routing edges. **Residuals (not fixed, noted honestly):** a "summarize in a friendly casual voice" prompt still returns plain `draft` instead of the more-suited `ai_prompt` (documentation alone didn't overcome the strong "draft a reply" prior); `team_route.config.rules` / `trigger.config.map` come back empty (structurally present, not populated with the prompt's specifics) — next candidates if this needs another pass. |
| 19f | **Round 2 — closed the 19e residuals + a crash.** Re-tested with prompts targeting the 7 node types 19e's battery hadn't exercised directly (`identify`/`case_lookup`/`clarify`/`handover`/`notify`/`policy_gate`/`task_dispatch`) plus retests of the 19e residuals. Found: (1) the `ai_prompt` residual **was** phrasing-sensitive, not unfixable — a prompt contrasting "not the standard formal reply" correctly triggered `ai_prompt`; (2) `case_lookup`'s edges referenced a fabricated `.found` field (the node actually sets `prior_resolutions`/`investigation_hints` — no such field exists); (3) `task_dispatch` was wired from a fabricated same-run `entities.approval` branch, misunderstanding that its Slack approval is asynchronous (via `action_requests`, outside the graph) and that it's meant to be reached from an upstream `policy_gate` match; (4) `notify_human.config.channel` (the enum `'slack'\|'salesforce_chatter'\|'both'`) got a channel *name* like `'#manager-approvals'` written into it instead of the separate `slack_channel` field; (5) `http_request`/`transform` template values used a fabricated `{{state.x}}` root instead of the real dotted-path-directly-against-state syntax (`{{context.x}}`); (6) the 19e minimality rule wasn't enough — `team_route.config.rules` still came back empty even given an explicit 3-way mapping in the prompt; (7) a malformed model response (a bare string where a node object was expected) **crashed** `assemble_candidate` with an unhandled `AttributeError` instead of degrading to a warning like every other bad-shape case — a real robustness gap, not a prompt-wording issue. | **Fixed (2026-09-03).** `interpreter/flows/assist.py`: corrected the `case_lookup` (real output fields), `task_dispatch` (upstream `policy_gate` + async Slack approval, not a same-run branch), `notify_human` (`channel` enum vs `slack_channel` name), `http_request`/`transform` (correct `{{...}}` template root) one-liners; discovered mid-fix that my own `policy_gate` doc needed a correction too — unlike `team_route`, its rules live in the separate `policy_rules` **table**, not inline node config, and it sets `policy.matched`/`policy.action`/`policy.task` (no `.pass`) — documented explicitly so the model doesn't invent either. `_RULES` gains: a concrete worked example for populating a node's own documented config (the `team_route.config.rules` shape, verbatim) instead of just prose; a rule against a gratuitous unrequested terminal ("don't add a terminal node the request didn't ask for" + "`auto_reply` needs an upstream `draft`/`ai_prompt`, never wire it after anything else"); and a rule against inventing a dotted field a node doesn't actually set. `interpreter/flows/flow_candidate.py::assemble_candidate` — a non-dict entry in `raw_nodes`/`raw_edges` now degrades to a warning and is skipped, instead of an unhandled `AttributeError` (shared by `assist` **and** `mermaid_import`, so both get the hardening). **Verify:** `tests/test_assist.py` + `tests/test_mermaid_import.py` 20/20 unchanged; a synthetic malformed-entry test confirms the crash is gone (warning, not an exception). **Live re-test (real Groq):** all 7 previously-unexercised node types now come back correct in isolation; re-running the full original 19e 10-prompt battery end-to-end afterward shows every case (including `custom_ai_step` and `salesforce_case`, the two that had been weakest) now producing a correct, zero-warning graph — `custom_ai_step` now uses `ai_prompt` even on the original (non-contrastive) phrasing. **Residual, accepted as a tradeoff, not chased further:** an unrealistically narrow single-step prompt (e.g. "just resolve the sender, nothing else") can now fail structurally (an orphan node, no edges) rather than quietly padding itself with a nonsensical terminal — `check_flow` requires every node to be connected to at least one edge, which is fundamentally in tension with "keep it minimal" for a true one-node request; real multi-step prompts are unaffected. |
| 19g | **A full field/config audit, all 25 node types.** On request: does *any* node type have a field-name or config mismatch beyond what 19e/19f already found? Systematically read every remaining handler in `interpreter/registry.py` (`confidence_gate`, `extract`, `kb_lookup`, `clarify`, `ask_human`, `handover`, `auto_reply`, `sf_writeback`, `identify`, `retrieve`) against `_TYPE_DOC` / `_CONDITION_NAMES`. Confirmed accurate and no action needed: `confidence_gate` (`.pass`/`.score`/`.threshold`/`.tier`/`.groundedness`/`.forced_escalation` all real), `sf_writeback` (config genuinely optional, sensible defaults), `ask_human`/`handover`/`auto_reply` (no config required), `identify` (`sender.known`/`.match`/`.account_matched`/`.account_name` all real, confirmed via `clarify`'s own use of them), `retrieve` (`retrieval_score` real, top-level). `kb_lookup` without `config.collections` checked and found to degrade gracefully (searches the tenant's available internal collections broadly, not a no-op) — no fix. Found one more real bug: **`extract` silently no-ops without `config.fields`** (`h_extract`: no `fields` -> returns `entities: {}` immediately, no LLM call) — and 19f's `clarify` test output had exactly this (`extract` node with `config: {}`), meaning that generated flow's `entities.product`/`entities.error` conditions were permanently false, so its `clarify` branch would have fired on *every* case regardless of what the customer wrote — a functional bug that had gone unnoticed even after 19f's fixes. | **Fixed (2026-09-03).** `interpreter/flows/assist.py`: the `extract` one-liner now says plainly that `config.fields` is REQUIRED (not optional — unlike most other node configs) with a worked example (`{"product": "the product or plan mentioned", "error": "the exact error text"}`), mirroring how 19f fixed `team_route`. **Verify:** `tests/test_assist.py` 8/8 unchanged. **Live re-test (real Groq), the exact clarify prompt that exposed the bug:** `extract.config.fields` now comes back populated with sensible field definitions and the branch condition (`not entities.product or not entities.error`) is now genuinely conditional instead of permanently true. |


**Phase 20 = email channel — auto-respond to inbound mail, configured from
the UI (added 2026-08-30 from a scoping conversation). Chunks: 20a
credentials + Supabase Vault + API · 20b inbound poller · 20c outbound +
hard guard · 20d web Channels panel + docs · 20e inbound → Salesforce
Case + the L0/L1 email flow. Sequential. 20a–20d BUILT + verified
(2026-08-30); 20e BUILT + live-configured (2026-08-30), live mail e2e
pending a clean test sender.** Decisions: **both** providers —
Gmail via OAuth (reuses Phase 15 `GOOGLE_CLIENT_ID`) and other mailboxes
via IMAP/SMTP + an app-password; the credential (password / refresh token)
is stored in **Supabase Vault** (`vault.secrets`), never in
`tenant_integrations` in the clear and never returned to the browser; a
workspace **owner** configures it and can change it later; auto-replies go
out from the **same mailbox** (optional `no_reply_addr` field built, unset
for now); the **hard guard** is that the LangGraph flow's outcome gates
sending — email goes out only on `outcome.action == "auto_reply"`,
`ask_human`/`handover` leave the message for a human, plus a per-channel
`auto_send_enabled` master switch (default **off**) and loop-breakers
(skip `no-reply`/`mailer-daemon`/`Auto-Submitted`/`List-Id` senders and
the bot's own mail).

| Phase | Scope | Status |
|---|---|---|
| 20a | **Credentials + Vault + API.** Store a per-tenant mailbox config from the UI; secret in Supabase Vault; owner-gated; a "test connection" endpoint. | **Built + verified (2026-08-30).** Migrations `034` (`tenant_integrations` += `config`/`vault_secret_id`/`status`/`last_poll_at`/`last_error`/`cursor`/`updated_by` + a `status` check) and `035` (`public.integration_secret_{put,get,delete}(tenant, kind, …)` — SECURITY DEFINER wrappers over `vault.create_secret`/`vault.decrypted_secrets`; execute revoked from `anon`/`authenticated`, granted to `service_role`) — **applied**; Vault round-trip verified (create / update-by-name / get / delete). `interpreter/mailbox.py` — `MailboxConfig` (secret kept out of `repr` + `public_status()`), `load_channel`/`save_channel`/`delete_channel`/`set_status`, `test_connection` (IMAP+SMTP login check, or a Gmail token refresh), `gmail_authorize_url`/`gmail_profile_email`, and pure `parse_message`/`is_autoreply`/`looks_like_bot_address`. API: `GET /api/integrations/email` (any member; status only, never the secret), `PUT` / `DELETE` / `POST …/test` (owner), `GET …/google/{authorize,callback}` (owner; callback public). **Verify:** 10 offline pytest (`test_mailbox.py`) + 3 offline API (401/403 gating) + 3 integration (`test_api.py`: IMAP creds via **Vault** → status shows `configured` with no password anywhere → flip `auto_send_enabled`/`active` without re-sending the password → DELETE clears it; `test` reports a bad host cleanly; a demoted viewer gets 403 on every write). Gmail-OAuth path is built but needs `GOOGLE_CLIENT_ID`/`SECRET` + the `.../email/google/callback` redirect registered — an operator step (same status as Phase 15/18d). |
| 20b | **Inbound poller.** `ingestion/email_watch.py --once` — IMAP/Gmail fetch new mail → loop-breaker filters → `parse_message` → enqueue `run_flow` keyed on `Message-ID` against the tenant's published flow for `config.team`; mark processed (label/move, no delete); update `last_poll_at`/`last_error`. | **Built + verified (2026-08-30).** `interpreter/mailbox.py` += `list_active_channels`, `fetch_new`/`mark_processed` (IMAP `UNSEEN SINCE` + `BODY.PEEK[]` → `\Seen`; Gmail `q="is:unread in:inbox newer_than:Nd"` + `format=raw` → remove `UNREAD`), pure `should_process` (drops no-From / auto-responder / own-mail / empty) + `thread_key` (thread-root id → `case_id`, so a customer's reply lines up with the run that asked, Phase 17d). `ingestion/email_watch.py` (`--once` / `--tenant` / `--lookback` days / `--limit` / `--dry-run` / loop) — per active channel: resolve the published `(tenant, team)` flow (none → `status='error'`), fetch, filter, `jobs.enqueue("run_flow", {flow_id, case, idempotency_key=<msg-id>}, dedupe_key="email:<msg-id>")`, mark read, `set_status('active')`; a fetch failure → `set_status('error', last_error=…)`, never crashes the tick. No SF-style watermark (job dedupe + `\Seen` are the cursor). **Verify:** 8 offline pytest (`test_email_watch.py` — `should_process`/`thread_key`; answerable mail → one enqueue with the right keys + marked; auto-responder / own-mail skipped-but-marked; dry-run enqueues+marks nothing; no-flow → error status; fetch exception caught) + 1 integration (active Globex channel via **Vault** → `tick` resolves the published flow, IMAP to a bogus host fails, `status='error'`/`last_error` written to the live DB, cleaned up). `python -m ingestion.email_watch --once` with no active channel → notice + exit 0. |
| 20c | **Outbound + hard guard.** `interpreter/emailer.py` SMTP/Gmail send (`From` = `no_reply_addr` or the mailbox; stamps `X-Support-Bot: 1`); the **worker** (not a flow node — keeps the graph channel-agnostic) sends after an email-sourced run *only* when `outcome.action == "auto_reply"` and the channel's `auto_send_enabled` is on; `need_info` sends only if the `clarify` node's own `auto_send` is set; everything else is left for a human. | **Built + verified (2026-08-30).** `interpreter/emailer.py` — pure `decide(outcome, cfg, clarification) → (send_reply|send_questions|needs_human|noop, meta)` (the guard: `auto_reply` sends only with the master switch **on** and a non-empty draft; `need_info` sends only with the switch on **and** `clarification.auto_send`; `ask_human`/`handover`/switch-off/empty-draft → `needs_human`), and `send_reply(cfg, to, subject, body, in_reply_to, references)` — builds a threaded `EmailMessage` stamped `X-Support-Bot: 1` + `Auto-Submitted: auto-replied`, sends via SMTP (imap provider) or `gmail.users().messages().send` (gmail), **dry-run with no creds, never raises**. `interpreter/mailbox.mark_needs_human(cfg, message_id)` — looks the message up by Message-ID and re-marks it unread + `\Flagged` / `STARRED` (the poller marked it read on enqueue). `api/worker._run_flow` → `_email_post_run(final, case, flow, sb)` for `case.channel == "email"`: applies `decide`, sends or flags, returns the delivery in the job result; wrapped so a delivery failure never fails/retries the run. **Verify:** 12 offline pytest (`test_emailer.py` — the full `decide` matrix; `send_reply` dry-run / missing recipient / threaded-bot-stamped headers via a monkeypatched SMTP; `_email_post_run` auto_reply→send, ask_human→flag+no-send, need_info+opt-in→questions, no-channel→skip) incl. 1 integration (a real Vault-loaded channel with no SMTP host → `auto_reply` → `decision=send_reply`, `delivery.dry_run=True`). |
| 20d | **Web + docs.** A **Channels** nav panel (owners): provider picker / Connect Gmail, IMAP form, from-name, optional no-reply, team, folder, the `auto_send_enabled` toggle, Test-connection, status (last poll / last error). Editors read-only. `docs/EMAIL_SETUP.md`. | **Built + verified (2026-08-30).** `web/src/channels/ChannelsView.tsx` — owner-only panel: provider radio (IMAP / Gmail, Gmail disabled + labelled when `gmail_available:false`), team + from-name, IMAP host/port + SMTP host/port + mailbox login + **app-password field that says "leave blank to keep"**, folder, optional reply-from, an **auto-send** toggle (copy: "off = every reply waits for a human") and an **active** toggle; **Test connection** / **Save** / **Disconnect**; a status banner (`status` + `last_poll_at` + `last_error`, red on `error`). Gmail: a **Connect Gmail** button that pops the OAuth consent window. `api.email.{status,save,test,remove,googleAuthorize}` + `EmailChannel`/`EmailChannelSave` types; `App.tsx` gains a **Channels** nav for owners (next to Team). `GET /api/integrations/email` now also returns `last_poll_at`/`last_error`. `docs/EMAIL_SETUP.md` (the safety model, app-password steps for Gmail/Outlook, the cron, the Gmail-provider operator steps). **Verify:** web tsc + vitest (6) + build green; the 3 email API integration tests still green with the status change. |
| 20e | **Inbound → Salesforce Case + the L0/L1 email flow.** An inbound email has no `sf_id`, so `ask_human`/`sf_writeback`/`handover` were inert on the email channel. New **`sf_case`** node (`interpreter/registry.h_sf_case` → `salesforce.ensure_case`): resolve the Contact (`sender.contact_id` from `identify` → exact Contact by email → create one; business-domain sender w/ no Account → create the Account), **reuse the Contact's most recent open Case within `reuse_open_days` (14)** else create one (`Origin='Email'`, `SuppliedEmail`, `ContactId`/`AccountId`), and merge `sf_id` + the Account `Tier__c`/`BillingCountry` back into `state.case` so `classify` gates on the real tier. Migration **`036`** seeds + publishes **`e5e5e5e5-…`** "Email L0/L1 — inbound to Salesforce" (tenant Acme, **team `email`**): `identify → sf_case → retrieve → classify → sf_writeback → draft → confidence_gate → {handover (enterprise) \| auto_reply (pass) \| ask_human = SF Chatter on the Case (fail)}`; the 3 gate edges are mutually exclusive + exhaustive. Portable `interpreter/flows/flow_email_l0l1.json`. | **Built + live-verified (2026-08-30).** Offline pytest green — 6 in `test_sf_case.py` (`ensure_case` dry-run/keeps-existing-id, `h_sf_case` passthrough + account-snapshot merge + reused-Case summary, portable flow compiles + 3-way routing). **Live-configured:** flow published (loader+`build_graph` clean against the live DB); SF **Account `Gundam Vishnu (Gmail)` + Contact `gundamvishnu7@gmail.com`** created (`Tier__c='basic'` so it exercises auto_reply/ask_human, not the enterprise handover); the **email channel saved to Vault** for tenant `00000000-…` (provider imap, `imap.gmail.com`/`smtp.gmail.com`, team `email`, `auto_send_enabled=true`, `status='active'`) — `test_connection` → IMAP+SMTP OK. **Synthetic e2e — PASSED (2026-08-30, real Groq + live SF + live SMTP)**, driven through `api.worker._run_flow` against the published flow: (A) *"How do I turn on a Zap?"* → `identify` matched the Contact/Account → `sf_case` **created Case 00001033** (Origin=Email) → `retrieve` real KB hit → `classify` topic=zap-activation tier=basic → `sf_writeback` **wrote Priority/Module__c/Description live** → `draft` (groundedness 1.0) → gate PASS (0.988/0.50) → `auto_reply` → **real email sent via Gmail SMTP** (~17 s cold / ~10 s warm; the ~7 s embedding-model load is one-time per worker process). (B) *"Refund for my annual plan"* → `sf_case` **reused open Case 00001033** (thread correlation) → `classify` topic=refund → gate **forced-escalate** → `ask_human` **posted Chatter on the Case** (Connect mention 404 in this org → plain FeedItem fallback) → `email.decision=needs_human`, message flagged, **nothing sent**. `ingestion/email_watch.py` gained `--from <addr>` (only-process filter, leaves other mail untouched). **Real inbound e2e — PASSED (2026-08-30):** 6 real emails from `vishnu.r@urbanpiper.com` fetched from the live Gmail via IMAP → `identify` (1st = `none`) → `sf_case` **created Account "Urbanpiper" + Contact + Case 00001034** from the business domain, then the next 5 **reused open Case 00001034** → `sf_writeback` live each time → all → `handover` (see the fail-closed note) → `email.decision=needs_human`, flagged, nothing sent. Inbound fetch + Case bootstrap + thread-reuse + the guard all confirmed against real mail. **Finding (1) — FIXED (2026-08-30):** `classify` gains a `default_tier` config knob — when the CRM gives no *recognisable* tier (missing, or a non-canonical value like the SF standard `Account.Type`="Customer"), the node uses `default_tier` instead of letting `_norm_tier` fail closed to `enterprise`; a real basic/premium/enterprise on the account still wins. `classification.tier_defaulted` records it. The email L0/L1 flow's `classify` carries `default_tier:"basic"` (migration `036` + the live v1 snapshot updated; portable JSON updated). **Live-verified:** account with `Tier__c=null`/`Type="Customer"` + doc-answerable question → `tier=basic`, gate 0.996/0.50 **PASS → auto_reply → real email sent** (was `handover`). New `_tier_known()` helper; the global `_norm_tier` fail-closed is unchanged. **Finding (2) — FIXED (2026-08-30):** the poller no longer relies on read-state. `interpreter/mailbox.py` — `MailboxConfig.cursor` (persisted to the existing `tenant_integrations.cursor` jsonb: `{imap_uid}` / `{internal_date_ms}`), pure `_imap_search_args`/`_gmail_query` (`UID >cursor` / `after:<epoch>`, no `UNSEEN`/`is:unread`), `_fetch_imap`/`_mark_imap` switched to UID commands, `set_cursor()`; `email_watch.poll_channel` advances the cursor past **every** message a tick saw (handled or skipped) and skips the cursor bump on a `--from` test run. `\Seen` / `UNREAD`-removal are now courtesy-only. The live channel's cursor was seeded to the current INBOX head (UID 28777). **Finding (3) — FIXED (2026-08-30):** `api/worker._run_flow` and `POST /flows/{id}/run` now pass `final["case"]` (the graph's mutated case) to `record_run` / `_email_post_run`, not the pre-run input — so `runs.case_payload.sf_id` is populated (live-verified) and `build_row`'s `pending` check (which gates the Phase 11 `check_resolution` job on `case.sf_id`) now fires for email-created Cases. **140 offline pytest** green (+8 across the three fixes). **Test data from this session (SF Account "Urbanpiper" + Contact + Cases 00001033/00001034, 8 `runs` rows) was deleted; the `gundamvishnu7@gmail.com` bootstrap Account/Contact was kept.** |

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

## Hardening roadmap (phases 7–13) — detail

From the 2026-08-29 self-review. Each phase is a small, verifiable chunk;
keep the "same interpreter, more capability" discipline. Migration numbers
below are indicative — take the next free number when you build it.

### Phase 7 — Evaluation & calibration

- `eval/e2e/` — 30–50 cases, each with `{gold_action, notes}` and a rubric
  for the draft. `run_e2e.py` runs each through the **real** pipeline
  (`build_graph().invoke`) and reports: auto-send **precision** (of cases
  the bot auto-replied, how many drafts were sendable), escalation
  **precision** (of `ask_human`/`handover`, how many actually needed a
  human), and a mean rubric score (LLM-judge with a fixed rubric).
- Calibrate `confidence_gate`: is `retrieval_score` monotonic with
  answerability on the e2e set? Pick thresholds from a precision target
  ("auto-sends ≥ 95% acceptable") and report the coverage that buys.
  Consider Platt/isotonic on the combined score.
- `interpreter/groundedness.py` — claim-extract the draft, check each claim
  is entailed by a retrieved chunk (small model or Groq judge); expose
  `groundedness` in state; let `confidence_gate.config` weight it in.
- `eval/qrels_hard.jsonl` — multi-hop / keyword-heavy / near-duplicate-pair
  questions that separate dense vs hybrid vs +rerank vs **+graph** (the
  graph stage is currently unmeasured). Update `eval/README.md`.
- Timing + tokens: each handler already returns a `trace` entry — add
  `elapsed_ms`, and `tokens_in/out` when `llm.available()`. `record_run`
  stores totals; `GET /runs/stats` surfaces p50/p95 latency + token/run.
- Fix fail-open tier: `registry._norm_tier` unknown → `"enterprise"` (or an
  explicit `"unknown"` tier with the strictest override) + a warn log.
- Verify: `run_e2e.py` prints the precision/coverage table; a regression
  guard in CI that auto-send precision doesn't drop below a floor.

### Phase 8 — Flow versioning & safe writes

- Migration: either a `flow_versions` table (immutable snapshot of
  nodes+edges+meta per version) or version-scoped `flow_nodes`/`flow_edges`
  with a `flows.published_version` pointer. Editing a `published` flow in
  the UI forks `version+1` as `draft`; **Publish** flips the pointer;
  **Rollback** re-points to an older version.
- `runs` gains `flow_version` (+ `flow_hash`). Every run is reproducible;
  every definition change is auditable (`who`/`when` via `created_by`).
- `replace_flow_graph(flow_id, version, nodes jsonb, edges jsonb)` Postgres
  function, `SECURITY INVOKER`, one transaction — `api` calls it via RPC
  instead of the current delete/delete/upsert/upsert sequence.
- Optimistic concurrency: `PUT /flows/{id}` body carries the loaded
  `version`; server 409s if it moved. UI shows "reload — someone else
  saved".
- Verify: `test_api.py` covers fork-on-edit, publish swap, rollback, and
  the 409; a run's `flow_version` matches the definition it executed.

### Phase 9 — Test coverage & CI

- Move `tests/` to `pytest` (keep the plain-`python -m` entry working).
- `tests/test_api.py` — `fastapi.testclient.TestClient`, a seeded test
  tenant + a minted/stubbed token: 401 without a token, `GET /flows`
  RLS-scoped, `PUT` 422 on refs/orphan/cycle/unknown-type, `run` returns a
  `run_id` and records, cross-tenant `GET` → 404, `DELETE` cascade.
- `web`: Vitest for `graph.ts` (dict↔RF, dagre), one Playwright smoke
  (stubbed auth → load flow → add node → Save).
- De-brittle `test_multiflow`: assert `a.outcome != b.outcome` on identical
  input + the structural facts (offboarding always `handover`), not the
  exact score-path; optionally pin a frozen corpus snapshot for it.
- `.github/workflows/ci.yml` — `pip install -r requirements.txt`,
  `pytest`, `cd web && npm ci && npm run build && npx tsc -b`. Required
  check on PRs to `main`.

### Phase 10 — Event-driven pipeline

- Trigger: `ingestion/sf_case_watch.py` — a Salesforce **Change Data
  Capture** / Platform Event subscriber (or a polling worker on
  `Case WHERE Status='New' AND <no run yet>`) that, per new Case, resolves
  the tenant's published `support` flow and enqueues a run. Runs on GitHub
  Actions cron or a small always-on worker.
- Queue: `arq` (Redis) or a Postgres-backed `jobs` table + `SELECT … FOR
  UPDATE SKIP LOCKED` worker. `POST /flows/{id}/run` **enqueues** and
  returns `{run_id, status:"queued"}` immediately; a worker executes and
  updates the `runs` row; the web Run panel + Runs view subscribe via
  Supabase Realtime on `runs`.
- Idempotency: unique partial index on `runs(flow_id, case_id)` for
  non-failed runs within a window / an `Idempotency-Key` header;
  `auto_reply` / `sf_writeback` / `ask_human` check for a prior completed
  run for the case and no-op.
- Verify: fire the same Case twice → one run, one Chatter post; kill the
  worker mid-run → the job is retried, not lost.

### Phase 11 — Human-in-the-loop feedback

- After `ask_human`, a follow-up job (or the Phase 10 worker on a delay)
  reads the Case's actual outbound reply and computes `human_action`
  (`sent_as_is` / `edited` / `rewrote` / `no_reply`) and `edit_distance`
  vs the bot draft; stored on the `runs` row (or a `run_feedback` table).
- Runs view: "draft acceptance rate" per team / topic / tier over time;
  a list of the worst-edited drafts.
- Accepted drafts flow into `eval/e2e/` as new gold cases and into a
  few-shot pool the `draft` node can sample from (config-gated).
- Verify: seed a Case, `ask_human`, edit the reply in SF, run the job →
  the `runs` row shows `edited` + a plausible distance.

### Phase 12 — Real multi-tenancy

- Migration: `sources` (source_id, tenant_id, kind `zapier_docs|markdown|
  notion|gdrive|slack`, config jsonb, status), RLS via `tenant_members`.
  `doc_chunks` / `zapier_docs` gain `source_id` (backfill the existing rows
  to a "zapier-public" source shared by all tenants). `retrieve` node
  `config.sources` names which to search; `retrieval.hybrid_retrieve`
  filters by `source_id`.
- Ingestion becomes per-source: `ingestion/sources/<kind>.py` with a common
  `fetch → chunk → embed → upsert(source_id)` shape; the daily workflow
  loops over active sources.
- `tenant_integrations` (tenant_id, kind, secret jsonb — encrypted via
  Supabase Vault / pgsodium). `interpreter/salesforce.py` and future Slack
  resolve creds from here keyed by the flow's `tenant_id` instead of `.env`.
- A real second source: ingest a small Markdown SOP set for the Globex
  tenant; a Globex flow retrieves from `[globex-sop, zapier-public]`.
- Defensive: every service-role write (`record_run`, `sf_writeback`,
  flow save) asserts the row's `tenant_id` matches the flow's.
- Verify: Acme and Globex retrieve different top docs for the same query
  because they point at different sources; `sop_conflicts.py` now has real
  divergence to find.

### Phase 13 — Security & hardening

- `api`: verify the Supabase JWT signature in `caller` (fetch JWKS or use
  the project JWT secret) — currently the `sub` is decoded unverified and
  used for logging; RLS is the only real gate. Reject expired/forged.
- Rate-limit `POST /flows/{id}/run` and `/runs` (per user + per tenant) —
  `run` has real external side effects.
- Run `/security-review` over the accumulated `api/` + `web/` diff; address
  findings. Re-check CORS, error verbosity, and that no endpoint leaks
  cross-tenant data via error messages.
- `sop_conflicts.py`: once Phase 12 gives divergent per-team retrieval,
  confirm it surfaces a real conflict and Groq-judges it; add it to CI as
  a non-blocking report.

## Self-serve knowledge & internal actions (phases 14–16) — detail

Added 2026-08-29 from a scoping conversation. Problem statement, in the
user's words: *"if the bot wants to check an internal workflow or
configuration where the internal team can check & update … not a single
document upload or Google Doc link for all teams … give an option to
users, they can add at any point … if the agentic flow reaches there &
checks the information, give that one, otherwise ignore it."*

Two distinct needs came out of that:

1. **Unstructured** — team-authored SOPs / runbooks / config notes the
   **draft LLM reads** as authoritative context. Changes *what the reply
   says*. → Phases 14 (in-app + upload) and 15 (Google Docs, kept synced).
2. **Structured** — UI-defined **rules that change what the bot does**:
   override routing, or fire an internal task (with a human approving in
   Slack first) such as opening a GitHub issue and tagging a team. →
   Phase 16.

Migration numbers below are indicative — take the next free number when
you build it (`021_recalibrate_globex_gate.sql` is the last one on disk).

Design decisions already settled in that conversation:

- Knowledge is **per tenant**, organised as **many named collections**
  (not one flat KB) — each `kb_lookup` node picks which collection(s).
- Editors are **existing `tenant_members`** (the same people who use the
  flow editor) — no new role. Support agents / managers do **not** log
  into this platform; they live in Salesforce + Slack, so Phase 16
  approvals happen **in Slack**, not on a dashboard.
- Google Docs use a **per-tenant Google OAuth** connection and are **kept
  in sync**, not imported once.
- Free tooling: **GitHub Issues** for tasks, **Slack** for approvals
  (both have adequate free tiers). Alternatives noted but not chosen:
  GitLab/Linear/Jira for tasks; Discord/Telegram/Teams/email or an
  in-app `internal_tasks` table for approvals.
- A rule's inputs come from the **case text + classification/extraction**
  (e.g. "reports for FY 2024" → an `extract` node pulls
  `entities.report_period`), **not** a live lookup into an internal
  system. Rules evaluate against `state` fields only.

### Phase 14 — Internal knowledge base (unstructured), self-serve

- **Migration `022_knowledge_base.sql`:**
  - A collection = a row in **`sources`** with `kind='internal_kb'` and
    `tenant_id` set (reuses Phase 12 scoping + `resolve_sources`
    unchanged). `sources` gains an insert/update/delete RLS policy for
    `kind='internal_kb'` scoped via `tenant_members` (today it's
    select-only).
  - **`kb_entries`** — the editable source of truth: `entry_id uuid pk`,
    `source_id → sources`, `tenant_id` (denormalised for RLS), `title`,
    `body_md`, `status` (`active|archived` — soft-delete per the
    `zapier_docs.status` rule), `created_by/at`, `updated_by/at`,
    `embed_hash`, `chunk_count`, `embedded_at`. Full CRUD RLS via
    `tenant_members`.
  - Content flows into the **existing** `zapier_docs` + `doc_chunks` on
    save (same as `ingestion/sources/markdown_source.py` already does):
    `zapier_docs.url = 'kb://<source_id>/<entry_id>'`, `source_id` set,
    `raw_text = body_md`; `doc_chunks` chunked + embedded (local
    `fastembed`, 384-dim). So retrieval needs **zero new code** —
    `match_doc_chunks(p_source_ids := [collection ids])` already filters.
- **Shared ingest helper:** factor `embed_entry(sb, source_id, url, title,
  body_md)` out of `markdown_source.py` into
  `ingestion/sources/kb_common.py` (chunk via `scraper.chunk_markdown`,
  embed via `scraper.get_embedder`, upsert `zapier_docs`/`doc_chunks`,
  delete stale chunks). `markdown_source.py` calls it too.
- **Interpreter — `kb_lookup` node** (`interpreter/registry.py`):
  `@register("kb_lookup")`. Config: `collections` (names, resolved
  tenant-scoped like `kb_sources`), `query` (templated over `state`,
  default = case text), `top_k`, `use_rerank`, `min_score`, `out_key`
  (default `"internal_kb"`). Calls `hybrid_retrieve(query,
  kb_sources=collections, tenant_id=…, use_graph=False)`; writes
  `state[out_key] = {matches, score, checked: True}`; one `trace` entry.
  **Runs only if the graph routes to it** — that's the "otherwise ignore
  it" for free. `validate_flow.py` learns the type (not added to
  `EXPECTED_TYPES`).
- **`h_draft`** folds `state["internal_kb"]` into the prompt above the
  public context, labelled `# Internal runbook (authoritative)`;
  `groundedness.check` counts internal chunks as valid corpus.
- **API** (`api/kb.py` router, RLS-scoped, `rate_limit(user, "kb_write",
  60)`): `GET/POST /api/kb/collections`, `PATCH/DELETE
  /api/kb/collections/{id}` (soft), `GET/POST
  /api/kb/collections/{id}/entries`, `GET/PATCH/DELETE
  /api/kb/entries/{id}`. Entry write: chunk+embed **inline** under a size
  threshold (~8 KB), else enqueue a `jobs` row `kind='embed_kb_entry'`
  and return 202. `PATCH` re-embeds only if `body_md` hash changed.
  Uploads (`.md`/`.txt` direct; `.pdf` via `pypdf`; `.docx` via
  `python-docx`) convert to markdown server-side, then same path.
  Service-role client does the `zapier_docs`/`doc_chunks` write **after**
  an RLS check that the caller can see the parent collection (mirrors the
  interpreter's scoped-vs-service split).
- **Worker:** `api/worker.py` gains an `embed_kb_entry` handler (reuses
  `kb_common.embed_entry`).
- **Web** (`web/src/kb/`): a **Knowledge** tab beside Flows / Runs.
  `KbCollections` (list + New), `KbCollection` (entries + Add),
  `KbEntryEditor` (title + markdown `<textarea>` + preview + upload
  button; Save / Archive; "embedded ✓ / pending"). `Inspector.tsx`: when
  a `kb_lookup` node is selected, a multi-select of the tenant's
  collections bound to `config.collections`, plus `top_k` / `query` —
  same friendly-form treatment `confidence_gate` gets. `NodeCard` +
  palette register `kb_lookup` with an icon.
- **Seed (`023_seed_kb_checkpoint.sql`):** a `globex-billing-runbook`
  collection + one entry ("Refund approval limits: <$200 auto, $200–2k
  lead, >$2k manager"), and a `kb_lookup` node on the Globex flow's
  billing branch feeding `draft`.
- **Verify:** unit — insert entry → chunks appear under that `source_id`
  only → another tenant's flow can't retrieve them. Integration
  (`test_api.py`) — create collection, add entry, entry becomes
  retrievable via a run through a flow with a `kb_lookup` node,
  cross-tenant `GET` → 404. An `eval/e2e` case that only gets the right
  answer when the internal entry is consulted.

### Phase 15 — Google Drive / Docs connector

- **Migration `024_gdoc_sources.sql`:** `sources.kind` gains
  `'gdoc'`; `sources.config` holds `{doc_id, doc_url, last_modified,
  last_synced_at}`. A `gdoc_sync_state` isn't needed — `config` + the
  `zapier_docs` rows carry it.
- **Per-tenant Google OAuth:** one platform-level Google Cloud OAuth
  client (`GOOGLE_CLIENT_ID/SECRET` in `.env`), scopes
  `drive.readonly` + `documents.readonly`. `GET
  /api/integrations/google/authorize` → consent URL; `GET
  /api/integrations/google/callback` stores the **refresh token** in
  `tenant_integrations (tenant_id, kind='google', secret jsonb)`
  (flagged for Vault encryption — same open debt as the SF creds).
- **Link a doc:** `POST /api/kb/collections/{id}/gdoc {doc_url}` → resolve
  `doc_id`, `files.get` for `modifiedTime` + name, `documents.get` →
  flatten to markdown, `kb_common.embed_entry` with url
  `gdoc://<doc_id>`. One `sources` row of `kind='gdoc'` per linked doc
  (or an entry under an `internal_kb` collection — decide at build; a
  distinct kind keeps "synced, don't hand-edit" obvious).
- **Sync:** `ingestion/sources/gdoc_sync.py --once` — for every active
  `gdoc` source with a tenant Google token, compare `modifiedTime` to
  `config.last_modified`; if newer, re-export + re-embed (replace that
  doc's chunks), bump `config`. Add to `.github/workflows/daily-sync.yml`
  (or its own cron). Token refresh handled by the Google client lib;
  a revoked token → mark the source `status='error'` + surface in the UI.
- **Unlink:** soft-delete — `status='deleted'` on the `sources` row, its
  `doc_chunks` dropped, `zapier_docs` row kept as `status='deleted'`
  (the `missed_runs` / soft-delete rule).
- **Web:** in `KbCollection`, a "Link Google Doc" action (disabled until
  the tenant has connected Google, with a "Connect Google" button that
  runs the OAuth popup). Linked docs show a 🔗 + "synced 5 min ago",
  read-only body.
- **Verify:** connect a test Google account, link a doc, edit the doc,
  run `gdoc_sync.py --once` → the collection's retrieved text changes;
  unlink → chunks gone, flow falls back to the next source.

### Phase 16 — Structured policy rules + internal task actions

- **Migration `025_policy_rules.sql`:** `policy_rules` (`rule_id`,
  `tenant_id`, `team`, `name`, `priority int`, `when jsonb`, `then jsonb`,
  `status`, audit cols). RLS via `tenant_members`. `when` is a **JSON
  predicate tree** (`{all|any: [...]}` of `{field, op, value}` over
  `state` paths like `entities.report_period`, `classification.topic`,
  `tier`) — evaluated by a small safe evaluator in
  `interpreter/policy.py`, **never** `eval`/code strings from the UI.
  `then` is `{type: "route", action: "ask_human"|"auto_approve"}` **or**
  `{type: "task", task: "github_issue", repo, labels, assignees,
  approver: {slack_channel|slack_user}, title_tmpl, body_tmpl}`.
- **`extract` node** (`@register("extract")`): config `fields` (name →
  description); one LLM call pulls them from the case into
  `state["entities"]`. Cheap model (`FAST_MODEL`), JSON, stub-safe.
  Placed before `policy_gate` when a rule needs a derived value like a
  fiscal year.
- **`policy_gate` node** (`@register("policy_gate")`): loads active
  `policy_rules` for `(tenant_id, team)`, evaluates `when` in `priority`
  order, applies the first match. `type:"route"` sets
  `state["policy_action"]` → an edge `condition.if =
  "policy_gate.route == 'ask_human'"` diverts the flow (deterministic —
  independent of `draft_confidence`; this is the teeth the Phase 7 e2e
  misses showed were missing). No match → pass through unchanged.
- **`task_dispatch` node** (`@register("task_dispatch")`): for a matched
  `type:"task"` rule, insert an **`action_requests`** row (`id`,
  `run_id`, `tenant_id`, `kind`, `payload jsonb`, `status`
  `pending|approved|rejected|expired|done`, `slack_channel`, `slack_ts`,
  `decided_by`, `decided_at`; unique `(run_id, kind)` = idempotent) and
  post a Slack message with **Approve / Reject** buttons to the rule's
  approver. The flow ends on a `dispatched` outcome; the external effect
  is **not** done inline.
- **Slack app (platform-level, multi-workspace):** `SLACK_CLIENT_ID/
  SECRET/SIGNING_SECRET` in `.env`. `GET
  /api/integrations/slack/authorize` + `/callback` → per-tenant bot token
  in `tenant_integrations (kind='slack')`. One public endpoint `POST
  /api/integrations/slack/interactions` — verify Slack's signing
  secret, match the `action_requests` row, set `approved`/`rejected` +
  `decided_by`, and on approve enqueue `jobs kind='create_github_issue'`.
  A cron expires `pending` rows older than `APPROVAL_TTL_H` (default 24)
  and edits the Slack message to say so.
- **GitHub integration** (`interpreter/github.py`, `client_for(tenant_id)`
  pattern): a per-tenant token (`tenant_integrations kind='github'`, or a
  shared `GITHUB_TOKEN` fallback). Worker `create_github_issue` handler —
  `POST /repos/{repo}/issues` with `title`/`body`/`labels`/`assignees`
  from `payload`, then edits the Slack message to "✅ opened
  {owner/repo}#{n}" and writes the issue URL onto the `runs` row.
  Idempotent on `action_requests.id`.
- **Web:** a **Rules** tab (per team): list rules by priority, a
  form-based editor for `when` (field/op/value rows, all/any groups) and
  `then` (route vs task, with a repo/label/approver picker for task). A
  read-only **Approvals** panel showing recent `action_requests` +
  status. `Inspector` gains `policy_gate` / `task_dispatch` / `extract`
  forms; palette + `NodeCard` register them.
- **Seed (`026_seed_policy_demo.sql`):** a Globex rule — `when
  entities.report_period older than 2 years`, `then task github_issue`
  repo `globex/support-ops`, approver `#support-leads` — plus an
  `extract` + `policy_gate` + `task_dispatch` on the Globex flow's
  data-export branch.
- **Verify:** unit — the JSON-predicate evaluator (all/any/ops, missing
  field = no match, no code execution). Integration — a case matching a
  `route` rule escalates regardless of a high stubbed `draft_confidence`;
  a case matching a `task` rule creates a `pending` `action_requests` +
  (stubbed Slack) message; simulate the Slack "approve" callback →
  `jobs` row → (stubbed GitHub) issue + Slack edit; the same run
  approved twice = one issue.

## Immediate next step

**2026-09-05 — Phase 29 CLOSED and live-verified: step 5 (autonomous
reasoning-session continuation) done + confirmed end to end against real
Groq/Supabase/Slack, all 5 steps complete. This is the most recent work in
this file (see the top-of-file note: this doc is edited in place, not
strictly appended to — check dates, not physical position).** New
`interpreter/reasoning.autonomous_continue()` gives the bot one bounded,
tool-calling shot at closing a stalled `clarifying` Slack dialogue's still-
open critical pointers itself — reusing `complete_with_tools` (step 1) +
`hybrid_retrieve` exactly like `h_agent` (step 2) rather than a
reimplemented ReAct loop — before `sweeps.reasoning_ttl` escalates +
abandons a session the human agent stopped replying to. Grounded-only (a
pointer is marked answered only off documentation a real `search_kb` call
returned, never an ungrounded guess, so the offline stub path never
resolves anything, deterministically); still requires an explicit human
`send` before anything reaches the customer — this unsticks the dialogue,
not the approval gate, same "never act silently" discipline as step 4's
self-critique. Wired into `reasoning_ttl` for `clarifying` sessions only
(`awaiting_handoff` = nobody ever engaged, still escalates as before;
`awaiting_approval` already has a draft awaiting explicit approval — not
touched). 661 offline tests green (8 new: 6 in `tests/test_reasoning.py`,
2 in `tests/test_sweeps.py`).

**Live-verified same day, two parts (see full writeup under "Phase 29 —
Agentic AI" below for the complete detail):** (A) the mechanism alone —
real Groq tool-calling (2 iterations), real KB retrieval, a correctly
grounded answer, and a non-critical pointer correctly left untouched, all
with zero side effects. (B) the full sweep wiring — a real backdated
`reasoning_sessions` row + a real Slack thread, `reasoning_ttl` run for
real, the session resolved to `awaiting_approval` with a real draft, a
real `case_events` row landed, and the actual Slack reply was confirmed
via `conversations.replies`. All test artifacts (DB rows, Slack messages)
deleted afterward — the live DB is back to zero non-terminal
`reasoning_sessions` rows. One non-blocking friction item found along the
way: Acme's `tenant_integrations` Slack row shows `status: "inactive"`
despite the bot token being live and fully functional — a stale display
flag, not a functional gap; not chased further this session.

**Phase 29 status: all 5 steps done, all 5 live-verified — the whole
track is closed.** Next real work is picking a fresh chunk — see the
"what's next" discussion from this session (a second real connector
beyond Salesforce/Slack to prove FR-47 generalizes, Playwright e2e on the
web editor, or wiring `eval/e2e`/`sop_conflicts.py` into CI — none started
yet, nothing committed to).

**Old step-4 note, superseded by the above as "most recent," kept for its
own history:** self-critique wired into KIL's
`draft_change`, live. `interpreter/kb_writeback.py`'s
`_self_critique()` runs the exact same `integrity.check(statement,
[new_body])` shape `eval/writeback/run_writeback_eval.py` already used to
*grade* drafts post-hoc — now it runs live, inside `draft_change` itself,
with one bounded LLM-only retry when the confirmed statement still
`contradicts` the drafted body. The verdict rides on the `change` dict and
`_post_card` warns the Slack approval card whenever it isn't a clean
`entails`. 653 offline tests green (7 new). Real before/after via the
*existing* eval, unmodified: resolution 1.000 (7/7), lift +0.611 — matches
the pre-existing baseline within noise, an honest ceiling-effect result
(this eval set has no first-draft failures for the retry to catch) — see
the full Phase 29 step 4 writeup below for why the new unit tests, not
this eval, are what actually prove the retry mechanism works. Phase 29:
steps 1-4 done, 5 (autonomous reasoning-session continuation) not started.

**Old step-3 note, superseded by the above as "most recent," kept for its
own history:** the agent-vs-baseline eval, real number in hand. New
`eval/agent/run_agent_eval.py`
compares Acme's real live `agent` node against a single `h_retrieve()`
call on the 10-question hard qrels set, reusing `run_eval.py`'s own
`score()` rather than reimplementing it. **Real result:** baseline and
agent scored identically (hit@1 0.200, MRR@10 0.240) — only 1/10 questions
reformulated, and that one didn't change the outcome. This ran during the
same sustained Groq/OpenRouter rate-limiting seen all session (weak
fallback model doing most of the groundedness scoring that decides
whether to reformulate) — a genuine caveat, not a reason to dismiss the
result. Net: **step 3 is answered (a null result under real conditions),
not "proven agent helps"** — see the full writeup under "Phase 29 —
Agentic AI" below for the two caveats and what would be needed for a
cleaner signal. Phase 29: steps 1-3 done, 4-5 not started. No production
code changed — purely a new eval script, 646 offline tests still green
(unaffected).

**Old test-coverage note, superseded by the above as "most recent," kept
for its own history:** the three "live-verified once" spots (Slack
approval buttons, GitHub issue creation, KIL approval flow) now have real
offline test coverage. This is the most recent work in this
file (see the top-of-file note: this doc is edited in place, not strictly
appended to — check dates, not physical position).** Third of the three
tracks scoped earlier in the day (connector generality → production
hardening → this). Confirmed by grep before writing anything, not assumed:
- `slack_socket.dispatch_action`'s KIL-c review-card branches
  (`review_correct/wrong/dismiss`) and the KIL-d/Phase-16 approval-card
  branches (`kb_approve/kb_reject/approve/reject`) had zero coverage —
  `test_slack_socket.py` only exercised the reasoning-session action
  branches (`cx_send`/`cx_edit`/...). Added 8 tests.
- `api/worker.py`'s `HANDLERS` dict dispatches `_create_github_issue` and
  `_apply_kb_change` for real jobs, but only the *library* functions they
  call (`github.create_issue`, `kb_writeback.apply_kb_change`) had
  coverage — the worker-level wrapper (status/idempotency checks, the
  Slack card update, error → `action_requests.status='error'`) did not.
  New `tests/test_worker_job_handlers.py`, 10 tests.
- `interpreter/approvals.py` (P3/FR-44 — the one place `dispatch_action`,
  the signed HTTP Slack callback, and `/api/review-tasks/{id}/resolve` all
  funnel an approval decision through) had zero dedicated tests at all.
  New `tests/test_approvals.py`, 11 tests.
- `interpreter/github.py` itself (token resolution, `create_issue`'s HTTP
  call) also had zero dedicated tests — found while tracing the GitHub
  path, not part of the original 3-item list. New `tests/test_github.py`,
  10 tests.

646 offline tests green (was 607 before this chunk). Purely additive —
no production code paths changed, so no live-verification step was needed
this time (unlike the previous two chunks).

**Old production-hardening note, superseded by the above as "most
recent," kept for its own history:** every tenant secret is now
Vault-encrypted, not plaintext. Picked as the next
track after connector generality. Two of the four candidate items turned
out already done: `interpreter/jobs.py::fail()` already has exponential
backoff (earlier robustness pass); a real `/security-review` already ran
2026-09-03 (see "Known issues" below) — a stale leftover note elsewhere in
this file claimed otherwise, fixed in place. The shared-LLM-quota issue
stays a documented, deliberate billing tradeoff, not a code defect.

That left one real, confirmed gap: migration `035` (Phase 20a) built
Vault-backed `integration_secret_put/get/delete()` SQL functions and
`interpreter/mailbox.py` already used them correctly for the email
channel — but its own comment said *"a later phase can migrate the Slack /
SF / Google rows onto the same mechanism"* and that phase never happened.
`interpreter/salesforce.py` (`client_for`/`save_tenant_org`), `slack.py`
(`_bot_token`), `gdrive.py` (`_integration`), `llm.py` (`_tenant_keys`,
BYOK), and `github.py` (`token_for`, found in a final sweep, not part of
the original 4) all read/wrote `tenant_integrations.secret` in the clear —
real Salesforce JWT keys / OAuth refresh tokens, Slack bot tokens, Google
refresh tokens, tenants' own pasted LLM keys, GitHub PATs.

Fixed: new `interpreter/vault_secrets.py` (a thin `get`/`put`/`delete`
wrapper generalizing the exact pattern `mailbox.py` already used inline)
+ every one of those 5 modules' read/write call sites switched to it.
`tenant_integrations.secret` now holds only non-sensitive display fields
(username, domain, workspace name, `has_credentials`) — never the real
value. Salesforce is per-org, so it namespaces its Vault kind as
`f"salesforce:{org_label}"`, giving each connected org its own entry under
the existing `(tenant_id, kind, org_label)` PK (migration `082`) with no
schema change needed. **A real correctness bug found and fixed along the
way, not hypothetical:** `GET /api/integrations/salesforce` was calling
`redact_org_secret()` a second time on data that's now already redacted —
recomputing `has_credentials` from a dict with no secret keys left, which
would have silently reported `False` for a fully connected org. Fixed by
not re-redacting already-safe data.

The 5 live rows in `tenant_integrations` were backfilled via a new
`scripts/backfill_vault_secrets.py` (idempotent, `--dry-run` first) —
**verified live**, not just offline: a direct `salesforce.list_queues()`
call against the real org after the backfill returned all 12 real queues,
proving `client_for` correctly resolves Vault-sourced creds, and
`tests/test_llm_byok.py -m integration` (a real round trip through the
API) passed unmodified. 607 offline tests green (was 598) — 9 new in
`tests/test_vault_secrets.py`; `test_salesforce_multi_org.py`'s and
`test_slack_introspection.py`'s fake-Supabase fixtures gained an `.rpc()`
method to keep exercising the real vault-backed path instead of a stale
direct-table-read shape.

**Old connector-generality note, superseded by the above as "most
recent," kept for its own history:** A gap audit found `docs/REQUIREMENTS.md`'s FR-47 ("connectors are data, not a
hardcoded node handler") had been marked "built" without actually being
true — no `ConnectorSpec`/registry existed anywhere, and 9 of 26
`registry.py` node types were (and still are) hardwired straight to
Salesforce. Built the real thing: `interpreter/connectors.py`
(`ConnectorSpec`/`ActionSpec` registry) + a generic `connector_action`
node, with `salesforce`/`slack` builtins (thin wrappers, `salesforce.py`/
`slack.py` unmodified) and, more importantly, **user-definable named
actions on a tenant's own HTTP connection** (migration `083`,
`connection_actions`) — so a brand-new third-party API (Zendesk, Jira,
anything REST) is addable as a first-class connector from the web UI with
**zero Python changes**. `http_request`'s request logic was extracted
into `connections.execute()`, shared by both paths — its existing 8 tests
pass unmodified, no behavior change. Web: one generic `ConnectorActionForm`
(connector → action → dynamically-rendered params) plus a "manage
actions" panel on the Connections tab, instead of a new bespoke Inspector
form per vendor. 600 offline tests green (13 new in `tests/test_connectors.py`),
`scripts/verify_migrations.py` clean, `web`'s `tsc -b && vite build` clean.
See FR-47 in `docs/REQUIREMENTS.md` for the full detail.

**Follow-on completed the same day (2026-09-04):** 7 of the 9 originally
SF-hardwired node handlers (`sf_writeback`, `sf_case`, `notify`,
`ask_human`, `handover`, `identify`, `clarify`) plus `alert.alert_human`
(behind `notify_human`) were migrated to call Salesforce/Slack through
`connectors.invoke(tenant_id, "salesforce"|"slack", "<action>", params)`
instead of importing `salesforce.py`/`slack.py` directly — added 4 new
Salesforce actions (`ensure_case`, `log_email_message`, `identify_sender`,
`send_case_reply`) and widened Slack's `post_message` action (`webhook`/
`blocks`) to cover every call site. **Zero behavior change, proven, not
assumed:** `salesforce.py`/`slack.py` themselves are untouched, and every
existing test that monkeypatches `salesforce.<verb>`/`slack.<verb>`
directly — dozens of them, across `test_sf_case.py`, `test_notify_and_type.py`,
`test_case_control_plane.py`, `test_resilience.py`, `test_salesforce_multi_org.py`,
and more — still passes unmodified, since the monkeypatch still lands on
the same underlying function one indirection layer down. A new
`tests/test_sf_handlers_use_connectors.py` (11 tests) additionally asserts
the *connector/action name* each handler now uses, so a future accidental
revert to a direct call would be caught even if behavior happened to still
look right. 598 offline tests green (was 587); `scripts/verify_migrations.py`
clean (no schema change this round); `web`'s build unaffected (backend-only).

**`sf_context` is a deliberate, documented exception** — see its own
module docstring. It's a bespoke, `want`-driven fan-out of several SOQL
reads (Account hierarchy, Contacts, Leads, Case history, team) into one
nested result, not a single named write action with a flat params dict;
forcing it into the connector-action shape would lose that flexibility or
need one action per read, neither of which is what a connector *action* is
for. Left as a normal internal helper — this is a considered judgment call,
not an oversight, so don't "finish" it later without re-examining whether
that's actually the right shape.

**Self-serve multi-tenant/multi-org Salesforce + Slack connector, and a
flow editor that fetches real data instead of hardcoding it — DONE and
browser-verified (2026-09-03).** See "Multi-tenant / multi-Salesforce-org
scoping" below for the full chunk-by-chunk history. Connector +
introspection + OAuth + org-label threading through every SF-touching
node handler; every flow editor node whose config is genuinely backed
by an external system (Salesforce Case fields/Queues/Users/connected-orgs,
Slack channels/users/usergroups, per-tenant HTTP Connections, internal
KB collections) renders a real picker instead of raw JSON, including
edge conditions and Salesforce User/Queue @mention targets. **Actually
clicked through in a real browser** (headless Chromium set up from
scratch in this sandbox — see "First real browser click-through" below
for how, since there's no browser here by default) — found and fixed 4
real bugs in the process (a Slack-meta endpoint that always reported
"not connected" for every tenant due to an RLS-vs-service-role client
mismatch; an edge-condition quick-insert that mashed text together with
no separator; `sf_writeback`'s form going blank on a real published
flow whose config relies on the interpreter's own defaults; a `notify`
node's `target_by_module`/`mention_id` fields that had real saved data
but no picker at all). This whole thread is closed.

**Current focus (2026-09-03): "complete the multi-tenant project
end-to-end, very robust, flexible and easy"** — the user's own framing,
deliberately broad; scoped down via `AskUserQuestion` into three
tracks, of which the browser click-through above was the first and the
robustness pass (below) was the second.
**Systematic robustness pass — DONE (2026-09-04).** Error-handling/retry
audit across node handlers + external connectors, plus a real
multi-tenant concurrency stress test. Three parts, all on branch
`browser-verified-picker-fixes`:
- Part 1 (`cddb5e5`) — the `available()` self-serve-tenant gating bug
  present in every SF write/read path, plus a CI-breaking upsert bug
  found along the way.
- Part 2 (`e3cdc64`) — job retry backoff + failed-job visibility.
- Part 3 (`a14137c`) — the `available()` bug reached 5 more call sites
  beyond `salesforce.py` (routing.py, sf_context.py, attachments.py,
  worker.py), plus a real cross-tenant cache leak: `_intake_queue_id`
  and `routing.py`'s `queue_member` cache keyed only on queue
  name/org_label, not `tenant_id` — two tenants sharing an underlying
  SF org (true for today's two demo tenants) would silently get each
  other's cached Group id. Both caches now key on `(tenant_id,
  org_label)`.
- `tests/test_multitenant_concurrency.py` (added 2026-09-04) —
  genuinely interleaved concurrent flow runs for two tenants via a
  thread pool (not sequential), proving no cross-tenant config/cache
  leak under real concurrency, the exact condition the part-3 bugs
  needed to surface. `pytest tests/test_multitenant_concurrency.py -m
  integration`: 2 passed against live Salesforce/Supabase.

**Third track — guided onboarding + self-explanatory editor + LLM BYOK,
DONE (2026-09-04).** The user restated the same broad goal even more
concretely ("easy onboarding with Salesforce, Slack, OpenRouter, Claude
— given token choose model — easy editor design, make the platform
understandable"); scoped via `AskUserQuestion` into three ordered
chunks, built in that order:

- **Guided setup wizard** — `web/src/onboarding/OnboardingWizard.tsx`,
  a new always-visible "⚙ Setup" nav item. Four skippable steps
  (Connect Salesforce / Connect Slack / choose an AI model / create a
  first flow from a template) that reuse the existing OAuth-connect
  logic rather than duplicating it; auto-shows once for a brand-new
  tenant (localStorage-tracked per tenant, not per-session) since
  previously "Connect Salesforce" and Slack connect each lived in a
  separate tab (Connections, buried under Admin; Slack connect was
  oddly inside the Rules tab) with nothing walking a new user through
  either.
- **Self-explanatory editor** — every one of the 26 registered node
  types (`interpreter/registry.py`) now gets a one/two-sentence purpose
  blurb in the Inspector (`NODE_HELP` map, `web/src/flows/Inspector.tsx`),
  sourced from the actual handler docstrings, not guessed. Particularly
  closes the confusion between the four similar-sounding "get a human
  involved" nodes (`notify` doesn't reassign the Case; `ask_human`
  escalates + pauses for a reply; `handover` is terminal; `notify_human`
  is the actual Slack/Chatter delivery mechanism for the other three).
- **LLM BYOK** — a tenant can now paste their own Groq/Anthropic/
  OpenRouter key (Admin → Connections → "AI models" panel) instead of
  sharing this deployment's own; the `ai_prompt` node's `model` field is
  a real dropdown (grouped by provider, `GET /api/models`) instead of
  free text. Backend: `interpreter/llm.py`'s `complete()` /
  `complete_with_tools()` and every internal dispatch/chain function now
  take an optional `tenant_id`, resolving that tenant's own key with a
  fallback to this process's env key — per-key client caching (not
  per-tenant; the common case is many tenants sharing the platform's one
  key) mirrors `salesforce.py`'s `client_for` safety pattern from the
  robustness pass. Reuses the existing `tenant_integrations` table
  (kind='llm') — no migration. Threaded `tenant_id` through all 12 real
  `llm.complete*` call sites across `registry.py` + 5 other files;
  `reasoning.py`'s pluggable `LLMFn` interface was deliberately left on
  the platform default (a disclosed, narrow gap — that one node type
  doesn't honor a tenant's own key yet — rather than risk breaking its
  test-double contract for a lower-traffic path).

Live-verified, not just offline-tested: `tests/test_llm_byok.py` (new,
`-m integration`) round-trips a real key through the real
`tenant_integrations` row against the live Globex tenant via the actual
FastAPI app — 3/3 passed. Full offline suite 574/574 after fixing the
19 test doubles the new `tenant_id`/`api_key` kwargs broke (narrower
monkeypatched signatures needed `**kw`). `tests/test_multiflow.py`
re-run live after the refactor: 4/4 (one transient failure on the first
pass was pre-existing live-LLM-call flakiness — Groq's retired
`llama-3.3-70b-versatile` 404'd, OpenRouter's free tier also errored
that attempt, landing on a fallback model whose confidence just missed
the auto-reply threshold that one run — confirmed by an isolated re-run
passing cleanly, not a regression from this change). `web/`: `tsc -b`,
`vite build`, `vitest run` all clean.

Also fixed along the way, from live user feedback on the running app:
the Triggers panel's webhook URL wasn't actually ellipsizing (`<code>`
is inline; `text-overflow: ellipsis` needs `display: block/inline-block`
to do anything) so a real URL spanned half the page — fixed, plus made
the panel collapsible with a purpose blurb; the sidebar nav's 12 items
were jammed into one unwrapped horizontal row inside the 240px sidebar —
regrouped into collapsible categories (Build / Knowledge / Admin). A
user-reported "I can see another account's Slack connection" turned out
not to be a cross-tenant leak — traced live against the DB (RLS policy,
`_caller_tenant`, and the Slack-status/meta endpoints all independently
verified correctly scoped) to the same real account owning a second,
unnamed leftover demo tenant from earlier test sessions that already had
a real Slack connection from 2026-08-29 — fixed by giving that tenant a
real name ("Acme (demo)") instead of the confusing `workspace 00000000`
label, not by touching any access-control code.

**Not yet started, still open**:
- **An onboarding UX walkthrough** — actually drive the new-tenant path
  (create workspace → connect Salesforce/Slack → auto-detect modules/
  teams → build first flow) as a brand-new user would, and fix
  friction. The wizard above makes this materially easier but hasn't
  itself been driven end-to-end by a fresh browser session — still the
  next real-world test of it.

Deferred, **explicitly out of scope until asked for again**: Google
Calendar meeting-scheduling as its own node type (design decided — time
from BOTH the customer's message AND Google Calendar free/busy; needs a
new `interpreter/gcalendar.py` + widening `gdrive.py`'s OAuth scopes,
with a reconnect caveat). Also not started: Phase 16's rule form
builder (policy rules still edited as raw JSON) and Phase 14's file
upload (`.pdf`/`.docx`) for the KB — raised as options in the same
`AskUserQuestion` round but not picked this time.

**Phase KIL — Knowledge Integrity Loop (COMPLETE a–f + live-verified,
2026-09-02; PR #29).** Catch
new info that contradicts the KB or case history (inbound tickets, bot
drafts, human replies), send it to a manager, and fold confirmed
corrections back into the KB under one-click approval. Plan artifact:
`https://claude.ai/code/artifact/0ee3262d-4eaf-4669-bf3e-a16d8e9d3dff`;
requirements FR-33…FR-39 in `docs/REQUIREMENTS.md`. Decisions locked:
post-send review + 5% sampling (not a gate); after handover flag-to-manager
only; LLM-drafted KB diff the manager one-click approves; NLI judge over
retrieved passages (claim graph deferred to KIL-g). Manager = the routed-team
`@cx-*-oncall` usergroups.

- **KIL-a — built + live-backfilled (2026-09-02).** `ingestion/case_graph_sync.py`
  syncs the Salesforce Case *lifecycle* (any status, not just closed) into
  Neo4j: one `(:Case)` + one `(:Message)` per turn (description · in/outbound
  `EmailMessage` · `CaseComment` — a `[bot draft…]` comment → `role='draft'`
  `author_kind='bot'` · Chatter `FeedItem`). `case_memory.sync_case_lifecycle()`
  holds the Cypher; `Message.id` / `Account.sf_id` constraints added.
  Resumable via **`graph_sync_state`** (migration `064`, RLS like `system_health`).
  Idempotent; Neo4j/SF down → exit 0. Live backfill: **92 dummy Cases → 174
  Messages** (108 inbound / 42 draft / 24 agent_reply), `tenant_id` on every
  node, 14 Cases with a full `draft → agent_reply` pair. 8 offline tests
  (332 total). The vector side stays with `case_memory_sync --from-salesforce`.
- **KIL-b — built (2026-09-02).** `interpreter/integrity.py` `check(statement,
  contexts, *, kind) → {relation, flagged, novel, verdicts, backend}` — a Groq
  NLI judge (deterministic negation-mismatch heuristic without a key).
  Wired into `h_draft` (checks the draft + the inbound customer text vs. prior
  resolutions + internal KB + retrieved docs → `state.integrity`); `h_draft`
  trace shows `integrity=<relation>`. `h_confidence_gate` gains
  `escalate_on_integrity_conflict` (default true): a draft that `contradicts`
  established knowledge → forced escalation. `state.py` + `builder._context`
  gain `integrity`. Eval `eval/integrity/` (24 labelled cases): real-Groq
  **acc 0.958 · flag precision 1.000 · recall 1.000** — clears the ≥ 0.80 bar
  for KIL-c. 12 offline tests (344 total).
- **KIL-c — built (2026-09-02).** Migration `065`: **`review_tasks`** (RLS
  read like `action_requests`, unique `(run_id, kind)`). `interpreter/review.py`
  `judge_human_reply()` — after a human reply is *sent*, runs the KIL-b judge
  on it against the run's KB + case-history context; `contradicts`/`novel` →
  a `human_reply_review` task, else `REVIEW_SAMPLE_RATE` (5%) → a `sample`
  task, and posts a Block-Kit card (Correct → update KB / Wrong → coach /
  Not a conflict) to the routed-team `#cx-*` channel + manager usergroup.
  Hooked into `slack_socket._deliver` + `worker._check_resolution` (both
  best-effort). `slack_socket.dispatch_action` resolves the `review_*` clicks.
  8 offline tests (352 total).
- **KIL-d — built (2026-09-02).** Migration `066` (`kb_entries.{source_review_task,
  supersedes_entry_id, approved_by, provisional_until}` + `provisional`/
  `superseded` statuses). `interpreter/kb_writeback.py`: `draft_change` (LLM /
  fallback) proposes a `kb_entries` create-or-supersede; the review card's
  **Correct** button raises an `action_requests(kind='kb_change')` + an
  Approve/Reject card; `kb_approve` → worker `_apply_kb_change` →
  `kb_writeback.apply_kb_change` writes the entry **`provisional`**
  (`origin='review_writeback'`, `provisional_until = now+7d`), supersedes +
  de-indexes the old one, enqueues `embed_kb_entry`, MERGEs
  `(:KBArticle)-[:SUPERSEDES]->`. `promote_provisional()` ages provisional →
  active. Web Review tab + REST still pending. 8 offline tests (360 total).
- **KIL-e — built (2026-09-02).** Migration `067` (`handoff_watch_state` —
  cursor + rate-limit/dedup, RLS like `system_health`).
  `interpreter/handoff_watch.py` `watch_case()` — for an escalated Case, runs
  `integrity.check` on every message newer than `last_seen_ts` against the
  run's KB + case-history context, and (LLM-gated) checks the `pointer_bank`
  for still-open critical questions; a hit posts one flag to the reasoning
  thread / routed-team channel, capped at `HANDOFF_MAX_FLAGS` (3) and deduped
  by signature. **Never contacts the customer.** New `sweeps.handoff_watch`
  in the `api.worker` loop (every 5 min, self-re-enqueue, `SWEEP_DRY_RUN`).
  6 offline tests (366 total).
- **KIL-f — built (2026-09-02).** `interpreter/kil_metrics.py` `compute()` —
  flag precision / false-flag rate / agent-correction rate / median
  time-to-review from `review_tasks`, the KB-writeback funnel + promotion
  rate, knowledge freshness, weekly contradiction count. API `GET
  /api/review-tasks`, `POST /api/review-tasks/{id}/resolve` (→
  `kb_writeback` on `correct`), `GET /api/kil/metrics`. Web **Review** tab
  (`web/src/review/ReviewView.tsx`) — tiles + the queue with Correct / Wrong
  / Not-a-conflict. `promote_provisional` now holds an entry while an open
  contradiction still references it (the poisoning guard); a `kb_promote`
  sweep runs it every 6 h. `sop_conflicts`-as-a-sweep is a noted follow-on.
  3 offline tests + web build green (369 total).

**Phase KIL COMPLETE (a–f, 2026-09-02); g (atomic claim graph) deferred.**
The loop runs end to end — contradiction caught → gate escalates or a review
card is raised → manager confirms → LLM-drafted KB diff approved → worker
writes a `provisional` entry (superseding the wrong one) → auto-promotes
after 7 days unless still disputed. **Live-verified 2026-09-02** — smoke
drove b→c→d→f against real Supabase / Groq / Slack (`review_tasks` row +
`#cx-l1` card → LLM diff → `action_requests` → `apply` → `provisional`
`kb_entries` row → metrics); 4 bugs found + fixed (partial unique index vs.
PostgREST upsert; a bad `sources` column; non-uuid supersede id; a watcher
context selecting a missing column). PR **#29** (`phase-kil` → `main`).

---

## Roadmap — post-KIL hardening → platform (2026-09-02)

From an end-of-KIL review. **Correctness before features → CI safety net
before the invasive refactor → consolidate before building on top → the
generic `RunContext` unlock before triggers / connectors / onboarding.**
Requirements FR-40…FR-49 in `docs/REQUIREMENTS.md`. Do these in order; each
is a verifiable chunk.

| # | Phase | Chunk | Depends on |
|---|---|---|---|
| **P1** ✅ | Correctness (done 2026-09-02, branch `phase-p1`) | **1a** `case_graph_sync` + `case_memory_sync` are now `api.worker` sweeps (every 60 min, self-re-enqueue) + steps in `daily-sync.yml` · **1b** migration `068`: `doc_chunks.entry_status`; the 3 `match_doc_chunks*` fns drop `superseded` chunks + return `entry_status`; `embed_entry(status=)` stamps it; `h_draft` puts `provisional` context **behind** confirmed in a labelled "UNVERIFIED" block; `promote_provisional` flips the chunks too · **1c** migration `069`: `graph_sync_state` / `handoff_watch_state` reads scoped to `tenant_members`; `handoff_watch.watch_case` derives `tenant_id` from the Case's latest run instead of hardcoding. 371 offline tests; migrations 068/069 applied; live retrieval verified. |
| **P2** ✅ | CI safety net (done 2026-09-02) | **2a** `scripts/verify_migrations.py` — parses CREATE TABLE / ADD COLUMN / CREATE FUNCTION / INDEX out of `db/migrations/*.sql` and diffs against the live schema via a new `introspect_schema()` RPC (migration `070`); flags missing objects **and** live tables no migration creates. In `ci.yml`. · **2b** a new `integration` CI job runs `pytest -m integration` (self-skips without secrets) + `tests/test_kil_integration.py` — 3 real-DB tests covering the `on_conflict` idempotency, the KIL writers' columns, and the RPC; 3 of the 4 KIL smoke bugs would fail here. 375 offline + 3 integration tests; live drift check: clean. |
| **P3** | Quality pass | `/simplify` + `/code-review` over the KIL diff + sweep/worker layer; consolidate the **3 approval code paths** into one `interpreter/approvals.py`; dedupe `case_graph_sync` ↔ `case_memory_sync`; one shared `tests/fakes.py` for the recording-fake `_SB` | P2 |
| **P4** ✅ | Unified approvals (done 2026-09-02) | `interpreter/approvals.py` (P3) + `GET /api/approvals` (open `review_tasks` + pending `action_requests`, RLS-scoped) + `POST /api/approvals/action-requests/{id}` (`approve`/`reject` via REST → `decide_action_request` + Slack card edit). The web **Approvals** tab (renamed from Review) adds an *Awaiting approval* section — KB-change diff / task payload with Approve & publish / Reject — above the flagged-replies queue. A manager never has to be in Slack. |
| **P5** ✅ | Structural unlock (**COMPLETE 2026-09-02**) | **P5a** — `CaseState.context` (a declared, merge-friendly `operator.or_` bag); `builder.initial_state(flow, case=, context=)` used by all 3 invoke sites (`run.py` / `worker` / `api`); `builder._context` exposes `context` + `input` (`context or case`) so an edge condition is transport-neutral; `RunIn.context`. A no-Case flow now runs: payload survives the graph, nodes read/write `state['context']`, edges branch on `input.*`. **P5b** ✅ — `interpreter/triggers.py` (`webhook_context` / `schedule_context`) + a `trigger` entry node (`map` / `defaults` / `required` → `context._missing`); `worker._run_flow` + `POST /api/triggers/{flow_id}` thread a generic `context`. A webhook-driven Case-less flow runs end to end. **P5c** ✅ — `registry._run_text(state)` — `retrieve`/`classify`/`draft` build their prompt from the Case, else `context.subject`/`title` + `context.body`/`text`/`query`/`message`/`question`, else a field dump. **P5d** ✅ — `runs.build_row` records the `context` payload (minus `_`-fields) as `case_payload` for a Case-less run. **P5 COMPLETE** (a–d, 2026-09-02); a flow can now run entirely on a webhook payload. | P2 |
| **P6** ✅ | Triggers + connectors (**COMPLETE 2026-09-02**) | **P6a** — migration `071` `flow_triggers`; public `POST /t/<token>` (token = credential) → `context` run; `GET/POST/DELETE /api/flows/{id}/triggers`; FlowEditor triggers strip. Live-verified. **P6b** ✅ — `interpreter/cron.py` (5-field matcher, no dep) + `sweeps.fire_schedules` (every 1 min): each due `kind='schedule'` trigger → a `run_flow` job (deduped per trigger-minute), `last_fired_at`/`fire_count` bumped; 60-min lookback cap so a restart fires once, not a flood. **P6c** 🔶 — migration `072` `connections` (per-tenant `{base_url, auth}`, secret service-role-only); `interpreter/connections.py` (`resolve`/`auth_headers`/`redact`); `registry.h_http_request` — call any API under a named connection's base_url, `{{context.x}}` templating, absolute-URL rejection, result → `context[out_key]`; `GET/POST/DELETE /api/connections` (redacted read). `transform` node (`map` by dotted path · `set` templated · `drop` · `into`); a **Connections** web tab (owner-only) to add/list/delete. **P6c COMPLETE.** | P5 |
| **P7** ✅ | Self-serve onboarding (**COMPLETE 2026-09-02**) | **P7a** `interpreter/templates.py` + 4 `interpreter/flows/templates/*.json` (support-autoreply / triage-and-route / draft-then-approve-in-slack / webhook-rag-qa); `GET /api/templates` + `/api/templates/{id}` (candidate graph); FlowList "📋 From template…" picker. **P7b** ✅ file-upload KB ingestion — `interpreter/fileimport.py` (pdf via pypdf, docx via python-docx, md/txt/csv/json direct; 8 MB / 200k-char caps); `POST /api/kb/collections/{sid}/upload` (base64 body, no multipart dep) → a `kb_entries` row (`origin='file'`) → embedded; KnowledgeView '⬆ upload file' button. · **P7c** ✅ "crawl this URL" — `ingestion/webcrawl.py` (BFS, same host + path prefix, `max_pages`/`max_depth` caps, SSRF host block, robots.txt, headings→markdown); `POST /api/kb/collections/{sid}/crawl` → a `crawl_site` worker job that lands each page as a `kb_entries` row (`origin='crawl'`, dedup by title) + enqueues one `embed_kb_entry` per page; KnowledgeView '🌐 crawl a site'. · **P7d** ✅ self-serve workspace — migration `073` `tenants` (named); `POST /api/tenants` (creates the workspace + owner row); the no-workspace screen becomes a **Create a workspace** form; a fresh workspace shows a 3-step **Get started** checklist in FlowList. **P7 COMPLETE.** | P5, P6 |
| **P8** ✅ | KIL depth (**COMPLETE 2026-09-03**) | **P8a** ✅ KIL learning report — `kil_metrics.digest()` (this-week vs last-week deltas via `compute_window()`, recurring `top_contradictions` from `review_tasks.verdict.salient` count ≥ 2, latest `review_writeback` KB changes) + `render_digest()` (Slack markdown, ▲/▼ arrows); `GET /api/kil/digest` (`?format=md` → text); `sweeps.kil_digest` — a 30-min worker sweep that, once per ISO week per tenant with Slack connected + `tenant_integrations.config.digest.channel` set (opt-in `weekday`/`hour`, default Mon 14:00 UTC), posts the report and stamps `cursor.kil_digest_week`. FR-49. · **P8b** ✅ provisional / superseded KB surfacing — `GET /api/kb/collections/{sid}/entries` returns `status` + `origin` + `provisional_until` + `supersedes_entry_id` + `source_review_task`; `/api/kb/collections` adds `provisional_count`. Web Knowledge tab: a **held: disputed** badge (amber, with the auto-promote date) on `provisional` entries, a dimmed **superseded** badge (hidden behind a "show N retired" toggle), a **from a review** badge on `review_writeback` entries, and a per-collection "N held" pill in the sidebar. FR-49. · **P8c** ✅ KIL observability + eval depth — `scripts/health_check.py` gains a `slackbot`-stale check and `_kil_problems()` (flag precision below `HEALTH_KIL_PRECISION_MIN`=0.5 over ≥8 resolved reviews in 7d; > `HEALTH_KIL_OPEN_MAX`=12 review tasks open past 48h). New evals: `eval/review/` — human-reply flag precision/recall (KIL-c) over 18 labelled replies (real-Groq: acc 0.78, flag P 0.69 / R 1.00 — the human_reply judge over-flags benign action-confirmations; acceptable under the post-send-sample design, not a gate); `eval/writeback/` — does `kb_writeback.draft_change` resolve the contradiction (KIL-d) + a keyword-coverage answer-lift proxy over 12 cases (real-Groq: resolution 1.00, lift +0.63). `tests/test_health_kil.py` + `tests/test_evals_offline.py` guard the plumbing. FR-49. **P8 COMPLETE** (a–c). **KIL-g** (atomic claim graph) stays deferred — it needs the loop to have accumulated real adjudications first. | KIL |
| **P9** ✅ | Usage & billing dashboard (**COMPLETE 2026-09-03**) | A new, self-contained feature (not KIL/hardening depth) — the piece that lets this stop being a demo. Migrations `074` (`runs.tokens_total` / `runs.tokens_by_model`, rolled up at write time in `interpreter/runs.py::build_row` from the trace's per-node `tokens`) and `075` (`tenants.plan`, `'free'`/`'pro'`, admin-set, no payment processing behind it). `interpreter/registry.py`'s three token-tagged trace sites (`h_classify`, `h_draft`, `ai_prompt`) now also record which `model` ran, so a flow mixing the free Groq default with an opt-in paid Anthropic node prices correctly. `interpreter/billing.py` — `estimate_cost_usd()` (illustrative list pricing; Groq/OpenRouter default to \$0), `PLAN_LIMITS` (`free`: 200 runs / 500k tokens per month, `pro`: unlimited), `usage_summary()` (pure, offline-tested). `GET /api/billing/usage?tenant_id=&period=YYYY-MM` — owner-gated like `/api/members`, defaults to the current UTC month. Web **Billing** tab (owner-only, next to Team/Channels/Connections): run/token/cost tiles, a quota bar per limited metric (amber ≥80%, red ≥100%), a plain-CSS daily-tokens bar chart (no new chart dependency), a tokens-by-model breakdown, a month prev/next picker. `tests/test_billing.py` (10 tests, offline). **Residual (not built):** real payment processing (Stripe/invoicing/card capture) — explicitly out of scope, `plan` stays an admin-set label; KIL judge LLM calls (contradiction check, human-reply review, `kb_writeback.draft_change`) run outside `runs` rows and aren't counted in `tokens_total` yet. FR-50. | — |

**Roadmap status: P1–P9 all COMPLETE (2026-09-02/03).** The only deferred
item is **KIL-g** (the atomic claim graph) — it is gated on the loop having
run in production long enough to accumulate real manager adjudications, so
there is nothing to build for it yet. Next real work is live verification:
wire a tenant's `tenant_integrations.config.digest` so the weekly KIL report
posts to Slack, run `eval/review` + `eval/writeback` with `GROQ_API_KEY`
set as a pre-merge check whenever the judge prompts or `draft_change` change,
and apply migrations `074`/`075` on any environment that hasn't yet (this
one already has, via the Supabase MCP).

**Phase 29 — Agentic AI, a 5-step ordered list (infra-first, same
discipline as Phase 28): 1. tool-calling plumbing in `interpreter/llm.py`
✅ · 2. a new `agent` node type (a bounded ReAct loop consuming #1) ✅ · 3.
multi-hop research/retrieval (piggybacks on #2) ✅ (closed 2026-09-04 with
a real, honest null result — see below) · 4. self-critique on KIL's
`draft_change` · 5. autonomous reasoning-session continuation.**
Before starting, an Explore agent confirmed nothing like this existed:
every one of the (then) 26 `interpreter/registry.py` node handlers made
exactly one fixed `llm.complete()` call; `interpreter/reasoning.py`'s
Slack "reasoning sessions" are entirely human-driven (the LLM never
picks its own next action); `llm.complete()` had no `tools` parameter.

**Step 1 — tool-calling plumbing (COMPLETE 2026-09-03).** Purely
additive: a new `complete_with_tools()` function alongside `complete()`,
not a parameter on it — a tool-calling response is structurally richer
than a string (zero or more tool calls plus optional text), so bolting
it onto `complete()`'s `-> str` signature would give that function a
contingent return type for a module all 26 node handlers import.
`interpreter/llm.py` gains `ToolCall`/`ToolResult` dataclasses and
`complete_with_tools(messages, *, system, tools, model, max_tokens,
temperature)`, dispatching to new `_groq_complete_tools`/
`_anthropic_complete_tools` (Groq + Anthropic only in v1, matching
CLAUDE.md's "Groq default, Anthropic opt-in"; OpenRouter/vision
tool-calling deferred) — each translates a provider-agnostic
`messages`/`tools` shape into that SDK's real wire format (Groq:
OpenAI-style `tool_calls` + `role:"tool"` messages; Anthropic:
`tool_use`/`tool_result` content blocks) and sets `last_usage` exactly
like `complete()` does, so a future agent node's token spend is picked
up by P9's billing dashboard with zero new plumbing. No cross-provider
fallback chain for tool calls (unlike `complete()`) — an error raises
visibly rather than silently retrying on a different provider's
tool-calling implementation. The stub path (no API key) never calls a
tool — a canned final answer, matching `_stub()`'s existing
"deterministic, not smart" philosophy. **Verify:** 8 new offline tests
in `tests/test_resilience.py` (Groq + Anthropic tool-call parsing,
final-text parsing, multi-turn wire-shape assertions, the no-key stub,
an unsupported-provider `ValueError`) — 26/26 in that file, confirming
zero regression to the 26 existing `complete()` call sites (the file's
pre-existing 18 tests are untouched). Full offline suite unaffected.
**Live-verified against real Groq**: a full agentic loop — `word_length`
tool call → fed back the real result → the model's final answer
correctly reflected it. No `ANTHROPIC_API_KEY` in this environment (a
pre-existing gap, same as `complete()`'s Anthropic path per CLAUDE.md)
— the Anthropic wire-format translation is covered by mocked offline
tests instead, same rigor already accepted for `_anthropic_complete`.
**Next:** step 2 — the `agent` node type.

**Step 2 — the `agent` node type (COMPLETE 2026-09-03).** Scoped
narrowly, not a general-purpose agent: a "first identify where a bounded
loop earns its cost" pass (before building anything) found the ROI case
in the code itself — `h_retrieve` builds exactly one query, does exactly
one `hybrid_retrieve` call, no reformulation; when phrasing doesn't
lexically/semantically match the docs, `retrieval_score` is low →
`groundedness` is low → `confidence_gate` escalates. `eval/qrels_hard.jsonl`
already existed as a hand-built "multi-hop / keyword-heavy" eval set for
exactly this weakness and was sitting unused (`PROJECT_SCOPE.md` had
already flagged it: "the run isn't wired into a regression floor yet").
Design principle: **selective, not "always agentic"** — the loop only
spends extra tokens on cases that would otherwise be escalated anyway;
the first pass costs exactly what separate `retrieve`+`draft` nodes would.

New `@register("agent")` `h_agent` in `interpreter/registry.py`: a
drop-in for a `retrieve`+`draft` pair (config nests `retrieve`/`draft`
sub-configs). Composes the *existing* `h_retrieve`/`h_draft` handlers
directly (no reimplemented context-building/prompt logic — `h_draft`
already computes `groundedness` internally, so no separate judge call
needed) rather than duplicating their logic. Loop: run retrieve+draft →
if `groundedness.score >= groundedness_threshold` (default 0.6) or
`max_iterations` (default 3) reached, stop; else ask the model, via
`complete_with_tools` (step 1) with two tools (`search_kb(query)` /
`give_up()`), whether a different query would help — a genuine ReAct
decision, not a fixed retry heuristic — and loop with the reformulated
query if so. Tracks the best-scoring attempt across iterations (not just
the last — a reformulation could in principle score worse). Every field
a `confidence_gate` reads (`retrieval_score`/`draft_confidence`/
`groundedness`) is still produced, so nothing downstream changes.
`h_retrieve` gains one additive hook, `config.query_override` (every
existing caller leaves it unset, so behavior/cost is byte-for-byte
unchanged for every flow that doesn't opt into `agent`). Token spend
across all internal attempts + the reformulation calls is summed into
one collapsed trace entry (`type: "agent"`, matching the "each handler
appends exactly one trace entry" convention) so P9's billing pipeline
picks it up with no new plumbing. `_TYPE_DOC["agent"]` (`assist.py`) and
`NODE_DEFAULTS["agent"]` (`api/main.py`) added so the AI Flow Copilot and
palette both know its shape — the 19e/19f/19g lesson (an undocumented
node type gets hallucinated) applied proactively this time instead of
discovered the hard way.

**Bug found via live verification, fixed before calling this done:**
unlike `complete()`, `complete_with_tools` has no cross-provider fallback
and raises visibly on a transient error (by design, per step 1) — the
first live smoke run hit a real Groq 429 *inside the reformulation call*
and it crashed the whole node, which would have turned "an optional
retry on a hard case" into "a rate limit on the optional step kills a
flow run that already had a perfectly good first-pass draft in hand."
Fixed: `_agent_reformulate` now catches any exception and degrades to
"give up, keep the best attempt so far" — matching the codebase's
established best-effort convention elsewhere (`audit.record()`, KIL's
Slack posting, email delivery). A regression test
(`test_agent_survives_a_reformulation_call_error`) simulates exactly
this. **Verify:** 6 new offline tests in `tests/test_interpreter.py`
(first-pass-clears-threshold costs exactly one retrieve+one draft and
never calls the reformulation tool at all; a low-groundedness first pass
reformulates and the better second attempt wins; `max_iterations` is a
hard cap even if the model never gives up; the reformulation-error
regression above; the offline stub never reformulates, deterministic;
`h_retrieve`'s `query_override` hook in isolation) — 490 offline tests
green overall, zero regression. **Live-verified against real Groq**
(during a period of heavy shared-quota rate-limiting — see the Known
issues entry on that — which made this an unusually strong real-world
test): a case deliberately phrased to avoid the KB's own wording started
at `retrieval_score=0.039`/`groundedness=0.0` (a genuine first-pass
miss); the model's real reformulation produced *"Zapier webhook catch
hook URL documentation"*, jumping retrieval to `0.997`; a second
reformulation brought groundedness to `0.66` (clears the default 0.6
threshold); the loop correctly stopped at exactly 3 attempts
(`max_iterations`); no crash despite sustained `RateLimitError`s
throughout, confirming the resilience fix holds under real conditions,
not just the mocked regression test. **Not yet done, deliberately not
built now:** no seed flow has been switched to use `agent` in place of
its `retrieve`+`draft` pair (CLAUDE.md: "don't change the seed flows'
models... without a reason" — adopting this in Acme/Globex's live flows
is a separate, explicit decision, not a side effect of building the node
type) and `eval/qrels_hard.jsonl` hasn't been run through `agent` yet to
get a real before/after number (worth doing before deciding whether to
adopt it anywhere, per the original "identify ROI before committing"
framing). **Next:** step 3 — multi-hop research/retrieval — largely
already covered by what step 2 built; revisit whether it needs anything
beyond wiring `agent` into a real flow and measuring against
`qrels_hard.jsonl`.

**Adopted live (2026-09-03) — Acme/support now runs through `agent`.**
User's call, after the ROI-identification framing above. Not a
migration (a one-off content change to one specific seed flow's graph,
not schema/seed data every environment needs) — a script mirroring
`api.main.publish_flow`'s exact logic (validate the draft with the real
`check_flow`, snapshot `flow_versions` with the real `definition_hash`,
bump the `flows` row) rewired the live graph: the standalone `retrieve`
+ `draft` nodes are gone, replaced by one `agent` node in `draft`'s old
position (config reuses both nodes' old settings verbatim — same model,
same `top_k`, per CLAUDE.md's "don't change the seed flows' models
without a reason"). New topology: `classify → sf_writeback → agent →
confidence_gate → {handover|ask_human|auto_reply}` (was `retrieve →
classify → sf_writeback → draft → confidence_gate → …` — `retrieve` no
longer needs to be the entry point since it never actually depended on
`classify`'s output; `classify` is now the entry node). Published as
version 4. Fully reversible — the prior version's `flow_versions` row is
untouched, so `rollback_flow` restores the old retrieve+draft pair
instantly if needed. **Verified live:** `test_multiflow.py` 4/4 passed
against the real published flow, including the exact
`[ACME-support-auto_reply]` case that had been intermittently failing
all session under the shared-quota flakiness documented above — it
passed this run. Total runtime (417s for 4 cases) was noticeably slower
than the pre-agent baseline (~2-4 min), consistent with the agent's
extra retrieve+draft+judge calls firing under today's tight quota — a
real cost, not free.

**`qrels_hard.jsonl` before/after — not run, and not a clean fit as-is.**
`ingestion/eval/run_eval.py --qrels hard` evaluates raw retrieval recall
in isolation (a bare question → does the top hybrid_retrieve result
match `relevant_urls`), independent of any node — it never touches
`draft`/`groundedness`, so it can't exercise `agent`'s actual decision
loop (which only reformulates when a *draft's* groundedness score is
low; there's no draft in that harness at all). Getting a real
before/after would need a small **purpose-built** comparison script
(feed each of the 10 hard questions through `h_agent` vs. a single
`hybrid_retrieve` call, compare recall+iteration count) — not built yet,
deliberately, both because it's a different piece of work than "wire it
into a flow" and because running it live right now would hit the same
quota pressure that made this session's own verification slow (10
questions × up to 3 agent iterations each, under the fallback chain seen
all session, could plausibly take 20-30+ minutes and mostly exercise the
weak fallback model rather than a clean signal). Flagged for the user
rather than silently spent.

**Step 3 — CLOSED (2026-09-04) — the purpose-built comparison, built and
run for a real number.** New `eval/agent/run_agent_eval.py`: pulls Acme's
*real, live* `agent` node config from the published flow (not a guess),
runs each of the 10 hard questions through one direct `h_retrieve()` call
(baseline, zero LLM cost) and one `h_agent()` call (up to 3 rounds), scores
both through `ingestion/eval/run_eval.py`'s existing `score()` — reused,
not reimplemented, so the numbers are directly comparable to that file's
own dense/sparse/hybrid/hybrid_rerank blocks.

**Real result, run live against Supabase/Neo4j/Groq (2026-09-04):**
baseline and agent scored **identically** — hit@1 0.200, hit@5 0.400,
MRR@10 0.240, same 6/10 questions missed, same top-1 doc on every single
question. Only **1 of 10** questions triggered a reformulation at all
(`h06`, the throttling question), and even that reformulation didn't
change the top-ranked result. 21,958 tokens spent on the agent side for
zero measured retrieval gain.

**Two honest caveats, not swept under the rug:**
1. **This run landed during the same sustained Groq/OpenRouter
   rate-limiting seen all session** — the log shows repeated 429s
   forcing the fallback chain down to `nvidia/nemotron-3-ultra-550b-a55b:
   free` for most calls, and the one reformulation attempt that did fire
   hit a tool-call JSON-parsing error on that fallback model (`Failed to
   parse tool call arguments as JSON`) — caught cleanly by the exact
   `_agent_reformulate` resilience fix from step 2 ("keeping best attempt
   so far"), a second live confirmation that fix holds, but it also means
   a weak model's groundedness scoring drove the "should I reformulate"
   decision most of the time, not a clean-quota run of the intended model
   (`llama-3.3-70b-versatile`, itself a retired Groq name routed through
   the roster).
2. **n=1 reformulation event is not enough to conclude the loop never
   helps** — it's enough to conclude it *did not help on this specific
   10-question hard set today*. The original ROI framing (step 2) was
   "only spend extra on cases a confidence_gate would otherwise escalate
   anyway" — a pure-retrieval eval with no `confidence_gate` in the loop
   doesn't test that framing directly; it tests whether reformulation
   improves *raw retrieval rank*, which turned out to be rare-to-trigger
   and, when triggered, unhelpful in this sample.

**Net: step 3 is answered, not "proven agent helps."** The honest number
is a null result under real conditions, which is a legitimate answer to
"does the multi-hop loop earn its cost" — not a reason to revert Acme's
live adoption (the step-2 live `test_multiflow.py` run already showed it
producing a correct outcome, just slower/more expensive), but also not
evidence to expand `agent` to more flows without a cleaner-quota rerun or
a version of this eval that goes through the full `confidence_gate`
decision rather than raw retrieval rank alone.

**Step 4 — self-critique on KIL's `draft_change` (DONE 2026-09-04).**
`interpreter/kb_writeback.py::draft_change` proposed a KB rewrite and sent
it straight to a Slack approval card with zero automatic verification it
actually resolved the contradiction — despite the exact check already
existing: `eval/writeback/run_writeback_eval.py` (P8c) already graded this
*post-hoc* via `integrity.check(statement, [new_body])`. Step 4 wires that
same check into `draft_change` itself, live: `_self_critique()` runs it on
every draft (LLM-drafted or the deterministic fallback) and, when the
confirmed statement still `contradicts` the drafted body **and** a real
LLM produced it (retrying the deterministic fallback would just reproduce
the same text), asks the model to revise once with the critique as
feedback and re-checks. The verdict — `entails`/`neutral`/`contradicts` +
whether it was retried — ships on the `change` dict either way (never a
silent block, matching KIL's whole "flag to a human" philosophy) and
`_post_card` adds a `:warning:` line to the Slack card whenever it isn't
a clean `entails`, so the approving manager sees the machine's own doubt
about its work instead of a confident-looking card regardless of quality.
`_llm_draft` was extracted verbatim from the old inline call so both the
first attempt and the one bounded redraft share it — no duplicated
prompt-construction logic. 7 new offline tests in `tests/test_kb_writeback.py`
(clean-entails stamps correctly, a `contradicts` verdict retries exactly
once and the retried draft ships, no-LLM mode stamps the verdict without
retrying, a malformed redraft keeps the original, both `_post_card`
branches) — 653 offline tests green, zero regression to the file's
existing 9. **Real before/after, reusing the existing eval unchanged**
(`python -m eval.writeback.run_writeback_eval`, live Groq): resolution
rate **1.000** (7/7 valid cases), answer lift **+0.611** (keyword coverage
0.30→0.92) — matching the pre-self-critique KIL-d baseline (resolution
1.00, lift +0.63) within noise, not a dramatic jump. **Honest read:** this
eval set was already at a resolution ceiling before self-critique existed
— no case in it fails on the first draft, so the retry path had nothing
to catch here and this measurement can't demonstrate improvement on its
own; the new unit tests are what prove the retry mechanism itself works
(a `contradicts` verdict really does trigger exactly one redraft and the
better result really does ship). The value add is a safety net for a
future harder case plus reviewer transparency, not a lift on this
particular 12-case set. **Phase 29 status: steps 1-4 done, 5 (autonomous
reasoning-session continuation) not started.**

**Step 5 — autonomous reasoning-session continuation (DONE 2026-09-05).**
The Phase 29 kickoff note flagged the exact gap this closes: `interpreter/
reasoning.py`'s Slack reasoning dialogue (Phase 24) is entirely
human-driven — when the responsible agent stops replying mid-`clarifying`
dialogue, `sweeps.reasoning_ttl` had exactly two moves, nudge once then
escalate + abandon, throwing away the questions already asked and any
partial answers. New `reasoning.autonomous_continue(session, case, ...)`
gives the bot one bounded, genuinely agentic shot at closing the
still-open **critical** pointers itself before that happens — reusing
`complete_with_tools` (step 1) + `hybrid_retrieve` exactly like `h_agent`
(step 2/`registry.py`) rather than reimplementing a ReAct loop: the model
decides whether a KB search would help or whether to `give_up`, it isn't
a canned retry. **Grounded-only, by design:** a pointer is marked answered
only off documentation a `search_kb` call actually returned (a separate
JSON-graded pass, reusing the `_ingest`-style contract), never an
ungrounded guess — so the stub path (no tool ever called) never resolves
anything, deterministically, same discipline as every other agentic piece
this phase built.

Wired into `sweeps.reasoning_ttl` via `_try_autonomous_continue`: only for
a stale session in `clarifying` state (`awaiting_handoff` means nobody
ever engaged — a different problem, still escalates as before;
`awaiting_approval` already has a draft awaiting an explicit human `send`
— auto-sending it would break the "never act silently" rule the rest of
this codebase holds to, including step 4's own self-critique). On success
the session moves to `awaiting_approval` with a real draft (composed via
the existing `_compose_draft`, grounded in what the bot found) and the
Slack thread gets an explicit "*you'd gone quiet, so I dug through the
docs myself — here's a draft (unconfirmed by you, please review)*"
message — **a human `send` is still required**, this only unsticks the
*dialogue*, not the approval gate. On failure (nothing found, or the
model itself gives up) the existing escalate + abandon path runs
unchanged. `reasoning_ttl`'s return dict gains a `continued` list
alongside `nudged`/`escalated`.

8 new offline tests (6 in `tests/test_reasoning.py` covering
`autonomous_continue` itself — trivial-resolve on no open gaps, a
grounded resolve, giving up when the model never searches, surviving a
tool-call exception, the `max_iterations` cap, the no-API-key stub path
never resolving; 2 in `tests/test_sweeps.py` covering the sweep wiring —
resolves and moves to `awaiting_approval`, falls back to escalate when
unresolved — the pre-existing nudge/escalate test needed one small fix
alongside these, see below).

**Live-verified (2026-09-05), two parts, real Groq + real Supabase KB +
real Slack, both cleaned up after:**

**Part A — the mechanism in isolation, no side effects.** Called
`autonomous_continue()` directly with one open critical pointer (`h06`
from `eval/qrels_hard.jsonl`, a known KB-answerable hard question — *"429
throttling — API vs webhook vs polling request limits and retry-after"*)
against the real Acme KB. The model made **2 real tool-calling
iterations** (not a canned reply), `hybrid_retrieve` pulled real chunks
from `docs.zapier.com/integrations/build/throttling`, and the grounded
grading pass correctly marked the pointer answered — a second,
**non-critical** pointer included in the same call was correctly left
untouched, confirming the "critical gaps only" scoping works live, not
just in the mocked tests.

**Part B — the full sweep wiring end to end.** Posted a real root message
into the live dev Slack workspace, inserted one throwaway
`reasoning_sessions` row (`state='clarifying'`, the same open pointer,
`updated_at` backdated 5h) pointed at that thread, then ran
`sweeps.reasoning_ttl(sb, dry_run=False)` for real (first confirmed zero
other open sessions existed, so nothing else could be swept up as a side
effect). Result: `{"continued": ["LIVE-CHECK-B"], "escalated": [],
"nudged": []}` — the session flipped to `awaiting_approval` with a real,
better-grounded draft (this run's search surfaced specific numbers: 10k
requests/5min for webhooks, 100 items/poll, `Retry-After`/`ThrottledError`
handling), a real `case_events` row (`action='autonomous_continue'`)
was written, and `conversations.replies` confirmed the actual Slack
message landed in-thread verbatim: *"You'd gone quiet, so I dug through
the docs myself — here's a draft (unconfirmed by you, please review)…"*.
Test artifacts (the DB row, the `case_events` row, both Slack messages)
were deleted afterward — the live DB has zero non-terminal
`reasoning_sessions` rows again, same as before the test.

**One friction point, not a code bug:** the live Acme
`tenant_integrations` row for Slack shows `status: "inactive"` even
though the bot token in Vault is live and fully working
(`slack.connected()` → `True`, the post actually landed) — `status` is
apparently a stale display flag from an earlier session, not derived from
whether the token actually works. Worth a look if the web UI's
Connections tab is trusted to reflect real connectivity, but out of scope
for this chunk.

**Phase 29 status: all 5 steps done, all 5 live-verified.**

## Multi-tenant / multi-Salesforce-org scoping (2026-09-03)

User asked for a scope of what's needed for real multi-tenant + multi-
Salesforce-org support, plus whether org-structure changes (team/module/
workflow) and Supabase+Neo4j together actually isolate two tenants
safely. Answered from an evidence-based audit (file:line citations, not
assumption) rather than assumed. Findings:

- **Postgres/Supabase: solid.** RLS-enforced, re-verified via a
  dedicated security-review fork this session (zero cross-tenant
  findings) on top of the original Phase 4 `rls_check.sql` verification.
- **Neo4j: had a real, unenforced gap — fixed below.** One shared Aura
  instance for all tenants (no Fabric/per-tenant database); the
  Case/Account/Message/Reply graph MERGEd on a Salesforce record id
  alone (`sf_id`/`case_sf_id`/`id`), with `tenant_id` stamped via a
  post-merge `SET`, not part of the merge key. Salesforce record ids are
  per-org, not globally unique — two tenants on different orgs sharing
  an id would silently MERGE into the *same* node, cross-contaminating
  case history. The public-docs graph (`:Doc`/`:Section`, shared
  `zapier-public` corpus) and the KB-article graph (`:KBArticle`, keyed
  on a Postgres UUID) were never at risk — this was specific to the
  Case-history graph.
- **Salesforce connector: isolated correctly, but hard-capped at one org
  per tenant.** `client_for(tenant_id)` never bleeds credentials across
  tenants. `tenant_integrations` has `primary key (tenant_id, kind)` — a
  tenant can have at most one `kind='salesforce'` row, full stop.
  Multi-org (sandbox+prod, or several business units each on their own
  org) isn't unbuilt, it's schema-blocked. **Not built this session** —
  scoped as its own feature below.
- **Org-structure-change resilience: routing is safe, taxonomy is
  brittle.** `policy_rules`/`notify_targets` are genuine per-tenant
  tables — a customer renaming/adding a team needs zero code changes.
  `salesforce.py::map_case_fields` (topic → `Module__c`/`SubModule__c`/
  `Region__c`) is one hardcoded Python dict shared by *every* tenant, not
  per-tenant data — this is the same bug class migration 079 already hit
  once live (an unmapped topic rejected by a restricted SF picklist).
  `scripts/sf_support_setup.py`'s picklist values and this runtime
  mapping are two separate hardcoded sources of truth with nothing
  keeping them in sync. **Not built this session** — scoped below.

### Fixed this session: the Neo4j merge-key gap

`interpreter/case_memory.py` — both `_MERGE_CYPHER` (Phase 21 resolved-
case sync) and `_LIFECYCLE_CYPHER` (KIL-a Case-lifecycle sync) now key
every MERGE on `(sf_id, tenant_id)` (`Case`/`Reply`/`Account`) or
`(id, tenant_id)` (`Message`), not the Salesforce id alone; the
`SIMILAR_TO` cross-match (`UNWIND $similar … MATCH (o:Case {sf_id: …})`)
now also pins `tenant_id`, so a similarity edge can never link two
different tenants' cases. `ingestion/neo4j_sync.py::ensure_constraints`
drops the old single-property uniqueness constraints
(`case_sf_id`/`reply_case_sf_id`/`message_id`/`account_sf_id`) and
replaces them with composite `(id, tenant_id)` ones — without this, the
Cypher fix alone would make two tenants sharing an id start throwing
constraint-violation errors on every sync (fail-loud, better than silent
corruption, but still broken) instead of correctly creating two separate
nodes. Confirmed **Neo4j Aura (this project's instance) supports
composite uniqueness constraints** — applied live, old constraints gone,
new ones present (`SHOW CONSTRAINTS` checked directly). `Module`/
`CaseType`/`Agent` taxonomy nodes were deliberately left global/shared
(not part of the flagged risk — no case *content* crosses tenants
through a shared `:Module {name:"Billing"}` node the way it would
through a miskeyed `:Case` node; nothing in the codebase traverses
taxonomy nodes to reach another tenant's cases).

**Verify:** `tests/test_case_graph_sync.py`'s Cypher-string assertion
updated to match the new keys (1 test); full offline suite 499/499.
**Live-verified directly against the collision scenario** (not just code
review): synced two `sync_case_lifecycle()` calls with the *same*
`sf_id` under two different `tenant_id`s — confirmed via a live `MATCH`
query that this now produces **two separate, correctly-attributed
nodes** (previously would have silently produced one merged node under
whichever tenant synced first). Cleaned up the synthetic test nodes
afterward.

**Not retroactive.** This fixes the merge key going forward; it does not
scan existing graph data for already-collided nodes from before the fix
(no evidence any real collision has happened — Acme/Globex test data
doesn't share Salesforce ids — but a full audit/cleanup script for a
customer environment with real overlap risk is out of scope of this fix
and would be its own chunk if ever needed).

### Multi-Salesforce-org support — connector layer BUILT (2026-09-03), node/UI wiring not yet

User's call: build the connector/schema foundation first (same
infra-first pattern as Phase 29's tool-calling plumbing before the
`agent` node consumed it), not the whole stack in one pass — there's
also no live use case yet to wire against (checked the live project:
zero `kind='salesforce'` rows exist at all; both Acme and Globex
currently fall back to the single shared env-configured org).

Migration `082`: `tenant_integrations` gains `org_label text not null
default 'default'`; the primary key widens from `(tenant_id, kind)` to
`(tenant_id, kind, org_label)`. Backward compatible in both directions —
every existing row backfills to `org_label='default'`, and every
existing `client_for(tenant_id)` call site (20+ of them across
`salesforce.py`, `routing.py`, `sf_context.py`, `attachments.py`,
`api/worker.py`, `sweeps.py`, `handoff_watch.py`, …) keeps resolving
exactly the same thing since none of them pass a third argument.
`email`/`google`/`slack` rows keep behaving as one-per-tenant by
convention (nothing varies `org_label` for those kinds) — not
schema-enforced, matching the table's existing loose `kind text` typing.

`interpreter/salesforce.py`: `client_for(tenant_id, org_label=None, sb=)`
resolves per `(tenant_id, org_label)`, caches per that pair, falls back
to the shared env client exactly as before when no row exists for that
tenant+org. New `save_tenant_org` / `list_tenant_orgs` / `delete_tenant_org`
— the write side (nothing existed before this; SF creds were always
env-only or a single ad-hoc row, never a self-serve flow, unlike
`email`/`google`/`slack` which already have one).

**Verify:** 6 new offline tests (`tests/test_salesforce_multi_org.py` —
different orgs resolve to different cached clients, unaffected by each
other, deleting one doesn't disturb another, no-tenant/no-org-label
always hits the env client, a stale cache is invalidated on re-save).
Full offline suite 505/505, zero regression to any of the 20+ existing
`client_for()` callers. `python -m scripts.check_migrations` clean (no
drift). **Live-verified against the real Supabase project** — saved two
distinct org connections (`prod`/`sandbox`) under a throwaway tenant,
confirmed both persisted with the right distinct secrets via a raw row
query, confirmed Acme's own `tenant_integrations` rows were untouched,
cleaned up after.

**Deliberately not built in this pass** (the "infra-first, wire up
later" half): no flow node (`sf_case`/`sf_writeback`/`sf_context`/
`identify`) has an `org` config field yet to actually pick a non-default
org at run time — they'd all need `org_label` threaded from node config
through every Salesforce function they call down to `client_for`, which
touches most of `salesforce.py`'s 1150+ lines and is real, separate work
better done once there's an actual second org to route a real flow to,
not speculatively. No web UI either — unlike email's full Channels tab,
there was no existing Salesforce connection UI to extend; building one
from scratch is its own chunk.

### Self-serve Salesforce connection + org introspection (2026-09-03)

User's framing: "if we can do this we can sell this product" — self-serve
onboarding (collect SF/Slack/Gmail creds when a workspace is created) +
auto-discovery of the customer's real Modules/Groups/permissions, so the
flow editor's dropdowns are fetched from their org, never hardcoded.
That's three substantial systems, not one feature; this chunk builds the
first two — the self-serve **connection UI** and the **introspection
API** the third (dynamic editor dropdowns) will consume. Slack/Gmail
already had this exact shape (`ChannelsView`, OAuth authorize/callback);
Salesforce had none of it — env-var-only.

**A real, resolved question along the way**: does the platform need its
own Salesforce Connected App (OAuth) to build this, or is the existing
JWT bearer path enough? Answered directly — JWT is functionally
sufficient for introspection (once authenticated, `describe()`/SOQL work
identically regardless of how the session was obtained); the actual
difference is *who* does Salesforce-admin setup work: OAuth needs it
once from the platform operator (a Connected App), JWT needs it from
*every customer* (their own Connected App + certificate + integration
user). No Connected App was available this session, so this chunk built
the JWT-based path — real and shippable today — with OAuth as a future
drop-in swap (the connector just stores whatever creds dict
`_build_client` needs; it doesn't care how they were obtained).

**Field-mapping strategy, decided**: customers map platform concepts
("module", "region") onto whichever of *their own* Case fields already
serve that purpose — the platform does not create custom fields in a
customer's org on connect (unlike this project's own dev-org setup via
`scripts/sf_create_fields.py`). Less invasive, more sellable to a
company with an existing SF setup. The mapping *UI* itself isn't built
yet (that's part of the dynamic-editor work below) — this chunk builds
the introspection data it would consume.

**Built:**
- `interpreter/salesforce.py` — `test_connection(creds)` (log in, back
  out, `{ok, error}}`, mirrors `mailbox.test_connection`);
  `describe_case_fields(tenant_id, org_label)` (the org's real Case
  fields — picklist/multipicklist/reference/string/textarea/boolean
  types, i.e. anything a customer might reasonably use for
  module/region/priority-style categorization — with real active
  picklist values, not the platform's assumed field names);
  `list_queues(tenant_id, org_label)` (real Queues the connected user can
  see); `introspect_org()` (both, degrading independently — a
  Case-describe failure doesn't hide Queues the caller can see, and vice
  versa); `redact_org_secret()` (only `SF_USERNAME`/`SF_DOMAIN` are safe
  to echo back, matching `connections.redact()`'s split).
- `api/main.py` — `GET/PUT/DELETE /api/integrations/salesforce[/{org_label}]`
  (owner-gated, built on the multi-org connector from the prior chunk)
  and `GET /api/integrations/salesforce/{org_label}/schema` (editor-gated
  — editors build flows, not just owners) + `POST .../test`. Every write
  logs through the audit system (`salesforce_org.connected`/
  `.disconnected`).
- `web/src/channels/ConnectionsView.tsx` gains a **Salesforce** section —
  connect/test/disconnect one or more named orgs (JWT fields: username,
  Connected App consumer key, private key, domain), plus a **"fetch from
  org"** button per connected org showing real field/queue counts —
  proof the introspection actually returns real org data, not yet wired
  into node-config dropdowns (that's the explicitly-deferred third
  system).

**Verify:** 12 new offline tests (`tests/test_salesforce_multi_org.py` —
`redact_org_secret`'s safe/unsafe split, `test_connection` success and
failure, `describe_case_fields` filters to mappable types and drops
inactive picklist values, `list_queues` shapes real rows,
`introspect_org` degrades each section independently on error) + 2 new
`test_api.py` tests (a 401 check on all 5 endpoints; **a live
end-to-end round-trip test using this environment's real `.env` SF
creds** — connects a throwaway-labelled org through the real API,
confirms the response never echoes back `SF_CONSUMER_KEY`/
`SF_PRIVATE_KEY`, fetches the real schema and asserts a genuine standard
field (`Priority`) comes back, disconnects, confirms it's gone). Full
offline suite 512/512, zero regression. `tsc -b` / `vitest` (6/6) /
`npm run build` clean. `scripts.check_migrations` clean.

**Deliberately not built in this pass** (the third system, and the
biggest one): no dropdown/search-suggestion UI in the FlowEditor's
Inspector consumes this data yet — `sf_writeback`'s field_map,
`team_route`'s team list, `notify`'s target groups are all still raw
JSON editing. That's real, spread-out frontend work across every
SF-aware node type, and needs the org-config-field threading from the
prior chunk's residual too (a node has to know *which* connected org to
introspect). Also not built: fine-grained "can this integration user
actually assign to this Queue" permission modeling — `list_queues`
returns what's visible to the connected user via normal sharing rules,
not a full permission-set/profile analysis (Salesforce's permission
model is deep enough that this is its own investigation, not a
by-the-way addition).

### Salesforce OAuth — a real "Connect Salesforce" button (2026-09-03)

Follow-on to the JWT-based connection above, per the user's explicit
ask to improve toward OAuth. New `interpreter/salesforce_oauth.py`
(mirrors `gdrive.py`'s OAuth shape exactly): `available()` (gated on
`SF_OAUTH_CLIENT_ID`/`SF_OAUTH_CLIENT_SECRET` — unset in this
environment, so the feature degrades to "not configured" everywhere,
same as Google before its own Connected App existed);
`authorize_url`/`exchange_code` (Salesforce's OAuth 2.0 web-server
flow — `_login_base()` resolves `login`/`test`/a My Domain token to
the right host); `refresh_access_token`/`client_from_oauth` (a
Salesforce access token is short-lived and `simple_salesforce` has no
bundled refresh helper unlike Google's `Credentials`, so a fresh access
token is minted from the stored `refresh_token` on every use — the
resulting client is still cached process-lifetime by `client_for`,
exactly as the JWT path already is). `salesforce.py::_build_client`
gains a 4th auth mode, checked first (`SF_OAUTH_REFRESH_TOKEN` +
`SF_OAUTH_INSTANCE_URL` — no `SF_USERNAME` at all, so it must be
detected before the existing modes' unconditional `creds["SF_USERNAME"]`
access). `redact_org_secret` treats `SF_OAUTH_INSTANCE_URL` as safe
metadata (like a domain), never the refresh token.

`api/main.py`: `GET /api/integrations/salesforce/oauth/status` (public
— lets the web UI hide the button when unconfigured, matching
`gmail_available()`'s pattern), `GET .../oauth/authorize` (owner-gated,
mints a nonce into a new `_sf_oauth_state` dict — separate from the
existing Google/Slack `_oauth_state` since this flow carries two extra
fields, `org_label`+`domain`, through the round trip), `GET
.../oauth/callback` (public, exchanges the code, calls the existing
`save_tenant_org` — the same write path the JWT form uses, so both auth
modes land in the identical place downstream). `web/src/channels/
ConnectionsView.tsx`'s Salesforce panel now leads with a **"Connect
Salesforce"** button (hidden with an explanatory note when the server
has no Connected App configured) and tucks the JWT form into a
collapsed "Advanced" `<details>` — the easy path is the default, the
technical one is still there for whoever needs it.

**Verify:** 13 new offline tests (`tests/test_salesforce_oauth.py` —
`authorize_url`'s login/sandbox/My-Domain host resolution,
`exchange_code`'s real request shape and missing-refresh_token error,
`refresh_access_token` posting to the *stored* instance URL not a fixed
host, `client_from_oauth` building a client from a mocked fresh token,
`_build_client`'s OAuth-first dispatch never touching `SF_USERNAME`,
`redact_org_secret`'s safe/unsafe split for the new key) + 1 new
`test_api.py` 401/public-status check. Full offline suite 525/525, zero
regression. `tsc -b` / `vitest` 6/6 / `npm run build` clean.
**Not live-tested against a real Salesforce Connected App** — none is
registered for this platform yet, so the actual authorize→consent→
callback round trip is unverified against a real org (the unit tests
cover every function up to and after the point a real browser
would visit `login.salesforce.com`, which is the part that needs a
real Connected App to exercise). The whole Docker stack (`api` +
`worker`/`poller`/`cdc`/`slackbot`, since `salesforce.py` is shared)
was rebuilt and restarted so this environment's live containers
actually run today's code — this was **also** the root cause of an
earlier confusing moment this session: the Docker images bake the app
in at build time (only `sf_jwt`/the model cache are live-mounted), so
every backend change made across several PRs today had been sitting
unapplied in the running containers until that rebuild.

### Multi-org selection reaches every SF-touching flow node (2026-09-03)

Closes the "node/UI wiring not yet" gap the connector chunk explicitly
deferred. Per the user's direction after a brief calendar-scheduling
detour ("keep it scoped... let's move back to our multi-tenant
workflows and build them end to end without deviation"): every node
handler that can touch Salesforce now reads an optional `config.org`
and threads it all the way down to `client_for(tenant_id, org_label)` —
connecting multiple orgs (prior chunk) was previously unusable from any
actual flow, since nothing ever passed anything but the implicit
`'default'`.

**Mechanical, not a redesign** — `interpreter/salesforce.py`'s 13
public functions that take `tenant_id` (`identify_sender`,
`update_case_fields`, `post_chatter`, `add_case_comment`,
`log_email_message`, `assign_case`, `ensure_case`, `send_case_reply`,
`user_email`, `find_case_by_thread`, `agent_response_since`,
`latest_inbound_email`, `org_metadata`) gained a matching optional
`org_label` parameter, purely additive; `sf_context.py::load`,
`routing.py`'s `queue_member`/`_sf_queue_id`/`_sf_team_member`/
`resolve_notify_target`, and `attachments.py`'s `extract`/
`_sf_case_files` got the same treatment (`routing`'s cache keys also
gained the org label, so the same queue ref doesn't return a stale hit
across two different orgs). Every `registry.py` node handler that calls
any of these (`sf_writeback`, `sf_case`, `identify`, `ask_human`,
`notify`, `notify_human` via `alert.py::alert_human`, `handover`,
`clarify`, `sf_context`, `attachments`) now passes `config.get("org")`
through. `config.org` unset (every existing flow, including the one
just adopted onto the `agent` node) resolves exactly what it always
did — this is additive surface, not a behavior change to anything that
doesn't opt in.

**Bug found and fixed along the way**: `describe_case_fields`/
`list_queues` (the introspection functions from two chunks ago)
already *accepted* `org_label` in their signature but never actually
passed it into their own `client_for()` call — introspection always
silently looked at the *default* org's schema regardless of which org
was asked for. Caught by a blanket verification script (every call site
of every org_label-taking function, checked for the argument actually
being passed) rather than by inspection — worth remembering as a
technique for this class of "the parameter exists but nothing threads
into it" bug.

**Verify:** 12 new offline tests (`tests/test_salesforce_org_threading.py`
— one per node handler, monkeypatching the specific function each one
calls and asserting `org_label`/the positional org arg received matches
`config["org"]`, plus the "org unset stays `None`" case). Found and
fixed one real regression while wiring this in:
`tests/test_case_control_plane.py`'s `capture` fixture's fake
`update_case_fields` had a strict keyword-only signature that didn't
accept the new `org_label` kwarg — every node routing through
`_cp_write` was silently failing (caught + logged, not raised) and
never actually recording its Case field write in the test, masked as a
downstream `KeyError` on the assertion rather than the real cause.
Fixed the fixture; not a real product bug, since the actual
`update_case_fields` function's real signature was always correct —
only the test's fake was stale. Full offline suite 537/537, zero other
regressions. Not re-run against a real second live org in this pass —
`client_for`'s own org-resolution mechanism was already live-verified
in the connector chunk; this chunk only proves (via the new tests) that
every handler's `config.org` genuinely reaches that already-proven
mechanism, which is what was actually in question.

### Flow editor Inspector: real Salesforce data instead of hardcoded/raw JSON (2026-09-03)

Per the user's direction ("put all the focus on the multi tenant flow
with fetching details from salesforce, slack and other channels showing
in editor these") — the previous chunks built the org-per-tenant
connector, introspection, and org-label threading, but the flow editor
itself still had two real gaps: `salesforce_meta` (the endpoint the
editor's dropdowns call) wasn't tenant/org-aware at all, and the node
most tied to "don't hardcode SF fields" (`sf_writeback`) had no custom
form — just raw JSON.

**Bug found and fixed — `api/main.py::salesforce_meta` was a single
global cache** (`_SF_META_CACHE = {"at": ..., "data": None}`), calling
`org_metadata()` with no `tenant_id`/`org_label` at all. Every tenant,
regardless of which org they'd connected via the multi-org connector,
saw whichever org's metadata happened to be cached first — a real
cross-tenant data leak in the editor UI (not RLS-bypassing, since it's
metadata not case data, but still wrong). Fixed: cache is now keyed
`(tenant_id, org_label)`, 5 min TTL per key, `org_metadata(tid, org)`
called with both.

**Bug found and fixed — `org_metadata()` gated on the wrong check.** It
called `available()` (env-var creds only) before doing anything, so a
tenant with their own connected org via `tenant_integrations` and zero
env creds always got `available=False` and empty dropdowns — the exact
tenant this whole self-serve flow exists for. Rewritten as a thin
adapter over `introspect_org()` (which resolves via `client_for(tenant_id,
org_label)` directly, no env-only gate). Regression test added
(`test_org_metadata_available_even_with_no_env_creds_if_the_tenant_org_resolves`)
that deletes all env SF creds, confirms `available()` is `False`, then
confirms `org_metadata` still returns `available=True` when the
tenant's own `client_for` resolves.

**Editor UI**: `web/src/flows/Inspector.tsx` — the SF-meta cache is now
keyed per `(tenantId, orgLabel)` instead of one global value;
`QueuePicker`/`ClarifyForm`/`NotifyForm` take a required `tenantId` prop
threaded from `FlowEditor.tsx`'s `flow.tenant_id`. New `OrgPicker`
(dropdown of the tenant's connected orgs from
`GET /api/integrations/salesforce`, falls back to a plain text box when
the tenant has 0-1 orgs — no point picking from nothing). New
`SfWritebackForm`: `field_map` (the config `sf_writeback` actually
writes with) is now a src-key → real-SF-field dropdown sourced from
`meta.case_fields`, not free text — the field list live-updates when a
different `org` is picked via `OrgPicker`. `ask_human`/`handover` gained
a `QueuePicker`-backed `queue` field (previously raw JSON only, despite
routing to a real SF Queue). All four fall back to plain inputs/JSON
when the org can't be reached, same degrade-gracefully rule as the
existing `QueuePicker`.

**Deliberately scoped out of this pass**: `sf_writeback`'s `value_maps`
(mapping classifier output values to picklist option values) still uses
the generic raw-JSON fallback rather than a nested per-field editor —
`field_map` was the higher-value, higher-frequency target.
`team_route`'s config routes to platform-internal team-name strings, not
Salesforce data, so it doesn't fit this ask and wasn't touched. **Next
up, not started**: the Slack half of the same ask — `list_channels`
doesn't exist in `interpreter/slack.py` yet, `SCOPES` needs
`channels:read` added (reconnect required for already-connected
tenants), plus a `GET /api/integrations/slack/meta` endpoint and
`ChannelPicker`/`UserPicker` components for `notify_human`'s
`slack_channel`/`mention.slack_user_id` fields (currently free text).

**Verify**: 3 new tests in `tests/test_salesforce_multi_org.py`
(`org_metadata` derives from `introspect_org`; the no-env-creds
regression case above; reports an error only when both sections are
empty), 2 new tests in `tests/test_api.py` (401 without a token; a live
`@pytest.mark.integration` test against the real Globex tenant
confirming `GET /api/salesforce/meta?org=...` is tenant-scoped and
degrades gracefully for an unknown org label). Full offline suite
541/541. Frontend: `tsc -b` clean, `vitest run` 6/6, `vite build` clean
(729 kB bundle, pre-existing size warning, not from this chunk). Not
yet live-verified in the browser against the real `acme-dev` connected
org (still live for the demo tenant from the introspection chunk) —
next step before shipping.

### Every node with a real data source gets a real picker (2026-09-03)

Per the user's follow-up ("check all the nodes whichever we can give
picker, give them that") — a full sweep of every `registry.py` node
handler for config fields backed by an external system, not just the
`sf_writeback`/queue fields from the previous chunk. Split into two
halves: Slack introspection (new capability) and closing the remaining
Salesforce-org gaps (same capability, more nodes).

**Slack — new.** `interpreter/slack.py` gained `list_channels`
(`conversations.list`, public+private), `list_users` (`users.list`,
filtered to real humans — bots/deleted/Slackbot dropped), `list_usergroups`
(a richer public sibling of the existing `_usergroup_index` handle→id
cache), and `workspace_meta()` combining all three with the same
degrade-independently shape as `salesforce.introspect_org` —
`available:false` only when the tenant hasn't connected Slack at all,
never when one section fails. `SCOPES` widened to add `channels:read,
groups:read,users:read` (existing already-connected tenants keep
working with no reconnect needed — checked live against Globex's real
bot token, which already carried these scopes). New
`GET /api/slack/meta` endpoint, tenant-scoped 5 min cache (same pattern
as `salesforce/meta`'s fix from the previous chunk, not the old global
bug). `notify_human`'s `slack_channel` and `mention.slack_user_id` are
now `ChannelPicker`/`SlackUserPicker` dropdowns instead of free text,
plus a new `ByTeamOverride` control (rows keyed by the `team_route`
team names) for `slack_channel_by_team` and `mention.slack_user_by_team`.
`mention.mention_id` (the Chatter/SF id fallback) stays free text — no
SF User-listing function exists yet, noted below.

**Salesforce — closing gaps.** Several node forms already read
`config.org` (from the org-threading chunk two back) but had no UI to
*set* it — `ClarifyForm`, `NotifyForm`, and the `ask_human`/`handover`
block all silently fell back to the raw JSON editor for org selection.
All four now render `OrgPicker`. New `SfCaseForm` for the `sf_case`
node (previously raw JSON only, despite being the node that actually
creates the Case): `OrgPicker` + `Origin`/`Status` dropdowns sourced
live from `meta.case_fields` picklists (verified live against
`acme-dev`: `Status` = New/Triaged/In Progress/Working/Escalated/
Waiting on Customer/Resolved/Closed, `Origin` = Phone/Email/Web), plus
`reuse`/`create_contact`/`create_account` controls. New `RetrieveForm`
for the `retrieve` node's `kb_sources` (same collection-checkbox pattern
as the existing `KbLookupForm`, previously not reused here despite
`retrieve` being the more commonly-wired node). New `HttpRequestForm`
for `http_request.connection` — a real per-tenant `Connection` slug
dropdown (Data tab), reusing the already-existing `api.connections.list`
that nothing in the editor consumed yet. `IdentifyForm`/`SfContextForm`/
`AttachmentsForm` gained the same `OrgPicker` the org-threading chunk
made possible but never wired into their forms.

**Deliberately not touched** — surveyed and ruled out, not missed:
`team_route`'s `rules`/`default` route to platform-internal team-name
strings, not fetched data (same call from the previous chunk, held).
`classify`/`extract`/`transform`/`trigger`/`case_lookup`/`auto_reply`
have no config field backed by an external system — thresholds, dotted
paths, and LLM-defined field names, not something to fetch a picklist
for. `ai_prompt.model` is a free-text model id; it's a *fixed* roster
(`interpreter/llm.py::MODELS`), not something fetched live from a
connector, so left as text rather than stretching this chunk's scope to
static-constant dropdowns. `agent`'s nested `retrieve`/`draft` sub-configs
would reuse `RetrieveForm`/`AiPromptForm` reasonably cleanly but weren't
done in this pass — small enough to fold into the next editor chunk
rather than block this one on it. `notify.target_by_type`/
`fallback_target` and `notify_human.mention.mention_id` stay free text —
picking a specific Salesforce User needs a new `list_active_users`
introspection function that doesn't exist yet (same class of gap as
Slack's user list did before this chunk); noted as the next Salesforce
introspection addition if this becomes a real pain point.

**Verify:** 6 new offline tests (`tests/test_slack_introspection.py` —
channel/user/usergroup filtering, degrade-to-empty on any Slack error,
`workspace_meta`'s combine-three-sections shape) + 2 new
`tests/test_api.py` tests (401 without a token; a live
`@pytest.mark.integration` test against the real Globex Slack workspace
— 200 with the right key shape). Live-verified directly against Globex's
real Slack workspace before wiring the endpoint: 12 real channels
(`cx-l1`, `cx-billing`, `cx-tier2`, …), 1 real human user
(bots/Slackbot filtered out correctly), 7 real usergroups. Full offline
suite 548/548. Frontend: `tsc -b` clean, `vitest run` 6/6, `vite build`
clean (739 kB bundle, same pre-existing size warning). Not yet clicked
through in a real browser.

### Closing the last gaps: SF User picker + edge conditions (2026-09-03)

The user pushed back after the previous chunk ("still there are few
nodes & edges need to be addressed from salesforce & slack") — two real
gaps remained, one explicitly noted as deferred and one not surveyed at
all.

**Salesforce User picker — the noted gap.** New
`salesforce.list_active_users(tenant_id, org_label)`: `SELECT Id, Name,
Email FROM User WHERE IsActive = true AND UserType = 'Standard'`
(filters out Salesforce's own system/integration/automation user types;
best-effort like `list_queues`' sharing-rule caveat — it can't tell a
*named* Standard-type integration user from a real agent). Folded into
`introspect_org`/`org_metadata` as a third `users` section, degrading
independently like `case_fields`/`queues` already did. New
`SfMentionPicker` (users + queues in one `<select>` with `<optgroup>`s,
since a Chatter mention accepts either) replaces free text for
`notify.target_by_type`/`fallback_target` and
`notify_human.mention.mention_id`; `NotifyHumanForm` also gained the
`OrgPicker` it was missing for resolving that mention against the right
org. Live-verified against `acme-dev`: 6 real active Standard-type
users returned, `org_metadata`'s `users` key populated end to end.

**Edge conditions — not surveyed in the last two chunks at all.**
`EdgeInspector`'s `if` expression was a plain textarea with a static
hint string ever since it was built — never touched by "give it a
picker" because it isn't a *node*. But `_context()`
(`interpreter/builder.py`) exposes real fields a condition can branch
on that ARE Salesforce-backed: `classification.case_type` (the real
Case `Type` picklist value) and `routed_team` (the `team_route` node's
output). `EdgeInspector` now takes a `tenantId` prop and offers two
quick-insert dropdowns — real Case Type values (from the tenant's
default-org schema) and the `team_route` team names — that splice a
comparison clause into the expression at the cursor position (a
`textarea` ref tracks `selectionStart`/`selectionEnd`), plus `&&`/`||`
buttons. Deliberately did **not** add a Slack-backed quick-insert:
`_context()` exposes nothing from Slack, so there is no genuine value
to pick from there — inventing one would have repeated the exact
mistake being fixed (a `sf_context.case.Module__c` snippet was drafted
and then removed before shipping, once grepping `sf_context.py::load`'s
actual return shape confirmed no `case`/`Module__c` key exists there —
caught before merge, not after).

**Verify:** `tests/test_salesforce_multi_org.py` gained
`test_list_active_users_shapes_the_real_org_users` plus updated
assertions on `introspect_org`/`org_metadata`'s three-section shape
(`_FakeSFClient.query` now dispatches on `FROM User` vs `FROM Group`).
Full offline suite 549/549. Frontend: `tsc -b` clean, `vitest run` 6/6,
`vite build` clean. Live-verified `list_active_users` and the
end-to-end `org_metadata` `users` field against the real `acme-dev`
org. Not yet clicked through in a real browser — three editor chunks
in a row now share that same open item.

### First real browser click-through of the picker work — 4 bugs found, all fixed (2026-09-03)

Per the user's direction ("complete full multi tenant project end to
end & with very robust... flexible and easy") the session split into
three tracks; this is the first: an actual browser session driving the
flow editor, closing the "not yet clicked through" item every picker
chunk this session had carried. No headless browser was available in
this sandbox by default — set one up from scratch: `playwright` (npm,
already vendored in scratchpad from an earlier attempt) + Chromium
binary (already cached), but `chrome-headless-shell` was missing
`libnspr4.so`/`libnss3.so`/`libasound.so.2` and `apt-get install`
needs root (no passwordless sudo here). Worked around it with
`apt-get download` (no root needed, just fetches `.deb`s) +
`dpkg-deb -x` to extract the `.so` files into a scratch directory, then
`LD_LIBRARY_PATH` pointed at it for the Chromium launch — no system
changes, nothing installed outside the scratchpad. Logged in as a real
account (`scripts/set_editor_password.py` to set a password on the
existing Supabase Auth user) and drove the actual live `email` flow
(the most fully-built real flow: `identify`, `sf_case`, `retrieve`,
`sf_writeback`, `handover`, `team_route`, `clarify`, `notify`,
`ask_human`, `notify_human`, plus an edge) exactly as a user would —
clicking every node, reading the rendered Inspector, checking the
browser console.

**Bug 1 — real, significant: `GET /api/slack/meta` always reported
`available:false`, for every tenant, regardless of a real connection.**
The endpoint passed the caller's RLS-scoped client (`c.sb`) into
`workspace_meta`; `tenant_integrations` has RLS enabled with **no
policy at all** (service-role only, by design — it holds secrets), so
that client silently saw zero rows for every tenant. Every
`notify_human` channel/@mention picker built this session had been
silently falling back to plain text this whole time in the one place
that would have shown it: a real browser. `salesforce_meta` never had
this bug because `org_metadata` doesn't take an `sb` argument at all
(defaults to its own service-role client internally) — `slack_meta`
should have matched that pattern from the start. Fixed: stopped
passing `sb=c.sb`; tenant scoping is already enforced by
`_caller_tenant`'s RLS-backed membership check before this line runs,
same security model `salesforce_meta` already uses. **Root cause of
why no test caught it**: the one integration test for this endpoint
used `if body["available"]:` instead of asserting it — and its own
premise was wrong too (copied "Globex has connected Slack live" from
the `salesforce_meta` test's comment without checking; only Acme has
ever connected Slack). Fixed both: added an offline regression test
(`test_slack_meta_does_not_leak_the_rls_scoped_client_into_workspace_meta`,
overrides the `caller` dependency with a fake whose `.sb` is a
sentinel and asserts `workspace_meta` is called without it — so this
class of bug can't silently reappear) and corrected the integration
test to assert Globex's real (correct) state instead of a guessed one.
Live-verified in the browser after the fix: `notify_human`'s channel
picker now shows real dropdowns.

**Bug 2 — real: edge-condition quick-insert mashed text together with
no separator when the textarea hadn't been focused first.**
`EdgeInspector`'s new quick-insert dropdowns (previous chunk) used
`textarea.selectionStart`, which is `0` — not `null` — on an unfocused
textarea, so `?? ifExpr.length` never triggered and every insert landed
at position 0: picking "Case Type → Question" against the existing
`tier == 'enterprise'` produced `classification.case_type ==
'Question'tier == 'enterprise'`, a syntax error, with zero clicks
required to hit it (this is the *point* of a quick-insert — clicking it
without first clicking into the box). Fixed: only trust
`selectionStart`/`End` when `document.activeElement === textarea`;
otherwise treat it as "append", auto-joined with `&& ` when there's
already non-empty content. Verified live: the same repro now produces
`tier == 'enterprise' && classification.case_type == 'Question'`.

**Bug 3 — a real UX gap, not a wire-format bug:** `sf_writeback`'s
`field_map` form went completely blank (zero rows) on the live email
flow's actual `sf_writeback` node, which has `config: {}` — the
interpreter's own `field_map = config.get("field_map") or {defaults}`
fallback means this node genuinely writes 6 fields on every run
(`Priority`, `Type`, `Topic__c`, `Module__c`, `SubModule__c`,
`Region__c`), but the *editor* showed nothing, reading as "this node
does nothing." Fixed: when `config.field_map` is `undefined` (never
configured — distinct from an explicit `{}`, which a user could set by
deliberately clearing every row), the form now shows the interpreter's
own default map, labeled "showing the platform default — not yet saved
on this node," and editing any row seeds the full explicit map into
config on that first edit. Live-verified: the form now shows all 6 rows
with their real Salesforce field already matched.

**Bug 4 — a real coverage gap, found by comparing the rendered form
against the actual saved config:** the live `notify` node's raw config
had `target_by_module` and a top-level `mention_id` — two real fields
`h_notify` (`interpreter/registry.py`) has always read, neither of
which `NotifyForm` exposed (only `target_by_type`/`fallback_target`
had pickers). `target_by_module` is exactly the same shape as the
already-built `target_by_type` section, just keyed by `Module__c`
instead of `Case.Type` (real values already sitting in
`meta.modules`), and `mention_id` is the same kind of SF User/Queue id
as everything else `SfMentionPicker` already covers. Added both — live
data confirmed the module override renders all 7 real `Module__c`
values, and the Chatter @mention correctly resolved the live flow's
saved `mention_id` to "Gundam Vishnu."

**Verify:** every node type in the live flow clicked (10 types + an
edge), zero browser console errors across the full walkthrough, before
and after all four fixes. Full offline suite 550/550 (the new offline
regression test). Both live `@pytest.mark.integration` Slack/Salesforce
meta tests pass. Frontend `tsc -b` / `vitest run` (6/6) / `vite build`
all clean. Docker `api` rebuilt and re-verified live after the fix
(the fix lives in `api/main.py`, served by the Docker container, not
the Vite dev server used for the click-through itself).

### Robustness pass, part 1 — the `available()` bug was in every SF write/read, not just introspection (2026-09-03)

Track 2 of the three the user selected (browser click-through — done
above; robustness pass — this and the next entry; onboarding UX
walkthrough — not started). Spawned a forked agent to survey
`interpreter/registry.py`'s node handlers and the connector modules for
real error-handling gaps (not theoretical ones). Its top finding —
`update_case_fields`'s Salesforce write had no guard against a
transient failure and would `raise`, killing the whole case run — led
to checking every other function in `salesforce.py` for the same
`available()` gate, since `update_case_fields` also had it. That
turned into the real, much bigger finding.

**The bug**: `available()` only ever checks *env* vars
(`SF_USERNAME`/`SF_CONSUMER_KEY`/...). `org_metadata` was already fixed
two chunks ago for gating on it before `client_for` got a chance to
resolve a tenant's own connected org. **The same `if not available():`
gate turned out to be sitting in front of 12 more functions** —
`latest_inbound_email`, `agent_response_since`, `identify_sender`,
`update_case_fields`, `post_chatter`, `add_case_comment`,
`find_case_by_thread`, `log_email_message`, `user_email`,
`assign_case`, `ensure_case`, `send_case_reply` — meaning **every
Salesforce read and write** (not just the dropdown-metadata reads fixed
before) silently dry-ran forever for any self-serve tenant with their
own connected org and zero env creds. This is the core self-serve
product story from the start of this session ("if the user creates the
workspace we need to collect these details") — quietly broken for
every write the whole time, only invisible because this dev box's
`.env` happens to carry real SF creds, so `available()` was always
`True` here regardless of which tenant a call was for. `get_case`
(no `tenant_id` param — genuinely env-only by design, used by the CDC
subscriber/worker hydration) and `pubsub_auth` (platform-wide Pub/Sub
auth) keep their `available()` gates; those are correct.

**Fix**: new `_try_client(tenant_id, org_label)` — `client_for(...)`,
or `None` when neither the tenant's own org nor the env fallback
resolves (catches `_build_client`'s `KeyError` on a bare `{}` creds
dict). Every one of the 12 functions now calls `_try_client` first and
branches on `is None`, instead of gating on `available()` before ever
trying. Also fixes the fork's original finding along the way:
`update_case_fields` used to `raise` on any non-field-error (rate
limit, 5xx, timeout, expired session) — the one write path in the
module that didn't match every sibling's "best-effort, never raises"
docstring — now logs + returns `{"error": ...}` like the others. Its
`append`-mode `sf.Case.get(case_id)` read gained the same treatment (a
transient failure there no longer blocks the field write).

**Verify:** 11 new tests in `tests/test_salesforce_multi_org.py`
(`_try_client` resolving/failing correctly; a parametrized check that
6 of the 12 functions no longer return the dry-run shape once
`client_for` resolves a fake tenant client; `update_case_fields`
writing for real *and* degrading instead of raising on a simulated
`REQUEST_LIMIT_EXCEEDED`; `ensure_case`'s `dry_run` flag tracking the
real client, not `available()`). Full offline suite 561/561. **Live-verified against the real `acme-dev`
Salesforce connection**: `salesforce.available = lambda: False` (forcing
the exact lie the old code effectively told itself for every self-serve
tenant), then called `identify_sender` for tenant
`00000000-0000-0000-0000-000000000000` — correctly resolved the real
Contact/Account (`Gundam Vishnu` / `Gundam Vishnu (Gmail)`) instead of
returning `{"reason": "salesforce not configured"}`.

**Bonus catch, unrelated to the above but found while re-running this
PR's CI**: the `integration` job failed 5 tests with `postgrest.
exceptions.APIError: ... there is no unique or exclusion constraint
matching the ON CONFLICT specification (42P10)` — `interpreter/
mailbox.py::save_channel`'s upsert still named `on_conflict="tenant_id,
kind"`, the *old* `tenant_integrations` primary key from before
migration `082` (two chunks ago, multi-org Salesforce) widened it to
`(tenant_id, kind, org_label)`. Postgres rejects an `ON CONFLICT` column
list that doesn't exactly match a real constraint, so every email-
channel connect/save had been silently broken since `082` shipped —
missed because no test exercises `save_channel` against live Postgres
in the offline suite, and this box's local testing that session didn't
happen to touch the email channel. Fixed: `on_conflict="tenant_id,kind,
org_label"` + `org_label: "default"` in the row (email channels don't
vary it, same "one per tenant by convention" note `082`'s own comment
already made — this just makes the upsert's conflict target match the
constraint that comment assumed). The `google`/`slack` OAuth-callback
upserts in `api/main.py` were checked too — they don't pass
`on_conflict` at all, so they already use the *real* primary key
implicitly and were never broken. Verified: the 5 previously-failing
tests (`test_email_channel_configure_status_and_disconnect`,
`test_tick_finds_an_active_channel_and_records_a_fetch_error`,
`test_post_run_against_a_real_channel_dry_runs_the_send`, plus 2 more)
now pass live. The other 2 CI failures on this run
(`test_seeded_flow_routes_as_designed` / `test_same_case_diverges_
across_tenants`) are the already-documented shared-LLM-quota flakiness
below, not a regression. A re-run also showed
`test_kb_entry_roundtrip_embeds_and_scopes` failing (`hybrid_retrieve`
not finding the expected chunk) — passed cleanly in isolation locally
right after, so timing/embedding-variance flake, not a regression from
this chunk either (nothing here touches retrieval).

### Robustness pass, part 2 — job retry had zero backoff, and a permanently-failed job was invisible (2026-09-03)

The fork's original survey (part 1, above) flagged this as its #2
finding, alongside the `available()` bug that turned out to be the
bigger story. Two separate gaps in `interpreter/jobs.py`/`api/worker.py`:

**No backoff on retry.** `jobs.fail()` set a retried job straight back
to `status="queued"` without ever touching `run_after`, so it kept
whatever (already-past) value it had — `claim_job()` could pick it
right back up on the very next poll. A transient outage (a rate limit,
a brief Salesforce blip — exactly what part 1 just made `update_case_
fields` degrade from instead of crashing) got hit 2-3 times back to
back with zero delay, worsening the very failure it was retrying from.
Fixed: `fail()` now sets `run_after` to `now() + min(30s * 2^(attempts-1),
15min)` on every retry that isn't the last one.

**A `status='failed'` job was invisible.** Once `attempts >=
max_attempts`, a job flips to `'failed'` and nothing anywhere —  no
sweep, no health check, no Slack alert — ever looks at that status
again. A real case that failed 3x (plausibly *because* of the
`available()` bug in part 1) would rot in the `jobs` table forever with
no one notified. Fixed: new `sweeps.failed_jobs_sweep` (every 10 min,
following the exact `queue_sweep`/`_page()`/`SWEEP_DRY_RUN` pattern
already established) pages once per newly-failed job, windowed by
`updated_at` so the same job isn't re-paged every tick without needing
a new "already alerted" column. Registered in `api/worker.py`'s
`_SWEEP_EVERY_MIN`/`HANDLERS` alongside the other periodic sweeps —
no new pattern invented.

**Verify:** new `tests/test_jobs.py` (5 tests — backoff grows with
attempts, is capped, is skipped once attempts are exhausted) +
3 new tests in `tests/test_sweeps.py` (pages once per failed job,
dry-run pages nothing, a query failure degrades cleanly). Full offline
suite 569/569. Live-verified `failed_jobs_sweep` against the real
`jobs` table (dry-run, 0 failed jobs currently — clean query, no
schema mismatch).

### Robustness pass, part 3 — the `available()` bug reached beyond `salesforce.py`, plus a real cross-tenant cache leak (2026-09-03)

While designing the multi-tenant concurrency stress test the user's
third selected track asked for, a broader grep for `salesforce.
available()` across the whole codebase (part 1 only checked
`salesforce.py` itself) turned up the same bug in 5 more call sites
still in the live case-processing path: `routing.py`'s
`queue_member`/`_sf_team_member`/`_sf_queue_id` (resolve a Case's
notify/handover target), `sf_context.py::load` (the `sf_context` node,
runs on most flows), `attachments.py::_sf_case_files`, plus 3 redundant
`available()` gates in `agent_reply.py` and `api/worker.py` sitting in
front of functions part 1 had *already* fixed internally — the outer
gate still skipped them entirely for a self-serve tenant. All now use
`salesforce._try_client` (routing/sf_context/attachments) or just
dropped the redundant outer check (agent_reply/worker, since the inner
call already resolves the right tenant). `worker.py::_case_owned_by_user`
had its own direct unguarded `client_for` call, fixed the same way.

**A second, distinct bug found in the same sweep, this one a real
cross-tenant leak, not just a self-serve dry-run gap**:
`salesforce.py::_intake_queue_id` cached the `AI_Intake` queue's Group
id keyed **only by the queue's constant name** — a single global slot,
not per tenant. `routing.py::queue_member`'s cache had the same shape
(`queue_ref` + `org_label`, no `tenant_id`). Since
`scripts/sf_support_setup.py` has every tenant provision identically-
named queues, the **second** tenant to create a Case or resolve a
notify target would silently get the **first** tenant's cached
(wrong-org) Group id — real cross-tenant Case-ownership risk once two
tenants are on genuinely separate Salesforce orgs. Today's two demo
tenants (Acme/Globex) happen to resolve to the exact same underlying
org (confirmed live: `AI_Intake`'s Group id is identical for both),
which is exactly why this stayed invisible — it only became visible by
reading the code with "would this actually work for two *real*,
*different* customer orgs" in mind, the question a concurrency stress
test exists to force. Fixed: both caches now key on
`(tenant_id, org_label)`.

**Verify:** new `tests/test_routing_tenant_scoping.py` (5 tests) —
directly proves the fix for the cross-tenant scenario: two fake tenant
clients with different queue members behind the *same* `queue_ref`,
asserting tenant B's `queue_member()` call returns tenant B's real
member, not tenant A's cached one (and the equivalent for
`_intake_queue_id`). Full offline suite 574/574, zero regressions
despite touching 5 files. Re-ran this PR's CI: the mailbox fix's 5
tests stayed green; the 2 documented shared-LLM-quota-flaky
`test_multiflow` tests plus (newly, this run) `test_kb_entry_
roundtrip_embeds_and_scopes` failed on CI but passed cleanly in
isolation locally right after — consistent with the same shared-quota
root cause (reranking/embedding also draws on the shared provider
pool), not a regression from this chunk (nothing here touches
retrieval).

### Scoped, not built: per-tenant case-taxonomy config

Move `map_case_fields`'s module/region/case-type mapping from a global
hardcoded dict into a per-tenant table (same shape as `policy_rules` —
RLS-scoped, editable via the web), with `scripts/sf_support_setup.py`
reading from that same table instead of carrying its own separate
hardcoded picklist list. Removes the two-sources-of-truth bug class
migration 079 already hit once. Medium-sized: new table + rewritten
mapping function (with a fallback default for tenants that never
configure it, so existing behavior doesn't regress) + setup-script
rework + a web admin surface to edit it.

### Scoped, not built: Google Calendar meeting scheduling

User's ask: a flow node that schedules a meeting with a customer.
Design decided, deliberately not built yet (explicitly deferred to stay
focused on finishing the multi-tenant/Salesforce thread end to end
first): **its own node type** (not folded into an existing one); the
meeting time is decided by **both** — an LLM step (like the existing
`extract` node) pulls a proposed date/time out of what the customer
actually wrote, **and** the node checks the connected Google Calendar's
free/busy for an actually-open slot around that proposal, rather than
either (a) blindly picking a fixed offset from now, or (b) trusting the
extracted time without checking the rep's real calendar. Needs: a new
`interpreter/gcalendar.py` (mirroring `gdrive.py`'s OAuth-credential
shape, likely widening `gdrive.py`'s Google OAuth `SCOPES` to include
Calendar — note this means **already-connected tenants would need to
reconnect** to grant the new scope, since Google doesn't retroactively
expand an issued refresh token's scope), a free/busy API call, slot-
picking logic, and event creation with the customer as an attendee
(optionally a Google Meet link via `conferenceData`). Not scoped in
detail beyond this design — do that as its own pass when picked back up.

**Audit log coverage extended (2026-09-03) — closes a real gap in Phase
28 step 1.** User's request: "improve the logs, who changed what." Step 1
wired `audit.record()` into 6 endpoints (publish/rollback/delete flow,
remove member, approval decisions, connections); auditing 20+ other
mutating endpoints that clearly count as "who changed what" for an
admin/support-manager trust story. Added: `tenant.created`,
`invitation.{created,revoked,accepted}`, `flow.created`,
`flow.sf_entry_{set,unset}`, `trigger.{created,deleted}`,
`kb_collection.{created,updated,deleted}`,
`kb_entry.{created,updated,deleted}` (create/upload/Google-Doc-link/
edit/delete — a Google Doc re-sync counts as `kb_entry.updated`),
`email_channel.{connected,configured,disconnected}`,
`policy_rule.{created,updated,deleted}`. **Deliberately not audited:**
`PUT /api/flows/{flow_id}` (the draft autosave/Save-button path) — every
explicit save during active editing would flood the log with WIP noise;
the decision points that matter (publish/rollback/delete) were already
covered by step 1, matching that step's original scoping choice. Run-
triggering endpoints (`run`/`enqueue`/`/t/{token}`/the Salesforce hook)
stay unaudited too — those are executions, already recorded in `runs`,
not config/content mutations. **Verify:** the full offline suite (499
tests, zero regression) plus the full **live** integration suite (42/42
passed, ~9.5 min) — most of the new call sites are exercised by
pre-existing integration tests (`test_policy_rule_crud`,
`test_invitation_create_list_revoke`,
`test_email_channel_configure_status_and_disconnect`,
`test_kb_entry_roundtrip_embeds_and_scopes`, etc.), so this was a
genuine live runtime check, not just an offline compile check. Confirmed
by querying the live `audit_log` table directly after the run: all 16
new action types that got exercised appear with correct rows (e.g.
`policy_rule.created/updated/deleted`, `kb_entry.created/updated/deleted`,
`email_channel.connected/configured/disconnected`). **Not live-fired in
this pass** (no existing test happens to call them, though they're the
same pattern as everything that did verify): `tenant.created`,
`invitation.accepted`, `trigger.created`/`trigger.deleted`.

**Web workspace switcher (2026-09-03) — found live, from the user's own
account.** User reported seeing `tenant_id required (you belong to
several tenants)` in the UI. Root cause, confirmed by querying the live
DB directly: the account genuinely owns 2 workspaces (the seeded Acme
tenant + a self-created one), and `web/src/App.tsx` **never had a
workspace switcher at all** — it only ever checked "0 memberships"
(show the create-workspace screen) vs. "1+" (silently use... nothing
explicit; every API call just omitted `tenant_id` and relied on
`api/main.py::_caller_tenant()`'s "if exactly 1 membership, infer it"
shortcut). The moment an account has 2+ memberships, every tenant-scoped
listing/creation endpoint (Team, Channels, Connections, Rules, Billing,
Activity, creating a flow or a KB collection) 400s — this was never
caught before because every account used in testing this whole session
happened to belong to exactly one tenant.

**Fix:** `App.tsx` now tracks a real `tenantId` (from `listTenants()`),
persisted per-user in `localStorage` (`workspace:<user_id>`). With 2+
memberships and no valid stored choice, a **"Choose a workspace"**
picker screen (same visual style as the existing "Set up your
workspace" screen) lists each workspace + the caller's role in it; once
chosen, a `<select>` switcher appears in the header (only when 2+
memberships — no clutter for the common single-tenant case) to change
it later. Every tenant-scoped view (`FlowList`, `BillingView`,
`ConnectionsView`, `TeamView`, `ChannelsView`, `RulesView`,
`KnowledgeView`, `ActivityView`) now takes an explicit `tenantId` prop
and is remounted (`key={tenantId}`) on switch so it refetches cleanly,
instead of relying on the backend's single-membership inference. Along
the way, fixed a second latent bug the switcher surfaced: `role`/
`canEdit`/`isOwner` were previously derived as the **best role across
ALL memberships** — an owner-in-A-but-viewer-in-B account would see
owner-only tabs while actually working in B. Now derived from the
*current* tenant's own role.

Backend: `GET /api/rules` gains an optional `tenant_id` filter (it
previously mixed every visible tenant's rules into one unlabeled list —
`RulesView` had been inferring the "current" tenant from `rules[0]`,
fragile and simply wrong once rules from 2 tenants were mixed together).
Every other touched endpoint (`connections`, `members`/`invitations`,
`email` channel, `kb.createCollection`, `createFlow`, `rules.create`)
already accepted an optional `tenant_id` server-side — only the
frontend had never been sending it.

**Not fixed, a smaller residual noted, not chased further:** `GET
/api/invitations` (RLS-scoped, no tenant filter) can show pending
invites from more than one owned tenant mixed together, unlabeled, for
an account that owns 2+ workspaces — a display nit, not an error path,
lower priority than the switcher itself.

**Verify:** `tsc -b` clean, `vitest run` 6/6, `npm run build` clean,
full Python offline suite 499/499 (touched `list_rules` only). **Not
live-browser-verified in this pass** — no working headless-browser
screenshot tool in this sandbox (playwright's chromium needs system
libs requiring `sudo`, unavailable here) and no way to sign in
programmatically as the real reporting account without its password;
Vite HMR picked up every file change cleanly with no console errors.
Needs the user's own browser refresh to close the loop.

**Phase 28 — a 6-step ordered feature list (infra-first, each step
reuses the last): 1. platform activity/audit log ✅ · 2. flow-version
rollback audit trail + `flow_versions` retention ✅ · 3. billing quota
enforcement ✅ · 4. per-flow cost breakdown ✅ · 5. flow templates
marketplace ✅ · 6. bulk KB export/import ✅. PHASE 28 COMPLETE
(2026-09-03) — all 6 steps done, no open phase.**

**Step 1 — platform activity/audit log (COMPLETE 2026-09-03).**
Migration `076` `audit_log` (append-only, `case_events`-style
conventions — free-text `action` slug not an enum, RLS member-read only
/ **no write policy**, every insert goes through the service-role
client, matching `review_tasks`). `interpreter/audit.py::record()` —
best-effort, never raises (an audit failure must not break the mutation
it's recording). Wired into 6 existing endpoints in `api/main.py`:
`publish_flow` / `rollback_flow` (records `from_version`/`to_version` —
this already closes most of step 2's "rollback audit note" gap) /
`delete_flow` / `remove_member` / `decide_action_request_ep` (KB-change
+ task approvals) / `create_connection` + `delete_connection`.
`GET /api/audit` (member-readable, like Runs — not owner-gated). Web
**Activity** tab (next to Runs): a plain table + an action-type filter
built from the events seen. `tests/test_audit.py` (3 offline tests).
**Live-verified end-to-end** via a real authenticated request through
the actual FastAPI app (not just the helper): publish →
`flow.published`; rollback → `flow.rolled_back` with
`{"from_version": 10, "to_version": 9}`; add/remove a connection →
`connection.added` / `connection.removed`; `GET /api/audit` returned all
four, RLS-scoped to the caller's tenant.

**Step 2 — flow-version rollback audit trail + retention (COMPLETE
2026-09-03).** The "rollback note" half was already closed by step 1's
`rollback_flow` wiring (above) — this step is the remaining
`flow_versions` retention/pruning debt noted in "Known issues" below.
Migration `077` `purge_old_flow_versions(keep_last=20, min_age_days=90)`
— unlike `purge_old()`'s blind age cutoff for `runs` (pure telemetry), a
`flow_versions` row is a live rollback target, so this keeps the last N
versions per flow *and* anything newer than `min_age_days`
unconditionally, and — critically — **never deletes a flow's currently
published version regardless of age or rank** (`rollback_flow`
re-points `published_version` at an old version number directly rather
than re-snapshotting, so a naive "keep the N highest version numbers"
policy could otherwise delete a live rollback target out from under a
flow). `scripts/purge_old.py` now also calls this RPC (new
`--fv-keep-last` / `--fv-min-age-days` flags), so the existing nightly
`daily-sync.yml` "Purge old jobs + runs" step covers it automatically,
no workflow change needed. **Live-verified** against a throwaway flow
with 10 backdated `flow_versions` rows (spanning ~180 days) and an
intentionally *old, low-ranked* published version: `purge_old_flow_versions(3,
100)` correctly kept the old published version (protected despite being
rank-10 and 160 days old) plus the recent top-ranked ones, and deleted
only the genuinely old, unpublished, low-ranked rows — then
`python -m scripts.purge_old` against the real (only ~9-day-old)
database correctly no-opped (`flow_versions=0`), proving the default
settings don't touch live data.

**Step 3 — billing quota enforcement, warn-only (COMPLETE 2026-09-03).**
User's explicit call: warn, never block — a hard block risked silently
dropping a real inbound customer email once the live tenant crosses the
free plan's 200 runs/month (it was already at ~20-40 from this session's
own testing). `interpreter/billing.py::check_and_warn(sb, tenant_id)` —
called from `interpreter/runs.py::record_run` right after every run is
recorded (so it fires regardless of trigger: manual, webhook, email,
Salesforce CDC), best-effort. Computes the tenant's current-month
`usage_summary()` (reusing P9); at ≥80% logs `billing.quota_warning`, at
≥100% `billing.quota_exceeded` via `audit.record()` (step 1) — **at most
once per (tenant, period, level)**, deduped against `audit_log` itself
(no new table). If the tenant already has a Slack digest channel
configured (`tenant_integrations.config.digest.channel`, reused from
P8a's KIL digest — no new external setup required), also posts a
one-line heads-up there; unlimited (`pro`) plans and channel-less
tenants skip that step silently. Nothing about a run's own execution or
response is affected — nothing checks the return value to gate anything.
`tests/test_billing.py` gains 7 offline tests. **Live-verified** against
an isolated throwaway tenant with synthetic `runs` rows: 160/200 (80%)
→ `billing.quota_warning` logged once, a second call correctly deduped
(no duplicate row); pushed to 200/200 (100%) → `billing.quota_exceeded`
fired. Also **live-verified the real Globex tenant stayed silent** at
its actual 38/200 (19%) usage — no false positive.

**Found and fixed in passing — a real, pre-existing bug, not
environment noise:** `interpreter/runs.py::record_run()` had dead code
— a `return run_id` sitting *after* the unrelated `_int_env()` helper
function instead of at the end of `record_run`'s own body, so the
function always implicitly returned `None` regardless of whether the
run was recorded. This has been silently breaking `POST
/api/flows/{id}/run`'s `run_id` response field, the worker's job
result, and the CLI's "recorded run …" message — and was the actual
cause of `tests/test_api.py::test_run_returns_a_run_id` and
`test_publish_snapshots_and_run_records_the_version`, two tests
mischaracterized as "the known live-Supabase seed-flow gap" in every
verification report across this entire session (P8c through step 2 of
this phase) without their actual failure text being checked each time.
Moving the `return` statement fixed both; the other 4 previously-lumped
failures were re-verified individually and are genuinely the seed-flow
gap (`FlowNotFound`), unrelated.

**Step 4 — per-flow cost breakdown (COMPLETE 2026-09-03).** The
Billing tab (P9) only ever showed tenant-wide totals — no way to see
*which flow* is spending the tokens. `interpreter/billing.py::usage_summary()`
gains an optional `flow_names` param and groups every run's
`tokens_total`/`tokens_by_model` by `flow_id` into a new `by_flow` list
(`{flow_id, name, runs, tokens, estimated_cost_usd}`, sorted by tokens
descending; a run with no `flow_id` — shouldn't normally happen, but
handled — is still counted in `runs_count`/`tokens_total`, just dropped
from the per-flow rows rather than crashing). `GET /api/billing/usage`
now also selects `flow_id` on the `runs` query and joins tenant's
`flows.name` for display. Web Billing tab gains a **by flow** table
(flow / runs / tokens / est. cost) between the daily chart and the
tokens-by-model breakdown. `tests/test_billing.py` gains 2 tests
(grouping/sorting/naming, and the no-flow-id-drops-cleanly case).
**Live-verified** against the real Globex tenant: `by_flow` correctly
attributed all 41 runs / 26,983 tokens to "Globex Support —
human-review-first" with the real name resolved via the `flows` join
(single-flow tenant, so multi-flow grouping/sorting was proven by the
offline unit test instead).

**Step 5 — flow templates marketplace / save-as-template (COMPLETE
2026-09-03).** `interpreter/templates.py` (P7a) only ever served 4
built-in, file-shipped templates — no way for a user to save one of
*their own* flows as a reusable template. **Scope decision (narrower
than "marketplace" implies):** save-as-template is scoped **within the
tenant that saves it** — private to that workspace. True cross-tenant
sharing (a flow's structure becoming visible to other customers) is a
materially bigger, security-sensitive decision this codebase has never
made elsewhere (multi-tenancy is RLS-enforced everywhere, "no
cross-tenant leak" explicitly verified in Phase 12) — deferred, not
built, per "don't build ahead."

Migration `078` `flow_templates` — same RLS split `flows` itself uses
(migration `032`: `is_tenant_member` read / `is_tenant_editor` write,
via the caller's own RLS-scoped client, not `_service` — this table
isn't secret-bearing). `interpreter/templates.py` gains
`list_custom`/`custom_graph`/`save_as_template`/`delete_custom`
alongside the untouched built-in `list_templates`/`graph`. API:
`POST /api/flows/{id}/save-as-template` (snapshots the flow's current
*draft*, same pattern as `publish_flow`'s `load_flow(status="draft")`);
`GET /api/templates` now merges built-in + the caller's custom ones
(best-effort — a tenant-resolution failure silently falls back to
built-in-only, never breaks the base gallery); `GET
/api/templates/{id}` falls back to a custom lookup when the id isn't
built-in; `DELETE /api/templates/{id}` (custom only — 404 for a
built-in id, which isn't a database row). Both save and delete log
through Phase 28 step 1 (`template.saved` / `template.deleted`). Web:
FlowEditor gains a **💾 Save as template** button next to Publish
(prompts for name/description, same no-modal UX as the rest of the
app); FlowList's existing "📋 From template…" picker shows custom
entries tagged "(custom)", plus a **🗑 delete a custom template**
button (prompts for a name match + confirms) that appears once at
least one exists. `tests/test_templates.py` (existing file from P7a)
gains 6 new offline tests for the custom-template functions; the
existing built-in-gallery test's exact-shape assertion was updated for
the new `source` field. **Live-verified end-to-end** through the real
FastAPI app: saved a template from the live Globex flow → appeared in
the merged list tagged `custom` → its graph fetched cleanly (9 nodes,
0 errors) → `audit_log` gained `template.saved` → deleted it → gone
from the list → `audit_log` gained `template.deleted` → confirmed the
built-in gallery (`support-autoreply`) still works unaffected.

**Step 6 — bulk KB export/import (COMPLETE 2026-09-03, PHASE 28
COMPLETE).** No backup/restore path existed for a KB collection — losing
one meant re-authoring every entry by hand. `interpreter/kb_backup.py`
(new, pure — no Supabase calls, same split as `billing.py`/
`kil_metrics.py`): `export_bundle()` shapes a downloadable JSON
(`{collection: {name, description}, entries: [{title, body_md,
status}]}`, active + provisional only — not archived/superseded, matching
what a "restore this collection" workflow wants: the current KB, not its
retired history); `normalize_import_entries()` validates/dedupes an
untrusted uploaded bundle (missing title/body → dropped with a warning,
duplicate title within the bundle → dropped, capped at
`MAX_IMPORT_ENTRIES`=500) — never raises, degrades to warnings, matching
`flow_candidate.assemble_candidate`'s style for untrusted JSON. `GET
/api/kb/collections/{sid}/export` (synchronous — reads are cheap). `POST
/api/kb/collections/{sid}/import` — async, mirrors P7c's `/crawl`
exactly: enqueues a new `import_kb_bundle` worker job
(`api/worker.py::_import_kb_bundle`, upsert-by-title within
`origin='import'` so re-importing the same backup updates existing
entries rather than duplicating, embeds each entry via the existing
`embed_kb_entry` job off-thread) and logs `kb.import_started` through
Phase 28 step 1. No migration — `kb_entries.origin` is already a
free-text column (024), `"import"` is just a new value, same convention
as `manual`/`gdoc`/`file`/`crawl`. Web KnowledgeView gains **⬇ export**
(downloads a `<collection>-backup.json` via a Blob URL) and **⬆ import
backup** (file picker → POST, alerts with the accepted count + any
warnings) next to the existing crawl/upload buttons.
`tests/test_kb_backup.py` (new, 7 offline tests for the pure module).
**Live-verified end-to-end through the real (rebuilt) Docker stack** —
not just the API layer: exported the real `globex-billing-runbook`
collection (1 entry) → imported it into a throwaway collection → the
**live worker container** picked up the job and logged `job … done` →
confirmed in the DB the entry landed `status=active`, `origin=import`,
genuinely chunked + embedded (`chunk_count=2`, a real `embedded_at`) →
a second import with a missing-body entry and a duplicate title
correctly accepted 1 and reported 2 warnings, also verified embedded →
cleaned up the throwaway collection. **PHASE 28 COMPLETE** — all 6
steps of the ordered feature list are done; no open phase.

**Phase 27 — the Case Control Plane (done, 2026-09-01/02).** One
AI-managed Case queue: classify + route + track `Status` + hand off via
Omni-Channel, so no case sits unowned / unmoved / unwatched. Full design +
build plan: `docs/` artifact
`https://claude.ai/code/artifact/403e72fa-beef-4f12-809f-7511f6d81ca0`;
Salesforce-org steps: **`docs/CASE_CONTROL_PLANE_SF.md`**.

Repo side done (branch `phase-27-case-control-plane`, 312 offline pytest):
- **27a** — migration `062`: `case_events` (append-only per-Case audit log,
  RLS, separate from `runs.trace`) + `sla_policy` ((tier, routed_team) →
  ack/resolve; seed ack 30 / resolve 4·8·24h). `scripts/sf_support_setup.py`
  gains a `cp_fields` stage (9 Case fields incl. `Routed_Team__c` picklist),
  Status += `Triaged`/`In Progress`/`Resolved`, queues `AI_Intake` /
  `Unrouted_Review` / `SLA_Breach`.
- **27c** — `interpreter/case_events.py` (record/link_run); `registry.py`
  `_cp_fields`/`_cp_write` wired into every node: `sf_writeback` → `Status =
  Triaged` (guarded — won't downgrade an advanced Case) + `Routed_Team__c` +
  run linkage; `confidence_gate` → `AI_Confidence__c`; `ask_human`/`handover`
  → `Escalated` + `Routed_Team__c` + `Next_Action_Due__c` (+30m) +
  `Escalation_Reason__c`; `clarify` → `Waiting on Customer` (+72h) or
  `Escalated` when exhausted; `notify`/`notify_human` → `In Progress` +
  `Handoff_Slack_Ts__c`. `salesforce.get_case`/`ensure_case` now surface
  `Status`/`OwnerId`.
- **27d** — `interpreter/sweeps.py`: `queue_sweep` (overdue / stuck /
  escalated-unaccepted → nudge+re-route, then `SLA_Breach__c` + page),
  `cdc_reconcile` (Cases with no `runs` row → enqueue), `reasoning_ttl`
  (stale `reasoning_sessions` → nudge → escalate+abandon). Run in the
  `api.worker` loop, self-re-enqueue; `SWEEP_DRY_RUN=1` / `SWEEPS_DISABLED=1`.
  `health_check` flags an SLA-breach spike.
- **27e** — migration `063`: `notify_targets` gains
  `slack_channel`/`slack_usergroup`/`urgency` + `match_kind = routed_team`.
  `routing.resolve_slack_route` (routed_team > module > case_type →
  `#cx-*` + `@*-oncall`; miss → `#cx-unrouted`). `alert.py` reasoning-thread
  root @mentions the usergroup + carries tier/type/team/confidence/nearest
  resolutions.

**27b Omni-Channel — scripted + live (2026-09-01).** No Flow, no Apex, no
pipeline code. `scripts/sf_omni_setup.py` creates the `Support_Case`
ServiceChannel, 3 presence statuses + the channel link, `RC_Standard` /
`RC_Priority` QueueRoutingConfigs attached to the 7 `Team_*` / reason
queues, and `PC_Support_Agent` (capacity 3). **Key finding:** once a queue
has a routing config, Salesforce *auto-creates* the PendingServiceRouting
when a Case's OwnerId is set to it — which `ask_human` / `handover` already
do via `assign_case(queue=…)`. Verified live (a re-assign to `Team_CSM`
produced a ready PSR). Two Setup-only bits remain: grant the agent permset
access to the presence statuses, and add the Omni widget to the console so
an agent can go "Available".

**27f / 27g — applied live (2026-09-01).**
- **27f** part A: `salesforce.ensure_case` sets `OwnerId = AI_Intake` on every
  pipeline-created Case. Part B **run**: `scripts/sf_assignment_cutover.py`
  backed up the live `Standard` rule (`scripts/_assignment_backup/`, 6 stock
  entries) and swapped it for one catch-all → `AI_Intake`; `--restore` reverts.
  Verified: rule now has 1 entry, `formula=true → Queue AI_Intake`.
- **27g**: `scripts/sf_backstops.py` **run** — `Close_Needs_Type` validation
  rule live + active (no Close without `Type` + `Description`). The two list
  views are a 60-second Setup task (column tokens fight the metadata API);
  native Escalation Rule skipped (the `queue_sweep` covers it).

**27h interactive card — built (2026-09-01).** `alert._handoff_card` renders
the reasoning-thread root as a Block Kit card (Send as-is / Edit in thread /
Reassign… / Not my team); `slack_socket.dispatch_action` handles the clicks
(Socket Mode `interactive` envelopes over the existing WSS); a `route: <team>`
reply updates `Routed_Team__c` + re-assigns the queue + writes a
routing-correction `case_events` row. One Slack-app toggle to activate:
Interactivity ON (no request URL under Socket Mode). 319 offline pytest.

**Design gaps closed (2026-09-01) — every "Closed by" in the artifact's Gaps
table is now real code:**
- The Slack reasoning dialogue's delivery (`slack_socket._deliver`) sets
  `Status = Resolved` + a `case_events` `action=send` row when the reply
  actually lands.
- `_check_resolution`'s final give-up (`no_reply`) now flips
  `SLA_Breach__c` + pages + writes a `breach` event — not just a `runs` row.
- `queue_sweep` gained two branches: a stale `Waiting on Customer` Case
  auto-resolves (design state-machine WOC→Resolved timer) with a "reply to
  reopen" Chatter note; an `Escalated` Case that Omni never routed
  (`Routed_Team__c` empty / still `AI_Intake`-owned) is dead-lettered to
  `Unrouted_Review` + paged.
- `slack_socket` re-drives every open `reasoning_sessions` thread on a WSS
  reconnect (a dropped socket no longer strands a dialogue mid-turn).
- `/api/trace/<case>` folds in `case_events` as the timeline spine.

**Slack workspace live (2026-09-02).** All 8 `#cx-*` channels created and
the `support_automation` bot invited to each; 7 on-call usergroups created
(`@cx-l1-oncall` / `@cx-tier2-oncall` / `@cx-csm-oncall` / `@cx-sales-oncall`
/ `@trust-oncall` / `@billing-oncall` / `@cx-leads`). Bot granted
`usergroups:read` + `usergroups:write`. Socket Mode verified connecting
(`socket connected`, no scope errors) with the operator's `SLACK_APP_TOKEN`.
Code: `slack.usergroup_ref()` resolves an on-call handle to `<!subteam^ID>`
(a bare `@handle` in message text does **not** page a group — it was inert);
`alert.alert_human` now calls it. `notify_targets` csm/sales rows corrected
to `@cx-csm-oncall` / `@cx-sales-oncall` to match the created handles.
`slack.SCOPES` (OAuth install) gains `usergroups:read`. 324 offline pytest.

**SF Setup done (2026-09-02).** Presence-status access + the Omni utility
widget + an agent going "Available — Cases" — all applied by the operator
(the presence menu only offered Offline because *zero* `ServicePresenceStatus`
grants existed — sys-admin does **not** get them automatically; a
`CP_Omni_Presence` permission set with the 3 statuses is the fix). The E-6001
"Couldn't Connect to Telephony" toast was the stock Phone/Voice softphone
utility (org has 0 CallCenters) — unrelated to Omni; removed from the console.

**27g "single queue" layout (2026-09-02).** The two design list views already
existed and match artifact §Salesforce column-for-column: **`Live Queue`**
(`Closed=0`, scope Everything; Case# · Subject · Status · Routed_Team__c ·
Next_Action__c · Next_Action_Due__c · AI_Confidence__c · owner) and
**`SLA Breach`** (`SLA_Breach__c=true` AND `Closed=0`; + Escalation_Reason__c).
Deployed via `sf project deploy` (mdapi, v64): a Case **compact layout**
`AI_Control_Plane` (Status · Routed_Team__c · Next_Action_Due__c ·
AI_Confidence__c · Priority) for the highlights panel, and an **"AI Control
Plane" section** (Routed_Team__c/Escalation_Reason__c/AI_Confidence__c/
Handoff_Slack_Ts__c | Next_Action__c/Next_Action_Due__c/Last_AI_Run_At__c/
Last_Run_Id__c) spliced after "Case Information" on `Case Layout` +
`Case (Support) Layout`. Additive — every existing section preserved; verified
by retrieve.

Remaining (Setup clicks, not metadata): set `AI_Control_Plane` as the primary
Case compact layout; `Live Queue` → Kanban grouped by Status, sort
Next_Action_Due__c ↑, pin as default; add the Omni Supervisor tab for leads.
Everything upstream is code-complete.

**27a live-applied + 27a/c/d live-verified (2026-09-01).** Migrations `062`/
`063` applied; `sf_support_setup.py --only queues --only cp_fields --only
types --only fls` run against the org (3 queues, 9 Case fields, Status
values — a Checkbox field needed `defaultValue` set explicitly, fixed).
Then dry-ran the sweeps against the real org/Supabase (PR #23):
- **Found + fixed a false-breach storm** — `queue_sweep` flagged all 92
  pre-existing open Cases as SLA breaches, because "stuck" judged purely on
  `Status` + `CreatedDate` age with no way to tell a Case the pipeline has
  started managing (has `Next_Action_Due__c`/`Routed_Team__c`/
  `Last_AI_Run_At__c` set) from the pre-cutover backlog. Now skips untouched
  Cases entirely (`cdc_reconcile` is the backstop for those) and measures
  "stuck" from `Last_AI_Run_At__c` when set, not `CreatedDate` (a re-triaged
  Case has an old `CreatedDate` but should read as fresh).
- Verified end-to-end on a real Case (`00001189`): `update_case_fields`
  write + `case_events` insert both succeed through live FLS/RLS; the sweep
  then correctly reads it as touched-but-not-overdue. Reverted after.
- `reasoning_ttl` / `cdc_reconcile` verified against a real Supabase client
  (0 stale sessions; correctly found the existing `runs` row via
  `case_payload->>sf_id` OR `case_id`, no duplicate enqueue).
- The CI pipeline itself needed two fixes unrelated to Phase 27 but only
  now surfaced (this branch's PR was the first to run true CI with zero
  `.env`): `test_fallback_chain_uses_the_roster` never set `GROQ_API_KEY`
  so `_dedup_available` correctly dropped the Groq model in every CI run;
  and `web/src/flows/graph.ts` + `FlowEditor.tsx` had two independent
  `TERMINAL` node-type sets that drifted apart after Phase 24d removed
  `auto_reply`'s send path — consolidated into one exported set.

**Audit remediation pass done (2026-09-01) — WF / NEO / SB / Oracle items.**
One batch closing the yellow/green items from the deployment audit. 291
offline pytest + web build green.
- **24d** (committed `57c9bbc`): removed the Salesforce "approve" shortcuts —
  `_send_bot_draft` / `bot_send_draft` trigger / `looks_like_send_command` /
  the `Send_Bot_Draft_to_Customer` QuickAction + `Bot_*` fields in the SF org.
  Slack reasoning is the only send path now.
- **WF-1** — migration `061`: the email flow's `confidence_gate` now carves
  `answer_mode == 'action'` out of every self-serve branch → `handover`
  (a human performs the action). `escalate_answer_modes: ["action"]` on the
  node; email `sf_entry` flow → **v13**; portable `flow_email_l0l1.json` updated.
- **WF-3** — `h_clarify`'s round counter keys on `runs.case_payload->>'sf_id'`,
  not `runs.case_id` (which `get_case` fills with the CaseNumber → the counter
  silently reset every pass, so `max_rounds` never bit).
- **WF-4** — `scripts/health_check.py`: alerts when >30 % of the last hour's
  runs fell back to the offline LLM stub, and when a `neo4j` heartbeat exists
  but has gone stale (graph enrichment silently off).
- **WF-5** — `POST /api/trace/{key}/retry` (auth + tenant-scoped): re-enqueues
  the flow for the Case behind a run/case/sf_id/case-number key. Editor gets a
  **retry** button on the trace view (`web/src/trace/TraceView.tsx`).
- **WF-6 / NEO-3** — `case_memory_sync` tags same-account near-duplicates and
  `case_memory.py` MERGEs `(:Case)-[:DUPLICATE_OF]->(:Case)` for score ≥ 0.92
  same-account pairs where the other case resolved earlier. `_enrich_from_sf`
  now also pulls `AccountId` / `IsClosed` / `ClosedDate`; a Case that closed
  and reopened is marked `resolution_kind="reopened"`, `generalizable=False`
  (dropped from the citable set).
- **NEO-2** — `neo4j_sync` emits a `beat("neo4j", …)` heartbeat after each sync.
- **NEO-4** — `neo4j_sync.ensure_constraints` adds uniqueness constraints for
  `:Case {sf_id}`, `:Reply {case_sf_id}`, `:Module {name}` (try/except).
- **NEO-5** — `case_memory._graph_duplicates(sf_ids, tenant_id=…)` now pins
  **both** ends of the `(:Case)-[:DUPLICATE_OF]->(:Case)` traversal to the
  caller's tenant, so a duplicate edge from another tenant's Case on the
  shared graph can't boost this tenant's ranking. `lookup()` forwards its
  `tenant_id`.
- **SB-2** — the running stack needs no pooler (PostgREST HTTP only). The one
  direct connection — the nightly `pg_dump` on the Oracle VM — is documented
  to use the **Session pooler** URI (`…pooler.supabase.com:5432`), not
  "Direct connection" (IPv6-only on the free tier; the Always-Free VM has no
  IPv6) and not the transaction pooler (`pg_dump` needs session state). See
  `docs/DEPLOY_ORACLE.md`.
- **SF-1** — one intake path, decided **and enforced**: Salesforce native
  Email-to-Case (Path B) opens the Case, CDC fires `case_created`; the IMAP
  poller (Path A) is off. New `SF_INTAKE_MODE` env (default `salesforce_e2c`;
  `poller` / `both` opt in): `mailbox.poller_is_intake()` gates
  `list_pollable_channels` **and** `email_watch.tick` → both return nothing
  unless the poller is in the mode, so an `active` channel row can't silently
  double-create Cases. `config.validate_env` warns on a bad value; `.env.example`
  documents it. (The DB channel row is also `status='inactive'` today.)
- **SB-5** — CLAUDE.md: migrations are applied by hand only (Supabase MCP
  `apply_migration` / SQL editor), never the CLI; `.sql` files are the source
  of truth, `supabase_migrations` is not kept in sync.
- **SB-6 / Oracle** — `docs/DEPLOY_ORACLE.md`: nightly `pg_dump` cron (Supabase
  free tier has no PITR), `chmod 600` on the copied secrets, `fastembed`
  pre-warm after `up -d`, and a "stop the API / Caddy-for-TLS" note since
  nothing needs the API inbound (CDC, not HTTP callout).
- All audit items (WF / NEO / SB / SF / Oracle) are now closed.

**Phase 26 done (2026-09-01) — live free-model roster + signature/logo filter.**
OpenRouter's free tier churns weekly (all classic `:free` slugs already gone),
so nothing is hardcoded. Migration `060` = `llm_roster` + `signature_hashes`.
`scripts/refresh_llm_roster.py` (daily via `daily-sync.yml` + VM cron): scores
whatever costs $0 on OpenRouter *today* by vendor reputation / context window /
param hint / modality, writes 6-deep free chains + a cheapest-capable-paid tail
for `text` / `vision` / `video`. `interpreter/roster.py` caches it (no-op under
pytest); `llm._fallback_chain` / `_vision_chain` / `_video_chain` build from it
— free-first, premium only on total failure; env vars still override.
`attachments.looks_like_signature()` + `signature_hashes` skip an image before
any OCR / vision call when it's tiny / banner-shaped / named like an inline sig
(`image00x.png`, `logo`, `linkedin`…) or the same md5 has been seen 2+ times
from that sender domain — node `skip_signatures` (default on) + `min_image_px`,
editor checkbox. 296 offline pytest (8 new). Also: runtime image 962→719 MB
(scraper / Google / pytest deps split into `requirements-{ingest,connectors,dev}.txt`;
`MEDIA=1` build arg for the OCR/video stack).

**Phase 25 done (2026-09-01) — multimodal + Salesforce context, intelligence
in nodes.** Routing decisions stay deterministic edge expressions; an
`ai_prompt` node writes structured output and edges branch on it.
- `llm.complete(images=[(bytes, mime)])` → a **vision chain**: free OpenRouter
  vision models → paid Anthropic Haiku (`LLM_VISION_MODELS` / `LLM_VISION_PAID`)
  → deterministic stub. `_run_chain` extracted; Anthropic/OpenRouter build
  image content blocks.
- **`attachments` node** (`interpreter/attachments.py`) — Case images via
  `ContentDocumentLink`→`ContentVersion` blob + local **RapidOCR** (ONNX, CPU,
  no torch; import-guarded). Writes `state.attachments` / `.attachment_text`
  (folded into `classify` + `draft` for free) / `._attachment_blobs` (bytes for
  vision, not persisted). **Video** (opt-in `video: true`): audio transcript
  via **faster-whisper** (CT2, CPU, no torch) + `ffmpeg` keyframe sampling →
  each frame OCR'd; transcript + on-screen text join `attachment_text`, 2
  keyframes go to `_blobs`. Config `video_frames` / `video_max_seconds`.
  Dockerfile gains `libgl1 libglib2.0-0 libxcb1 ffmpeg`.
- **`sf_context` node** (`interpreter/sf_context.py`) — Account (+ parent =
  organization), Contact + siblings, Lead, Case history, Account team Users →
  `state.sf_context`. Best-effort.
- **`ai_prompt` node** — `{system}`/`{user}` templates interpolating
  `{case.subject}` / `{sf_context.account.tier}` / `{attachment_text}` /
  `{ai.x}`; `model`, `temperature`, `max_tokens`, `output_key`, `json_schema`,
  `images` (`none`|`auto`), `cache`, `on_error`. Writes `state.ai[output_key]`
  — a declared channel with an `operator.or_` reducer so a dynamic key isn't
  dropped by the graph merge.
- `state.py`: `attachments` / `attachment_text` / `_attachment_blobs` /
  `sf_context` / `ai` channels. `builder._context` exposes `sf_context` / `ai`
  / `attachments` to edge conditions. `classify` `tier_field`/`region_field`
  resolve against state first.
- Editor: `AiPromptForm` / `SfContextForm` / `AttachmentsForm` in
  `Inspector.tsx`; `NODE_DEFAULTS` entries. Comprehensive template
  `interpreter/flows/flow_sf_comprehensive.json` (every node config filled;
  **not seeded** — flip `sf_entry` when ready). Gate edges also route
  `answer_mode == 'action'` and `ai.triage.churn_or_legal_risk` → `handover`.
- 288 offline pytest (16 new). OCR + video path verified live in the worker container.

**Phases 0–20 built. No open phase.** Migrations `001`–`044`
(`034`/`035` = Phase 20a: `tenant_integrations` poller columns + the
Supabase-Vault `integration_secret_*` RPCs; `036` = Phase 20e: the
"Email L0/L1 — inbound to Salesforce" flow, team `email`, tenant Acme;
`037` = Phase 20f: that flow's `sf_case` → thread-based Case reuse;
`042` = Phase 20k: the `flows.sf_entry` flag — applied 2026-08-31 via the
Supabase SQL editor, `f0f0f0f0-…` (router) backfilled `sf_entry=true`;
`043` = Phase 20l: `sf_cdc_state` — applied 2026-08-31 via the SQL editor;
`044` = Phase 20n: the `notify` node + `Case.Type` triage — **applied
2026-08-31** via `apply_migration` to the email L0/L1 flow (the only flow in
this DB; it's the `sf_entry` flow), published as **v3**. The Case-router
flow `f0f0f0f0…` was never seeded to this DB — migration `044`'s router half
lives in `scripts/seed_router_flow.py` + the portable JSON for whenever it
is stood up; `045` = Phase 20o: `notify_targets` — **applied 2026-08-31**,
seeded 7 rows for tenant `00000000…`; `046` = Phase 20p: the email flow →
the single comprehensive workflow (`team_route` + 5-way gate) — **applied
2026-08-31**, published **v4**). Migrations `047`–`053` land the resilience
work and the `notify_human` / double-tag fixes — see the Phase 23* entries
below; the email `sf_entry` flow is now at **v9**.
296 offline pytest (24a-f + 25 + 26) tests + web tsc/vitest (6)/build +
`tests/test_multiflow.py` (needs Groq quota). **`docs/REQUIREMENTS.md`** is
the spec; its §9 tracks gaps.

**Next test email should show:** exactly 1 run, 1 inbound EmailMessage, and
— on an `ask_human`/`handover` escalation — ONE Chatter @mention (from
`notify_human`) plus ONE private `[bot draft …]` CaseComment. If Slack is
wired (`SLACK_ALERT_WEBHOOK` or per-tenant OAuth) the same escalation also
posts to `#support-escalations` (or the per-team channel).

**To resume the bot after a human:** reply either as a **Case Comment** or as
a **reply on the bot's Chatter @mention post** — both are picked up within
`FEEDBACK_POLL_MIN` (5 min, up to 12 checks). "send it" / "send this to the
customer" sends the bot's stored draft as-is; a substantive note is applied
to that draft first. Requires the email channel's `auto_send_enabled=true`
(it is, for tenant `00000000…`), else the polished reply is only left as a
draft CaseComment.

**Phase 24 (2026-09-01, in progress): human-in-the-loop reasoning before any
response.** New model: no case gets an AI answer from automation — every
response goes through a Slack reasoning dialogue with the responsible agent
(a bank of 4–6 per-type "pointer questions" the bot works through *in full*,
bot proposing / agent confirming), then an explicit send confirmation. The
SF approve shortcuts (Chatter `send`, the "Send Bot Draft" button) are being
removed. Slack becomes bidirectional via **Socket Mode**.
- **24a done:** migration `055` removes `auto_reply` from the email flow
  (→ **v11**); `confidence_gate.pass` now routes to `notify_human` like every
  other branch. Portable JSONs + `seed_router_flow.py` regenerated. The only
  path that emailed a customer automatically is gone; `notify` / `clarify`
  remain as interim draft-for-review nodes (folded into `notify_human` in 24c).
- **24b done:** migration `056` = `reasoning_sessions` (RLS, one-open-per-case
  unique index) + `pointer_bank` (seed bank per Case.Type, 7 rows).
  `interpreter/reasoning.py`: `build_pointers` (seed + LLM top-up, 4–6),
  `open_session`, and the pure `advance(session, text, *, case, llm_fn)` state
  machine — `awaiting_handoff → reasoning → drafting → awaiting_approval →
  sent|abandoned`. It works through **every** pointer (test proves it doesn't
  draft after 3/4 answers), `edit:` re-drafts, `_is_approve` gates the send.
  `handle_agent_message` is the DB-facing wrapper. 11 new tests.
- **24c done (code):** `interpreter/slack_socket.py` + a `slackbot`
  compose service — one persistent Socket Mode WebSocket (`apps.connections.open`
  → `websockets`), acks every envelope, routes `message`/`app_mention` events
  through `dispatch()` → `reasoning.handle_agent_message` → posts the reply
  in-thread → on `action == "send"` runs `_deliver` (reuses `agent_reply`,
  records a `slack_reasoning` run, marks the escalated run `guided_resume`).
  `alert.alert_human` reworked: the Slack post is now the **thread root**
  ("*I have not replied to the customer.* Reply `take` …"), it opens the
  `reasoning_sessions` row and stamps `slack_channel`/`slack_thread_ts`, and it
  no longer drops a `[bot draft]` CaseComment. `slack.post_message` gained
  `thread_ts`; `slack.lookup_user_by_email` + `salesforce.user_email` map the
  SF agent → Slack. **Live-verified**: pointer bank, `build_pointers` (6),
  `open_session` + dedupe + `not_.in_` filter, one `handle_agent_message`
  turn, all against the real DB + Groq. Socket connection itself needs the
  operator's `SLACK_APP_TOKEN`. Also fixed `llm._RECOVERABLE` to skip a
  retired provider model (OpenRouter `:free` → 404) instead of hard-failing.
  266 offline pytest.
- **Operator TODO (24c):** enable Socket Mode + create the `xapp-` token, add
  bot scopes (`im:history`, `channels:history`, `groups:history`,
  `app_mentions:read`, `users:read.email`) + event subs (`message.channels`,
  `message.groups`, `message.im`, `app_mention`), reinstall, put
  `SLACK_APP_TOKEN` in `.env`, and give the agent's Slack member id for
  `notify_human`'s `mention.slack_user_id` (or rely on the email lookup).
- **24e done (2026-09-01):** the dialogue no longer walks 4–6 pointers one at
  a time. `reasoning.plan_questions` (LLM) prunes the seed bank to what THIS
  case needs (a basic case → 1–2, each flagged `critical`), `_ask_all` asks
  them **in one message** with the bot's read on each, `_ingest` (LLM) maps
  the agent's free-form reply back to the questions, and it sends at most
  `max_rounds` (default 3) short follow-ups only for still-open *critical*
  points before drafting anyway. States: `awaiting_handoff → clarifying →
  drafting → awaiting_approval → sent|abandoned` (`cursor` = round counter).
  Migration `057` (`max_rounds` column) + `058` (node config + label → v12).
  `alert_human` passes `max_rounds` + kb_hits. **Editor:** `NotifyHumanForm`
  in `Inspector.tsx` (channel / slack_channel / max clarify rounds / @mention
  ids), `graph.ts` TERMINAL = `notify_human`, `NODE_DEFAULTS` gains
  team_route/case_lookup/notify/clarify/notify_human. 271 offline pytest +
  web build green. `_norm()` tolerates pre-24e session rows.
- **24f done (2026-09-01) — ops hardening from the audit:**
  - **C1**: `/api/trace/{key}` is now tenant-scoped — resolves the caller's
    `tenant_members` set and filters every `runs` / `jobs` / `tenant_integrations`
    read to it (was service-client, any tenant).
  - **C3/C4**: migration `059` — expression indexes on
    `runs.case_payload->>'sf_id'|'case_number'`, `runs.case_id/created_at`,
    `jobs.payload->>'run_id'`, `jobs.payload#>>'{case,sf_id}'`,
    `jobs(status,created_at)`; a `purge_old(jobs_days, runs_days)` SQL fn +
    `scripts/purge_old.py`.
  - **D1**: `daily-sync.yml` now also runs `ingestion.case_memory_sync --once`
    and `scripts.purge_old` (case_memory was never refreshed on a schedule).
  - **E4**: `docker-compose.yml` `x-svc` gets `logging: json-file 10m×3`.
  - **E5**: `docs/DEPLOY_ORACLE.md` — VM cron block (health_check /
    case_memory_sync / purge_old).
  - **C2 / SB-2**: the running stack uses the PostgREST HTTP client, not a
    direct `postgresql://` socket, so the direct-connection cap is N/A. The
    only direct connection — the Oracle VM's nightly `pg_dump` — is documented
    to use the **Session pooler** URI (`…pooler.supabase.com:5432`), see
    DEPLOY_ORACLE.md.
  272 offline pytest.
- **24d (still pending):** remove the SF shortcuts + the `check_resolution`
  comment-send path.

**Phase 23h (2026-09-01): "Send Bot Draft to Customer" quick action + stop
accidental sends.** Two problems: (a) humans use Chatter for internal
cross-talk / investigation notes, and `check_resolution` was turning *any*
new comment into a customer email; (b) there was no one-click "send it".
- **`salesforce.looks_like_send_command`** + `agent_response_since` now
  returns `is_send_command`. `_check_resolution` only emails on an **explicit**
  directive (`send` / `send: <edits>` / `lgtm` / `approved` …); a plain note is
  appended to the run's `human_reply` as context and polling continues. On
  give-up: `human_handling` if a human left notes or owns the Case (owner id
  starts `005`), else `no_reply`.
- **Quick action** (`scripts/sf_deploy_send_draft_action.py`): the button
  can't call our API (this org's outbound callouts 503 through a proxy — same
  reason the Phase 20i Apex hook was retired), so it **arms a Case field**.
  Deploys `Case.Bot_Send_Draft__c` (checkbox) + `Bot_Send_Note__c` (long text)
  + a system-context Screen Flow `Send_Bot_Draft_to_Customer` + a Flow quick
  action. CDC (`plan._send_draft_armed` → `RunSpec(trigger="bot_send_draft")`)
  picks up the change; `worker._send_bot_draft` emails the newest run's draft
  (folding in `Bot_Send_Note__c` edits), records a `quick_action` run, marks
  the escalated run `guided_resume`, and clears the field (a self-write, so
  CDC's `bot_user_id` filter stops a loop).
- **Operator step:** run the deploy script, then Setup → Object Manager →
  Case → Page Layouts → drag "Send Bot Draft to Customer" onto the action bar.
- No DB migration (the fields live in Salesforce). 296 offline pytest (24a-f + 25 + 26) (11 new).

**Phase 23g (2026-09-01): `notify_human` → a real Slack channel (live test prep).**
Slack was already connected for tenant `00000000…` (`tenant_integrations`
kind='slack', workspace **speedy** `T0BTDSDTFB5`, bot `support_automation`
`U0BT4RG2UP9`, scopes `chat:write` + `chat:write.public`). Migration `054`
(DB-only — channel ids are workspace-specific, portable JSONs keep the
`#channel` placeholders) points `notify_human`'s `slack_channel` +
`slack_channel_by_team.*` at channel id **`C0BTPTFNXS8`** for every team →
email flow **v10**. Worker restarted to load it. To trigger: an inbound email
whose subject/body hits a `team_route` csm/sales keyword ("renew", "our
contract", "add seats" → csm; "pricing", "which plan", "quote" → sales) from a
non-enterprise sender → gate edge `routed_team in ('csm','sales') and tier !=
'enterprise'` → `ask_human` → `notify_human` posts to `C0BTPTFNXS8` (bot
token, `chat.postMessage`) **and** Chatter. Slack @mention still TODO — needs
the rep's Slack member id (`U…`) in `mention.slack_user_id` (the current
`mention_id` is a Salesforce id).

**Phase 23f (2026-09-01): the re-engage poller now reads a Chatter reply.**
Case 00001185 went to `clarify` (`need_info`); the bot @mentioned the rep on
the Case **feed**; the rep replied *on that feed post* — "send this response
to customer." — and nothing happened, the run went stale. Cause:
`salesforce.agent_response_since` only read the **`CaseComment`** object, never
a Chatter **`FeedComment`** (a reply on a feed item), which is the natural
place to answer since that is where the @mention lives.
- `agent_response_since` now takes the newest of a `CaseComment` **or** a
  `FeedComment` since the run time, strips rich-text HTML, and skips the bot's
  own notes (`[bot draft…]`, `[triage]…`, the escalation @mention text) via
  `_looks_bot_written` / `_BOT_COMMENT_PREFIXES`.
- `agent_reply.resume_from_guidance` / `polish` gain a `draft` arg — the
  guidance is applied *on top of* the bot's original draft; a bare approval
  ("send it", "send this response to customer", "lgtm", …) sends the stored
  draft **verbatim, no LLM call**. `_check_resolution` passes `row["draft"]`.
- Verified live: the queued `check_resolution` tick picked up the FeedComment
  and emailed the original draft to the customer (SMTP, mirrored to the Case
  as an outbound EmailMessage); run → `guided_resume`. 296 offline pytest (24a-f + 25 + 26).

**Phase 23e (2026-09-01): stop the double Chatter tag on an escalated Case.**
Case 00001184 showed 3 bot feed posts and the rep @mentioned twice: `ask_human`
posted its *own* @mention Chatter note + a private draft `CaseComment`, **and**
the downstream `notify_human` (`channel: both`) posted a *second* @mention note.
Fix — `ask_human`/`handover` gain `config.post_note` (default `true` for
back-compat); when `false` the node is **routing-only** (still reassigns the
queue) and `notify_human` owns the single human ping. `notify_human`/`alert_human`
gain `config.draft_comment` — when `true` it drops the reviewable draft as **one**
private `CaseComment` (so the draft survives `ask_human` going quiet). Migration
`053`: email flow **v9** — `post_note:false` on `ask_human`, `draft_comment:true`
on `notify_human`. Portable JSONs + `seed_router_flow.py` carry it. Also fixed a
non-hermetic test (`test_notify_human_alerts_slack_and_chatter` was resolving a
live queue member). 243 offline pytest (4 new). Expected on the next test email:
**1 run, 1 inbound EmailMessage, ONE @mention, ONE draft comment.**

**Phase 23d (2026-09-01): `notify_human` node — tag a person, Slack and/or Chatter.**
The escalation used to *stop* at `ask_human` (SF-Chatter only). New
`@register("notify_human")` (`interpreter/alert.py::alert_human`,
`slack.post_message`): posts the Case link + summary + draft to **Slack**
(tenant bot token + channel, else `SLACK_ALERT_WEBHOOK`) **and/or Salesforce
Chatter** — `config.channel = both | slack | salesforce_chatter`. Person is
resolved by the *flow*: `mention.slack_user_id` / `_by_team`,
`mention.sf_user_id` / `sf_team` (→ a `Team_<team>` queue member via
`routing.queue_member`) / `mention_id` fallback. Slack channel:
`slack_channel` / `slack_channel_by_team[routed_team]`. Pass-through (keeps the
upstream `outcome`), so `record_run` still schedules the resolution check.
Migration `052`: email flow **v8** — `ask_human → notify_human`,
`handover → notify_human`. Router flow + seeder carry it too. `post_chatter`
is now fully non-raising. 236 offline pytest (2 new). **Operator TODO:** set
`SLACK_ALERT_WEBHOOK` (or connect Slack per-tenant) + edit the
`slack_channel*` placeholders on the `notify_human` node.

**Phase 23c (2026-09-01): Case 00001182/00001183 debug.**
- **Double EmailMessage → double run** — `log_email_message` stripped angle
  brackets for its idempotency check but Email-to-Case stores
  `MessageIdentifier` *with* them, so `sf_case` created a 2nd EmailMessage →
  2nd `EmailMessageChangeEvent` → extra run. Now matches `IN ('<mid>','mid')`.
  `latest_inbound_email` returns the EmbMsg `id`; `_run_flow` collapses the
  Case + EmailMessage CDC events onto one run via `email:<EmbMsg id>`.
- **Phase 20m never fired for `clarify`/`notify`** — `runs.build_row` only
  set `human_action='pending'` (→ schedules the resolution check) for
  `ask_human`/`handover`. Now `notify` + `need_info` on a real Case count
  too, so an agent CaseComment on a clarified/notified Case gets polished
  into a customer reply.
- **@mention never worked — wrong endpoint, not OD-4.** `post_chatter` hit
  `connect/records/feed-elements` (404 → plain FeedItem). Fixed to
  `chatter/feed-elements`; verified live. `routing.queue_member(queue)` →
  an active member (Chatter can't @mention a Queue); `h_clarify` gains
  `mention_team`/`mention_queue`/`mention_id`, `h_notify` mentions a queue
  target's member or `mention_id`. Migration `051` sets `mention_team` + a
  fallback `mention_id` (Gundam Vishnu) on the email flow's clarify+notify.
  `h_notify.config.draft_inline` collapses the note + draft-comment into one
  feed row.

**Phase 23b (2026-08-31): Salesforce-end gap fixes.**
- **Cross-path Case dedup** — `_thread_msg_ids` now also includes the mail's
  **own** Message-ID, so the poller's `sf_case` reuses a Case that Email-to-Case
  already opened for the same mail instead of creating a duplicate. (Safe to
  run both intake paths; still recommend one.)
- **CDC `case_created` delivers on the first pass** — `_run_flow` overlays the
  Case's inbound `EmailMessage` and sets `channel="email"` for **both**
  `case_created` and `inbound_email` triggers, and collapses them onto one
  run via `idempotency_key = email:<mid>` (was: `case_created` ran with
  `channel="salesforce"` → `auto_reply` sent nothing; the parallel
  `EmailMessageChangeEvent` job re-ran to actually deliver).
- **`update_case_fields(append=…)` is idempotent** — skips a `[triage]` block
  already present (was stacking one per re-run).
- **`resolve_notify_target` TTL-cached** (`NOTIFY_ROUTE_TTL_S`, default 300 s)
  + `SF_DEDUP_WRITES=0` to skip the `_recent_duplicate` SOQL — keeps under the
  DE API cap on Oracle.
- **`notify.config.attention_fields`** — optional Case-field writes (e.g.
  `{"Bot_Attention__c": true}`) so a record-triggered SF Flow can send an
  Email Alert (OD-4: Connect @mention 404s on this DE org).
- **`scripts/sf_support_setup.py --only permset`** — a least-privilege
  `Support_Bot_Integration` Permission Set for the integration user.
- 234 offline pytest (5 new). **Operator TODO:** assign the permset + downgrade
  the integration user's profile. (The "keep the Gmail poller `inactive`" TODO
  is now enforced in code — see SF-1 under the 2026-09-01 audit pass: default
  `SF_INTAKE_MODE=salesforce_e2c` keeps the poller off regardless of the DB row.)

**Phase 23 (2026-08-31): resilience — Tier 1 + Tier 2.**
- **LLM fallback chain** (`interpreter/llm.py`): `complete()` tries the chosen
  model → `LLM_FALLBACK_MODEL` (an **OpenRouter** `:free` model) → the Groq
  default → the stub, skipping any provider that rate-limits/errors. In-process
  classify **cache** (`cache=True`, `LLM_CACHE`). `_openrouter_complete` via
  plain httpx. Fixes the "Groq daily quota → whole pipeline dead" failure.
- **Channel auto-recovery** (`interpreter/mailbox.py`): `list_pollable_channels`
  = active + errored-**and-due** channels (backoff 1→30 min by consecutive
  `error_retries`, cleared on a good poll). One IMAP timeout no longer parks a
  channel forever.
- **Heartbeat + alert** (migration `050` `system_health`; `interpreter/health.py`):
  worker / poller / cdc `beat()` every ~20 s; `/api/health` returns each
  component's heartbeat age; `scripts/health_check.py` (cron / cron-job.org)
  alerts to `SLACK_ALERT_WEBHOOK` if a component is silent >15 min or the
  `run_flow` failure rate >50 %/h.
- **Salesforce write idempotency** (`salesforce._recent_duplicate`):
  `add_case_comment` / `post_chatter` skip an identical row posted on the Case
  in the last 3 h — was stacking duplicate draft comments on re-runs.
- **CDC self-write filter** (`sf_pubsub/plan.py`): a Case `OwnerId` change whose
  `commitUser` is the integration user (an `ask_human`/`handover` reassignment)
  is dropped — the bot no longer re-triggers on its own writes.
- **Migration CI** (`scripts/check_migrations.py`, wired into `ci.yml`): numbering
  gaps/dupes + every portable flow compiles. **Config validation at boot**
  (`interpreter/config.py::validate_env`) on worker/api/poller/cdc — fails loud
  on a bad `SUPABASE_URL` / missing key (the `.com`-vs-`.co` typo class).
  `drive_live_scenarios.py` now needs `--go` to enqueue (dry by default).
  `case_memory_sync` enriches `case_number`/`Type`/`tier` from Salesforce +
  `--reindex-stale DAYS`.
- 231 offline pytest (9 new in `test_resilience.py`). Migration `050` applied;
  Docker stack rebuilt; heartbeats verified live (`/api/health` →
  `{"worker": 15.0, "poller": 5.9}`).

**Phase 22 (2026-08-31): one timeline per Case.** `GET /api/trace/{key}`
(`key` = Salesforce Case number / Case id / run_id / job_id; a bare Case
number is resolved to its Id via SOQL) stitches **`jobs` + `runs` + every
trace node + errors** into one time-ordered story — so "why did the bot do
this / why did the Case fail / why these labels / why did it go stale" is
one lookup, not a SQL hunt across three tables + `docker logs`.
`api/trace.py::build_timeline` (pure) flags: `degraded_llm` (any node ran in
stub mode — the Groq-quota tell), `stale_jobs` (stuck `running` past the
10-min reclaim window), `failed_jobs` (+ the error text), `labels_written`
/ `labels_skipped` (from `sf_writeback`), `final_queue`, total ms / tokens.
`?format=md` → a plain-text report to paste into a demo. Web **Trace** tab
(`web/src/trace/TraceView.tsx`): a search box → the timeline, each node
expandable to its full `data` (gate math, retrieval, SF payload); errors in
red, a "LLM STUB (quota)" badge, "copy as text". Read-only, auth-gated, uses
the service client (to join `jobs`). 4 new tests (`test_trace.py`); 222
offline pytest. **Verified live** against Case `500jV0…5y4DxQAI` — shows the
3 Groq-429 job failures, the manual-retry run, every node with timings, the
`classify [stub]` flag, `team_route` "matched 'account manager' → csm",
`sf_writeback` labels, gate `0.573 vs 0.50 → PASS`, `ask_human → Team_CSM`.

**Phase 21 (2026-08-31): Case-resolution memory — answer from past
resolutions, not just docs.** Migrations `048` (`case_memory` table: one row
per resolved Case + a 384-d embedding + `match_case_memory` pgvector kNN,
RLS) and `049` (splice `case_lookup` into the email flow between
`sf_writeback` and `draft` → **v7**).
- **`interpreter/case_memory.py`** — `looks_specific` / `redact` (the
  "pattern vs proof" heuristic: a resolution that cites the customer's own
  IDs / timestamps / log lines is **not** `generalizable` — hint only, never
  reply copy); `classify_resolution_kind`; `lookup()` — kNN in Supabase,
  then taxonomy (type/module/tier) + recency + `DUPLICATE_OF` (Neo4j) boosts,
  split into `citable` (quotable) vs `hints` (leads). `sync_graph()` MERGEs
  Case/Reply/Module/Agent + `SIMILAR_TO` edges into Neo4j (best-effort).
- **`ingestion/case_memory_sync.py`** — populates it from resolved `runs`
  rows (`human_action` in {sent, edited, guided_resume}, minus the bot's own
  "review before sending" drafts) and, with `--from-salesforce`, closed
  Cases. Backfilled 14 rows from this env.
- **`classify`** gains `answer_mode` (informational | diagnostic | action |
  status). **`case_lookup`** node: skipped for `action`; for `diagnostic`
  the near-matches become `investigation_hints` only (`prior_resolutions`
  forced empty — the bot must not state a customer-specific fact from
  memory). **`draft`** takes a "Prior resolved cases" block (CONFIRMED
  DUPLICATE leads); groundedness now counts a cited prior resolution as a
  source. `confidence_gate` gains opt-in `escalate_answer_modes` (off).
- Degrades fully: no `case_memory` rows / no embedder / Neo4j down → a
  no-op, `draft` behaves as before. **Verified live:** informational query →
  2 citable past "how to set up a Zap" replies (rel 0.84); diagnostic query
  ("CalloutException in MY hook") → 0 citable + 8 hints, `draft` used KB
  only, outcome `clarify` (didn't guess). 218 offline pytest (13 new in
  `test_case_memory.py`).
- **Deferred (Phase 21b/c):** an `investigate` step that pulls the
  customer's own logs for diagnostic Cases; `DUPLICATE_OF` / `Incident`
  clustering; the expertise graph driving `notify`/`ask_human` routing;
  routing `answer_mode=action` straight to a human.

**2026-08-31 — live scenario sweep (7 real Cases through v6).** Drove
`scripts/drive_live_scenarios.py` (A–G, senders mapped to the tier
accounts). **6/7 correct first pass:** how-to→`auto_reply` (real draft, SMTP
sent); billing→`notify` "Billing team [table:sf_queue]" **owner unchanged**;
renewal→`ask_human`→**Team_CSM**; cancel/GDPR→`handover`→**Team_Offboarding**;
enterprise-tier→`handover`→**Enterprise_Support**; `Case.Type` set on every
Case. **One miss → fixed:** "Locked out, SSO/Okta" was LLM-typed `Problem /
Bug`, so it missed `escalate_types` and the topic `sso-login` didn't
token-match `account-access` → it went to `clarify`. **Migration `047`**
widened the gate: `escalate_modules += "Account & Login"`, `escalate_topics
+= sso/saml/login/locked out/lockout/2fa/mfa/password reset`. Re-drove C →
`notify` (`forced: topic 'sso-login' ~ 'sso'`). (It resolves to the Support
eng lead via the `Problem / Bug` `notify_targets` row — an SSO outage as a
technical incident; add a classify override or a topic-keyed row if login
issues should always hit the identity rep.)

**2026-08-31 — case study: "sent a mail, no automation response".** Root
cause: **Groq free-tier daily token quota (200K TPD) exhausted** by the
day's testing — every `run_flow` job was failing 3× with `RateLimitError
429` at the classify/draft node. `interpreter/llm.py::complete()` now
catches a post-retry rate-limit and returns the deterministic stub, so a
Case is still routed + escalated (only draft quality degrades). The
re-enqueued job then ran clean: the email ("need help for account manager
zappi") → Email-to-Case → Case `00001170` → CDC → `classify(stub)` →
`team_route` matched "account manager" → **csm → `ask_human` → reassigned to
Team_CSM** + Chatter note + draft `CaseComment`; **no customer email** (csm
owns the relationship — correct). Also observed: the intake is now
**Salesforce Email-to-Case + CDC**, and the Gmail *poller* channel is
`status='error'` (an IMAP read timeout) so that redundant second path is
off. For real-quality drafts today, add `ANTHROPIC_API_KEY` +
`LLM_DEFAULT_MODEL=claude-sonnet-5` / `LLM_FAST_MODEL=claude-haiku-4-5` to
`.env` and rebuild the worker.

**Phase 20p (2026-08-31): the email `sf_entry` flow is now the single
comprehensive workflow — every team, every scenario.** v3 had no team
routing; v4 splices `team_route` (classify → team_route → sf_writeback) and
widens `confidence_gate` to a **5-way** split:
`(enterprise tier OR routed_team==offboarding) → handover [Enterprise_Support
/ Team_Offboarding]` · `(routed_team ∈ {csm,sales}, non-enterprise) →
ask_human [Team_CSM / Team_Sales]` · `(support + gate PASS) → auto_reply` ·
`(support + FAIL + forced escalation) → notify [Case.Type → notify_targets;
Case stays in Team_Email]` · `(support + FAIL + not forced) → clarify [ask
the customer; 2 rounds → Team_Support]`. Migration `046` applied → **v4**;
portable `flow_email_l0l1.json` + `flow_case_router.json` + `seed_router_flow.py`
all carry the same 13-node shape. `scripts/run_scenarios.py` (fast routing
check, no LLM) + `tests/test_flow_scenarios.py` (12 cases) — a 10-scenario
matrix (how-to/vague/billing/login/bug/renewal/pricing/cancellation/enterprise
× basic/premium/enterprise tiers) all route as expected against the live v4.
Docker stack rebuilt on v4; SF tier accounts already exist (Northwind
Ltd=premium/EMEA, Globex Enterprise=enterprise/NA, Indie Dev Co=basic).
**Live e2e (clarify-exhausted, agent re-engage, customer reply, real inbound
email) still to run with the worker.**

**Phase 20o (2026-08-31): `notify` targets come from a central table, not
node config.** So a flow editor never pastes Salesforce ids. Migration `045`
= `notify_targets` (tenant-scoped, RLS via `is_tenant_member`/`is_tenant_editor`):
`(match_kind ∈ {case_type, module}, match_value, resolver ∈ {static,
sf_team_role, sf_queue}, sf_target_id, sf_team/sf_role, sf_queue, label,
active)`. `interpreter/routing.py::resolve_notify_target(tenant_id, case_type,
module)` reads it — `sf_team_role` does a **live** two-hop SOQL (Queue Group
id → its member User; single-level, SOQL forbids nested semi-joins),
`sf_queue` resolves the Queue Group id. `h_notify` consults it when the
node's own `target_by_type`/`target_by_module` have no match (an override
still wins); then `fallback_target`. Seeded 7 `case_type` rows for tenant
`00000000…`: Billing→`Billing_Escalations` queue, Feature Request→`Support_Tier2`
queue, the rest→Support team lead (`sf_team_role`). **Live-verified 2026-08-31**
against the org — Billing→`00GjV0…` (queue), Account/Login·Bug·How-to·Question·Other
→ User `005jV0…` (the roster member, resolved live). 3 new tests
(`test_notify_and_type.py`, 13 total in the file; 193 offline).
- **Editor pickers (2026-08-31):** `GET /api/salesforce/meta` (`api/main.py`,
  5-min in-proc cache → `salesforce.org_metadata()`) returns the org's live
  **queues + `Case.Type` / `Module__c` picklists**. The flow editor's Inspector
  wires them in — `clarify`'s *handover queue* is now a `<select>` of real
  queues (`QueuePicker`), `notify`'s *override by Case.Type* rows come from the
  live picklist; both degrade to a text box / hardcoded list when the API has
  no SF creds, and keep any value not in the list. `web/src/api.ts`
  `api.salesforce.meta()`, `SfMeta` type, `useSfMeta()` hook.
- Still **no standalone `notify_targets` admin screen** — table rows are
  managed via SQL; the pickers above live in the flow-node forms.

**Phase 20n (2026-08-31): ask an internal rep without handing off the Case
+ `Case.Type` on every pass.** The "what happens after we're not confident"
branch was under-specified: every low-confidence Case went to `ask_human`,
which reassigns the Case out of its queue.
- **`Case.Type`** — `classify` now emits `case_type` (LLM `type` →
  `salesforce.normalize_case_type`, else `map_case_type` keyword fallback,
  stub-safe); `sf_writeback` default field-map gained `case_type → Type`,
  written at first triage and on every customer-reply re-run. It was never
  populated before — the field a queue owner filters by was blank. FR-26.
- **`notify` node** (`interpreter/registry.h_notify`) — Chatter ping
  (@mention when the target is a real User/Group id) + draft `CaseComment`,
  **no `OwnerId` change**. Target: `Case.Type` → `Module__c` →
  `fallback_target`. `confidence_gate` gained `escalate_types`
  (`["Billing", "Account / Login"]`). FR-27.
- **`clarify` `handover_queue`** — round cap (2) exhausted → reassign to
  `Team_Support` so a human owns it. FR-28.
- **Flows**: `flow_email_l0l1.json` v2 — `ask_human` **removed**; gate splits
  `enterprise → handover / pass → auto_reply / fail+forced → notify /
  fail+benign → clarify`. `flow_case_router.json` v2 — `notify` + `clarify`
  added; `ask_human` kept for csm/sales (they own the Case), `handover` for
  enterprise/offboarding. Migration `044`; seeder `scripts/seed_router_flow.py`
  regenerated (portable JSON == seeder output verified). Web Inspector: a
  `notify` target-by-Type form + a `clarify` handover-queue field;
  `graph.ts` TERMINAL += `notify`.
- **Re-engagement after the rep answers** = the existing Phase 20m resume
  poller (agent CaseComment → `agent_reply.resume_from_guidance`), unchanged.
- **Verify:** 190 offline pytest (10 new in `tests/test_notify_and_type.py`);
  web tsc + vitest + build green. Migration `044` **applied 2026-08-31** —
  email flow `e5e5e5e5…` now published **v3** (`ask_human` removed; `notify`
  + `clarify` added; gate 4-way); the live v3 snapshot was pulled back and
  re-checked with `build_graph` + a route smoke (Billing→`notify`,
  vague→`clarify`, how-to→`auto_reply`; `Case.Type` populated). **Not yet
  live-verified end to end** — needs the deployed `worker`/`cdc` restarted
  (they cache the flow per process) and a real Case through it.

**Phase 20m (2026-08-31): the bot now re-engages after a human** — an
agent CaseComment on an escalated Case → bot polishes it into a customer
reply and sends it (`interpreter/agent_reply.py`); `_check_resolution`
polls instead of firing once; CDC `inbound_email` re-runs use the latest
message, not the stale Description. Needs the worker running to take
effect — the queue was found idle (14 stuck `check_resolution` jobs) when
this was diagnosed.

**Deploy** (`docs/DEPLOY.md`): the runtime = `worker` + `cdc` + `poller`
(all outbound-only, no public URL) + optional `api`. `docker-compose.yml`
runs them locally / on a VM; `Procfile` + `railway.json` for Railway
(paid — no free tier); `deploy/run_all.py` + `deploy/Dockerfile` bundle
all three under one supervisor + health port for a single-container free
host (Hugging Face Spaces, no card). Git-based deploys need
`SF_PRIVATE_KEY` inline (the `sf_jwt/` file is git-ignored). API is on
Vercel (`support-automation-ashy.vercel.app`); set `WEB_ORIGINS` there.

Phase 20l's CDC subscriber is **live-verified** (2026-08-31): a real
inbound `EmailMessage` on Case `500jV0…` streamed off `/data/
EmailMessageChangeEvent` → `inbound_email` job enqueued, other events
ignored, replay cursor persisted for both topics. Run it with
`python -m ingestion.sf_cdc_watch` (docker-compose `cdc` service).
Phase 18d's button is built but signing in with Google needs the Supabase
dashboard Google provider enabled first (`docs/GOOGLE_SETUP.md`
§"Google sign-in"); the Phase 20 Gmail *provider* needs the same
`GOOGLE_CLIENT_ID`/`SECRET` + a redirect registered (the IMAP path needs
nothing server-side).

**2026-08-31 — Phase 20m: the bot re-engages after a human.**
Bug found in testing: after `ask_human`, an agent answers on the Case and
the bot never responds. `check_resolution` only *recorded* the human's
action (Phase 11) and only fired once.
- **`interpreter/salesforce.py`**: `latest_inbound_email(case_id)` and
  `agent_response_since(case_id, since_iso)` (newest CaseComment as
  *guidance*, newest outbound EmailMessage as *agent-handled-it*).
- **`interpreter/agent_reply.py`** (new): `resume_from_guidance(case,
  guidance, cfg, tenant_id)` — one LLM polish of the agent's answer into a
  customer-facing reply, delivered on the case's channel (SMTP + mirror to
  the Case, or `send_case_reply`); `cfg.auto_send_enabled` off → left as a
  draft CaseComment.
- **`api/worker._check_resolution`** reworked into a bounded poller
  (`FEEDBACK_POLL_MIN`=5 × `FEEDBACK_MAX_CHECKS`=12, after the initial
  `FEEDBACK_DELAY_MIN`=20): agent CaseComment → `resume_from_guidance` +
  parent run `human_action='guided_resume'` + a `source='agent_resume'`
  run row; agent outbound email → score the draft as before; nothing yet →
  re-enqueue with `checks+1` (`dedupe_key={run_id}:{n}`).
- **`api/worker._run_flow`**: a CDC `inbound_email` re-run now overlays the
  newest incoming EmailMessage onto `case.body/subject/from` — was
  re-triaging the stale Case Description. (The email *poller* path already
  passed the fresh message.)
- **Customer replies** re-run the whole flow (poller enqueue already
  worked; CDC path fixed above). **Agent guidance** takes the focused
  polish-and-send path (no re-triage — the agent's answer is the truth).
- **Verify:** 180 offline pytest (6 new in `test_agent_resume.py` — the
  three `_check_resolution` branches + noop + the two delivery modes).
  Not yet live-verified (needs the worker running against the org).

**2026-08-31 — Phase 20l: durable Salesforce → engine push via CDC +
Pub/Sub API.**
- **Why:** the Phase 20i Apex HTTP callout is fire-and-forget (an API
  outage loses the event) and covers only *new* Cases. CDC covers new
  Cases, **inbound emails on an existing Case**, and **queue (owner)
  changes** in one subscription, and every event has a `replay_id` we
  persist so a restart resumes (72h retention).
- **`ingestion/sf_pubsub/`** — vendored `pubsub_api.proto` (Salesforce
  Pub/Sub API v1) + generated `pubsub_api_pb2*.py` (regen recipe in the
  package `__init__`); **`plan.py`** — pure `plan_events(payload,
  replay_hex) → [RunSpec]` (Case CREATE → `case_created`, Case UPDATE with
  a moved `OwnerId` → `case_owner_changed`, inbound `EmailMessage` CREATE
  → `inbound_email` for its `ParentId`; DELETE/GAP/outbound ignored);
  **`subscriber.py`** — `PubSubSubscriber`: one gRPC `Subscribe` stream
  per topic (thread each), Avro decode via `fastavro` + `GetSchema`
  cache, flow-control window, `UNAUTHENTICATED` → mint a fresh JWT token
  and resubscribe, exponential backoff, `run(max_events=…)` for a smoke
  drain.
- **`ingestion/sf_cdc_watch.py`** — `python -m ingestion.sf_cdc_watch`
  (`--topics`, `--max-events`); no SF creds → exits 0 with a notice.
- **`interpreter/sf_ingest.py`** — `resolve_entry_flow_id(sb)` +
  `enqueue_case_run(sb, case_id, *, dedupe_key, idempotency_key,
  trigger, flow_id?)`, the **one enqueue path** now shared by the HTTP
  hook and the CDC subscriber. Per-event dedupe keys: `sfcase:{id}` (new
  Case — same key the hook already used, so both push paths can run
  during a migration without double-processing), `sfowner:{id}:{replay}`,
  `sfemail:{msgId}`.
- **`interpreter/salesforce.pubsub_auth(refresh=?)`** → `(access_token,
  instance_url, org_id)` for the gRPC metadata; `reset_client()` forces a
  new session.
- **Migration `043`** (written, not yet applied): `sf_cdc_state (topic pk,
  replay_id bytea, event_count, updated_at)` — service-role only, like
  `jobs`.
- **`api/main.salesforce_case_hook`** refactored onto `sf_ingest`
  (response drops the unused `flow_id` field; still `{job_id, deduped}`).
- **docker-compose**: a `cdc` service (`restart: unless-stopped`).
  `requirements.txt` += `grpcio` / `protobuf` / `fastavro`.
- **Verify:** 173 offline pytest (11 new in `test_sf_pubsub_plan.py` —
  the planner matrix + stub-import + creds-less CLI no-op). **Live e2e
  (2026-08-31, `--max-events 3`):** subscribed both topics at LATEST; a
  real inbound `EmailMessage` on Case `500jV000005eD6wQAE` →
  `plan_events` → one `run_flow` job (`trigger=inbound_email`,
  deduped `sfemail:{id}`); 3 other change events decoded and correctly
  ignored (no jobs); `sf_cdc_state` holds a 29-byte replay id per topic
  → restart resumes. Job left `queued` (no worker was running — that
  leg is Phase 10/20i).
- **Transition note:** with both push paths live a new Case dedupes
  (shared `sfcase:{id}` key); once CDC is confirmed, retire the Apex
  trigger from `scripts/sf_deploy_case_hook.py` (or keep it as a
  belt-and-braces fallback — the dedupe makes that safe).

**2026-08-31 — Phase 20k: pick which flow the Salesforce hook runs.**
- **Migration `042`** (applied 2026-08-31): `flows.sf_entry boolean`
  + partial-unique `uq_one_sf_entry_flow_per_tenant` (`where sf_entry`).
  Backfilled `sf_entry = true` on the published `team='router'` flow
  (`f0f0f0f0-…`, tenant `00000000-…`) so behaviour is unchanged.
- **`POST /api/hooks/salesforce/case`** now resolves the entry flow by
  `sf_entry = true and status = 'published'` (was: hard-coded
  `team = 'router'`). Zero/many matches → a 500 that tells you to set the
  toggle.
- **`PUT /api/flows/{id}/sf-entry {sf_entry: bool}`** (editor-role) — sets
  the flag, first clearing it on the tenant's other flows so there's
  always ≤1. `sf_entry` is surfaced in `GET /api/flows` + `GET
  /api/flows/{id}`.
- **Web**: a **Salesforce entry** checkbox-pill in the flow editor
  toolbar (green when on); read-only viewers see a static pill when set.
  `api.setSfEntry()`, `FlowMeta.sf_entry`.
- **Scope note:** deliberately *not* the full `flow_bindings`
  (queue → flow) design — that stays a future phase. This is the
  one-flow-for-all-teams interim: mark one flow, delete or ignore the
  rest.
- **Verify:** 162 offline pytest (2 new — the SF hook needs its shared
  secret; `/sf-entry` needs a token) + an integration test
  (`test_sf_entry_is_one_per_tenant`: flipping the flag on flow B clears
  it on flow A); web tsc/vitest (6)/build green.

**2026-08-30 — Phase 20j: local Docker runtime + SMTP outbound (no cloud,
no credit card).**
- **`docker-compose.yml`** (+ `Dockerfile`, `.dockerignore`) — one image,
  three `restart: unless-stopped` services on the dev box:
  `poller` (`ingestion.email_watch --interval 15`), `worker`
  (`api.worker`), `api` (`uvicorn api.main:app`, `:8000`, `/api/health`
  healthcheck). `./sf_jwt` bind-mounted `:ro`; compose overrides
  `SF_PRIVATE_KEY_FILE=/app/sf_jwt/server.key` (the `.env` value is a host
  path). `model-cache` named volume keeps the fastembed ONNX model across
  rebuilds. Replaces the opaque GitHub-Actions cron for NFR-2. Usage +
  the Cloudflare-tunnel wiring for the SF push: **`docs/LOCAL_RUNTIME.md`**.
  *Not built/run here:* Docker isn't on this box — compose YAML + anchor
  merge validated, image build is the user's `docker compose up -d --build`.
- **Outbound reply now goes over SMTP** (FR-12 revised).
  `api/worker._email_post_run._deliver` calls `emailer.send_reply` (SMTP
  from the mailbox — what actually reaches the customer), then best-effort
  mirrors it onto the Case as an outbound `EmailMessage`
  (`salesforce.log_email_message`, `Incoming=false`, `status=_EM_SENT`).
  `salesforce.send_case_reply()` / the `emailSimple` action dropped from
  this path: this DE org has no Org-Wide Email Address and Deliverability =
  "System email only", so `emailSimple` returned `sent=true` while
  delivering nothing (that was the "no reply in mail" bug). `send_case_reply`
  is now only referenced by `registry.h_clarify` (Phase 17c). **160 offline
  pytest** green (`test_emailer.py`: the SF-reply test replaced by an
  SMTP-send + Case-mirror test and a send-failure test).

**2026-08-30 — Phase 20e: the email channel is LIVE-CONFIGURED for
`gundamvishnu7@gmail.com` (tenant Acme `00000000-…`, team `email`).**
The IMAP/SMTP app-password is in Supabase Vault; `test_connection` passes;
`auto_send_enabled=true`, `status='active'`. New **`sf_case`** node turns
an inbound email into a real Salesforce Case (create-or-reuse Contact /
Account / Case) so `sf_writeback` / `ask_human` / `handover` act on a real
record; migration `036` publishes the L0/L1 email flow
(`identify → sf_case → retrieve → classify → sf_writeback → draft →
confidence_gate → {handover | auto_reply | ask_human}`). A bootstrap SF
**Account + Contact for `gundamvishnu7@gmail.com`** exist (`Tier__c=basic`).
**Blocking the live mail e2e:** that Gmail is a noisy personal inbox — a
`--dry-run` poll skipped 44/50 as bulk but 6 marketing mails would still
enqueue. Do NOT run a blind live poll with auto-send on. Next: either a
dedicated support mailbox, or add a `--from <addr>` filter to
`ingestion/email_watch.py` and drive one clean test message through
`email_watch --once` + `api.worker --once` (measure latency, confirm the
Case + the threaded reply).

**2026-08-30 — Phase 20i: the Case-router workflow (the design doc, as
one flow) + team data + Salesforce push.**
- **New node `team_route`** (`registry.h_team_route`, pure) — keyword rules
  over the case → `state.routed_team` ∈ {support, csm, sales, offboarding}
  (renewal/expansion → csm, pricing/pre-sales → sales, cancellation/
  data-export → offboarding, else support). `_context` exposes
  `routed_team` for edge conditions.
- **`ask_human` / `handover` resolve the queue from `routed_team`** via a
  `queue_by_team` config + `_route_queue()`: a routed team keeps its own
  case (→ `Team_CSM` / `Team_Sales` / `Team_Offboarding`); a `support`
  billing-reason escalation → `Billing_Escalations`; enterprise →
  `enterprise_queue` (`Enterprise_Support`).
- **The flow** (`f0f0f0f0-…`, team `router`, published): `identify →
  sf_case → retrieve → classify → team_route → sf_writeback → draft →
  confidence_gate → {auto_reply | ask_human | handover}`. Migration `040`
  / `scripts/seed_router_flow.py` (canonical) / portable
  `flow_case_router.json`.
- **Salesforce team data** (`scripts/sf_seed_teams.py`): `Contact.Team__c`
  + `Contact.TeamRole__c` picklists; **2 real Users** (Sam Rivera =
  Support mgr, Casey Lin = CSM mgr — Dev Edition caps Users at 4) added to
  their `Team_*` queues; **13 Contacts** on an "Internal — Support Teams"
  Account, 1 Manager + 2 Members per team.
- **Salesforce → automation push**: `POST /api/hooks/salesforce/case`
  (`api/main.py`) — shared-secret (`SF_HOOK_SECRET`), pulls the Case,
  queues the router flow (deduped on Case Id). The SF side is a
  record-triggered Flow → HTTP Callout (no Apex; needs the API at a public
  URL — see `docs/SALESFORCE_SETUP.md` §2d). Live-verified: `curl` →
  202 → worker → Case owner set to the routed team's queue.
- **Live e2e** through the published router flow: renewal+seats → csm →
  `ask_human` → Team_CSM; pricing → sales → Team_Sales; cancel+export →
  offboarding → `handover` → Team_Offboarding; generic Zap issue →
  support → `auto_reply`; double-charge → support → `ask_human` →
  Billing_Escalations; enterprise SSO → `handover` → Enterprise_Support.
  159 offline pytest (6 new in `test_router.py`).

**2026-08-30 — Phase 20h: all-flows e2e test + wire the rest.**
`scripts/test_all_flows.py` runs representative cases (basic/premium/
enterprise + a vague one) through **every published flow** against live
Groq + Salesforce, checks the outcome + Case fields + owner queue, and
cleans up. First run confirmed all 7 flows branch correctly and surfaced
3 gaps, now fixed: (1) `h_ask_human` routes to `escalate_queue` when the
topic maps to the **Billing & Plans** module (`salesforce.map_case_fields`)
— not only when the gate's `forced_escalation` flag is set, so it's robust
to the classifier's exact slug; (2) **migration `039`** wires the Globex /
catalog / offboarding / Slack-approval flows' `ask_human` / `handover` to
queues (038 had only done the three Acme support flows); (3) the Globex
`sf_writeback` field_map gains the Case picklists (`Topic__c` / `Module__c`
/ `SubModule__c` / `Region__c`) like the others. 152 offline pytest (the ask_human queue test gains the billing-topic case).

**2026-08-30 — Phase 20g: Salesforce org set up for the flows.**
`scripts/sf_support_setup.py` (Metadata API, idempotent) created: **9
queues** (5 per-team + `Support_L0L1` / `Billing_Escalations` /
`Enterprise_Support` / `Support_Tier2`); `Case.Type` → software-support
values + `Case.Status` `Waiting on Customer`; `Case.Module__c` /
`Case.Region__c` Text → **restricted picklists** (drop-and-recreate),
new `Case.SubModule__c` (dependent on `Module__c`) + `Case.Topic__c`
Text(255) + FLS. `salesforce.map_case_fields(topic, country)` maps the
classifier's slug → `Module__c`/`SubModule__c` and country → `Region__c`
(`Topic__c` always gets the raw slug); `h_sf_writeback`'s default
field_map now targets these. `h_ask_human` / `h_handover` take a
`queue` (+ `ask_human` an `escalate_queue`, used on a forced-topic
escalation) and reassign `Case.OwnerId`. **Migration `038`** wires the
email / L0-L1 / triage flows to the queues + repoints the L0-L1
`sf_writeback` field_map. Live-verified: a billing case → `Module__c`
"Billing & Plans" / `SubModule__c` "Refunds" / `Topic__c` "billing-refund"
written, Case owner → **Billing Escalations** queue. 152 offline pytest
(3 new). Picklist values carry no default (blank until set).

**2026-08-30 — Phase 20f: the email conversation lives on the Salesforce
Case.** Five spec gaps closed (`docs/REQUIREMENTS.md` §9):
- **FR-6** `sf_case` `reuse: "thread"` (migration `037`) —
  `salesforce.find_case_by_thread()` matches the inbound email's
  `In-Reply-To` / `References` against `EmailMessage.MessageIdentifier` on
  open Cases; a new subject → a new Case (was: any open Case for the
  contact within 14 days).
- **FR-7** `sf_case` → `salesforce.log_email_message(incoming=True)`: the
  customer's mail is an `EmailMessage` on the Case (idempotent on
  `MessageIdentifier`), not only in the Description.
- **FR-12** `api/worker._email_post_run` replies via
  `salesforce.send_case_reply()` (outbound `EmailMessage`, threaded on the
  Case) whenever the case has an `sf_id`; SMTP is the fallback.
- **FR-13** `ask_human` leaves the drafted reply on the Case as a
  `Status='Draft'` `EmailMessage` (recipient + `Re:` subject prefilled)
  beside the Chatter note.
- **FR-14** `handover` calls `salesforce.assign_case(queue=…)` when the
  node has a `queue` / `owner_user_id` (resolves a Queue by DeveloperName
  or Name, sets `Case.OwnerId`); no target → outcome only.

Needs the org admin to **enable Email-to-Case** (C-1) for the Email action
+ Emails related list to show on the Case page — the `EmailMessage`
records are created via API regardless. 149 offline pytest (9 new in
`test_sf_case.py` / `test_emailer.py`).

**2026-08-30 — Phase 20 (email channel) COMPLETE — the whole round-trip
works in code.** A workspace **owner** points a support mailbox at a
tenant in the **Channels** tab (`web/src/channels/ChannelsView.tsx`):
provider IMAP+app-password or **Connect Gmail** (OAuth), team, from-name,
optional reply-from, an **auto-send** toggle (default off), an **active**
toggle, Test-connection. The password / refresh token is stored in
**Supabase Vault** (migration `035`), never returned to the browser.
`python -m ingestion.email_watch --once` (20b) polls each active channel,
drops auto-responders / list mail / the mailbox's own mail, and enqueues a
`run_flow` job keyed on `Message-ID` against the tenant's published flow.
`api/worker._email_post_run` (20c) then applies
`interpreter/emailer.decide()` — the **hard guard**: a customer-facing
email goes out **only** on `outcome.action == "auto_reply"` with the
channel's `auto_send_enabled` on and a non-empty draft; `need_info` sends
questions only if the `clarify` node opted in;
`ask_human`/`handover`/switch-off → `mailbox.mark_needs_human` re-flags the
message unread for a human, nothing sent. Outbound is threaded and stamped
`X-Support-Bot: 1` so the poller never answers it. `docs/EMAIL_SETUP.md`.
**Scheduling (2026-08-30):** `.github/workflows/email-automation.yml`
runs `email_watch --once` + `api.worker --once` every 5 min (best-effort
GitHub cron — the agreed stopgap; a persistent worker is the eventual
target, deferred). Needs repo secrets `SUPABASE_URL` /
`SUPABASE_SERVICE_KEY` / `GROQ_API_KEY` / `SF_USERNAME` / `SF_CONSUMER_KEY`
/ `SF_DOMAIN` / `SF_PRIVATE_KEY` — the mailbox password is **not** one
(it's in Vault). Architecture decision: **Path A** — the platform owns
each inbound channel and creates the SF Case itself (`sf_case` node), so a
future **Freshworks chat** channel slots in as another adapter feeding the
same queue. **Not yet done:** the workflow's secrets added on GitHub + a
first green scheduled run; a dedicated support mailbox (the configured one
is a noisy personal Gmail); migrating the existing Slack/SF/Google
`tenant_integrations` rows onto the same Vault mechanism.

**2026-08-30 — Phase 19 (assisted flow authoring) COMPLETE.** You no
longer hand-draw the graph: **⬇ From Mermaid** (paste a `flowchart` — a
deterministic parser in `interpreter/flows/mermaid_import.py`, no LLM),
**✨ From prompt** (`POST /api/flows/assist` → `assist.assist_generate`,
Groq), and **✨ AI edit** inside the editor (`POST /api/flows/{id}/assist`
→ `assist.assist_edit` + a `flow_diff` preview). All three produce a
*candidate* graph via the shared `flow_candidate.assemble_candidate`
(uuid-per-key, unknown type → `draft` + flag, `check_flow` + single-entry
rule → errors/warnings) that the editor loads as an **unsaved draft** —
Save/Publish still go through the validated `replace_flow_graph` path.
`llm._stub_fields` has an `assist` branch so it all runs offline/CI.
Live-verified against real Groq (generate + edit compile) and the API
(Mermaid import). See `docs/FLOW_AUTHORING.md`.
Every external integration has now been exercised end-to-end against a
real account: **Salesforce** (Phase 3, JWT), **Google Docs** (Phase 15,
OAuth → link → sync), **Slack + GitHub** (Phase 16, offboarding case →
Slack Approve → `GH-Alert#2` opened). Creds live in `.env` (gitignored);
`docs/{SALESFORCE,GOOGLE,SLACK}_SETUP.md` cover setup.

Open work is UI polish / bug-fixes surfaced during that live testing
(tracked separately) plus the small items below.

**2026-08-29 — Groq key added; LLM path now runs real.** Fallout fixed
(migrations `017`/`018`, `interpreter/llm.py`, `registry._context_block`):
- Groq retired `llama-3.x`; defaults are now `openai/gpt-oss-120b` (draft)
  / `openai/gpt-oss-20b` (classify, judges). `017` repointed seed flows.
- gpt-oss are reasoning models — `reasoning_effort="low"` + `draft`
  `max_tokens` 500→900 (`018`), and `llm.complete` now recovers from a
  Groq `json_validate_failed` 400 (salvage partial, else retry free-form).
- One doc chunk is ~27k chars → `_context_block` caps each chunk (1800)
  and the prompt block (7000) so a request can't exceed the free-tier
  8k TPM ceiling (was a hard 413). `_groq_call` backs off on 429.
- **RAG answer quality (15 Qs, real Groq draft + Groq LLM-judge):**
  answers the question **15/15**; grounded in retrieved docs **14/15**
  (the one miss added a code sample not in context — exactly what the
  `groundedness` node is there to catch).
- **e2e action eval re-run with real drafts (not the stub):** acc
  **0.636**, auto-send P **0.556** (10/18) — 8 `gold=ask_human`→`auto_reply`
  misses, all premium, all `draft_confidence` 0.93–0.99.
  **Fixed by `019_recalibrate_gate.sql`** (below): explicit gate blend
  (draft self-confidence weighted to .1) + `escalate_topics` →
  **acc 1.000, auto-send P 1.000, escalation P 1.000**, coverage 0.455
  (the 10/22 ceiling for this set).

Run the whole thing:
```
pytest -m "not integration" -q                        # offline suite (CI)
pytest -q                                             # + integration (needs .env)
python -m interpreter.run --list                      # 3 flows / 2 tenants
python eval/e2e/run_e2e.py                            # action eval + threshold sweep
uvicorn api.main:app --reload                         # backend :8000
cd web && npm install && npm run dev                  # editor + Runs view :5173
```
Editor login: `gundamvishnu7@gmail.com` → tenant Acme; `globex-owner@example.test`
(pw `editor-test-pw-8891`) → tenant Globex.

**Phase 17 (low-confidence recovery) is COMPLETE — 2026-08-29.** All four
chunks built + live-verified; migrations `029`–`031` applied. The
retrieval-gated flow `d4d4d4d4-…` is published **v4**:

```
retrieve → identify → classify → confidence_gate ─┬─ pass ────────────→ draft → answer_gate → auto_reply | ask_human
                                                   ├─ enterprise ──────→ handover
                                                   ├─ fail + escalation topic → ask_human
                                                   └─ fail + benign ──→ clarify   (ask the customer / their identity; round-capped → ask_human)
```

- **17a `clarify`** — LLM writes the specific missing-info questions →
  `outcome.action='need_info'`; posts to Chatter (agent) or, with
  `auto_send`, emails the customer.
- **17b `identify`** — resolves the sender (exact contact / email-domain →
  account / unknown); `clarify` asks unknown senders to confirm who they
  are.
- **17c** — Inspector forms for `clarify`/`identify`; `need_info` in the
  Runs filter + pill + a "waiting on the customer" banner;
  `salesforce.send_case_reply` (real `emailSimple` send).
- **17d** — `runs.clarify_round`; `h_clarify` counts prior `need_info`
  runs for the Case and after `max_rounds` (2) hands to a human
  (`reason='clarify_exhausted'`).

Follow-ups noted but **not** in scope: a routing branch that sends
unknown senders somewhere other than `clarify`; `sf_case_watch`
correlating the customer's reply back to the open `need_info` run
automatically (today the re-enqueue works, the round count is by
`case_id`).

Phase 17 (a–d) merged to `main` (PR #2). Phase 18a/18b/18c merged
(PRs #3 / #4 / #5). **Phase 18 (a–d) is COMPLETE — 2026-08-29.**

### Team access — how it works now

- `tenant_members.role` ∈ `owner` / `editor` / `viewer`, enforced by RLS
  (`is_tenant_member` / `is_tenant_editor` helpers, migration `032`) on
  every editable tenant table + a `_require_editor` / `_require_owner`
  pre-check in the API for clean 403s.
- **＋ New flow** infers the tenant (no uuid prompt); a `viewer` sees the
  editor read-only (Save / Publish / Delete / palette hidden, a
  "view-only" pill).
- An **owner** invites `email + can-view/can-edit` in the **Team** tab
  (`tenant_invitations`, migration `033`). No email is sent —
  `App.tsx` → `POST /api/invitations/accept` claims pending invites for
  the signed-in email on every load. Zero memberships → a "no workspace"
  screen.
- Sign-in: magic link, email+password, or **Continue with Google**
  (`Login.tsx`). Google needs the Supabase dashboard provider enabled —
  see `docs/GOOGLE_SETUP.md` §"Google sign-in for the editor" (3 steps,
  no code / no `.env`). Until then the button shows a provider error.

**Open (operator, not code):** enable the Supabase Google provider so
Google sign-in works live. No further build work in Phase 18.

### Standing context

- A **Slack-approval demo flow** was built in the Acme tenant during
  testing (data only, no repo change): flow `781cf1cc-…` (team
  `support-approvals`) + rule `a30b7d42-…` (`entities.refund_amount`>0 →
  Slack Approve/Reject in channel `C0BTPTFNXS8` → GitHub issue in
  `vishnuanalytics/support-automation`). The tenant `00000000-…` Slack
  integration row is restored in `tenant_integrations`.
- **18d uncommitted** (about to be): `web/src/auth/Login.tsx`,
  `docs/GOOGLE_SETUP.md`, this file.

Standing context (unchanged):

- A **Slack-approval demo flow** was built in the Acme tenant during
  testing (data only, no repo change): flow `781cf1cc-…` (team
  `support-approvals`) + rule `a30b7d42-…` (`entities.refund_amount`>0 →
  Slack Approve/Reject in channel `C0BTPTFNXS8` → GitHub issue in
  `vishnuanalytics/support-automation`). The tenant `00000000-…` Slack
  integration row is restored in `tenant_integrations`.
2. Polish: ~~Phase 16 `when`/`then` form builder~~ **done**; Phase 14
   file upload (`.pdf`/`.docx`); an `eval/e2e` case that only passes when
   an internal KB entry is consulted.
3. Local dev servers for the demo: `uvicorn api.main:app --port 8000`,
   `python -m api.worker`, `cd web && npm run dev`, and (for Slack
   interactivity) `npx cloudflared tunnel --url http://localhost:8000` —
   the tunnel URL goes in the Slack app's Redirect + Interactivity URLs
   and in `SLACK_REDIRECT_URI`.
3. Small eval-tooling follow-up: `run_e2e.py`'s threshold sweep still
   uses the legacy 1-D blend — rewrite it to sweep the new `weights` /
   honour `escalate_topics`, or drop it. Headline metrics (per-run) are
   correct; only the sweep table is stale.
3. Standing infra debt: Playwright e2e on the web; wire the `eval/e2e/`
   auto-send-precision floor into CI; deploy `api/` + `web/` + the worker
   + the `sf_case_watch` cron to real hosts; move rate-limit /
   token-cache state to Redis; encrypt `tenant_integrations.secret` with
   Supabase Vault. All noted under "Known issues / debt".

A Phase 7 follow-up, now **higher priority** after the real-draft e2e
re-run (acc dropped 0.909 stub → 0.636 real):

- **`draft_confidence` from a self-grading LLM is not calibrated** — the
  real model reports 0.93–0.99 on almost everything, so at weight 0.5 it
  drowns out `retrieval_score` (the one signal that actually separates
  e08/e20, retr≈0.01, from e10/e11, retr≈1.0). Rebalance the gate to
  lean on `retrieval_score` + `groundedness` (independent judge) and
  down-weight or drop model self-confidence.
- **Add an intent/policy pre-gate**: premium tier + {billing dispute,
  security/legal, account change, partner-API} → `ask_human` regardless
  of confidence. That alone fixes most of the 8 misses; the old `e11`
  SOC2 / `e12` Partner-API residuals are the same class.
- Re-baseline `eval/e2e` expectations for real-LLM mode before wiring an
  auto-send-precision floor into CI (the 0.909 figure was stub-only).

Quick wins available any time (not blocking a phase):
- a Groq key in `.env` moves `classify`/`draft` off the stub;
- deploy: ingestion already runs on GitHub Actions; `api/` + `web/` need
  hosting (Fly/Render + Vercel/Netlify) with `WEB_ORIGINS` / `VITE_*` set.

Default to Groq for any LLM calls (classification, draft generation).

## Known issues / debt

- **Fixed (2026-09-03) — first real security review of the accumulated
  api/+web/ surface.** Phase 13 (2026-08-29) called for this after Phase
  5/6; it never happened while Phase 5 through Phase 29 landed. Run as two
  parallel forked audits (authZ/RLS/secrets; injection/SSRF), same rigor
  as the `/security-review` skill. **AuthZ/RLS/secrets: clean** — every
  `_service` (RLS-bypassing) call site checked pairs with an explicit
  tenant-membership check, `_caller_tenant` genuinely resists tenant
  impersonation, `_verify_token` is authoritative (real Supabase Auth
  check, not a decode-only trust), webhook trigger tokens aren't a timing
  or cross-tenant vector, secrets never round-trip to a client, CORS has
  no wildcard+credentials hole. One sub-threshold note, not a finding:
  `GET /api/jobs/{job_id}` only tenant-checks jobs with a `flow_id` in
  their payload (`crawl_site`/`import_kb_bundle`/`embed_kb_entry` don't);
  not fixed since no endpoint anywhere leaks another tenant's `job_id` for
  an attacker to exploit it with — worth tightening for defense-in-depth,
  not urgent. **Injection/SSRF: two real findings, both fixed.**
  (1) `ingestion/webcrawl.py`'s "crawl this site" KB feature validated the
  *hostname string* against a private/loopback prefix regex before the
  initial request and before queueing each discovered link, but never
  re-validated after a redirect (`allow_redirects=True`) — a page that
  302s to `http://169.254.169.254/...` (cloud instance metadata) or an
  internal service got silently fetched and could land in the tenant's KB
  (and later an auto-sent reply). The string-match approach was also
  bypassable by DNS rebinding (a domain resolving publicly when checked,
  privately when requested). (2) `api/main.py`'s `create_connection` only
  checked the URL *scheme*, not the host — any tenant **editor** (not just
  an operator) could point a connection's `base_url` at internal
  infrastructure and reach it via an `http_request` node.
  **Fix:** new `interpreter/net_safety.py::is_public_http_url()` —
  resolves the real IP(s) (`socket.getaddrinfo`) and rejects private /
  loopback / link-local / reserved / multicast, not a hostname-string
  match; every resolved address must be safe, not just the first (a
  round-robin host could otherwise land on a private one). Wired into (1)
  `webcrawl._get_no_ssrf` — redirects are now followed by hand
  (`allow_redirects=False`), re-validating the resolved host before every
  hop, capped at 5; and (2) `create_connection`, rejecting an unsafe
  `base_url` at creation time (422). **Not touched, a separate, smaller
  residual:** `h_http_request` itself still uses `requests.request(...)`
  with default redirect-following against an already-validated
  connection's `base_url` — lower risk (the host was operator/editor-
  chosen and validated at creation, not attacker-supplied per-request),
  not flagged as a finding by the audit, left alone per not expanding
  scope beyond what was actually found. **Verify:** 9 new offline tests
  (`tests/test_net_safety.py` — IP literals, DNS-rebinding simulation,
  mixed-address host rejection; `tests/test_webcrawl.py` — redirect to a
  private host refused / a safe redirect still followed) + 1 new live
  integration test (`test_connection_base_url_rejects_a_private_target`,
  against the real endpoint, no cleanup needed since a rejected `base_url`
  never reaches the upsert) — 499 offline + this integration test green.
- **Fixed (2026-09-03):** this Supabase project never had Acme's original
  seed flows (Phase 0/3/4 — `1111…` `support`, `c3c3…` `offboarding`)
  applied at all, only their later Phase 20e `email` flow — `flows`
  had exactly 2 rows (Acme/email, Globex/support) instead of 4. Every
  `pytest -m integration` run all session (locally and in CI) failed
  `test_multiflow.py`/`test_queue.py`/`test_feedback.py` with
  `FlowNotFound` for `tenant=00000000… team=support/offboarding`,
  repeatedly (mis)diagnosed across several PRs as an unfixable
  environment gap rather than actually checked. Restored by re-applying
  migrations `003`→`008`→`009`→`019`'s exact seed statements by hand via
  the Supabase MCP (idempotent — every insert is `on conflict do
  nothing`; `019`'s global re-snapshot was scoped to `published_version
  is null` so it didn't touch Globex's already-current version). This
  surfaced one more real, previously-dormant bug: `008`'s `sf_writeback`
  node config mapped the classifier's *raw* `topic` slug straight onto
  the restricted `Module__c` picklist, instead of the picklist-safe
  `case_module` (`salesforce.map_case_fields()`-derived) every other
  flow's default `field_map` already uses — an unmapped topic like
  `"webhook-testing"` got rejected by Salesforce
  (`INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST`). Fixed going forward (not
  by editing `008` — migrations aren't amended) by a new migration,
  `079_fix_acme_sf_writeback_field_map.sql`. **Verified: all 7
  previously-"known-gap" tests now genuinely pass** (`test_multiflow.py`
  ×4, `test_queue.py` ×2, `test_feedback.py` ×1).
- **Fixed (2026-09-03):** `SUPABASE_ANON_KEY` was added as a repo secret
  but the `integration` job still failed `test_api.py` wholesale with
  `httpx.InvalidURL: ... '\n' at position 40` — the `SUPABASE_URL` secret
  itself carries a trailing newline (a common artifact of how secrets get
  pasted/piped in), landing right where `api/main.py` appends
  `/auth/v1/user`. `.strip()`ed every `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`/
  `SUPABASE_ANON_KEY` read at the point it comes out of `os.environ`
  (`api/main.py`, `ingestion/scraper.py`, `ingestion/neo4j_sync.py`,
  `scripts/set_editor_password.py`, `scripts/verify_migrations.py`,
  `interpreter/config.py`).
- **Fixed (2026-09-03):** with the above cleared, `test_queue.py::
  test_worker_runs_the_job_and_records_the_run` still failed
  intermittently in CI (`process_one(sb) is True` → `False`) — traced to
  a real race against this project's **own live infra**: the
  docker-compose `worker` container polls this exact Supabase project's
  `jobs` table continuously, and `jobs.claim()`/`claim_job()` is a global
  FIFO claim with no way to target one specific row, so the deployed
  worker can (and did) claim a test's own job before the test's
  `process_one()` call got to it. Fixed with migration `080` — a
  `claim_job(p_job_id uuid)` overload (Postgres dispatches by arg list,
  so the original zero-arg `claim_job()` used by every real poll loop is
  untouched) — plus `interpreter/jobs.py::claim()` and
  `api/worker.py::process_one()` gain an optional `job_id` kwarg;
  `test_queue.py`'s two job-claiming tests now target their own
  `job_id` (already returned by `jobs.enqueue()`), immune to the live
  worker. **Verified: `test_queue.py` 3/3 pass repeatedly.**
- **Known, accepted, not chased further — root cause confirmed
  (2026-09-03):** `test_multiflow.py::test_seeded_flow_routes_as_designed
  [ACME-support]` and `test_same_case_diverges_across_tenants` can
  intermittently fail (`ask_human` instead of `auto_reply`) — failed
  identically 3 CI runs in a row. Initially suspected a CI-vs-local
  environment difference (e.g. GH Actions' shared IPs getting rate-limited
  more than local); **investigated and ruled that out** — a local
  diagnostic run (`interpreter.llm._cache` cleared between calls, so each
  is a genuinely fresh LLM call, same as a cold CI process) showed the
  *exact same* fallback-chain exhaustion happening locally, right now:
  every provider (`llama-3.3-70b-versatile`, `openai/gpt-oss-20b`,
  both `google/gemma-4-*:free`) rate-limited/erroring in sequence, every
  single call landing on the last-resort fallback,
  `nvidia/nemotron-3-ultra-550b-a55b:free`. **Real cause: shared quota,
  not environment.** One Groq/OpenRouter key serves CI, local dev, *and*
  this project's always-on Docker stack (`worker`/`poller`/`cdc`/
  `slackbot`, all continuously making live LLM calls) — heavy testing
  compounds with the always-on stack's draw and exhausts it, so *any*
  caller (CI or local) gets routed to the weakest fallback model at that
  moment, whose classify/draft/groundedness output is different enough to
  flip Acme's `0.5` `basic`-tier confidence-gate threshold. Not per-request
  randomness — it's whichever model actually answers, decided by
  real-time shared-quota state. **Deliberately not fixed**: a capacity/
  billing concern (a separate CI-scoped API key, a paid tier, or pausing
  the local stack during test runs), not a code defect — changing the
  gate threshold to paper over it would be a product decision, not a bug
  fix.
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
  values aren't `basic/premium/enterprise`, so tier used to fall back to
  `basic` (the *most permissive* bar). **Fixed in Phase 7** —
  `_norm_tier` now returns `enterprise` (strictest) + a warn on an
  unrecognised value.
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
  so re-running the same Case grows the field. **→ Phase 10** (idempotency:
  a completed run for the case makes the terminal handlers no-op).
- Seeded `region` is a country ("United States" / "United Kingdom") not a
  region code — the org has State & Country picklists, so `BillingCountry`
  must be a real country; `get_case` reads it straight back into
  `account.region` and thus `Case.Region__c`. Map country→region in
  `get_case` if AMER/EMEA semantics are wanted.
- `Account.Tier__c` is a `Text(40)` custom field created by
  `scripts/sf_create_fields.py`; `get_case` prefers it over the standard
  `Account.Type` picklist for `classify`'s tier.

- ~~**Confidence gate under-escalates with a real LLM.**~~ **Fixed
  2026-08-29 (`019_recalibrate_gate.sql`).** Was: 8/8 e2e misses were
  premium `gold=ask_human` auto-answered, `draft_confidence` ~0.95
  swamping `retrieval_score` at the 0.5/0.5 blend. Now: `confidence_gate`
  takes an explicit `weights={retrieval,draft,groundedness}` (draft → .1)
  and an `escalate_topics` list that forces `ask_human` on
  billing/refund/pricing/legal/account-access/data-export/partner-api/
  cancellation intents. Real-Groq e2e → acc/auto-P/esc-P all **1.000**.
  *Residuals:* `escalate_topics` is a static list matched on slug tokens
  (`registry._slug_tokens`) — depends on `classify` emitting a matching
  slug, and is superseded by Phase 16's structured `policy_gate`;
  `run_e2e.py`'s threshold sweep still uses the legacy 1-D blend.
- **Groq free tier is 8k TPM.** `openai/gpt-oss-120b` at that ceiling
  means the e2e eval throttles heavily (p50 latency ~10 s/case, with
  429 backoff) and a large retrieval context can still 413 if
  `_context_block`'s caps are raised. Fine for demo / low volume; a
  paid tier or Anthropic key removes it.

### Design debt — addressed by the hardening roadmap (phases 7–13)

- **LLM providers:** `interpreter/llm.py` routes by model id — Groq
  (`openai/gpt-oss-*` default; retired `llama-*` names still map to Groq
  so old configs don't `KeyError`) or **Anthropic** (`claude-opus-5` /
  `sonnet-5` / `haiku-4-5`, opt-in). Set `ANTHROPIC_API_KEY` + optionally
  `LLM_DEFAULT_MODEL` / `LLM_FAST_MODEL` in `.env` to use Claude for
  `draft` / `classify` / the groundedness + SOP judges. Routing +
  stub-fallback + the Groq `json_validate_failed` / 429 recovery paths
  are unit-tested; a **live Claude call is unverified** (no key in this
  environment). Sampling params (`temperature` etc.) are not sent on the
  Claude path — rejected by the Claude 5 family.

From the 2026-08-29 self-review. Each is intentional MVP scope, not a bug:

- ~~The `confidence_gate` score is uncalibrated; no groundedness check;
  graph-expansion unmeasured.~~ **Phase 7:** `eval/e2e/` measures auto-send /
  escalation precision, `011` calibrated the Acme gate (auto-send P
  0.77→0.83), `groundedness.py` feeds the gate. *Residual:* 2 e2e cases
  (SOC2 / Partner-API) are relevant-but-not-a-doc-answer — need an intent →
  `ask_human` edge (flow-authoring follow-up). Graph-expansion now has
  `qrels_hard.jsonl` to score against but the run isn't wired into a
  regression floor yet.
- ~~Flows mutated in place; `version` decorative; `runs` doesn't record
  the flow version; `PUT` non-transactional; no optimistic concurrency.~~
  **Phase 8:** `flow_versions` snapshots, `runs.flow_version`,
  `replace_flow_graph` RPC, 409 on stale `PUT`. ~~*Residual:* rollback
  re-points + restores the draft but doesn't keep a "rolled back from vN"
  audit note; `flow_versions` has no prune/retention.~~ **Phase 28
  steps 1-2 (2026-09-03):** the rollback note is now an `audit_log`
  entry (`flow.rolled_back`, `{from_version, to_version}`);
  `purge_old_flow_versions()` (migration `077`) prunes old versions,
  never the currently published one.
- ~~No tests for `api/` or `web/`; no CI; brittle `test_multiflow`;
  hand-rolled runner.~~ **Phase 9:** `pytest` + `pytest.ini`,
  `tests/test_api.py` (offline + integration), `web` `vitest`,
  `.github/workflows/ci.yml` gating `main`. `test_multiflow` now asserts
  structural + relative invariants. *Residual:* no Playwright end-to-end on
  the web; the `eval/e2e/` auto-send-precision floor isn't wired into CI
  yet (needs Supabase creds in CI, or a recorded fixture).
- ~~Nothing triggers the flow; synchronous in-request; no idempotency.~~
  **Phase 10:** `jobs` queue + `claim_job()`, `api/worker.py`,
  `ingestion/sf_case_watch.py` polling trigger, `Idempotency-Key` + unique
  `(flow_id, key)` on `runs`, `(kind, dedupe_key)` on `jobs`. *Residual:*
  no always-on host here so the worker + trigger cron aren't deployed (code
  + `--once` verified); `POST /run` stays synchronous for the editor;
  `jobs.fail` retry is fixed-interval, not exponential-backoff.
- ~~`ask_human` is fire-and-forget — the human's resolution is dropped.~~
  **Phase 11:** delayed `check_resolution` job diffs the Case's outbound
  reply against `runs.draft` → `human_action` / `edit_distance`; Runs view
  shows a "draft kept %"; `scripts/harvest_feedback.py` -> golden cases.
  *Residual:* the diff is lexical (`SequenceMatcher`), not semantic; no
  UI to correct a mis-bucketed resolution; few-shot pool from accepted
  drafts is `harvest_feedback.py` output, not yet wired into the `draft`
  node.
- ~~Multi-tenancy real for reads only; one global corpus; one SF org.~~
  **Phase 12:** `sources` + `source_id` on chunks, `resolve_sources`
  scoping (no cross-tenant KB leak), a real `globex-sop` source,
  `tenant_integrations` + `salesforce.client_for(tenant_id)`. *Residual:*
  the `zapier-public` corpus is still shared by all tenants (fine — it's
  public); per-source *incremental* ingestion isn't built (markdown source
  is full-replace); `tenant_integrations.secret` is plain jsonb, not
  Supabase Vault / pgsodium encrypted.
- ~~`api`'s `caller` decodes the JWT without verifying its signature. No
  rate limiting on `/run`.~~ **Stale — both already fixed, found while
  grounding a 2026-09-03 feature-gap review** (no PR reference; predates
  this session's audit-log discipline). `api/main.py::_verify_token()`
  (line ~150) authoritatively checks every bearer token against Supabase
  Auth's `/auth/v1/user` (signature + expiry + revocation, 60s cache) —
  `Caller.__init__` calls it before anything else. A process-local
  token-bucket `rate_limit(user_id, bucket, limit, window)` (line ~199)
  is applied to `run` (20/min), `assist` (30 or 12/min per endpoint),
  `enqueue` (120/min), and public webhooks (300/min, keyed by trigger
  token). ~~**Still genuinely open:** no `/security-review` has been run
  over the accumulated `api/`+`web/` diff (Phase 5 through Phase 29).~~
  **Stale note (fixed 2026-09-04):** this claim was itself out of date —
  see "first real security review of the accumulated api/+web/ surface"
  above (2026-09-03, same "Known issues" section): it already ran, found
  and fixed 2 real SSRF issues, AuthZ/RLS/secrets came back clean. Two
  stale claims about the same fact, in the same file, is exactly the
  "edited in place, not appended" drift CLAUDE.md now warns about —
  fixed here rather than left for a third session to trip over.
  `sop_conflicts.py` exists but isn't wired into CI as a non-blocking
  report (needs Phase 12's divergent per-team retrieval to have something
  to actually find first — check whether that's true yet before wiring it).
