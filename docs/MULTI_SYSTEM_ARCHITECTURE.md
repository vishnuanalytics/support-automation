# Multi-system architecture: Postgres/Supabase, Neo4j, Salesforce/Zendesk, Slack, and future product-analytics connectors

Decision record, not a build plan. Captures the "no two systems own the
same fact" architecture worked out in a 2026-09-05 design conversation, so
a later session (or another engineer) doesn't have to re-derive it from
scratch. Update this file the same way `PROJECT_SCOPE.md` says to — in
place, as decisions change, not by appending a new dated blob every time.

## The goal this serves

The product's pitch to a client isn't "we glue your existing tools
together" — it's "the combination produces an output none of Salesforce,
Zendesk, Slack, or a raw analytics tool produce alone": a knowledge base
that keeps itself correct as real cases resolve (with a human still in
the loop), and — once a tenant's own product analytics is connected — a
live link between *what a customer's users actually did* and *why they're
filing support cases*. The architecture has to support that without
turning into N systems each holding a slightly-different copy of the
same fact, because that's what produces wrong answers under a customer's
scrutiny, not just wasted storage.

## Rule 1: every fact has exactly one system of record

No bidirectional sync of the same field between two systems. Pick one
owner per fact; everything else holds a *reference* to it (an id) plus,
where genuinely needed, a bounded/redacted excerpt for its own purpose —
never a second full copy kept "just in case."

