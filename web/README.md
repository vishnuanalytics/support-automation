# web/ — React Flow editor (Phase 5)

A canvas over the same `flows` / `flow_nodes` / `flow_edges` rows the
interpreter runs. Vite + React + `@xyflow/react`; Supabase Auth for login,
the Phase 5 `api/` for load / save / validate / run.

## Run

```bash
cp .env.example .env.local     # VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY
npm install
npm run dev                    # http://localhost:5173

# in another terminal — the backend it talks to
uvicorn api.main:app --reload  # http://localhost:8000  (vite proxies /api -> here)
```

Sign in with a Supabase account. You'll only see flows for tenants you're a
member of (`tenant_members`) — RLS, enforced by your session token.

## What it does

- **sidebar** — flows grouped by tenant, with a `＋ New flow` (prompts for
  tenant_id / team / name → `POST /flows`).
- **canvas** — nodes auto-laid-out (dagre) when they have no saved
  `position_x/y`; drag to reposition (persisted on Save). Conditional edges
  are animated and show their `if` expression.
- **palette** (top-left) — one button per registered node type; adds a node
  with that type's default config.
- **connect / delete** — drag between handles to add an edge; `Del` removes
  the selection.
- **inspector** (right) — edit a node's label + `config` JSON (with a
  friendly per-tier threshold form for `confidence_gate`), or an edge's
  `condition.if`.
- **Validate** — `POST /flows/{id}/validate`; shows refs / orphan / cycle /
  unknown-type errors without saving.
- **Save** — `PUT /flows/{id}`; 422 lists the structural errors, nothing is
  written.
- **Save draft** — `PUT /flows/{id}` (transactional). A stale save (someone
  else edited it) → `409` → the editor auto-reloads their version.
- **Publish** — snapshots the current draft into an immutable version; runs
  execute the published snapshot, not the live draft.
- **rollback ▾** — restore the draft + published pointer to an older version.
- header shows `published vN` (what runs use) and `draft rev` (the
  concurrency token).
- **Run a case** (right, below the inspector) — `POST /flows/{id}/run`;
  shows the trace, outcome, gate, Salesforce writeback, and retrieved docs.
  Every run is also persisted.

## Runs view (Phase 6)

The **Runs** tab (sidebar toggle) is the observability surface:

- stat tiles — total runs, count per outcome, per tier, and a
  low-confidence (`< 0.4`) count.
- a filterable table (all / auto_reply / ask_human / handover) of recent
  runs across every tenant you belong to, RLS-scoped.
- click a run → the **why**: each `trace` step (expandable to its `data`),
  the gate arithmetic (`retrieval · draft → score vs threshold → PASS/FAIL`),
  the retrieved docs, and the Salesforce writeback.
