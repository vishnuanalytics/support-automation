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

## 27f — Cutover (one entry queue)  ✅ applied (2026-09-01)

**A. Pipeline-created Cases** — `salesforce.ensure_case` sets `OwnerId =
AI_Intake` on every Case it creates (a REST create doesn't run assignment
rules), so CDC / email-poller Cases start in the one queue.

**B. Email-to-Case / Web-to-Case / manual Cases** — `scripts/sf_assignment_cutover.py`
**run** — the active `Standard` Case assignment rule now has one entry:
`formula=true → Queue AI_Intake` (was 6 stock entries). Backup at
`scripts/_assignment_backup/Case.assignmentRules.json`; revert with
`python scripts/sf_assignment_cutover.py --restore`.

---

## 27g — Backstops

### `Close_Needs_Type` validation rule  ✅ deployed (2026-09-01)
`python scripts/sf_backstops.py` — a Case can't be `Closed` without a `Type`
**and** a non-blank `Description`. `--remove` reverts.

### Two list views — 60-second Setup task (column tokens fight the API)
Cases tab → **New List View**:
- **Live Queue** — filter `Closed = false`; columns Case # / Subject /
  Status / `Routed_Team__c` / `Next_Action__c` / `Next_Action_Due__c` /
  `AI_Confidence__c` / Owner; sort by `Next_Action_Due__c` ↑.
- **SLA Breach** — filter `SLA_Breach__c = true` AND `Closed = false`.

### Native Case Escalation Rule — skipped (do by hand if wanted)
The app `queue_sweep` acts at 30 min and is the primary path. If you want a
worker-outage backstop: Setup → **Escalation Rules** → new rule, `Status`
equals `Escalated` AND `Owner` = a Queue, escalate 60 min after Last
Modified → re-assign to `SLA_Breach`.

---

## Slack (supports 27e)  ✅ live (2026-09-02)

Repo-side routing (`resolve_slack_route`, migration `063`) is done. In Slack —
**all done in the `speedy` workspace**:

1. Channels `#cx-l1`, `#cx-tier2`, `#cx-csm`, `#cx-sales`, `#cx-offboarding`,
   `#cx-billing`, `#cx-incident`, `#cx-unrouted` created; `support_automation`
   bot invited to each.
2. Usergroups `@cx-l1-oncall`, `@cx-tier2-oncall`, `@cx-csm-oncall`,
   `@cx-sales-oncall`, `@trust-oncall`, `@billing-oncall`, `@cx-leads` created
   with a test agent in each.
3. `notify_targets.slack_usergroup` reconciled to the real handles (csm/sales
   became `@cx-*-oncall`). **A bare `@handle` in message text does not notify a
   group** — `slack.usergroup_ref()` resolves it to `<!subteam^ID>` via
   `usergroups.list`, so the bot needs `usergroups:read` (granted).
4. Bot scopes: `chat:write`, `chat:write.public`, `channels:history`,
   `groups:history`, `im:history`, `app_mentions:read`, `users:read`,
   `users:read.email`, `usergroups:read`. App-level token `xapp-…`
   (`connections:write`) in `.env` as `SLACK_APP_TOKEN`. Socket Mode +
   Interactivity ON. Verified: `python -m interpreter.slack_socket` →
   `socket connected`.

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
