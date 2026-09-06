# Product-analytics connector — correlate what a user did with why they filed a case

Decision record, not a build plan — same convention as
`MULTI_SYSTEM_ARCHITECTURE.md` and `KB_SOURCE_CONNECTORS.md`. Nothing here
is built except what a section explicitly says is. This phase was chosen
2026-09-06 from a next-phase menu; PostHog is the first provider, Mixpanel
is documented (§7) but not built.

## The goal

`MULTI_SYSTEM_ARCHITECTURE.md` names the product's real pitch: *the
combination produces an output none of Salesforce, Slack or a raw
analytics tool produce alone.* Half of that is the self-correcting KB
(KIL, built). The other half: **a live link between what a customer's
users actually did in the product and why they're contacting support.**

Concretely, when a case comes in from `dana@acme.com`, the bot should be
able to know: she has hit the `api_key_invalid` event 4 times in the last
hour, her account's weekly active users are down 60%, she never completed
onboarding. That changes the draft ("I can see your API key started
failing at 14:02…") and the routing (a churning enterprise account →
human).

## Rule 1 still holds: no second copy of the analytics warehouse

Per `MULTI_SYSTEM_ARCHITECTURE.md` Rule 1, the tenant's Mixpanel/PostHog
stays the system of record for raw product usage. Neo4j holds a
**correlation layer**, not an event store:

| Fact | System of record | Neo4j holds |
|---|---|---|
| Every raw product event | The tenant's PostHog/Mixpanel | nothing |
| A person's recent-activity rollup | derived, refreshed on a schedule | `(:Contact)` properties (`last_seen_at`, `events_30d`, `active_days_30d`, `usage_trend`) |
| Which named milestones/features a person has used | derived | `(:Contact)-[:DID {last_ts, count}]->(:Feature {name})` — only the tenant's **configured** milestone list, not every event name |
| Person ↔ account | derived (identity resolution, see below) | `(:Contact)-[:AT_ACCOUNT]->(:Account)` + an `identity_match` stamp |

**Not** `(:Event {name, ts})` one-node-per-event. The arch doc sketched
that shape; on reflection it is a warehouse copy and it is unbounded.
Rollups + a bounded feature list are enough for the payoff (§5) and stay
inside Rule 1.

## The one hard problem, up front: identity resolution

Analytics tools identify users by a pseudonymous `distinct_id` /
`client_id`. Joining that to a Salesforce `Contact` / `Account` only
works if the tenant's own product instrumentation calls `identify(email)`
(or sets an `email` person property). **This platform cannot fix a
tenant's instrumentation.** Some tenants join cleanly; some have no
identified users at all.

