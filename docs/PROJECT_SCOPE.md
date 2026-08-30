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
hard guard · 20d web Channels panel + docs. Sequential. All four BUILT +
verified (2026-08-30) — Phase 20 COMPLETE; no live e2e against a real
mailbox yet (needs creds).** Decisions: **both** providers —
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
| 20c | **Outbound + hard guard.** `interpreter/emailer.py` SMTP/Gmail send (`From` = `no_reply_addr` or the mailbox; stamps `X-Support-Bot: 1`); the **worker** (not a flow node — keeps the graph channel-agnostic) sends after an email-sourced run *only* when `outcome.action == "auto_reply"` and the channel's `auto_send_enabled` is on; `need_info` sends only if the `clarify` node's own `auto_send` is set; everything else is left for a human. | **Built + verified (2026-08-30).** `interpreter/emailer.py` — pure `decide(outcome, cfg, clarification) → (send_reply|send_questions|needs_human|noop, meta)` (the guard: `auto_reply` sends only with the master switch **on** and a non-empty draft; `need_info` sends only with the switch on **and** `clarification.auto_send`; `ask_human`/`handover`/switch-off/empty-draft → `needs_human`), and `send_reply(cfg, to, subject, body, in_reply_to, references)` — builds a threaded `EmailMessage` stamped `X-Support-Bot: 1` + `Auto-Submitted: auto-replied`, sends via SMTP (imap provider) or `gmail.users().messages().send` (gmail), **dry-run with no creds, never raises**. `interpreter/mailbox.mark_needs_human(cfg, message_id)` — looks the message up by Message-ID and re-marks it unread + `\Flagged` / `STARRED` (the poller marked it read on enqueue). `api/worker._run_flow` → `_email_post_run(final, case, flow, sb)` for `case.channel == "email"`: applies `decide`, sends or flags, returns the delivery in the job result; wrapped so a delivery failure never fails/retries the run. **Verify:** 13 pytest (`test_emailer.py` — the full `decide` matrix; `send_reply` dry-run / missing recipient / threaded-bot-stamped headers via a monkeypatched SMTP; `_email_post_run` auto_reply→send, ask_human→flag+no-send, need_info+opt-in→questions, no-channel→skip) incl. 1 integration (a real Vault-loaded channel with no SMTP host → `auto_reply` → `decision=send_reply`, `delivery.dry_run=True`). |
| 20d | **Web + docs.** A **Channels** nav panel (owners): provider picker / Connect Gmail, IMAP form, from-name, optional no-reply, team, folder, the `auto_send_enabled` toggle, Test-connection, status (last poll / last error). Editors read-only. `docs/EMAIL_SETUP.md`. | **Built + verified (2026-08-30).** `web/src/channels/ChannelsView.tsx` — owner-only panel: provider radio (IMAP / Gmail, Gmail disabled + labelled when `gmail_available:false`), team + from-name, IMAP host/port + SMTP host/port + mailbox login + **app-password field that says "leave blank to keep"**, folder, optional reply-from, an **auto-send** toggle (copy: "off = every reply waits for a human") and an **active** toggle; **Test connection** / **Save** / **Disconnect**; a status banner (`status` + `last_poll_at` + `last_error`, red on `error`). Gmail: a **Connect Gmail** button that pops the OAuth consent window. `api.email.{status,save,test,remove,googleAuthorize}` + `EmailChannel`/`EmailChannelSave` types; `App.tsx` gains a **Channels** nav for owners (next to Team). `GET /api/integrations/email` now also returns `last_poll_at`/`last_error`. `docs/EMAIL_SETUP.md` (the safety model, app-password steps for Gmail/Outlook, the cron, the Gmail-provider operator steps). **Verify:** web tsc + vitest (6) + build green; the 3 email API integration tests still green with the status change. |

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

**Phases 0–20 built. No open phase.** Migrations `001`–`035` applied
(`034`/`035` = Phase 20a: `tenant_integrations` poller columns + the
Supabase-Vault `integration_secret_*` RPCs). 138 offline pytest tests
+ web tsc/vitest (6)/build + `tests/test_multiflow.py` (needs Groq quota).
Phase 18d's button is built but signing in with Google needs the Supabase
dashboard Google provider enabled first (`docs/GOOGLE_SETUP.md`
§"Google sign-in"); the Phase 20 Gmail *provider* needs the same
`GOOGLE_CLIENT_ID`/`SECRET` + a redirect registered (the IMAP path needs
nothing server-side). No live e2e of the email channel against a real
mailbox yet — pending IMAP or Gmail creds.

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
**Not yet done:** a live e2e against a real mailbox (needs IMAP or Gmail
creds); a GitHub Actions cron for `email_watch` + a running `api.worker`;
migrating the existing Slack/SF/Google `tenant_integrations` rows onto the
same Vault mechanism (the `integration_secret_*` RPCs are generic).

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
