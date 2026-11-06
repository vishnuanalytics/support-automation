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

If your organization doesn't allow a Slack app, a Claude Code session with a Slack connector can load
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
- Stored text is cut to about 400 characters with signed URL parameters removed. Text is redacted
  before any LLM call. Slack tokens live only in Vault.

## Known limits

- Salesforce is the only case reader. Jira and Freshchat links show "linked, status not checked".
- Settings is a JSON editor, not a form.
- A sync runs in the single worker, so a very large one delays other jobs. `MONITOR_SYNC_TIMEOUT`
  defaults to 600 seconds.
- GitHub bot comments and Slack bot messages are ignored when working out whose turn it is.
- The similar-case pool holds cases resolved in the last 180 days, up to 2,000 per sync.
- Not built: writing to Salesforce or GitHub, auto-replies, or status changes.
