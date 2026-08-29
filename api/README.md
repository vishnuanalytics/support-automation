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
uvicorn api.main:app --reload           # :8000
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
| `GET /flows/{id}` | full flow (nodes + edges), unvalidated |
| `PUT /flows/{id}` | save `{name,status,version,nodes,edges}`; `422 {errors}` if it fails refs/orphans/cycles/unknown-type; reconciles rows (delete removed, upsert rest) |
| `POST /flows/{id}/validate` | `{valid, errors}` for a posted flow dict, no write |
| `POST /flows/{id}/run` | body `{case}` → compile + `invoke` → `{trace, outcome, tier, confidence, confidence_gate, sf_writeback, retrieval, query}` |

New nodes/edges get client-generated UUIDs so `PUT` is a clean upsert.
`run` loads the flow with the service role (retrieval needs full read) but
first checks the caller can see it under RLS.
