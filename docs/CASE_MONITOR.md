# Case Monitor

One queue for leads and managers that answers: **whose turn is it on this case, and has anyone
noticed?** Open it from **Case Monitor** in the sidebar. Spec: FR-52 in `REQUIREMENTS.md`.

It shows two feeds together:

- **AI agent cases:** runs handed to a human, runs waiting on the customer, and open reasoning
  sessions, for the last 14 days.
- **Dependency cases:** open cases waiting on engineering or a third party in the connected
  case system. Salesforce is the only case reader so far.

The verdict is rule-based (`interpreter/monitor/verdict.py`). An LLM can summarise, but never
decides whose turn it is. Every statement cites its source.

## Try it

Open Case Monitor in an empty workspace and click **Load demo data**. Remove it with
**Clear demo data**. Real syncs never touch demo rows.

## Setup

1. Apply migration `116_case_monitor.sql` (already applied to the live project).
2. Rebuild so the worker and API have the code: `docker compose up -d --build`.
3. Connect GitHub and the workspace's Salesforce org under **Connections**, or use your CLI login
   (below) if you don't have a client ID and secret. A sync refuses to run without the workspace's
   own Salesforce login, and a failed login never falls back to the platform's org.
4. In **Case Monitor > Settings** (owners), add the config below and set `enabled` to `true`.
5. Each lead clicks **Connect Slack** (see below).

The worker checks every 5 minutes which workspaces are due a sync, using `sync_every_min`
(0 turns it off) and `sync_hour` in `timezone`. **Refresh now** queues one straight away. Only one
sync per workspace can run at a time.

### Salesforce without a client ID and secret

The Salesforce CLI login is OAuth and needs no Connected App credentials from you:

```
sf org login web --alias acme
python -m scripts.sf_connect_cli --alias acme                  # lists workspaces
python -m scripts.sf_connect_cli --alias acme --tenant <id>
```

This stores the login's refresh token in Vault, so the worker can sync without your laptop's CLI.
Syncs run with your Salesforce permissions. If the login stops working, the sync fails with
"reconnect it"; run the commands again. A Claude Salesforce connector can't do this job: only
Claude can use it, and only during a session.

### Example config (Acme)

```json
{
  "enabled": true,
  "lightning_url": "https://YOUR-ORG.lightning.force.com",
  "fields": {
    "waiting_on": "Waiting_Party__c",
    "waiting_sub_status": "Waiting_Detail__c",
    "link": "External_Link__c",
    "third_party": "Partner__c",
    "target_at": "Target_Date__c",
    "ageing": "Age__c",
    "last_reply_from": "Last_Reply_From__c",
    "brand": "Brand__c",
    "biz_id": "Location_ID__c",
    "business_id": "Business_ID__c",
    "product": "Product__c",
    "module": "Module__c",
    "sub_module": "Sub_Module__c",
    "bu": "Business_Unit__c",
    "case_type": "Category__c",
    "escalation_level": "Escalation__c",
    "escalated": "Is_Escalated__c"
  },
  "waiting_values": {"Engineer": "engineering", "Aggregator": "third_party"},
  "customer_reply_values": ["Customer Replied"],
  "customer_wait_sub_statuses": ["Awaiting Revert"],
  "team_replied_sub_statuses": ["Internal Team Replied", "External Team Replied"],
  "dependency_record_type": "DSP",
  "internal_email_domains": ["example.com"],
  "focus_partners": ["PartnerA", "PartnerB"],
  "third_party_label": "Aggregator",
  "customer_label": "Merchant",
  "eng_nudge_hours": 8,
  "third_party_nudge_hours": 24,
  "third_party_overrides": {"PartnerA": 12},
  "support_github_users": ["your-github-login"],
  "internal_slack_domain": "acme.com",
  "slack_team_mention": "<!subteam^S0123456>"
}
```

Leave `owner_username` out to monitor the whole team's dependency cases. Set it to limit the queue to
one person. Third-party emails only count on `dependency_record_type` cases (the case or its child),
because emails on other cases are with the customer. An automatic acknowledgement from a third party
counts as a reply.

