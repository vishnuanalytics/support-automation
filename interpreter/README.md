# interpreter/ — config-driven LangGraph interpreter (Phase 2)

A support agent here is **data, not code**: a `flows` row plus its
`flow_nodes` / `flow_edges` in Supabase. This package reads one such flow and
compiles it into a real LangGraph `StateGraph` at runtime. Nothing about the
runtime changes when the Phase 5 no-code UI starts writing those same rows.

```
loader.py      flow_id  ->  flow dict   (+ validate_flow.check_flow: refs, orphans, cycles)
builder.py     flow dict ->  compiled StateGraph
registry.py    node.type (free string) -> handler fn
conditions.py  edge.condition.if  -> safe boolean eval (AST whitelist, no eval())
retrieval.py   hybrid dense+sparse -> RRF -> Neo4j graph-expand -> cross-encoder rerank
llm.py         Groq free models, with a deterministic offline stub
salesforce.py  Case read/write + Chatter, real when SF creds present, else dry-run
state.py       CaseState (the shared graph state; `trace` is append-only)
run.py         CLI
```

## Run

```bash
# the Phase 0 seed flow, built-in sample case
python -m interpreter.run --flow 11111111-1111-1111-1111-111111111111

# a specific case file
python -m interpreter.run --flow 11111111-1111-1111-1111-111111111111 \
    --case cases/enterprise_bug.json

# select by team instead of id (uses the unique published flow for that team)
python -m interpreter.run --tenant 00000000-0000-0000-0000-000000000000 \
    --team support --status draft

# pull a live Salesforce Case and run it (needs SF creds in .env)
python -m interpreter.run --flow <id> --sf-case 500XXXXXXXXXXXXXXX

# just print the wiring
python -m interpreter.run --flow <id> --describe
```

No `GROQ_API_KEY` set → `llm.py` returns deterministic stubs, so the whole
graph still runs (CI, eval, demos). Set the key in `.env` to switch every
LLM call to the real API — nothing else changes.

## The flow it runs

Phase 0's Support flow (`003_seed_example_flow.sql`), + the `sf_writeback`
node added in Phase 3 (`008_seed_sf_writeback_node.sql`):

```
retrieve → classify → sf_writeback → draft → confidence_gate ─┬─ [pass & tier≠enterprise]  → auto_reply
                                                              ├─ [¬pass & tier≠enterprise] → ask_human
                                                              └─ [tier == enterprise]      → handover
```

`sf_writeback` pushes `classify` output (`urgency`→`Priority`,
`topic`→`Module__c`, `region`→`Region__c`, `summary` appended to
`Description`) onto the Salesforce Case named by `case.sf_id`. `ask_human`
with `channel: salesforce_chatter` posts a Chatter @mention on that Case.
Both no-op cleanly when there's no `sf_id`, and dry-run (log intent) when
there are no SF creds — see `../SALESFORCE_SETUP.md`.

`confidence_gate` score = `retrieval_weight·retrieval_score + (1−retrieval_weight)·draft_confidence`,
compared against a **per-tier** threshold
(`{basic: 0.35, premium: 0.45, enterprise: 0.6}` from the node's `config`).
Enterprise always routes to `handover` regardless of the gate — the stricter
bar for higher-value customers is the whole point of the design.

## Node handler contract

```python
@register("my_type")
def handler(state: CaseState, config: dict) -> dict:
    # config includes the node's jsonb config plus injected `_node_id` / `_label`
    return {"some_state_key": ..., "trace": [ {node_id, type, summary, data} ]}
```

Return a **partial** state update (LangGraph shallow-merges it). Append
exactly one `trace` entry. Add a new type here + (if it should be mandatory
for a "complete" flow) one line in `validate_flow.EXPECTED_TYPES`. No
migration — that's what the generic `type` column buys.

## Edge conditions

`condition` jsonb is `{}` (unconditional) or `{"if": "<expr>"}`. `<expr>` is a
small boolean expression over the state, evaluated by `conditions.py` against
a strict AST whitelist (bool ops, `not`, comparisons, names, attribute/index
access, literals — never `eval()`). Names available: `tier`, `region`,
`confidence`, `retrieval_score`, `draft_confidence`, `confidence_gate`,
`classification`, `outcome`, `case`. `confidence_gate.pass` works even though
`pass` is a Python keyword (`.keyword` is rewritten to `["keyword"]` before
parsing). A source node with multiple outgoing edges routes to the first
matching condition; an empty-condition edge on that node is the `else`.
