# AI Support Automation Platform

Multi-tenant support automation: agents read incoming cases, retrieve from a
knowledge base, draft replies, and either auto-send, ask a human, or hand
over — gated by a confidence score that is stricter for higher-value
customers. **The agent is data, not code**: node types, edges, and per-node
config live in Postgres and are interpreted into a real LangGraph
`StateGraph` at runtime, so the visual editor is a second client on the same
schema rather than a rewrite.

Reference domain: Zapier's public developer docs (`docs.zapier.com`).

## Layout

| path | what |
|---|---|
| `docs/` | `PROJECT_SCOPE.md` (phase status — the source of truth), `SALESFORCE_SETUP.md` |
| `db/migrations/` | `001…NNN` sequential, single-concern SQL |
| `ingestion/` | Phase 1 + 12 — `scraper.py` (Zapier docs), `sources/markdown_source.py` (per-tenant KB sources), `neo4j_sync.py`, `sf_case_watch.py` (trigger), `eval/` |
| `interpreter/` | Phase 2–4 — flow loader, `StateGraph` builder, node registry, safe condition eval, hybrid retrieval, Groq + Salesforce clients (real-or-dry-run). `flows/` validator, `cases/` samples |
| `api/` | Phase 5–6 — FastAPI: list/load/validate/save/run flows + `runs` observability (reuses `interpreter/`) |
| `web/` | Phase 5–6 — React + React Flow editor + a Runs view, over the same schema, Supabase Auth + RLS |
| `scripts/` | ops helpers — SF custom-field setup, seed data, RLS check, `sop_conflicts.py` |
| `tests/` | `test_interpreter.py` (offline), `test_multiflow.py` (integration) |

## Quickstart

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env    # fill SUPABASE_URL / SUPABASE_SERVICE_KEY; NEO4J_* and SF_* optional

# run a flow (stub LLM + SF dry-run when creds absent — works with a minimal .env)
python -m interpreter.run --list
python -m interpreter.run --flow 11111111-1111-1111-1111-111111111111 \
  --case interpreter/cases/basic_howto.json

python -m tests.test_interpreter          # offline unit tests
python -m tests.test_multiflow            # 3 flows / 2 tenants (needs Supabase)

# Phase 5
uvicorn api.main:app --reload             # backend on :8000
cd web && npm install && npm run dev      # editor on :5173
```

## Status

Phases 0–15 built — see `docs/PROJECT_SCOPE.md` for what each delivered
and how it was verified. Migrations `001`–`024` applied; daily ingestion
runs on GitHub Actions; 42 offline tests + integration tests.
Phase 14 (self-serve internal knowledge base + `kb_lookup` node) and
Phase 15 (Google Docs connector — live-unverified, needs a Google OAuth
client) are built. Phase 16 (structured policy rules + Slack-approved
internal actions) is planned — full spec in `docs/PROJECT_SCOPE.md`.

## Cost stance

Free/local by default: `fastembed` (ONNX `bge-small-en-v1.5`, CPU) for
embeddings, a local cross-encoder for rerank, Groq free models for LLM
calls, Supabase + Neo4j Aura free tiers, a personal Salesforce Developer
Edition org.
