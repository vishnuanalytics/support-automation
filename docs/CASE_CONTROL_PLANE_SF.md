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

The one hard-to-reverse step. Do it in a low-volume window.

1. Setup → **Case Assignment Rules** → export / screenshot the current active
   rule's entries (so you can restore).
2. Edit the active rule: delete every entry, add **one**:
   - Criteria: (no criteria — matches all), Assign to **Queue `AI_Intake`**,
     do not notify.
3. Confirm Email-to-Case / Web-to-Case still point at the standard intake (they
   feed the assignment rule, which now always lands in `AI_Intake`).
4. Create a Case from each channel → it lands in `AI_Intake`, owner = the
   integration user once the pipeline's first pass runs; escalations route via
   Omni (27b), not the assignment rule.

Rollback: re-add the old rule entries.

---

## 27g — Backstops

### Native Case Escalation Rule (belt-and-braces to the app sweep)
Setup → **Escalation Rules** → new rule, one entry:
- Criteria: `Status` equals `Escalated` **AND** `Owner` = a Queue.
- Escalation time: **60 minutes** after `Case: Last Modified`.
- Action: re-assign to `SLA_Breach`, notify the support manager.

(The app's `queue_sweep` acts at 30 min; this fires only if the worker is down.)

### Validation rule — no undocumented close
Setup → **Object Manager → Case → Validation Rules** → New:
```
AND(
  ISPICKVAL(Status, "Closed"),
  OR(ISBLANK(TEXT(Type)), ISBLANK(Description))
)
```
Error: "Set a Case Type and a resolution summary before closing."

### List views
Object Manager → Case → **List View Controls** (or the Cases tab):
- **Live Queue** — filter `Closed = false`; group/sort by `Status`, then
  `Next_Action_Due__c` ascending; columns: Case #, Subject, `Status`,
  `Routed_Team__c`, `Next_Action__c`, `Next_Action_Due__c`, `AI_Confidence__c`,
  Owner.
- **SLA Breach** — filter `SLA_Breach__c = true` AND `Closed = false`.

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

### Deferred — the interactive card buttons
**Send as-is / Edit in thread / Reassign… / Not my team** need the Slack app's
**Interactivity** request URL (or Socket Mode `interactive` envelopes) plus a
Block Kit card. Not built. Until then the thread works as a reasoning dialogue
(`@mention` / `take`), and reassignment is `Routed_Team__c` edited on the Case
(Omni re-routes). Track as Phase 27h.
