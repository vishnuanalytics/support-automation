# Requirements — AI support automation over Salesforce Cases

The **what and why**. `PROJECT_SCOPE.md` is the build log (what's done, what's
next); this file is the spec every phase is measured against. When a new
requirement surfaces, add it here first, then build.

Last updated 2026-08-30.

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
- **FR-12** On **auto_reply**, the response MUST be sent **via Salesforce**
  (`emailSimple` / outbound `EmailMessage` on the Case), threaded — so the
  whole conversation lives on the Case. _(current build sends via Gmail SMTP
  — MUST change.)_
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

### Platform
- **FR-17** Flows, thresholds, KB sources, and channel config MUST be
  editable in the web UI without code. _(built)_
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
- **NFR-2 Reliability:** the poller + worker MUST run continuously. GitHub
  Actions `schedule` alone is best-effort (observed: not firing for 30+ min)
  and is NOT sufficient on its own — an external pinger (cron-job.org)
  drives the workflow every 5 min. A persistent worker on an always-on host
  is the eventual target (OD-1).
- **NFR-3 Cost:** free tooling by default — Groq free tier for the LLM,
  local `fastembed` embeddings, no paid email API. Target ≤ $5/mo.
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

**Later**
- Freshworks chat channel (FR-20).
- Dedicated support mailbox (OD-2).
- Persistent-worker deployment (OD-1).
- Salesforce native Email-to-Case *intake* (Path B) as an alternative to the
  platform-owned poller.
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
| **OD-4** | Chatter @mention endpoint 404s in this org | Falls back to a plain FeedItem (works, no mention). |
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

Tracked in `PROJECT_SCOPE.md`; summary as of 2026-08-30:

| Requirement | Gap |
|---|---|
| FR-6 | Case reuse is 14-day-any-open-Case; needs thread-match. |
| FR-7 | No inbound `EmailMessage` on the Case (needs C-1). |
| FR-12 | `auto_reply` sends via Gmail SMTP, not Salesforce. |
| FR-13 | Escalation attaches a Chatter note but no email draft on the Case. |
| FR-14 | `handover` sets the outcome but doesn't reassign Case owner/queue. |
| NFR-2 | cron-job.org pinger not set up; scheduled runs unreliable. |
| C-1 | Email-to-Case not enabled in the org. |
