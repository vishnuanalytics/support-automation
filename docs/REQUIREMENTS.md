# Requirements — AI support automation over Salesforce Cases

The **what and why**. `PROJECT_SCOPE.md` is the build log (what's done, what's
next); this file is the spec every phase is measured against. When a new
requirement surfaces, add it here first, then build.

Last updated 2026-08-31 (Phase 21).

---

## 1. Purpose & success criteria

**Goal:** an inbound support request is triaged and answered by AI, working
entirely through a Salesforce Case, and escalated to a human whenever the AI
is not confident. Salesforce is the system of record for the conversation.

**Success is measured by:**

| Metric | Target |
|---|---|
| Cases auto-resolved (auto_reply, no human touch) | _[TBD — set a baseline after 1 week live]_ |
| Escalation precision (escalated Cases that genuinely needed a human) | ≥ 0.95 |
| Auto-send precision (auto-replies that were correct / not harmful) | ≥ 0.95 |
| Time from email arrival → Case created + first action | ≤ 2 min _[target]_ |
| Human review load | _[TBD]_ |
| Running cost | ≤ $5 / month |

---

## 2. Actors & channels

| Actor | Role |
|---|---|
| **Customer** | Sends the support request. |
| **AI agent** (the LangGraph flow) | Reads the Case, triages, drafts, decides auto-send vs escalate. |
| **Support human** | Handles escalations; reviews / edits / sends drafts. |
| **Workspace owner / admin** | Configures flows, thresholds, KB, channels. |

**Channels:** email now (one monitored mailbox). **Freshworks chat** is a
planned future channel — it must slot in as another adapter, not a rewrite
(see FR-19).

---

## 3. Functional requirements

### Ingestion
- **FR-1** Every inbound support email (from the monitored mailbox, past the
  loop-breakers) MUST result in a Salesforce Case being created, or a
  matching existing Case updated, within one poll cycle.
- **FR-2** The system MUST NOT process: auto-responders / vacation replies,
  list / bulk mail, mail from its own address, or its own outbound
  (`X-Support-Bot`). These are marked handled and skipped.
- **FR-3** The poller MUST advance by a persisted cursor (highest IMAP UID /
  Gmail internalDate), NOT by read-state — a message a human opens in the
  mail client must still be picked up. _(built)_
- **FR-4** A redelivered email (same `Message-ID`) MUST NOT create a
  duplicate Case or a duplicate run. _(built — job + run idempotency keys)_

### Case linkage
- **FR-5** Sender resolution: exact Salesforce Contact by email → else a
  Contact is created; a **business** email domain with no Account → the
  Account is created and linked. Free-mail domains get a Contact, no Account.
  _(built)_
- **FR-6** **Case reuse is thread-based:** a new email attaches to an
  existing open Case **only** when it is a genuine reply — its
  `In-Reply-To` / `References` point at a message already on that Case (for
  that contact). Otherwise a **new Case** is created. _(current build reuses
  any open Case for the contact within 14 days — MUST change to thread-match.)_
- **FR-7** The inbound email MUST appear on the Case as an **incoming
  `EmailMessage`** (From, To, Subject, body, MessageDate) — not only in the
  Case Description. _(not built — needs Email-to-Case enabled, see C-1.)_

### Triage
- **FR-8** Each Case MUST be classified for **tier** (basic / premium /
  enterprise), **topic**, and **urgency**. Tier comes from the Account;
  when the CRM value is missing or unmappable it defaults to `basic` for
  this channel (`default_tier`). _(built)_
- **FR-9** Triage output MUST be written back to Case fields (`Priority`,
  `Module__c`, `Region__c`) and appended to the Description. _(built)_

### Response
- **FR-10** The AI MUST draft a reply grounded in the knowledge base, with a
  groundedness check.
- **FR-11** A **confidence gate** MUST decide auto-send vs escalate, with
  per-tier thresholds. Topics **billing, refund, pricing, legal,
  account-access, data-export, cancellation** MUST always escalate,
  regardless of confidence. _(built)_
- **FR-12** On **auto_reply**, the response MUST reach the customer **and**
  the whole conversation MUST live on the Case. _(built — revised
  2026-08-30.)_ The reply is sent **over SMTP from the support mailbox**
  (`emailer.send_reply`) and then **mirrored onto the Case as an outbound
  `EmailMessage`** (`salesforce.log_email_message`, best-effort). The
  original "send via Salesforce `emailSimple`" was abandoned: this
  Developer Edition org has no Org-Wide Email Address and Deliverability =
  "System email only", so `emailSimple` returned `sent=true` while
  delivering nothing.
- **FR-13** On **ask_human**, the drafted reply MUST be attached to the Case
  for a human — a Chatter note **and** a ready-to-send email draft on the
  Case — and the inbound message flagged. Nothing is auto-sent. _(Chatter
  built; email draft on the Case not built.)_