## Slack

Each lead signs in with their own Slack account. The sync uses those logins to read threads, so it
can read any external channel a connected lead belongs to, without inviting a bot to each one.
If one lead isn't in a channel, it tries the next lead's login.

You don't need to log in every day. A Slack user token stays valid until it's revoked, so one login
is enough. If it stops working, the affected links show "Slack login expired" and the button
reconnects it. The exception is token rotation: if it's ever turned on for the Slack app, tokens
expire after 12 hours and this login would need refresh support, which isn't built.

In the Slack app settings (api.slack.com/apps):

1. Under **OAuth & Permissions > User Token Scopes** add `channels:history`, `groups:history`,
   `users:read`, `users:read.email` and `chat:write`.
2. Make sure `SLACK_REDIRECT_URI` is listed under **Redirect URLs**. It's the same callback the
   workspace bot install uses.
3. The app must be installable in your company workspace: either create it there, or enable
   **Manage Distribution** on an app from another workspace. A workspace admin may need to approve it.

### Third-party directory

`monitor_partners` holds each third party's channel, recipients, contacts, portal link, raising format
and escalation time for a workspace. Never store portal passwords in it. The channel sets the rule:

| Channel | Evidence | With no evidence |
|---|---|---|
| email | Emails on the raising record type, or emails to or from the partner's domains | Not raised |
| slack, jira, portal | The linked thread or ticket | Missing link |
| whatsapp | None: it can't be read | Checked by hand |

A third-party case with no linked case of the raising record type, as parent or child, shows as not
raised. If that linked case is closed while this one is open, it's our turn to update the customer.

### Follow-ups and alerts

**Follow-ups** lists what each agent owes today: cases to reply to, cases to raise, and waiting cases
that have gone quiet past their limit. Each agent has a copy-ready message. **Alerts** lists what
changed since you last looked, skipping acknowledgements. With the app open, it can also show
desktop notifications and play a chime. Use **Test alert** to check both. Browsers only allow sound
after a click on the page. Alerts are only as fresh as the latest sync, so set `sync_every_min` to suit.

### Escalations

**Escalations** lists open cases where the third party we're waiting on has sent no real update for
`escalation_days` (default 5), across all channels, and downloads as CSV. An update must
come from that party, so acknowledgements, customers and internal threads don't count. Cases on our
turn or never raised aren't listed. A support case folds into its DSP case. A review doesn't remove
a case: the silence is still true. Links that couldn't be read show as "Not read".

### Needs Me, Agents and the Wednesday Report

Case Monitor opens on three pages. Everything else is under **Older pages**, and Salesforce stays the
place to filter and browse.

**Needs Me** is what the lead must decide, in three groups:

- **Replies waiting:** an outside reply on GitHub, Slack or email has sat for `needs_reply_hours`
  (default 24) with nothing from us. These are grouped into one card per agent, because the fix is the
  same, and each card lists its cases with the last response and the next step in a few words. One
  reply on linked cases is one job, on the card of whoever owns the aggregator case. A reply with no
  cause, fix or request is labelled `no_answer`, and its step is to push the aggregator.
  If engineers answered and our newest email to the merchant came after that, the GitHub issue becomes
  "follow up with the merchant, GitHub open" under Agents, once the merchant has been quiet for
  `issue_quiet_hours`. Timelines are re-read hourly, because an email doesn't always change the case's
  last modified time.
- **No response past SLA:** aggregators to escalate, and GitHub issues gone quiet.
- **Agents:** help requests, logs about to expire, and issues to close or follow up.

Several GitHub issues of one kind become one card. Marking a card done hides it until something new
happens, or for a day at most.

**Agents** shows who raised what and what was wrong, worst first, for open aggregator cases raised in the
last 30 days plus GitHub issues closed with a `wrong_raise_labels` label. A case stops counting once it
closes. The raiser is whoever signed the first email out, falling back to the owner. A raise is wrong when:

- a required detail is missing, other than an item name
- the location belongs to another business, or the platform ID doesn't match that location
- every payload link in our emails is for another store

