# Case Control Plane — Salesforce runbook

The parts of Phase 27 that live in the Salesforce org, not the repo. Do these
with a System Administrator login. Design context:
[Case Control Plane doc](https://claude.ai/code/artifact/403e72fa-beef-4f12-809f-7511f6d81ca0).

Repo-side phases (already built): **27a** schema, **27c** pipeline writes state,
**27d** safety-net sweeps, **27e** Slack routing. This runbook is **27b**, **27f**,
**27g**.

---

## 27a (run this first — it's scripted)

```bash
python scripts/sf_support_setup.py --dry-run --only queues --only cp_fields --only types --only fls
python scripts/sf_support_setup.py --only queues --only cp_fields --only types --only fls
```

Creates: queues `AI_Intake` / `Unrouted_Review` / `SLA_Breach`; the 9
control-plane Case fields (`Routed_Team__c`, `Next_Action__c`,
`Next_Action_Due__c`, `Escalation_Reason__c`, `AI_Confidence__c`,
`Last_AI_Run_At__c`, `Last_Run_Id__c`, `Handoff_Slack_Ts__c`, `SLA_Breach__c`);
Status values `Triaged` / `In Progress` / `Resolved`; FLS on the admin profile.

Then apply migration `062` (Supabase MCP `apply_migration` or SQL editor) and
`063`.

> **`Routed_Team__c` values must be exactly** `support` `tier2` `csm` `sales`
> `offboarding` `billing` — the app writes these strings verbatim.

---

## 27b — Omni-Channel  ✅ scripted + live-applied (2026-09-01)

**No Flow, no Apex, no pipeline code.** `scripts/sf_omni_setup.py` does it
all through the API, and once a queue has a routing config Salesforce
**auto-creates** the `PendingServiceRouting` the moment the pipeline's
`assign_case(queue=…)` sets the Case owner to that queue. Verified live —
a re-assign to `Team_CSM` produced a ready PSR with zero extra code.

```bash
python scripts/sf_omni_setup.py --dry-run
python scripts/sf_omni_setup.py
```

Creates + wires: `ServiceChannel Support_Case` (Case, TabBased) · presence
statuses `Available_Cases` / `Busy` / `Away` + the channel-status link ·
`QueueRoutingConfig` `RC_Standard` (LeastActive, pri 2, 90s) on
`Team_Support` / `Support_Tier2` / `Team_CSM` / `Team_Sales` /
`Team_Offboarding` and `RC_Priority` (pri 1) on `Enterprise_Support` /
`Billing_Escalations` · `PresenceUserConfig PC_Support_Agent` (capacity 3) ·
running user assigned to it.

### The two things the script can't do (once, in Setup)
1. **Presence-status access** — Setup → Permission Sets → *(your agent
   permset)* → **Service Presence Statuses** → enable `Available — Cases`
   etc., and assign the **Presence Configuration** `PC_Support_Agent`.
   (System Administrators already have status access.)
2. **The Omni widget** — Setup → App Manager → Service Console → **Utility
   Items** → add **Omni-Channel** (and optionally **Omni Supervisor** as a
   tab for leads). An agent opens the console and sets presence to
   **Available — Cases** — until someone does, PSRs sit *ready and pending*.

### Smoke test
1. An agent goes **Available — Cases** in the console.
2. Run the pipeline on a case that escalates (or set `Status = Escalated` +
   `OwnerId` = a routing-configured queue by hand).
3. The Case pops in that agent's Omni widget → **Accept** → `OwnerId`
   becomes the agent, capacity −1. **Decline** → re-routes.

---

## 27f — Cutover (one entry queue)

Two parts. Part A (code) shipped; part B is the one hard-to-reverse step —
run it in a low-volume window.

**A. Pipeline-created Cases** — done. `salesforce.ensure_case` now sets
`OwnerId = AI_Intake` on every Case it creates (a REST create doesn't run
assignment rules), so CDC / email-poller Cases already start in the one queue.

**B. Email-to-Case / Web-to-Case / manual Cases** — `scripts/sf_assignment_cutover.py`:

```bash
python scripts/sf_assignment_cutover.py --dry-run   # backs up the current rule, shows the diff
python scripts/sf_assignment_cutover.py             # replace 'Standard' with one entry -> AI_Intake
python scripts/sf_assignment_cutover.py --restore   # redeploy the backup
```

The backup is written to `scripts/_assignment_backup/Case.assignmentRules.json`.
After the cutover, create a Case from each channel → it lands in `AI_Intake`;
the pipeline drives from there and escalations route via Omni (27b).

---

## 27g — Backstops

**`scripts/sf_backstops.py`** deploys the two low-risk ones via metadata:

```bash
python scripts/sf_backstops.py --dry-run
python scripts/sf_backstops.py            # validation rule + 2 list views
python scripts/sf_backstops.py --remove
```

- **`Close_Needs_Type`** validation rule — a Case can't be `Closed` without a
  `Type` and a non-blank `Description`.
- **Live Queue** list view — open Cases; columns Case # / Subject / Status /
  `Routed_Team__c` / `Next_Action__c` / `Next_Action_Due__c` / `AI_Confidence__c` / Owner.
- **SLA Breach** list view — `SLA_Breach__c = true` AND not closed.

### Native Case Escalation Rule — skipped (do by hand if wanted)
The app `queue_sweep` acts at 30 min and is the primary path. If you want a
worker-outage backstop: Setup → **Escalation Rules** → new rule, `Status`
equals `Escalated` AND `Owner` = a Queue, escalate 60 min after Last
Modified → re-assign to `SLA_Breach`.

---

## Slack (supports 27e)

Repo-side routing (`resolve_slack_route`, migration `063`) is done. In Slack:

1. Create channels: `#cx-l1`, `#cx-tier2`, `#cx-csm`, `#cx-sales`,
   `#cx-offboarding`, `#cx-billing`, `#cx-incident`, `#cx-unrouted`.
2. Create usergroups: `@cx-l1-oncall`, `@cx-tier2-oncall`, `@cx-csm`,
   `@cx-sales`, `@trust-oncall`, `@billing-oncall`, `@cx-leads`. Put the test
   agents in them.
3. Update `notify_targets.slack_usergroup` if your handles differ from the
   seed (`update notify_targets set slack_usergroup = '@…' where match_value = '…'`).
4. Invite the `slackbot` app to every channel (it needs `channels:history` +
   membership per the Socket-Mode setup).

### 27h — interactive card buttons  ✅ built (2026-09-01)
The handoff-thread root is now a Block Kit card: **Send as-is** (delivers the
draft) · **Edit in thread** (prompts for edited text) · **Reassign…** /
**Not my team** (prompt → agent replies `route: <team>` → `Routed_Team__c`
updated + queue re-assigned + Omni re-routes + a routing-correction
`case_events` row). `alert._handoff_card` builds it; `slack_socket.
dispatch_action` handles the clicks; the WSS loop routes `type: interactive`
envelopes.

**One Slack-app toggle:** api.slack.com/apps → your app → **Interactivity &
Shortcuts** → turn **Interactivity ON**. With Socket Mode there's no request
URL to fill — the `interactive` payloads then arrive over the same WebSocket.
No re-install / new scopes needed.