Design consequence (the arch doc's explicit instruction — *surface
match confidence, don't assume a clean join*):

- Every synced `(:Contact)` carries `email` (lower-cased) as its key
  **and** an `identity_match` property, one of:
  - `email` — the analytics person profile has an `email` that exact-matches
    a Salesforce Contact/Account contact we already know. High confidence.
  - `domain` — no email match, but the email's domain matches exactly one
    `(:Account)`'s known domain. Medium confidence; the edge is created
    but flagged.
  - `none` — person has no resolvable email. The `(:Contact)` is still
    created (so rollups exist) but has no `[:AT_ACCOUNT]` edge and is
    invisible to the account-level features.
- A per-tenant **coverage number** — `% of active analytics persons with
  identity_match != none` — is computed each sync and shown on the
  connector card, so a tenant with 8% coverage sees that before trusting
  any account-level signal.
- The `product_signal` node (§5) degrades cleanly: no match → the node
  sets `state.product_signal = {available: false, reason: "no identity
  match"}` and the flow continues exactly as today.

## Introducing `(:Contact)` into the graph

The graph has `(:Case)`, `(:Account {sf_id, tenant_id})`, `(:Module)`,
`(:Agent)`, `(:Message)` — but **no `(:Contact)` node** (case ingestion
only records `ContactId` / `Contact.Email` as `Message` author fields).

This phase adds `(:Contact {email, tenant_id})` and:

- `case_graph_sync` (KIL-a) gains a small change: when a Case has a
  `Contact.Email`, MERGE `(:Contact {email, tenant_id})` and
  `(c:Case)-[:FILED_BY]->(ct:Contact)`. This is the join target — a
  Contact the support side already knows about.
- The analytics sync MERGEs the **same** `(:Contact {email, tenant_id})`
  key and adds the rollup properties + `[:DID]` / `[:AT_ACCOUNT]` edges.
- Constraint: `(:Contact)` uniqueness on `(email, tenant_id)` in
  `ingestion/neo4j_sync.ensure_constraints`.

So a Contact node is co-owned: identity + case linkage from the SF side,
product-activity rollups from the analytics side. Neither writes the
other's properties.

## The connector

### Auth & config

Same pattern as `discourse` / `freshchat` / `github`:

- **Secret** (`vault_secrets.put(tenant_id, "posthog", {api_key})`): a
  PostHog **personal API key** (`phx_…`), scoped read-only where the
  tenant can (query:read, person:read).
- **Non-secret** (`tenant_integrations`, `kind='posthog'`, `config`):
  `{host, project_id, milestone_events: [...], account_domain_prop?}`.
  `host` is `https://us.posthog.com` / `eu` / a self-hosted URL.
- `GET/PUT/DELETE /api/integrations/posthog` — owner-gated, mirrors
  `/api/integrations/google` / `/slack`. `PUT` validates by calling
  `posthog.test_connection` (a cheap `SELECT 1` HogQL query).

### `interpreter/posthog.py`

Mirrors `interpreter/discourse.py`'s shape: `KIND = "posthog"`, `_key()`,
`available()`, `test_connection()`, plus:

- `fetch_person_rollups(tenant_id, sb, *, since=None, limit=...) ->
  list[PersonRollup]` — one HogQL query against
  `POST /api/projects/:id/query/` (`Authorization: Bearer <key>`):

  ```sql
  SELECT person.properties.email               AS email,
         max(timestamp)                         AS last_seen_at,
         count()                                AS events_30d,
         count(DISTINCT toDate(timestamp))      AS active_days_30d
  FROM events
  WHERE timestamp > now() - INTERVAL 30 DAY
    AND person.properties.email != ''
  GROUP BY email
  ```

  plus a second query filtered to `event IN (milestone_events)` for the
  `[:DID]` edges (`event, email, max(timestamp), count()`), and a
  30–60d window comparison for `usage_trend` (`down` / `flat` / `up`).
- `PersonRollup` dataclass: `email, last_seen_at, events_30d,
  active_days_30d, usage_trend, milestones: list[{name, last_ts, count}]`.
- Pagination via HogQL `LIMIT/OFFSET`; a hard cap
  (`POSTHOG_MAX_PERSONS`, default 5000) so a huge tenant can't blow the
  sync — same `exhaustive=False` discipline as the KB connectors.
- Everything best-effort: no key / PostHog down / query error → `[]` +
  a logged warning, never an exception into the sync loop.

## Neo4j sync

New worker job **`product_analytics_sync`** + a sweep (self-re-enqueue,
like `case_graph_sync`) + a step in `daily-sync.yml`:

1. Resolve the connection; `posthog.fetch_person_rollups(since=<watermark>)`.
2. For each rollup: MERGE `(:Contact {email, tenant_id})`, SET the rollup
   props + `synced_at`; MERGE `(:Feature {name, tenant_id})` +
   `(:Contact)-[:DID {last_ts, count}]->(:Feature)` for each milestone.
3. Identity resolution: if a `(:Contact {email})` already had a
   `FILED_BY` edge from a Case → `identity_match = 'email'`. Else if the
   domain matches exactly one `(:Account)` with a known domain →
   `identity_match = 'domain'` + MERGE `[:AT_ACCOUNT]`. Else
   `identity_match = 'none'`.
4. `graph_sync_state` row `scope = 'product_analytics:<tenant>'` — a
   `last_seen_at` high-water mark + `contacts_synced` / `coverage_pct`
   counters. (New `coverage_pct` column, migration.)
5. Account rollup: after contacts, one Cypher pass sets each
   `(:Account)`'s `active_users_30d` / `feature_adoption` /
   `usage_trend` from its `[:AT_ACCOUNT]` contacts, for §5's account-level
   signal.

`Account` domains: a new optional `account_domain` on the SF side (from
`Account.Website` or a `Domain__c` field) synced by `case_graph_sync`;
absent → domain matching is simply skipped for that tenant.

## The payoff: a `product_signal` flow node

A new node type (registry handler `h_product_signal`), dropped by the
flow author at triage/draft time — same "consulted only when the run
reaches it" model as `kb_lookup`:

- Resolves the filer's email from `state` (`case.contact_email` /
  `identify` node output / sender address).