| Fact | System of record | Everyone else holds |
|---|---|---|
| The case/ticket itself, the email, CaseComments, Chatter | **Salesforce** (or Zendesk, per FR-51's `case_connector`) | A reference (Case/EmailMessage Id) |
| Flow execution state, what the pipeline decided and why, the human's resolution text | **Supabase `runs`** | — this *is* the flow history; nothing upstream of it should be re-derived elsewhere |
| Relationships + semantic similarity ("is this the same issue as that one", "which cases connect to this account") | **Neo4j** | Bounded/redacted excerpts + a pointer back to the Supabase `run_id` / Salesforce Case Id for the full text |
| The human collaboration thread itself | **Slack** | Nothing needs to sync *into* Slack from these systems except a notification; Slack's own history is its own audit trail |
| Knowledge base content | **Supabase `kb_entries`** | Neo4j can link a `Case` to a `KbEntry` it resolved via/superseded, but doesn't hold KB text itself |
| A tenant's own product usage (once connected) | **The tenant's Mixpanel/GA4/GTM** | Neo4j holds `Event` nodes correlating to `Contact`/`Account`, not a full analytics warehouse copy |

**Two concrete violations of this rule exist today** (found while tracing
the actual code, not theoretical):

1. `case_graph_sync.py` re-polls Salesforce CaseComment/Chatter
   independently to build Neo4j `Message` nodes, instead of sourcing from
   `runs.human_reply` — the same text the worker already captured. Two
   Salesforce calls, two copies, and a real risk of drift between them if
   a comment is edited between the two syncs. **Fix: point `case_graph_sync`
   at `runs.human_reply` for the human-turn text.**
2. `runs.case_payload` keeps the full, uncapped input case forever (no
   retention policy exists anywhere in `db/migrations/`) even though
   Salesforce already durably owns that email. Same for `jobs.payload`,
   which is meant to be transient but isn't cleared post-processing.
   **Fix: retention window on `runs.case_payload`/`runs.trace` (collapse
   to a reference + short excerpt after N days); null `jobs.payload` once
   a job completes.**

Neither is built yet — noted here so whoever picks up "reduce DB/compute
cost" doesn't have to re-trace the code to find the same two things.

## Rule 2: multi-tenancy is one pattern, reused, not reinvented per system

Every tenant-scoped table already follows one shape: `tenant_id` +
Postgres RLS via `tenant_members` (`db/migrations/002_rls_and_constraints.sql`).
Salesforce/Zendesk connections, Slack, and (per FR-51) the case-system
choice all hang off the same `tenant_integrations`/`tenants` tables. A
future product-analytics connector (Mixpanel/GA4/GTM) is **not a new
pattern** — it's another row in the same connector shape:
`GET/PUT/DELETE /api/integrations/<provider>`, Vault-backed credentials,
owner-gated writes, same as Zendesk/Freshchat today. Neo4j itself has no
native multi-tenancy — every node this platform writes already carries a
`tenant_id` property (see `case_graph_sync.py`'s `MERGE` calls) and every
Cypher query must filter on it explicitly; there's no RLS equivalent in
Neo4j, so this is the one place a missing `tenant_id` filter is a
cross-tenant data leak, not just a bug. Worth a dedicated audit before any
new Neo4j-facing query surface (a natural-language-to-Cypher tool
especially) ships.

## The KB auto-update loop — already built, not a new design

The requirement "auto-update the knowledge base from any point in time,
with human approval" is the Knowledge Integrity Loop (KIL), and it's
**complete (KIL a–f, 2026-09-02/03)**, not something to design fresh:

1. **KIL-a** — every case's lifecycle + messages sync into Neo4j
   (`case_graph_sync.py`).
2. **KIL-b** — `interpreter/integrity.py` flags when a human's reply (or a
   draft) contradicts existing KB content.
3. **KIL-c** — a flagged contradiction becomes a `review_tasks` row, a
   human reviews it (Slack review card or the web Knowledge tab).
4. **KIL-d** — on approval, `kb_writeback.draft_change` rewrites the KB
   entry; the old version is marked `superseded`, not deleted (soft-delete
   convention, same as everywhere else in this codebase).
5. **KIL-e/f** — the resulting KB entry surfaces its provenance (`origin`,
   `source_review_task`) in the web UI, and `kil_metrics.py` produces a
   weekly Slack digest of what changed and why.

**One piece is deliberately still deferred: KIL-g, an atomic claim graph**
(gated on the loop having accumulated enough real adjudications first —
see `PROJECT_SCOPE.md` line ~1571). This is directly relevant to
everything discussed in this doc: a claim graph is exactly a Neo4j
structure (`Claim -[:SUPPORTED_BY]-> Case`, `Claim -[:CONTRADICTS]->
Claim`), so when KIL-g does get picked up, it's additive to the Neo4j
schema already described here, not a separate system.

## Product analytics (PostHog first), correlated with cases — Phase 30, in progress

**Now a scoped phase.** Full design in `docs/PRODUCT_ANALYTICS_CONNECTOR.md`
(2026-09-06); this section keeps only the architecture-level summary.

**Shape:** a new connector category, workspace-level, same self-serve
pattern as every other connector. **PostHog** is the first provider
(API-key auth, HogQL); Mixpanel is documented, not built.

**Revision to the original sketch:** *not* `(:Event {name, ts})`
one-node-per-event — that is a warehouse copy and unbounded, which breaks
Rule 1. Neo4j holds a **correlation layer**: `(:Contact {email,
tenant_id})` with recent-activity **rollup properties**, a bounded
`(:Contact)-[:DID {last_ts,count}]->(:Feature {name})` from the tenant's
*configured* milestone list, and `(:Contact)-[:AT_ACCOUNT]->(:Account)`.
The `(:Contact)` node is new (case ingestion only had `ContactId` as a
`Message` field); it is co-owned — identity + `[:FILED_BY]` from
`case_graph_sync`, activity rollups from the analytics sync, neither
writing the other's properties.

**Identity resolution** stays the hard problem and is handled per the
instruction below: every `(:Contact)` carries an `identity_match`
(`email` / `domain` / `none`) and each sync computes a per-tenant
**coverage %** shown on the connector card. The payoff — a
`product_signal` flow node — degrades to `{available: false}` when there
is no match and never blocks a run.

**The one hard problem, flagged before any build starts:** identity
resolution. Mixpanel/GA4 identify users by a pseudonymous
`distinct_id`/`client_id`; joining that to a Salesforce `Contact`/
`Account` only works if the tenant's own product already calls
`identify(email)` (or equivalent) in their analytics instrumentation.
That's outside this platform's control per-tenant — some tenants'
instrumentation will support a clean join, others won't have identified
users at all, and the correlation is only as trustworthy as that link.
Any future build of this should surface identity-match confidence
per-tenant rather than silently assuming every tenant's data joins
cleanly.

## Open questions this doc deliberately leaves open

- Retention window length for `runs.case_payload`/`runs.trace` (needs a
  compliance/support-debugging tradeoff call, not a technical one).
- ~~Whether a natural-language-to-Cypher "ask the graph anything" tool is
  the next Neo4j-facing feature to build~~ — **decided and built
  2026-09-06 as a composable *spec* compiler, not free-form text-to-Cypher**
  (`interpreter/graph_query.py`, `docs/GRAPH_QUERY.md`). The reason: this
  is one shared Community-edition Neo4j with no per-tenant DB and no custom
  read-only role, so the only tenant boundary is a `tenant_id` predicate
  per node — a free-form Cypher surface would make one validator gap a
  cross-tenant breach. Instead the LLM only emits a bounded JSON spec
  (`{entity, metric, group_by, filters, having, order_by, limit}`); a
  deterministic compiler produces read-only Cypher with `tenant_id` bolted
  onto every pattern and a `WITH`-guarded `WHERE`. `Reply`/`Message` text
  is not exposed. Owner-only. Live-verified: all metric shapes `EXPLAIN`
  clean on the real graph, and a bogus tenant returns 0. Residual (in the
  doc): on CE the boundary is app-code-only, and multi-hop path questions
  need a new compiler branch.
- The `DUPLICATE_OF`-never-fires gap was **root-caused and fixed
  2026-09-05, see `PROJECT_SCOPE.md`'s
  "Immediate next step"**: `account_id` was never set because
  `_enrich_from_sf`'s selection filter only checked `case_type`, not
  `account_id`, so a row that already had one (nearly every real row) was
  wrongly skipped for the other. Confirmed live: 20 case pairs score above
  the dup threshold, including a 6-case cluster sharing one real Salesforce
  AccountId, currently invisible to duplicate detection. **Fully fixed and
  live-verified 2026-09-05**: two more root causes surfaced while actually
  making it work (a `case_sf_id`-vs-real-Id mismatch, and `case_memory`
  never having an `account_id` column at all — migration `089`) — see
  `PROJECT_SCOPE.md`. Re-running the sync now produces all 20 real
  `DUPLICATE_OF` edges, zero new Case nodes.
- Whether the product-analytics connector is worth building before or
  after the reporting/exposure layer above — they're independent, but a
  tenant would only value one once the other exists.
