# Using the dashboard

A practical, task-oriented guide to the support-automation dashboard — for
someone using the product, not developing it. (For how the editor's
candidate-graph/import mechanics work under the hood, see
`FLOW_AUTHORING.md`; for the platform's architecture, see
`PROJECT_SCOPE.md`.) A condensed version of the flow-editor sections below
is also available in-app: open a flow and click **❓ Help** in the toolbar.

## What this product does

You connect a case system (Salesforce, HubSpot, or another connector) and a
knowledge source (docs, a help center, Google Drive…). You then build a
**flow** — a small decision tree that reads each incoming case, decides
what to do, and either answers it automatically, asks a person, or hands it
off. Nothing runs against real customers until you explicitly **publish**.

## Your first 10 minutes

A brand-new workspace lands on a **Get set up** wizard with five steps,
each optional in the sense that you can skip and come back, but the first
flow can't do anything useful until knowledge and a case system exist:

1. **Connect Salesforce** (or another connector, from Connections) — the
   source of truth; cases become real records here, and flows read/write
   Case fields, Queues and Users from it.
2. **Connect Slack** (optional) — lets a flow ping a human for approval or
   post to a channel instead of only leaving a note on the case.
3. **Choose an AI model** — every step that calls an LLM already runs on a
   free tier by default (no key needed to try the product); add your own
   Anthropic/OpenRouter key later if you want a specific model everywhere.
4. **Connect a knowledge source (required)** — the fastest path is
   crawling a public docs/help-center URL, no login needed. A flow with no
   knowledge source has nothing to ground an answer in.
5. **Create your first flow** — once a knowledge source exists, pick a
   template (a working flow you edit, not a blank canvas) or start from
   scratch.

You can reopen this wizard any time from **Setup** in the sidebar.

## The flow editor

This is where you'll spend most of your time. Open a flow from **Editor**
in the sidebar.

### Three ways to build

- **💬 Chat** (the default view when you open a flow) — describe what you
  want in plain English ("add a step that checks the customer's tier
  before replying") and the AI edits your current draft. Every response
  ends with a link to review the change on the Graph.
- **🗺️ Graph** — the visual canvas. **+ Add node** drops a new step; drag
  from a node's edge handle to another node to connect them. A viewer
  (read-only) role always lands here, since there's nothing to generate
  without edit rights.
- **Faster starts**, from the flow list before you open a specific flow:
  - **🧭 Set up a flow** — answer a few questions (what should it do, how
    cautious should it be), no blank canvas or prompt-writing required.
  - **📋 From template** — a pre-built flow (auto-reply with escalation,
    triage-and-route, draft-then-approve-in-Slack, webhook Q&A…) you edit
    from there.
  - **✨ From prompt** — one or two sentences, the AI builds the whole
    flow.
  - **⬇ From Mermaid** — paste a `flowchart TD` diagram; deterministic,
    no AI involved. See `FLOW_AUTHORING.md` for the exact syntax it
    understands.

None of these persist anything until you **Save draft** — they land as an
editable draft on the canvas first.

### Reading the canvas

- A node's border color is a signal: **blue** = trigger (where the flow
  starts), **amber** = terminal (where a path ends, nothing after it),
  **red** = invalid / won't build. The legend strip under the canvas is
  always visible as a reminder.
- Click a node to open its config in the side panel. Give it a **label**
  you'll recognize later — its type (e.g. `confidence_gate`) always stays
  visible above the label, so renaming it doesn't lose that information.
- Click an edge (the line between two nodes) to decide when that path is
  taken.

### Conditions, without writing code

Click an edge, then:

- Leave **conditional** unchecked and that path is always taken (use this
  for the "everything else" branch out of a node with several outgoing
  edges).
- Check it, then pick a **field** (Tier, Case channel, routed team, …), an
  **operator**, and a **value** from real dropdowns — add more rows to
  require several things at once (they combine with AND).
- **Advanced** switches to typing a raw expression, for anything the row
  picker can't express (`or`, `not`, nested logic). It's genuine Python
  syntax evaluated by the interpreter — use `and`/`or`, never `&&`/`||`.
