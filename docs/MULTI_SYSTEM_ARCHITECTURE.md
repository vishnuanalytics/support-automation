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

## Future: a tenant's own product analytics (Mixpanel/GA4/GTM), correlated with cases

Not built, not scoped as an active phase — a validated direction from
this conversation, recorded so it isn't lost.

**Shape:** a new connector category (`product_analytics_connector`),
workspace-level, same self-serve pattern as every other connector. In
Neo4j: `(:Event {name, ts, tenant_id})-[:BY_USER]->(:Contact)`, sitting
next to the `(:Case)-[:FOR_ACCOUNT]->(:Account)` structure that already
exists — one new node/edge type, not a redesign.

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
- Whether a natural-language-to-Cypher "ask the graph anything" tool
  (discussed as the leaner alternative to building N bespoke reports) is
  the next Neo4j-facing feature to build, versus fixing the
  `DUPLICATE_OF`-never-fires gap first (0 edges exist today despite 232
  `SIMILAR_TO` edges — see `case_memory_sync.py`; likely `account_id`
  never being set on Salesforce-sourced cases, not investigated further
  yet).
- Whether the product-analytics connector is worth building before or
  after the reporting/exposure layer above — they're independent, but a
  tenant would only value one once the other exists.
