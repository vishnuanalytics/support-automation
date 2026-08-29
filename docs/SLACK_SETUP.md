# Slack + GitHub setup (Phase 16)

Structured `policy_rules` can dispatch an **internal task** (`then.type =
"task"`). A `task_dispatch` node raises an `action_requests` row and posts
an Approve/Reject message to Slack; on approval a worker opens a GitHub
issue. All optional — without creds the row is still recorded, it just
isn't posted or actioned.

**Free Slack and free GitHub are enough.** Nothing here needs a paid plan.

## 1. Slack app (one, platform-level)

1. <https://api.slack.com/apps> → **Create New App → From scratch**.
2. **OAuth & Permissions → Bot Token Scopes**: `chat:write`,
   `chat:write.public`.
3. **OAuth & Permissions → Redirect URLs**: add exactly your
   `SLACK_REDIRECT_URI` (default
   `http://localhost:8000/api/integrations/slack/callback`).
4. **Interactivity & Shortcuts → Interactivity: On**. Request URL:
   `https://<public-api-host>/api/integrations/slack/interactions`.
   Slack must be able to reach this over HTTPS — for local dev run a
   tunnel (`cloudflared tunnel --url http://localhost:8000` or `ngrok
   http 8000`) and use the tunnel URL here.
5. **Basic Information** → copy **Client ID**, **Client Secret**, and
   **Signing Secret** into `.env`:

   ```
   SLACK_CLIENT_ID=...
   SLACK_CLIENT_SECRET=...
   SLACK_SIGNING_SECRET=...
   SLACK_REDIRECT_URI=http://localhost:8000/api/integrations/slack/callback
   ```

Restart the API. `GET /api/integrations/slack/status` → `configured: true`.

## 2. Connect a tenant

Web app → **Rules** tab → **Connect Slack** → approve the install in the
target workspace. The bot token lands in `tenant_integrations
(tenant_id, kind='slack')`. Invite the bot to the approval channel
(`/invite @yourbot` in `#support-leads`, or use a public channel with
`chat:write.public`).

## 3. GitHub

Either set a shared `GITHUB_TOKEN` (fine-grained PAT, **Issues: Read and
write** on the target repos), or a per-tenant token:

```sql
insert into tenant_integrations (tenant_id, kind, secret)
values ('<tenant>', 'github', '{"token": "github_pat_..."}');
```

The rule's `then.repo` is `owner/name`.

## 4. Author a rule

**Rules** tab → **＋ rule**. Example (matches the `026` seed):

```jsonc
// when
{ "field": "entities.report_age_years", "op": "gte", "value": 2 }
// then
{ "type": "task", "task": "github_issue", "repo": "acme/support-ops",
  "title_tmpl": "Data export request: {{case.subject}}",
  "body_tmpl": "...\n{{case.body}}",
  "labels": ["data-export"],
  "approval": { "slack_channel": "#support-leads" } }
```

Wire `extract → policy_gate → {task_dispatch if policy.task, else …}` in
the flow editor (the seed does this on the Acme offboarding flow).

## 5. Expire stale approvals

```
python -m scripts.expire_approvals      # cron; flips pending > APPROVAL_TTL_H to 'expired'
```