- Give the edge its own **name** (e.g. "VIP customers") in the same panel
  — the canvas shows that name instead of the raw condition, so you don't
  have to read an expression to understand your own flow later. Hover the
  pill on the canvas to see the real condition underneath.
- The toolbar's **Conditions** button lists every branching edge in the
  flow in one place, grouped by the node it branches from, instead of
  clicking through each one individually.

### Test before you publish

- **Test run** runs one real (or hand-entered) case through your current
  *draft* and shows exactly what happened at each step — no need to guess
  whether a change did what you intended.
- **Validate** checks the flow's structure only (no orphan nodes, no
  cycles, exactly one entry point) without actually running anything.

### Saving, publishing, undoing

- **Save draft** stores your edits as the working draft. Nothing customers
  see changes yet, and a real case system entry point keeps running
  whatever was last published.
- **Publish** makes the current draft the version that actually runs for
  real cases from here on.
- Made a mistake after publishing? **More ▾** has a rollback to any
  earlier published version — it re-points the "live" version, it doesn't
  delete history.
- **More ▾** also has Re-layout (auto-arrange the canvas), Import Mermaid,
  Save as template (reuse this flow's shape for future flows), and Delete
  flow.

## Node & edge reference — what problem each one solves

Every node type, grouped by what it's for, with the concrete problem it
solves and an example setup. Values shown match the real dropdowns/fields
in each node's Inspector panel — this isn't paraphrased, it's what you'll
actually see when you click the node.

### Getting a case in the door

- **trigger** — *Problem:* a non-case flow (a webhook, a scheduled job)
  has no "Case" object to start from, and its incoming payload's field
  names may not match what the rest of the flow expects.
  *Example:* a webhook sends `{"question": "...", "user_email": "..."}` —
  `map: {"question": "body", "user_email": "email"}` renames them to
  `context.body` / `context.email` for every later node to read
  consistently, and `required: ["body"]` flags a malformed payload
  (`context._missing`, branchable on an edge) instead of silently drafting
  from nothing.
- **identify** — *Problem:* you don't yet know who's actually contacting
  you, so nothing downstream can be scoped to the right account or tier.
  *Example:* email field `contact.email`, "match email domain → account"
  on — a message from `priya@northwind.example` resolves to the Northwind
  account even if Priya herself isn't a saved Contact yet.
- **sf_case** — *Problem:* an inbound message isn't a case/ticket in your
  system of record yet, and a reply on an existing thread shouldn't spawn
  a duplicate.
  *Example:* origin "Email", reuse "an open Case for a thread reply" — a
  second email in the same conversation lands on the same Case instead of
  opening a new one.

### Gathering context

- **sf_context** — *Problem:* the classifier and gate need more than the
  raw message to judge a case well (is this a top account? is there open
  history?), but pulling that by hand on every node would be slow and
  repetitive.
  *Example:* check "Account + parent hierarchy" and "Related Cases" only
  (skip Leads/team) for a lean flow that just needs tier + case history.
- **product_signal** — *Problem:* you want the bot to factor in what the
  customer actually did in your product (e.g. "usage dropped 40% this
  month"), not just what they typed.
  *Example:* default config — no PostHog connection needed to add the
  node; it's a safe no-op tenant-by-tenant until one exists, so it's safe
  to add early and it activates automatically once connected.
- **attachments** — *Problem:* a customer attaches a screenshot of an
  error, but `draft`/`classify` only ever see text.
  *Example:* source "Salesforce", OCR on, "skip signature/logo images" on
  — a screenshot of a stack trace gets OCR'd into context; the sender's
  email signature logo doesn't waste a call.
- **retrieve** — *Problem:* the bot needs to ground its answer in your
  actual docs instead of making something up.
  *Example:* `kb_sources` = your "Billing FAQ" collection, `top_k` = 5 —
  only billing docs are searched for a billing-team flow, keeping
  irrelevant product docs out of the context window.
- **kb_lookup** — *Problem:* one specific point in the flow needs an
  *internal-only* knowledge collection (e.g. an internal runbook) ahead of
  the public docs `retrieve` already searches.
  *Example:* collections = "Internal Escalation Runbook", `top_k` = 3,
  placed right before `draft` so its result is treated as authoritative.
- **case_lookup** — *Problem:* a similar case has probably been solved
  before, and reusing that real resolution beats generating a fresh
  answer from scratch every time.
  *Example:* default settings (`k`=3, `min_similarity`=0.35) — for a case
  about "refund for double charge," the 2 most similar past resolved
  cases feed `draft` as grounding, if any are similar enough.
- **correction_exemplars** — *Problem:* the bot keeps making the same kind
  of mistake a human has already corrected — those corrections should
  actually change future drafts.
  *Example:* placed right before `draft`, `k`=2 — the two most severe
  recent human corrections for this tenant are shown to the model as
  "don't do this again" examples.

### Understanding the case

- **classify** — *Problem:* every downstream decision (routing, gate
  thresholds) needs to know the case's tier/topic/urgency, but that's not
  handed to you directly.
  *Example:* `tier_field` = `account.customer_type` (the default) — a
  case from an Account with `customer_type = "enterprise"` classifies as
  tier `enterprise`, which the confidence gate then holds to a stricter
  bar.
- **extract** — *Problem:* a policy rule needs a specific fact pulled out
  of free-text (e.g. "how many years of data are they asking to delete"),
  and that's not a field anywhere.
  *Example:* field `report_period_years` — "how old are the requested
  reports, in years" — turns "please delete everything from the last 3
  years" into `state.entities.report_period_years = 3` for a policy rule
  to key on.

### Routing

- **team_route** — *Problem:* different kinds of cases need different
  owning teams, and you don't want to hand-sort them.
  *Example:* keyword rules — "renewal"/"add seats" → `csm`, "pricing"/
  "quote" → `sales`, "cancel"/"data export" → `offboarding`, else
  `support` (the built-in defaults) — sets `routed_team`, which
  `ask_human`/`handover` then resolve their queue from and edges can
  branch on.
- **policy_gate** — *Problem:* "if X then do Y" business rules (defined
  in the Rules tab, not code) need to actually run against every case,
  consistently, instead of living only in someone's head.
  *Example:* a rule "data-export request older than 2 years → raise an
  ops task" — matches, sets `policy.task`, which an edge condition
  (`policy.task != None`) routes to `task_dispatch`.
- **task_dispatch** — *Problem:* a matched policy rule's action (e.g.
  "open a GitHub issue") shouldn't fire blind — someone should approve it
  first.
  *Example:* wired on the edge `policy.task != None` right after
  `policy_gate` — posts a Slack Approve/Reject message; the GitHub issue
  itself only gets created once a person clicks Approve.

### Answering

- **draft** — *Problem:* someone has to actually write the reply, grounded
  in whatever context the earlier nodes gathered.
  *Example:* default model, `max_tokens` 500 — drafts a reply using
  `retrieve`'s KB chunks + `case_lookup`'s similar cases + any
  `correction_exemplars`, and reports its own confidence for the gate.
- **agent** — *Problem:* a single retrieve-then-draft pass sometimes isn't
  grounded enough, and you'd rather it try a reformulated search than
  hand off immediately.
  *Example:* `max_iterations` 3, `groundedness_threshold` 0.6 — a
  drop-in replacement for a plain `retrieve` + `draft` pair that retries
  with a reformulated query up to twice before giving up.
- **ai_prompt** — *Problem:* you need an AI step the built-in nodes don't
  cover (e.g. summarize a thread into 2 lines for a Slack alert).
  *Example:* user prompt `Summarize this case in one sentence for a
  Slack alert: {case.subject} — {case.body}`, `output_key` =
  `slack_summary` — the result is available downstream as
  `state.slack_summary`, and an edge can branch on it.

### Talking to other systems

- **http_request** — *Problem:* an integration this platform has no
  dedicated node for still needs to be called.
  *Example:* connection "internal-status-api" (Data tab), method GET, path
  `/v1/incidents`, query `{"account": "{{sf_context.account.id}}"}` —
  pulls that account's open incidents into context before drafting.
- **connector_action** — *Problem:* same idea, but for an action a
  *registered* connector already declares (Salesforce, HubSpot, Slack, or
  one of your own saved Connections) — no code needed to add a new one.
  *Example:* connector "slack", action "post_message" — posts a formatted
  message to a channel as one step in the flow, without a dedicated
  built-in node for it.
- **transform** — *Problem:* two nodes' data shapes don't line up (one
  produces `context.http.json.total`, the next needs `context.order_total`
  as a plain field), and that shouldn't need an AI call to fix.
  *Example:* `map: {"order_total": "context.http.json.total"}` — copies
  one nested value to a flat key the next node reads, with no LLM
  involved.
- **sf_writeback** — *Problem:* the triage this flow just did (priority,
  case type, routed team) is invisible in your case system unless
  something writes it back.
  *Example:* the default field map — `urgency → Priority`, `case_type →
  Type`, `case_topic → Topic__c` — so anyone opening the Case in Salesforce
  sees the triage, not just the raw email.

### Deciding the outcome

- **confidence_gate** — *Problem:* you need one consistent point that
  decides "is this answer good enough to send automatically," instead of
  every flow re-implementing that judgment call differently.
  *Example:* leave the default threshold at 0.35 for every tier, then set
  a tier override of 0.6 for enterprise — the exact same draft confidence
  that auto-replies for a basic-tier case now falls below the bar for an
  enterprise account and routes to a human instead.

### Sending the outcome

- **auto_reply** — *Problem:* a case that cleared the confidence gate
  should just be answered — no human needed.
  *Example:* wired on the gate's `confidence_gate.pass` edge — sends the
  drafted reply straight to the customer, no review step.
- **notify** — *Problem:* an internal rep should know about this case
  without the case actually changing hands.
  *Example:* case type "Billing" → target a specific queue id — a heads-up
  note lands on the case; ownership and queue stay exactly where they
  were.
- **ask_human** — *Problem:* the bot isn't confident enough to answer
  alone, but this isn't a dead end — a person's reply should become the
  answer sent back.
  *Example:* queue "Team_Support" — posts the draft + question to the
  queue; whatever a teammate replies with is read back and sent to the
  customer as the resolution.
- **notify_human** — *Problem:* a plain @mention isn't enough for a
  genuinely ambiguous case — you want the bot to ask the *specific*
  questions that matter and only draft once a person has actually engaged.
  *Example:* channel "Slack + case system note," max clarify rounds 3 —
  opens a Slack thread with the bot's own read on the case; the customer
  reply is drafted only after the agent works through the open points.
- **handover** — *Problem:* some cases (e.g. your highest-value accounts)
  should never get even a reviewed bot answer — a full human handoff,
  every time.
  *Example:* wired unconditionally for `tier == 'enterprise'`, ahead of
  the confidence gate — enterprise cases skip the gate entirely and go
  straight to a human queue, regardless of how confident the draft is.
- **clarify** — *Problem:* the case is missing details the bot needs (a
  reasonable follow-up question), and escalating to a human for something
  that simple wastes their time.
  *Example:* max questions 2, channel "email" — instead of escalating "what
  plan are you on?", the bot emails that question back to the customer and
  resumes once they reply.

### Edges — what problem each pattern solves

- **Unconditional edge** (the "conditional" checkbox left off) — *Problem:*
  a step should simply always run next, with no decision involved.
  *Example:* `identify → sf_case` — every case gets identified, then
  gets a Case created, no exceptions.
- **Conditional edge, row builder** — *Problem:* branch on a real field
  without writing an expression.
  *Example:* field **Case channel**, operator "equals," value `hubspot` —
  routes only cases that arrived via HubSpot down this path (other real
  fields available the same way: Tier, Region, Routed team, Urgency, Case
  type, Confidence gate passed, Policy task, …).
- **Conditional edge, Advanced** — *Problem:* the condition needs `or`,
  `not`, or nested logic the row builder can't express.
  *Example:* `tier == 'enterprise' or region == 'EMEA'` — two unrelated
  fields, combined with `or`; must use `and`/`or` (this is evaluated as a
  Python expression, not JavaScript — `&&`/`||` will fail).
- **Branching a node into several outcomes** — *Problem:* a node like
  `confidence_gate` needs to send different cases down genuinely different
  paths, not just one.
  *Example:* from `confidence_gate`: edge 1 conditional on
  `confidence_gate.pass` → `auto_reply`; edge 2 left unconditional (the
  catch-all) → `ask_human`. A node needs exactly one unconditional
  "everything else" edge, or zero if every path is conditional and none
  should ever be the default.
- **Naming an edge** — *Problem:* `tier == 'enterprise' or region ==
  'EMEA'` doesn't read as anything at a glance six months later.
  *Example:* name it "VIP customers" — the canvas shows that name instead
  of the expression; hover it to see the real condition underneath.

## The rest of the dashboard

| Tab | What it's for |
|---|---|
| **Runs** | Every time a flow executed, and what it decided (auto-reply / ask human / handover) — the log to check "did this actually work." |
| **Activity** | A feed across the whole workspace — flow edits, connector changes, everything, not just runs. |
| **Approvals** | Anything waiting on a human — Slack/task approvals a policy rule raised. |
| **Trace** | The full step-by-step detail of one specific run — what each node read, decided, and wrote. Open this when a run's outcome is surprising. |
| **Knowledge** | Manage what the bot can answer from — crawled sites, uploaded docs, Google Drive/Sheets, and other collections a `retrieve`/`kb_lookup` node reads. |
| **Ask** | Query the knowledge base directly, outside of a flow — useful for checking "does the bot actually know this" before you rely on it in a flow. |
| **Rules** | Structured policy rules a `policy_gate` node evaluates (e.g. "data-export request older than 2 years → open an ops ticket"), and Slack connection settings. |
| **Intake** | Intake checklists — the specific questions a `clarify` node asks for a given case type, and where its answers get written. |
| **Guide** | An editorial walkthrough of one real example flow (inbound email → Salesforce Case), stage by stage — good for understanding the platform's *shape* before you build your own. |
| **Team** *(owner)* | Who's in this workspace and their role. |
| **Connections** *(owner)* | Connect/manage Salesforce, HubSpot, Slack, and other integrations; your own AI provider keys. |
| **Billing** *(owner)* | Usage and plan. |

## Common tasks

- **"I want the bot to only answer confidently, and ask a person
  otherwise."** Open the flow's `confidence_gate` node — its thresholds
  (per customer tier) control exactly this; raise a threshold to make it
  more cautious.
- **"A specific kind of case should always go to a person."** Add a
  conditional edge before the auto-reply step (e.g. `case_type ==
  'Billing'`), routing it to `ask_human` or `handover` instead — see
  *Conditions, without writing code* above. Give it a name so it reads
  clearly on the canvas.
- **"I don't know why a case got the outcome it did."** Find it in
  **Runs**, open its detail, then **Trace** for the exact step-by-step
  reasoning.
- **"I changed something and I'm not sure it's safe."** Use **Test run**
  before **Save draft**; **Save draft** before **Publish** — publishing is
  the only step that affects real cases.
- **"I want to undo a bad publish."** **More ▾ → rollback** in the editor
  toolbar, pick the earlier version.

## If something looks wrong

- A node with a **red** border is invalid (an unregistered type) or "won't
  build" (more than one unconditional outgoing edge) — open it and fix the
  type, or make all-but-one of its outgoing edges conditional.
- A flow needs exactly one entry point. If more than one node has no
  incoming edge from anywhere in the flow, the toolbar shows a warning
  pill naming them — fix this before publishing, or the flow won't build.
- If the bot isn't finding an answer it should know, check **Ask** first
  (is the knowledge actually indexed?) before assuming the flow itself is
  wrong.