"Please check" is not counted against anyone. It means our records have no ID to compare, or no item
name was found, which is the weakest match. `platform_aliases` maps an aggregator's name to its catalogue
platform name when they differ.

The IDs come from `monitor_location_facts`. The hourly routine copies them from Metabase question
`ID-01`, refreshing each location weekly, so a new location's ID check can take up to an hour.

**Wednesday Report** is ready to copy and post by hand. The app sends nothing. The week starts on
`week_start_weekday` in `timezone`.

- **Pending from engineering:** open issues with an open case where tech still owes the resolution, by
  squad. Issues answered, unread, or waiting on support, a merchant or a partner are left out and counted.
  `waiting_on` comes from the AI reading the comments, so check the left-out counts.
- **Aggregators past SLA:** past their deadline, still waiting on the aggregator, and quiet for at
  least a day.

### Aggregator SLAs

`dsp_sla_rules` sets a clock per kind of issue, matched by module, sub-module or a subject keyword, with
the first match winning. Each rule has `escalate_hours` and `resolve_hours`. A case with no update by the
escalate mark needs escalating, and any case past the resolve mark is past SLA. An acknowledgement isn't
an update. Cases with no rule use their target date, escalating at `dsp_escalate_share` of it (default
half). **Weekly SLA** shows each aggregator's numbers. Closed cases keep no target date, so "closed in
time" only covers cases with a rule.

A rule's optional `goal_resolve_hours` is where it should end up. **Weekly SLA** marks a rule "Ready to
tighten" once at least 80% of at least 5 cases closed in time that week.

### Missing details

`required_details` lists what each kind of aggregator issue needs, first match wins. Every case needs a
business name. `location_id`, `platform_id` and the aggregator are read from fields. An order ID or item
name is found by pattern in the subject, description and emails, so an unusual format can be missed. Cases
raised from `sla_start` with something missing count on the Agents page.

### Log expiry

`log_retention_days` names the aggregators that delete logs early, and `log_topics` lists the modules
or sub-modules that depend on them. An open case with `log_warn_days` or fewer left goes on a Needs Me card
per aggregator, with a message for the agents. Cases don't record the order date, so the clock runs from
when the case was raised. The real deadline can be earlier, never later.

### New cases and old backlog

`sla_start` is an ISO date and time. Aggregator cases raised before it are old backlog: Weekly SLA counts
them apart, and they don't affect closed-in-time numbers or whether a rule is ready to tighten. With no
`sla_start`, every case is new. Needs Me still lists late old cases, because they still need chasing.

### Your workload

After each sync the worker saves the day's numbers to `monitor_daily_stats`. Weekly SLA compares the
average of the first 7 days with the latest 7, and judges a change once there are more than 7 days.
It counts work, not time spent, because the app doesn't track how long the lead takes.

### Business search

Engineering, Escalations and Weekly SLA each have a box that finds open cases by business name or ID.
The name comes from `fields.business_name` (default `Account.Name`). The ID comes from `biz_id`, or
`business_id` if that is empty. Weekly SLA lists late cases only, but a search covers every open
aggregator case. An Engineering issue with no open case has no business, so a search can't find it.

### Resolution Assistance

Rows in `monitor_help_requests` appear on Needs Me until someone other than the agent replies.
A bot's reply never counts as an answer. `assist_hours` sets the lead's hours, so requests outside them
are marked.

The app doesn't read Slack for these. An hourly Claude routine with Slack, Metabase and Supabase access writes
the rows, so a new request can take up to an hour to appear.

### Coaching

The Agents page counts closed issues that carried a `wrong_raise_labels` label in the last 30 days, and a
ready note for any agent with at least two. The agent comes from the `github_reporter_field` line in
the issue body.

### Engineering

**Engineering** lists every open GitHub issue labelled `github_support_label` (default `support`), in
each repository open cases link to, plus any other issue an open case links to. Issues are grouped by
squad label (`github_squad_suffix`), filter by squad, status and age (`tech_pending_days`, default 7),
and download as CSV. An issue with no open case shows "No open case".

