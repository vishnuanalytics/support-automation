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
    "case_type": "Category__c",
    "escalation_level": "Escalation__c",
    "escalated": "Is_Escalated__c"
  },
  "waiting_values": {"Engineer": "engineering", "Aggregator": "third_party"},
  "customer_reply_values": ["Customer Replied"],
  "customer_wait_sub_statuses": ["Awaiting Revert"],
  "team_replied_sub_statuses": ["Internal Team Replied", "External Team Replied"],
  "dependency_record_type": "DSP",
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
