# api/ — editor backend (Phase 5)

Thin FastAPI layer over `interpreter/` for the React Flow editor. Supabase
does auth + tenant isolation; this service only adds the parts that need
Python: structural validation, and compiling + running a flow.

Every request carries the caller's Supabase access token
(`Authorization: Bearer …`). Flow reads/writes go through a Supabase client
authed as that user, so the Phase 4 RLS policies scope everything. The
service-role client is used only for the interpreter's own machinery
(retrieval, running a compiled graph).

## Run

```bash
uvicorn api.main:app --reload           # :8000  — the HTTP API
python -m api.worker                     # the job worker (drains the `jobs` queue)
python -m api.worker --once              # ... or drain-and-exit (cron / tests)
```
Needs `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_ANON_KEY` in `.env`.
`WEB_ORIGINS` (comma-sep) overrides the CORS allow-list (default
`http://localhost:5173`).

## Endpoints (`/api`)

| method + path | does |
|---|---|
| `GET /health` | `{ok:true}` |
| `GET /node-types` | `{types:[…], defaults:{type:{…}}}` — palette |
| `GET /flows` | list, RLS-scoped to the caller's tenants |
| `POST /flows` | create `{tenant_id, team, name, status?}` → `{flow_id}` |
| `GET /flows/{id}` | the editable **working draft** (flow_nodes/edges) + `published_version` |
| `PUT /flows/{id}` | save the draft — one transactional RPC (`replace_flow_graph`). `409` if `body.version` ≠ the flow's current `version` (optimistic concurrency; bumped every save). `422 {errors}` on refs/orphans/cycles/unknown-type. Publishing is `/publish`, not a status change here. |
| `GET /flows/{id}/versions` | immutable published snapshots (`flow_versions`) — version, name, `definition_hash`, `created_by/at` |
| `POST /flows/{id}/publish` | snapshot the draft → new `flow_versions` row, point `published_version` at it. `422` if the draft is invalid. |
| `POST /flows/{id}/rollback` | body `{version}` — restore the draft from that snapshot and re-publish it |
| `POST /flows/{id}/validate` | `{valid, errors}` for a posted flow dict, no write |
| `POST /flows/{id}/run` | **synchronous** (the editor's "try a case") — loads the **published snapshot**, `invoke`s → `{run_id, …}`; persists a `runs` row incl. `flow_version`. Optional `Idempotency-Key` header → a run with that `(flow_id, key)` already recorded is returned as `{run_id, idempotent_replay: true}` without re-running. |
| `POST /flows/{id}/enqueue` | **async** (the Salesforce trigger) — body `{case, idempotency_key?}` → `202 {job_id}` (or `{deduped: true}`). A worker executes it. |
| `GET /jobs/{job_id}` | `{status, attempts, result, error}` — `result.run_id` once `done` |
| `GET /runs/stats` | `{total, by_outcome, by_tier, low_confidence}` over the last 500 visible runs |
| `GET /runs?flow_id=&outcome=&limit=` | RLS-scoped list, newest first |
| `GET /runs/{run_id}` | full run — `trace`, `gate`, `retrieval`, `sf_writeback`, `case_payload` (the "why") |

New nodes/edges get client-generated UUIDs so `PUT` is a clean upsert.
`run` loads the flow with the service role (retrieval needs full read) but
first checks the caller can see it under RLS.

## Feedback loop (Phase 11)

A run that ends in `ask_human`/`handover` on a real Case is stamped
`human_action = pending` and `record_run` schedules a delayed
`check_resolution` job. The worker then reads the Case's outbound reply
(EmailMessage / CaseComment) and diffs it against `runs.draft` →
`human_action` (`sent_as_is`/`edited`/`rewrote`/`no_reply`) + `edit_distance`.
`/runs/stats` reports `draft_acceptance` and `by_human_action`.
