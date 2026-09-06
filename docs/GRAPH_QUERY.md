# Ask the case graph (natural-language → composable spec → Cypher)

`interpreter/graph_query.py` + `POST /api/graph/ask` + the "Ask the case
graph" panel in the Approvals/Review tab.

A support manager types a question in English — *"how many API-module
cases were escalated last week"*, *"which accounts have more than 2
duplicate cases"*, *"average resolution time by tier for Billing"* — and
gets a table back, plus the exact query that produced it.

## Why this is not text-to-Cypher

The platform runs **one shared Neo4j Community-edition database**: no
per-tenant database, no custom read-only role (see
`MULTI_SYSTEM_ARCHITECTURE.md`). The only thing separating one tenant's
case text from another's is a `WHERE n.tenant_id = $tenant_id` predicate
on every node in a query. Handing an LLM a free-form Cypher surface would
make a single prompt-injection or a single validator gap a cross-tenant
data breach.

So the LLM's job is bounded to **English → a small JSON spec**. A
deterministic compiler turns the spec into Cypher, bolting `tenant_id`
onto every pattern. **The LLM never sees or writes Cypher.** Validating
"is this JSON a legal spec" is total and trivial; validating "is this
arbitrary Cypher safe" is not.

## The spec

```jsonc
{
  "entity": "case",                    // always "case" in v1
  "metric": "count",                   // count | list | avg_resolution_hours
                                       //   | distinct_accounts | duplicate_count
  "group_by": ["module"],              // 0–2 dimensions
  "filters": [                         // ≤ 6, each (field, op, value) from the allow-list
    {"field": "status",    "op": "eq",  "value": "escalated"},
    {"field": "opened_at", "op": "gte", "value": "2026-08-01"}
  ],
  "having":   {"op": "gte", "value": 2},          // optional, group_by aggregations only
  "order_by": {"field": "value", "dir": "desc"},  // optional
  "limit": 50                                     // clamped 1..200
}
```

**group_by dimensions:** `module`, `case_type`, `agent`, `account`,
`tier`, `status`, `routed_team`, `origin`, `resolution_kind`,
`is_closed`, `opened_month`.

**filter fields:** `status`, `tier`, `routed_team`, `origin`,
`resolution_kind`, `is_closed`, `module`, `case_type`, `agent`,
`opened_at`, `closed_at`, `subject` (contains), `has_duplicate`. Ops per
field are in `_FILTERS`; dates are ISO strings; `in` takes a list.

If the question can't be expressed with these names the LLM returns
`{"error":"unsupported"}` and the API answers 422 with a "try asking
about …" message — it never guesses.

## What the compiler guarantees

- **Every** compiled query filters `c.tenant_id = $tenant_id`, and that
  `WHERE` is always preceded by a `WITH` so it is a standalone row filter,
  never absorbed into a preceding `OPTIONAL MATCH` pattern (which would
  silently drop the scoping). A test asserts this for every metric shape.
- The `Account` node and the `DUPLICATE_OF` target `Case` also carry
  `{tenant_id: $tenant_id}` in-pattern (defence in depth).
- All filter values are **query parameters**, never string-interpolated —
  a value like `'; MATCH (n) DETACH DELETE n //` lands in `$f0`, inert.
- `_assert_safe()` re-scans the final string and refuses it if it matches
  any of `CREATE|MERGE|DELETE|SET|REMOVE|CALL|FOREACH|GRANT|REVOKE|LOAD
  CSV|USING PERIODIC`, or if the tenant predicate is missing.
- Execution uses `routing_=RoutingControl.READ` and a per-query timeout
  (`GRAPH_QUERY_TIMEOUT_S`, default 8s); results are `LIMIT`-clamped.

## Blast radius

Reachable nodes: `Case` (tenant-scoped), the content-free `Module` /
`CaseType` / `Agent` nodes (reached only through a tenant-scoped Case),
and `Account` (tenant-scoped). **`Reply` and `Message` text are not
exposed** through this surface at all.

## Access

Owner-only (`_require_owner`), same bar as billing and member management.

## Residual limitations (stated, not pretended away)

- On Community edition the tenant boundary in Neo4j is enforced **only in
  application code** (the compiler). There is no database-level backstop
  such as a per-tenant DB or a restricted role. The compiler is small and
  fully tested, which is far safer than validating free Cypher — but it is
  the whole barrier.
- Only single-anchor `Case` questions. Multi-hop path questions ("cases
  similar to cases an account escalated") need a new compiler branch — a
  follow-up chunk; today the tool answers `{"error":"unsupported"}`
  rather than guessing.
- `avg_resolution_hours` uses `datetime(c.closed_at)` / `c.opened_at`
  parsing; cases with unparseable timestamps are excluded by the
  `is_closed = true AND … IS NOT NULL` prefilter.