- One tenant-scoped Cypher read: the `(:Contact {email})`'s rollup props,
  its `[:DID]` features, and — if `[:AT_ACCOUNT]` — the account rollup.
- Sets `state.product_signal = {available, identity_match, last_seen_at,
  events_30d, active_days_30d, usage_trend, recent_features: [...],
  account: {active_users_30d, usage_trend} | null}`.
- `h_draft` folds a compact rendering of it into the prompt as
  **context, not instruction** ("Product activity for this user: …"),
  the same way an internal-KB hit is folded in. It counts toward
  groundedness only for claims the bot makes *about* that activity.
- `builder._context` exposes `product_signal` to edge conditions, so a
  flow can route on it (`product_signal.account.usage_trend == "down"`
  and `tier == "enterprise"` → `ask_human`).
- No signal / no identity match / graph down → `{available: false}` and
  the flow behaves exactly as it does today. **Never blocks a run.**

Later (not chunk 4): extend `interpreter/graph_query.py`'s allow-lists
with Event-derived dims so a manager can ask "escalations from accounts
with no activity in 30 days".

## Multi-tenancy

Every `(:Contact)` / `(:Feature)` node carries `tenant_id`; every Cypher
read filters it, same rule as the rest of the graph (no Neo4j RLS —
`MULTI_SYSTEM_ARCHITECTURE.md` Rule 2). `posthog` credentials are
per-tenant in Vault. The `product_analytics_sync` sweep iterates only
tenants with an active `posthog` integration.

## Chunk plan (one verifiable chunk each)

1. **This design doc.** ✅
2. **Connector + credentials.** ✅ (2026-09-06) `interpreter/posthog.py` —
   `PostHogConfig` (`host` / `project_id` / `milestone_events`), Vault-brokered
   API key (`_key`), `load` / `save` / `delete` / `available`, `_query` (one
   HogQL POST to `/api/projects/:id/query/`), `test_connection` (`SELECT 1`),
   `fetch_person_rollups(since, limit)` → `PersonRollup(email, last_seen_at,
   events_30d, active_days_30d, usage_trend, milestones[])` from three HogQL
   queries (base 30d rollup, 30-vs-prior-30d trend, milestone counts over
   90d; `_MAX_PERSONS`=5000 cap; milestone names validated against
   `_EVENT_NAME_RE` before inlining — no free text in a query). `GET/PUT/
   DELETE /api/integrations/posthog` + `/test`, owner-gated, `kind='posthog'`
   in `tenant_integrations` (no migration). Web: a "Product analytics —
   PostHog" panel in `ChannelsView` (host / project id / milestone list /
   key + Test connection). `tests/test_posthog.py` (18) + `test_api.py` +1
   guard; 945 offline green; tsc + build clean. Not live-verified (no
   PostHog project wired in this sandbox — `test_connection` is the live
   check when a key exists).
3. **`(:Contact)` in the graph** — `case_graph_sync` MERGEs
   `(:Contact {email})` + `[:FILED_BY]`; the `(email, tenant_id)`
   constraint; migration for `graph_sync_state.coverage_pct` and the
   optional `account_domain`. Backfill note.
4. **`product_analytics_sync` job + sweep** — the rollup MERGE, identity
   resolution, account rollup pass, `daily-sync.yml` step, coverage
   number on the connector card.
5. **`product_signal` node** — registry handler, `h_draft` fold-in,
   `builder._context` key, palette + Inspector, a seed flow wiring it.

## §7 — Mixpanel (documented, not built)

Same `interpreter/<provider>.py` contract. Differences: service-account
auth (`username:secret` basic, not a bearer key); person profiles via
`GET /api/2.0/engage` (returns `$email`, `$last_seen`, custom props);
events via `GET /api/2.0/events` or JQL (`POST /api/2.0/jql`) for the
milestone counts; `$distinct_id` is the person key, `$email` the join
prop. `usage_trend` needs two `/api/2.0/segmentation` calls (30d vs
prior 30d). Rate limits are stricter (≈ 60 queries/hour on the Query
API) — the sync must be daily, not hourly, and page conservatively.
Everything downstream (`(:Contact)` rollups, identity resolution,
`product_signal`) is provider-agnostic and unchanged.