- **FR-14** On **handover** (enterprise tier, or very low confidence), the
  Case MUST be routed to a human (owner / queue change) with the draft
  attached. Nothing is auto-sent. _(outcome built; owner/queue routing not.)_
- **FR-15** A per-channel master switch (`auto_send_enabled`) MUST gate ALL
  customer-facing sends; default **off**. Nothing goes out on any outcome
  other than `auto_reply` with the switch on and a non-empty draft. _(built)_

### Human-in-the-loop
- **FR-16** After an escalation on a real Case, the system MUST capture what
  the human did with the draft (sent as-is / edited / rewrote / no reply)
  and feed accepted drafts to the eval set. _(built for CRM Cases; the
  email-origin path needs the EmailMessage reply source wired in.)_
- **FR-16a** After `ask_human` / `handover`, the system MUST act on the
  human's response, not just record it: an agent's **CaseComment** is
  treated as the answer — the bot polishes it into a customer-facing reply
  and sends it (subject to the channel's auto-send switch); an agent's
  **outbound email** means the agent handled it directly (score only). The
  resolution check polls (`FEEDBACK_POLL_MIN` × `FEEDBACK_MAX_CHECKS`)
  instead of firing once. _(built — Phase 20m; `guided_resume`
  `human_action`, `source='agent_resume'` run rows.)_

### Platform
- **FR-17** Flows, thresholds, KB sources, and channel config MUST be
  editable in the web UI without code. _(built)_
- **FR-17a** Exactly one flow per workspace is the **Salesforce entry
  flow** — the one `POST /api/hooks/salesforce/case` runs. It MUST be
  selectable in the web UI (a toggle), not hard-coded to a team.
  _(built — `flows.sf_entry`, migration `042`; interim for the full
  queue → flow binding, which stays a future phase.)_
- **FR-18** All tenant data MUST be RLS-isolated. Mailbox / CRM secrets MUST
  live in Supabase Vault, never returned to the browser. _(built)_
- **FR-19** Every run MUST be recorded — trace, gate math, retrieval,
  outcome — for a per-Case "why did the bot do this" view. _(built)_

### Future channels
- **FR-20** A new channel (Freshworks chat) MUST be addable as an adapter
  that produces a `case` dict and enqueues `run_flow`; everything from
  `sf_case` onward is channel-agnostic and unchanged.

---

## 4. Non-functional requirements

- **NFR-1 Latency:** email arrival → Case + first action ≤ 2 min (target).
- **NFR-2 Reliability:** the poller + worker MUST run continuously.
  _(addressed 2026-08-30.)_ GitHub Actions `schedule` was too opaque and
  unreliable (observed: not firing for 30+ min; no visibility into what
  happened to a given mail). Replaced by a local **Docker Compose** stack
  (`docker-compose.yml`: `poller` + `worker` + `api`, `restart:
  unless-stopped`) run on the dev box — see `docs/LOCAL_RUNTIME.md`. Runs
  while the machine is on; not a substitute for a real always-on host
  (OD-1) but self-hosted, free, and fully observable via
  `docker compose logs`.
- **NFR-3 Cost:** free tooling by default — Groq free tier for the LLM,
  local `fastembed` embeddings, no paid email API. Target ≤ $5/mo. _(Phase 23:
  when Groq's daily token quota is spent, `llm.complete` fails over to an
  **OpenRouter `:free`** model, then the deterministic stub — a quota-exhausted
  provider no longer stalls the pipeline.)_
- **NFR-7 Resilience (Phase 23):** every long-running process writes a
  `system_health` heartbeat; `scripts/health_check.py` alerts when one goes
  silent or the job failure rate spikes. An errored email channel is retried
  with backoff, not parked forever. Salesforce writes (`add_case_comment` /
  `post_chatter`) are idempotent within a 3 h window. CDC ignores the bot's own
  Case writes. `validate_env()` fails a process fast on bad config.
- **NFR-4 Security:** bearer tokens verified server-side; per-user
  rate-limiting on write endpoints; secrets only in Vault.
- **NFR-5 Safety:** the system MUST NOT send a customer-facing email on any
  outcome other than `auto_reply` + switch-on + non-empty draft. Escalations
  and handovers never send.
- **NFR-6 Observability:** a failed job / run MUST surface its error; the
  channel MUST record `last_poll_at` / `last_error` / `status`.

---

## 5. Constraints & assumptions

- **C-1** Salesforce **Developer Edition**; **Email-to-Case MUST be enabled**
  (Setup → Email-to-Case → Enable) — prerequisite for FR-7 and FR-12
  (EmailMessage records + the Email action on Cases). _Not yet enabled._
- **C-2** Monitored mailbox: `gundamvishnu7@gmail.com` via IMAP/SMTP +
  app-password (interim; a dedicated `support@` address is a future item —
  OD-2). Secret in Vault.
- **C-3** LLM: Groq (`openai/gpt-oss-120b` draft, `openai/gpt-oss-20b`
  classify); Anthropic opt-in. **Daily free-tier quota applies** — an
  exhausted quota drops the flow to a deterministic stub (poor drafts).
- **C-4** Knowledge base: Zapier public docs (demo corpus) + per-tenant
  internal KB + linked Google Docs.
- **C-5** Scheduler: `.github/workflows/email-automation.yml` + a
  cron-job.org pinger. The pinger needs a GitHub fine-grained PAT with
  `actions: write` on this repo.
- **C-6** Multi-tenant: one email channel per (tenant, `kind`).

---

## 6. Scope boundaries

**In scope now**
- Email channel → Salesforce Case → triage → draft → {auto_reply via SF |
  ask_human | handover}, with the hard guard.
- EmailMessage on the Case (in + out); thread-based Case reuse.
- Web config UI; KB ingestion; run observability; human feedback loop.

**Decided — one intake path (SF-1, 2026-09-01)**
- **Salesforce native Email-to-Case (Path B) is the intake.** The mail routes
  straight to Salesforce, which opens the Case; CDC fires `case_created`. The
  platform-owned IMAP poller (Path A) is off. Enforced by `SF_INTAKE_MODE`
  (default `salesforce_e2c`): `mailbox.list_pollable_channels` / `email_watch.
  tick` yield nothing unless the mode is `poller` or `both`, so a channel row
  left `active` in the DB can't silently double-create Cases.

**Later**
- Freshworks chat channel (FR-20).
- Dedicated support mailbox (OD-2).
- Persistent-worker deployment (OD-1).
- Other channels (phone, web form, Slack).

**Explicitly out**
- The AI taking billing / refund / account-change actions — these always
  escalate (FR-11).
- Salesforce writes beyond: create Case / Contact / Account, update Case
  fields, create EmailMessage, post Chatter, change Case owner.

---

## 7. Open decisions & risks

| # | Item | Status |
|---|---|---|
| **OD-1** | Persistent-worker host (Oracle/GCP free VM vs. keep pinger) | Deferred; pinger is the interim. Risk: PAT dependency, Actions minutes. |
| **OD-2** | Dedicated `support@` mailbox vs. personal Gmail | Deferred; loop-breakers handle the noise but it's not production-clean. |
| **OD-3** | Groq daily quota exhaustion → stub drafts | May need a paid key at volume. |
| **OD-4** | ~~Chatter @mention endpoint 404s in this org~~ | **Resolved 2026-09-01 — was a wrong URL, not an org limit.** `post_chatter` used `connect/records/feed-elements` (→ 404 → no-mention FeedItem fallback); the correct endpoint is `chatter/feed-elements`. @mention works on the DE org. `notify` / `clarify` now @mention the resolved rep (a queue member via `routing.queue_member`, else `mention_id`). |
| **OD-5** | Email-to-Case not enabled → FR-7 / FR-12 blocked | Admin action required (C-1). |

---

## 8. Acceptance scenarios

- **AS-1** Unknown sender, KB-answerable question → new Case; Contact +
  Account created; inbound EmailMessage on the Case; fields set; gate PASS →
  reply sent **via Salesforce**, outbound EmailMessage on the Case.
- **AS-2** Known sender replies in-thread (`In-Reply-To` matches) → the
  existing open Case is updated; a new EmailMessage is appended; no new Case.
- **AS-3** Known sender, brand-new unrelated subject → a **new** Case.
- **AS-4** Billing / refund topic → escalate regardless of confidence; draft
  attached to the Case; Chatter to a human; nothing sent.
- **AS-5** Enterprise-tier sender → handover; Case reassigned to the human
  queue; nothing sent.
- **AS-6** Auto-responder / mail from our own address → ignored; cursor
  advances; no Case, no run.
- **AS-7** Same email redelivered → no duplicate Case, no duplicate run.

---

## 9. Current gaps vs. these requirements

Tracked in `PROJECT_SCOPE.md`; summary as of 2026-08-30.

**Closed in Phase 20f (2026-08-30):**

| Req | How |
|---|---|
| FR-6 | `sf_case` `reuse: "thread"` — `salesforce.find_case_by_thread()` matches the email's `In-Reply-To` / `References` against `EmailMessage.MessageIdentifier` on open Cases; a genuinely new subject → a new Case. Migration `037`. |
| FR-7 | `sf_case` calls `salesforce.log_email_message(incoming=True)` — the customer's mail becomes an `EmailMessage` on the Case, idempotent on `MessageIdentifier`. |
| FR-12 | ✅ revised (2026-08-30) — `api/worker._email_post_run._deliver` sends the reply over **SMTP** (`emailer.send_reply`) and then mirrors it onto the Case as an outbound `EmailMessage` (`salesforce.log_email_message`, `Incoming=false`, best-effort). `salesforce.send_case_reply()` / `emailSimple` dropped from this path — the DE org silently discarded those (no OWEA; Deliverability = System-email-only). |
| FR-13 | `ask_human` leaves the drafted reply on the Case as an internal `CaseComment` (`salesforce.add_case_comment`) beside the Chatter note — Salesforce rejects an API-created outbound draft `EmailMessage`. The agent copies it into the Email quick action. |
| FR-14 | `handover` calls `salesforce.assign_case(queue=…)` when the node config carries a `queue` / `owner_user_id` — resolves a Queue by DeveloperName or Name and sets `Case.OwnerId`. No target → outcome only, unchanged. |

**Added + built in Phase 20i (2026-08-30):**

| Req | How |
|---|---|
| **FR-21** Team routing | `team_route` node → `state.routed_team` ∈ {support, csm, sales, offboarding} from keyword rules (renewal/expansion → csm, pricing/pre-sales → sales, cancellation/data-export → offboarding, else support). The design doc's "One team, one flow" as a routing step. |
| **FR-22** Team-aware escalation | `ask_human` / `handover` resolve `Case.OwnerId` to the routed team's queue (`queue_by_team`); a `support` billing escalation → `Billing_Escalations`; enterprise → `Enterprise_Support`. |
| **FR-23** Team roster in SF | `Contact.Team__c` + `Contact.TeamRole__c`; 2 real Users (Support/CSM managers) + 13 Contacts, 1 Manager + 2 Members per team. `scripts/sf_seed_teams.py`. |
| **FR-24** Salesforce → automation push (callout) — **RETIRED 2026-09-01** | Was: `POST /api/hooks/salesforce/case` + an Apex trigger/`@future` callout (`scripts/sf_deploy_case_hook.py`). The API endpoint still exists (curl-verified) but the SF-side Apex is **removed** (`sf_deploy_case_hook.py --remove`) — with the API not on a public URL it threw `CalloutException: Unable to tunnel through proxy` and emailed the org admin per Case. Superseded entirely by FR-25 (CDC, outbound-only, no public URL). |
| **FR-25** Salesforce → automation push (CDC, Phase 20l) | `ingestion.sf_cdc_watch` — a long-lived gRPC client on the **Pub/Sub API** streaming `Case` + `EmailMessage` Change Data Capture. Enqueues a `run_flow` job for a new Case, a new **inbound email on an existing Case**, and a **queue (OwnerId) change**. Durable: last `replay_id` per topic persisted in `sf_cdc_state` (migration `043`) → restart resumes (72h retention). Shares `interpreter.sf_ingest.enqueue_case_run` with FR-24. **Live-verified 2026-08-31** — real inbound-email event → `inbound_email` job, other events ignored, replay cursor persisted. docker-compose `cdc` service. |

**Added + built in Phase 20n (2026-08-31):**

| Req | How |
|---|---|
| **FR-26** Case.Type on every pass | `classify` now also emits `case_type` (LLM `type`, constrained to the 7 `Case.Type` picklist values, with a deterministic keyword fallback — `salesforce.normalize_case_type` / `map_case_type`). `sf_writeback`'s default field-map gained `case_type → Type`, so the Case's `Type` is set at first triage and refreshed on every customer-reply re-run while it sits in the queue — the field a queue owner scans a list view by is no longer blank. `Module__c` is still written (finer product-area tag). |
| **FR-27** Ask an internal rep without a hand-off | New **`notify`** node (`interpreter/registry.h_notify`): posts a Chatter note (an @mention when the target is a real User/Group id, else names them) + the draft as a private `CaseComment`, and **never changes `Case.OwnerId`** — the Case stays in its open queue. Target resolves from `Case.Type` → `Module__c` → `fallback_target`. `confidence_gate` gained `escalate_types` (`["Billing", "Account / Login"]`) beside `escalate_topics` / `escalate_modules`. The email flow routes a forced escalation here; the router flow routes `support`-team forced escalations here (csm / sales still reassign via `ask_human`). Re-engagement after the rep answers is the existing Phase 20m resume poller. |
| **FR-27a** Central `notify` routing (Phase 20o) | The `Case.Type` → rep mapping lives in a per-tenant table **`notify_targets`** (migration `045`, RLS), not per-flow node config — a flow editor never pastes ids. `interpreter/routing.resolve_notify_target` reads it; a row resolves `static` (fixed id), `sf_queue` (Queue by name → id), or `sf_team_role` (the current member of `Team_<team>` — a **live** SOQL lookup, so it follows Salesforce roster changes). `h_notify` still lets a node-level `target_by_type` override win. Seeded for tenant `00000000…`; live-verified against the org. |
| **FR-27b** Editor pickers from live SF metadata | `GET /api/salesforce/meta` (`salesforce.org_metadata`, 5-min cache) serves the org's queues + `Case.Type` / `Module__c` picklists to the flow editor. The Inspector's `clarify` *handover queue* and `notify` *Case.Type overrides* use them (`QueuePicker` / `useSfMeta`), degrading to free text when SF is unreachable. No standalone `notify_targets` admin screen yet — rows managed via SQL. |
| **FR-29** One comprehensive `sf_entry` flow (Phase 20p) | The email flow (`e5e5e5e5…`, v4) covers every team + scenario: `identify → sf_case → retrieve → classify → team_route → sf_writeback → draft → confidence_gate`, gate 5-way → `handover` (enterprise / offboarding) · `ask_human` (csm / sales — they own the relationship) · `auto_reply` (support, confident) · `notify` (support, forced escalation — pings the `Case.Type` rep, Case stays in `Team_Email`) · `clarify` (support, unclear — ask the customer, 2 rounds → `Team_Support`). Migration `046`. Routing matrix verified by `scripts/run_scenarios.py` / `tests/test_flow_scenarios.py` (10 scenarios × 3 tiers). Live e2e pending. |
| **FR-28** Round-capped clarify → support queue | `clarify` gained `handover_queue`: once `max_rounds` (2) of asking the customer is exhausted it reassigns the Case to that queue (`Team_Support`) so a human owns it. The email + router flows send a non-forced low-confidence Case to `clarify` instead of a blind `ask_human`. |

Flows updated: `flow_email_l0l1.json` (`ask_human` removed, `notify` + `clarify` added, gate split 4-way) and `flow_case_router.json` (`notify` + `clarify` added, gate split 5-way). Migration `044` **applied 2026-08-31** to the live email flow → published **v3** (the router flow isn't seeded to this DB — its change stays in `scripts/seed_router_flow.py` + the portable JSON). Web Inspector gains a `notify` form + `clarify` handover-queue field.

**Added + built in Phase 21 (2026-08-31):**

| Req | How |
|---|---|
| **FR-30** Answer from resolution history | New `case_memory` store (migration `048`): one row per resolved Case + a 384-d embedding, `match_case_memory` pgvector kNN, RLS. `ingestion/case_memory_sync.py` populates it from accepted `runs` resolutions (+ `--from-salesforce` for closed Cases). New `case_lookup` node (migration `049`, email flow **v7**) recalls the closest resolutions and, when they closely match, `draft` grounds the reply in them (a CONFIRMED DUPLICATE leads). `interpreter/case_memory.py` does the kNN + taxonomy/recency/duplicate boosts; `sync_graph()` MERGEs `(:Case)-[:RESOLVED_BY]->(:Reply)` / `-[:ABOUT]->` / `-[:SIMILAR_TO]->` into Neo4j. All best-effort → no memory / no embedder / Neo4j down = a no-op. |
| **FR-32** One timeline per Case (observability) | `GET /api/trace/{Case number \| Case id \| run_id \| job_id}` merges `jobs` + `runs` + every trace node + errors into a time-ordered story. Flags `degraded_llm` (stub mode — Groq quota), `stale_jobs`, `failed_jobs` + error text, `labels_written`/`labels_skipped`, `final_queue`, ms/tokens. `?format=md` = a plain-text report. Web **Trace** tab: search → timeline, each node expandable to its `data`. Answers "why did the bot do this / why did the Case fail to create / why these labels / why did it go stale" without SQL-spelunking `runs` + `jobs` + `tenant_integrations` + `docker logs`. `runs.trace` was already the "why" (FR-19) — this surfaces it. |
| **FR-31** Pattern vs proof | `classify` emits `answer_mode` (informational \| diagnostic \| action \| status). `case_lookup` is **skipped for `action`** (a person does it) and for **`diagnostic`** the near-matches become `investigation_hints` only — `prior_resolutions` is forced empty so `draft` never states a customer-specific fact from memory; the draft prompt tells it to say what it will check and defer to a specialist rather than guess. A resolution whose text cites the customer's own IDs / timestamps / logs is stored `generalizable=false` → hint only, never reply copy. |

**Still open:**

| Req | Gap |
|---|---|
| C-1 | ✅ Email-to-Case enabled (2026-08-30) — inbound `EmailMessage` on the Case now works; the **Email** action + **Emails** related list are live. Outbound draft `EmailMessage` via API stays blocked by design → FR-13 uses a `CaseComment`. |
| NFR-2 | cron-job.org pinger not set up; scheduled runs unreliable. |
| FR-24 | ✅ retired 2026-09-01 — the Apex trigger/class/Named Credential were deleted (`sf_deploy_case_hook.py --remove`); they were emailing the admin a `CalloutException` per Case because the API isn't public. CDC (FR-25) is the sole push path. |
| FR-25 | ✅ done (2026-08-31) — migration `043` applied; subscriber live-verified against the org. Remaining: run it as a persistent process (docker-compose `cdc`) alongside the `worker`. |

**Added Phase KIL — Knowledge Integrity Loop (2026-09-02).** Catch new
information that contradicts the KB or case history (in inbound tickets, bot
drafts, and human replies), route it to a manager, and fold confirmed
corrections back into the KB under approval. Plan artifact:
`https://claude.ai/code/artifact/0ee3262d-4eaf-4669-bf3e-a16d8e9d3dff`.
Decisions locked: human-reply review is **post-send + 5% sampling** (not a
gate); after handover the bot **flags to the manager only** (never the
customer); a confirmed correction produces an **LLM-drafted KB diff the
manager one-click approves**; contradiction detection is an **NLI judge over
retrieved passages** (the atomic claim graph is deferred to KIL-g).

| Req | How |
|---|---|
| **FR-33** Case-lifecycle graph (**KIL-a — built 2026-09-02**) | `ingestion/case_graph_sync.py` — walks Salesforce Cases of **any** status (not just closed, unlike `case_memory_sync`) and MERGEs one `(:Case)` + one `(:Message)` per turn into Neo4j: the Case description, inbound/outbound `EmailMessage`s, `CaseComment`s (a `[bot draft…]` comment → `role='draft'`/`author_kind='bot'`, else `agent_note`), and Chatter `FeedItem`s. Message text redacted at write time (`case_memory.redact`, 6k limit). `interpreter/case_memory.sync_case_lifecycle()` holds the Cypher; `Message.id` + `Account.sf_id` uniqueness constraints added to `neo4j_sync.ensure_constraints`. Resumable via **`graph_sync_state`** (migration `064`, RLS like `system_health`) — a `case_graph:<tenant>` row holds the `LastModifiedDate` high-water mark + counters. Idempotent (MERGE); Neo4j/SF down → logs + exits 0. `--backfill` / `--since` / `--case` / `--dry-run`. **Live backfill:** 92 dummy Cases → 174 Messages (108 inbound / 42 draft / 24 agent_reply), `tenant_id` on every node, 14 Cases with a full `draft → agent_reply` pair. 8 offline tests. |
| **FR-34** Contradiction / integrity engine (**KIL-b — built 2026-09-02**) | `interpreter/integrity.py` — `check(statement, contexts, *, kind) → {relation: entails\|neutral\|contradicts, flagged, novel, verdicts:[{claim,relation,evidence,confidence}], backend}`. Groq NLI judge when a key is set, else a conservative deterministic heuristic (negation mismatch over shared terms → `contradicts`; unsure → `neutral`, since a false flag costs a manager's attention). `_summarize` is worst-of the strong (≥0.55 conf) verdicts. `contexts_from_state` assembles prior resolutions + internal KB + retrieved docs. Wired into `h_draft` — checks the **draft** and the **inbound** customer text against that context, → `state.integrity = {draft:{…}, inbound:{…}}` (new `CaseState` key + `builder._context` key). `h_confidence_gate` gains `escalate_on_integrity_conflict` (default **true**): a `draft` that `contradicts` KB/history is a forced escalation, same mechanism as `escalate_topics`. Eval: **`eval/integrity/`** (24 hand-labelled cases + `run_integrity_eval.py` → accuracy / flag precision·recall / confusion matrix). Real-Groq run: **accuracy 0.958, flag precision 1.000, flag recall 1.000** (1 miss, entails→neutral, not a flag) — clears the ≥ 0.80 ship bar for KIL-c. 12 offline tests (344 total). |
| **FR-35** Human-reply review + sampling (**KIL-c — built 2026-09-02**) | Migration `065`: **`review_tasks`** (RLS read like `action_requests`; unique `(run_id, kind)`). `interpreter/review.py` — `judge_human_reply(sb, run_row, reply_text, sample_rate?)` runs the KIL-b judge on a **sent** human reply against the same KB + case-history the run retrieved (`assemble_contexts` = `runs.retrieval` + `prior_resolutions` from the trace); a `contradicts` / `novel` verdict opens a `human_reply_review` task, else `REVIEW_SAMPLE_RATE` (5%) opens a `sample` task, and posts a Block-Kit card to the routed-team `#cx-*` channel @-mentioning the manager usergroup — **Correct → update KB** / **Wrong → coach** / **Not a conflict**, button `value` = task id. Hooked into `slack_socket._deliver` (the Slack-reasoning send path — widened its `runs` select to carry `retrieval`/`trace`) **and** `api/worker._check_resolution`'s outbound-email fallback; both best-effort. `slack_socket.dispatch_action` handles the `review_*` clicks → `review.resolve(status=correct\|wrong\|dismissed)` before the reasoning-session lookup. `correct` is where KIL-d picks up. 8 offline tests (352 total). |
| **FR-36** KB write-back loop (**KIL-d — built 2026-09-02**) | Migration `066` adds `kb_entries.{source_review_task, supersedes_entry_id, approved_by, provisional_until}` + the `provisional` / `superseded` statuses. `interpreter/kb_writeback.py`: `draft_change(task_row)` — an LLM (deterministic fallback without a key) proposes `{op: create\|supersede, title, body_md, rationale}`, choosing `supersede` when a review-task context ref is `kb://<sid>/<eid>` (an internal entry). `slack_socket.dispatch_action`'s **Correct** button → `draft_change` → `raise_kb_change` inserts `action_requests(kind='kb_change')` + posts an **Approve & publish / Reject** card (buttons carry the AR id), stamping `review_tasks.kb_change_id`. `kb_approve` → AR `approved` + enqueues `apply_kb_change`; the worker handler runs `kb_writeback.apply_kb_change`: supersede path marks the old entry `superseded` and `kb_common.delete_entry` pulls its chunks from retrieval; the new entry is written **`provisional`** (`origin='review_writeback'`, `provisional_until = now + KB_PROVISIONAL_DAYS` (7)), `embed_kb_entry` enqueued (accepts `provisional`), and `(:KBArticle)-[:SUPERSEDES]->` MERGE'd (best-effort). `promote_provisional(sb)` flips aged `provisional` → `active` (KIL-f adds the "no fresh contradiction" gate). Web Review tab + `/api/review-tasks` still pending. 8 offline tests (360 total). |
| **FR-37** Post-handover watcher (**KIL-e — built 2026-09-02**) | Migration `067`: **`handoff_watch_state`** (`case_sf_id` pk, `last_seen_ts`, `flags_sent`, `seen_sigs[]` — RLS like `system_health`). `interpreter/handoff_watch.py` `watch_case(sb, case)` — for one escalated Case: pull CaseComments + EmailMessages newer than `last_seen_ts`, run `integrity.check` on each new customer/agent turn against the run's stored KB + case-history context (`review.assemble_contexts`); a `contradicts`/`novel` verdict → one flag to the manager thread (the open `reasoning_sessions` thread if any, else the routed-team `#cx-*` channel). Then, LLM-gated, `_missed_pointers` asks which *critical* `pointer_bank` questions are still unanswered in the thread → a flag each. Rate-limited (`HANDOFF_MAX_FLAGS`, default 3) and deduped by signature (`sha1` of the salient claim / pointer). **Never posts to the customer.** New sweep `sweeps.handoff_watch(sb)` (SF: `Status IN ('Escalated','In Progress') AND Routed_Team__c != null`) wired into the `api.worker` loop every 5 min, self-re-enqueuing like the Phase 27d sweeps; honours `SWEEP_DRY_RUN`. 6 offline tests (366 total). |
| **FR-38** Integrity metrics + Review UI (**KIL-f — built 2026-09-02**) | `interpreter/kil_metrics.py` `compute(sb, tenant_id, days)` — from `review_tasks` + `kb_entries`: `flag_precision` (correct / (correct+wrong)), `false_flag_rate` (dismissed / resolved), `agent_correction_rate`, `median_time_to_review_h`, the KB-writeback funnel (entries / provisional / active / superseded / `promotion_rate`), `knowledge_freshness_days` (median age of active entries), and a per-ISO-week contradiction count. API: `GET /api/review-tasks?status=`, `POST /api/review-tasks/{id}/resolve` (RLS-checked read then service-role write via `review.resolve`; `correct` → `kb_writeback.draft_change` + `raise_kb_change`), `GET /api/kil/metrics?days=`. Web: a **Review** tab (`web/src/review/ReviewView.tsx`) — the metric tiles + the open-task list with the statement, the salient claim, the judged passages, and Correct / Wrong / Not-a-conflict. `kb_writeback.promote_provisional` gained the **poisoning guard**: an entry is held (not promoted) while an open `human_reply_review` task's contexts still reference it. New `kb_promote` sweep (`api.worker`, every 6 h) runs the promotion. `sop_conflicts` as a scheduled KB-vs-KB sweep is a noted follow-on (it needs the script refactored to return findings). 3 offline tests + web build green (369 total). |
| **FR-39** Atomic claim graph (**KIL-g — deferred**) | Extract `(:Claim)` nodes from KB + resolutions, `ASSERTS` edges, check every new claim against the stored set, track contradiction chains over time. Only after KIL-a–f prove the loop in real use. |

**Phase KIL is BUILT (a–f, 2026-09-02); g deferred. No open phase.** The loop
runs end to end in code: a contradiction in an inbound message / bot draft /
human reply is caught by the NLI judge → the gate escalates or a `review_tasks`
row + Slack card is raised → a manager confirms → an LLM-drafted KB diff is
approved → the worker writes a `provisional` entry (superseding the wrong one)
→ it auto-promotes after 7 days unless a fresh contradiction still disputes it.
Remaining: live end-to-end verification against the org + Slack.

**Phase KIL live-verified 2026-09-02** (PR #29) — the smoke drove b→c→d→f end
to end against real Supabase / Groq / Slack; 4 schema-shape bugs found + fixed.

---

**Added Phase P1–P8 — post-KIL hardening → platform (2026-09-02).** From an
end-of-KIL review; sequenced in `docs/PROJECT_SCOPE.md`'s Roadmap table.
Correctness first, then a CI safety net, then the generic-`RunContext` unlock,
then triggers / connectors / self-serve onboarding.

| Req | How |
|---|---|
| **FR-40** Scheduled case-history sync (**P1a — built 2026-09-02**) | `case_graph_sync` (KIL-a) and `case_memory_sync` (Phase 21) run only on a manual `--backfill`. Both MUST run on a schedule — a `worker` sweep (self-re-enqueue, like the Phase 27d sweeps) **and** in `.github/workflows/daily-sync.yml` — so new/updated Cases enter the Neo4j lifecycle graph + the pgvector resolution memory without a hand-run. `SWEEP_DRY_RUN` / a disable flag honoured. |
| **FR-41** Provisional-aware retrieval (**P1b — built 2026-09-02**) | A `review_writeback` KB entry lands `provisional` (KIL-d) but its chunks go into `doc_chunks` via the same path as `active` entries — so an **unverified** correction is retrieved and cited with equal weight and can override a `live` doc. Fix: stamp `doc_chunks` with the source entry's status; `h_draft` MUST down-weight / visibly flag `provisional` context and never let it override an `active`/`confirmed` passage; `superseded` chunks MUST be excluded from retrieval (today only `delete_entry` on the old URL does this — verify it actually fires). |
| **FR-42** Tenant-scoped infra + multi-tenant sweeps (**P1c — built 2026-09-02**) | `graph_sync_state` / `handoff_watch_state` are `select using (true)` — any tenant's user can read another tenant's Case ids + cursors. Scope reads to `tenant_members` like every other table. `handoff_watch.watch_case` hardcodes `DEFAULT_TENANT_ID`; the sweep MUST iterate the tenants that have escalated Cases. |
| **FR-43** Migration verification + integration CI (**P2**) | `scripts/verify_migrations.py` — diff the DDL in `db/migrations/*.sql` against the live `information_schema` and fail on drift (CLAUDE.md keeps `supabase_migrations` deliberately out of sync). A CI job MUST spin an ephemeral Postgres, apply every `.sql` in order, and run `pytest -m integration` against it (Salesforce-dependent tests mocked or skipped). All 4 KIL smoke bugs were invisible to the 369 offline tests. |
| **FR-44** Unified approvals (**P4**) | Approvals are split across 3 code paths / 2 transports (`/api/integrations/slack/interactions` signed HTTP, `slack_socket.dispatch_action` Socket Mode, KIL-d `kb_approve`) and there is **no web UI for `action_requests`** — a manager not in Slack can't approve a KB change or a GitHub issue. Consolidate into one `interpreter/approvals.py`; add `GET /api/approvals` (merging `review_tasks` + `action_requests`) + a web **Approvals** tab with a REST equivalent of every Slack button. |
| **FR-45** Generic run context (**P5**) | The run state is `CaseState` — every node handler, the trace, persistence, and `builder._context` assume a Salesforce support Case. A generic **`RunContext`** MUST be introduced alongside it, with `sf_case` as one adapter that produces it, threaded through `builder` / `registry` / `runs` / `trace`. This is the prerequisite for FR-46…FR-48. |
| **FR-46** Triggers (**P6**) | A flow MUST be startable by a hosted **webhook** (`POST /t/{flow_token}` → enqueue a run with the JSON body as the `RunContext`) and by a **schedule**, decoupled from Case/CDC/email plumbing. |
| **FR-47** Declarative connectors + connections manager (**P6**) | A connector MUST be data, not a hardcoded Python node handler — a minimal spec (`{auth: oauth2\|apikey, base_url, actions:[…]}`), plus a per-tenant **connections** UI (today only Slack/Google have a Connect button). |
| **FR-48** Self-serve onboarding (**P7**) | A new user MUST be able to reach first value in ~1 hour with their own data: a **template gallery**, a new-workspace **setup wizard** (pick template → connect trigger → add KB → test → publish), **file upload** (pdf/docx) + **"crawl this URL"** KB ingestion (today: in-app markdown + Google Docs only). |
| **FR-49** KIL observability (**P8**) | A weekly per-tenant **learning report** (entries corrected, flag-precision trend, knowledge-freshness delta, top recurring contradictions); the web KB tab MUST surface `provisional` / `superseded` entries with a "held: disputed" badge; `health_check.py` MUST cover KIL (flag precision < target, N open review tasks, `slackbot` quiet). Eval depth: human-reply review precision (KIL-c), whether a `draft_change` diff actually resolves the contradiction (KIL-d), end-to-end answer-accuracy lift. |
