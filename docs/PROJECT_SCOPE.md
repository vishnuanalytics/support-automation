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
| **P5** 🔶 | Structural unlock (**P5a done 2026-09-02**) | **P5a** — `CaseState.context` (a declared, merge-friendly `operator.or_` bag); `builder.initial_state(flow, case=, context=)` used by all 3 invoke sites (`run.py` / `worker` / `api`); `builder._context` exposes `context` + `input` (`context or case`) so an edge condition is transport-neutral; `RunIn.context`. A no-Case flow now runs: payload survives the graph, nodes read/write `state['context']`, edges branch on `input.*`. **P5b** — a `trigger` node + `interpreter/triggers.py` webhook/schedule adapters (feeds P6). **P5c** — `retrieve`/`classify`/`draft` handlers read `input.*` when there's no `case`. **P5d** — `runs`/`trace` record a generic context, not just `case_payload`. | P2 |
| **P6** | Triggers + connectors | webhook + schedule trigger endpoints; a minimal declarative connector spec + a connections-manager UI | P5 |
| **P7** | Self-serve onboarding | template gallery, setup wizard, file-upload KB ingestion, "crawl my site" — the "1 hour to value" path | P5, P6 |
| **P8** | KIL depth (parallelizable) | KIL learning report (weekly digest) → provisional KB tab → KIL eval depth (human-reply precision, `draft_change` quality) → **KIL-g** claim graph (needs the loop to have run in real use) | KIL |

**Immediate next step: P1 (1a + 1b).** 1b is the one that can put wrong info
in front of a customer — a `provisional` (unverified) KB correction is
currently retrieved and cited with the same weight as a `confirmed` entry.

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
  `replace_flow_graph` RPC, 409 on stale `PUT`. *Residual:* rollback
  re-points + restores the draft but doesn't keep a "rolled back from vN"
  audit note; `flow_versions` has no prune/retention.
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
- `api`'s `caller` **decodes the JWT without verifying its signature** (RLS
  is the only real gate). No rate limiting on `/run`, which has real
  external side effects. No security review of the Phase 5/6 surface.
  `sop_conflicts.py` finds nothing on the current seed data. **→ Phase 13.**