Each issue has a status, who it's waiting on and a one-line note:

| Status | Meaning |
|---|---|
| No tech reply | Only support has commented |
| Cause not confirmed yet | An engineer replied, with no confirmed cause |
| Cause given, not closed yet | The cause or decision is stated, and a step is still open |
| Answered or fix released | Nothing is left for engineers |
| Not read yet | The AI couldn't read it, usually a rate limit. The next sync retries |

An issue with no comment from outside `support_github_users` is "No tech reply" by rule. The AI reads
the rest, and only re-reads an issue when its thread changes. Text is redacted first. These statuses
never affect a case's verdict. Score them with `python -m eval.monitor.issue_status`.

### Email footers

List text that starts your email footer in `email_footer_markers`. Everything after it is hidden from
the conversation.

### GitHub accounts

List your support team in `support_github_users`, so their comments count as ours. List automation
accounts that GitHub doesn't mark as bots in `github_bot_users`, so they're ignored. A support login
missing from the list shows up as an engineer replying.

### Who sent an email

Unsent drafts are ignored everywhere, because Salesforce stores them as emails with Status 5.
A sender on `internal_email_domains` is our team. A sender on the partner's `email_domains`, a
subdomain of one, or a domain containing the partner's name is the third party. Anyone else is the
customer, who may use any domain. So a customer looped into a raised case shows as the customer, and
it's our turn.

### Real updates, past cases and AI drafts

With `llm_enabled` on, the latest third-party reply is sorted into acknowledgement, internal tag, bug
raised, solution, needs more info, or update. Clear acknowledgements are caught by rules first.
An acknowledgement keeps the case waiting on the third party. If a reply can't be sorted, it counts
as real, so a failure can never hide one. Text is redacted before any LLM call.

Resolved cases load into the "Resolved third-party cases" Knowledge collection with their whole
conversation, redacted:

```
python -m scripts.monitor_load_knowledge --tenant <id> --since 2026-08-01 --until 2026-11-01
```

It's safe to rerun: unchanged cases are skipped. Case pages use that collection to write AI drafts for
the customer or the third party. Drafts are only shown for copying, never sent.

### Where links come from

The link field comes first. If it has no URL, the sync looks at parent and child cases, then at the
case's posts and post comments, where it accepts only GitHub, Slack, Jira and Freshchat links. A link
found in a post is tagged in the queue.

### Without a Slack app

If your organization doesn't allow a Slack app, set `slack_via_claude` to true and a Claude Code session with a Slack connector can load
threads instead. Claude reads the threads linked from open cases with your own Slack access, then
calls `service.import_slack_threads`. The page shows when each thread was read. Syncs keep the last
import until a real read succeeds, so it goes stale until the next refresh. Tagging your team isn't
available this way.

### Tagging your team

**Write a message** on a case posts one reply in that case's linked Slack thread, as the person who
clicked. It's never automatic, it only accepts a thread linked to that case, and each post is
written to the audit log. In an external channel, external members can see the post.

## Read-only guarantees

- Salesforce: only SOQL through `query_all`.
- GitHub: only GET requests.
- Slack: only `conversations.replies` and `users.info`, plus the tag above.
- Reviewed and snooze stay in this app and are shared across the workspace. They're never written
  to Salesforce, and new activity brings a case back.
- Stored text is cut to about 400 characters, or 1,500 for GitHub comments and 2,000 for emails, with
  signed URL parameters removed. Text is redacted
  before any LLM call. Slack tokens live only in Vault.

## Known limits

- Salesforce is the only case reader. Jira and Freshchat links show "linked, status not checked".
- Settings is a JSON editor, not a form.
- A sync runs in the single worker, so a very large one delays other jobs. `MONITOR_SYNC_TIMEOUT`
  defaults to 600 seconds.
- GitHub bot comments and Slack bot messages are ignored when working out whose turn it is.
- The similar-case pool holds cases resolved in the last 180 days, up to 2,000 per sync.
- Not built: writing to Salesforce or GitHub, auto-replies, or status changes.
