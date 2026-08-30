# Email channel setup (Phase 20)

Point a support mailbox at a tenant and the platform runs every incoming
message through that team's **published** flow, then replies — but only
when the flow says so.

## The safety model

The LangGraph flow is the gate. After a message runs through it:

| flow outcome | what the email channel does |
|---|---|
| `auto_reply` (confidence gate passed) | send the drafted reply — **only if** the channel's *auto-send replies* switch is on **and** the draft is non-empty |
| `need_info` | send the clarifying questions — **only if** auto-send is on **and** the `clarify` node has `auto_send` set |
| `ask_human` / `handover` | send nothing; the message is re-marked **unread + flagged** for a human |
| anything else | nothing |

Extra guards, always on:

- **auto-send defaults to off.** Turn it on in the Channels panel once
  you trust the flow. With it off, every reply waits for a human.
- **loop-breakers.** The poller skips mail from `no-reply@` /
  `mailer-daemon@` / `postmaster@`, anything with `Auto-Submitted`,
  `List-Id`, `Precedence: bulk`, and the mailbox's own address. Outbound
  replies are stamped `X-Support-Bot: 1` + `Auto-Submitted: auto-replied`
  so nothing answers them.
- **idempotency.** Each message is enqueued once, keyed on its
  `Message-ID`; a redelivery is a no-op.
- messages are marked read, **never deleted**.

## Configure it (in the app)

**Channels** tab (workspace owners only):

1. Pick **IMAP / SMTP** or **Gmail**.
2. Set the **team** whose published flow should run (default `support`)
   and a **from name**.
3. **IMAP:** host/port for IMAP and SMTP, the mailbox address, and an
   **app-password** (not your account password — see below). Optionally a
   folder other than `INBOX`.
   **Gmail:** click **Connect Gmail** and finish the Google consent screen.
4. Optionally a separate **reply-from address**.
5. Leave **auto-send** off for the first runs. Tick **poll this mailbox
   (active)** and **Save**.
6. **Test connection** logs in and back out without saving.

The password / Google refresh token is stored in **Supabase Vault**
(encrypted at rest) and never returned to the browser.

### App-passwords

- **Gmail (IMAP path):** enable 2-Step Verification, then create an
  App Password (`myaccount.google.com` → Security → App passwords).
  IMAP `imap.gmail.com:993`, SMTP `smtp.gmail.com:587`. Or use the Gmail
  provider instead and skip the password entirely.
- **Outlook / Microsoft 365:** create an app password under Security info
  (requires MFA). IMAP `outlook.office365.com:993`, SMTP
  `smtp.office365.com:587`.

## Run it

Alongside a worker:

```
python -m api.worker                     # executes the enqueued runs + sends
python -m ingestion.email_watch --once   # one poll; cron this every ~5 min
python -m ingestion.email_watch --once --dry-run    # show, enqueue nothing
```

Cron (GitHub Actions or similar):

```
*/5 * * * *   python -m ingestion.email_watch --once && python -m api.worker --once
```

### GitHub Actions (the current stopgap)

`.github/workflows/email-automation.yml` runs both `--once` steps every
5 minutes. Add these repo secrets (Settings -> Secrets and variables ->
Actions) — the **mailbox password is not one of them**, it lives in
Supabase Vault:

| secret | value |
|---|---|
| `SUPABASE_URL` | `https://<project>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | service_role key |
| `GROQ_API_KEY` | for classify / draft |
| `SF_USERNAME` | Salesforce integration user |
| `SF_CONSUMER_KEY` | Connected App consumer key (JWT flow) |
| `SF_DOMAIN` | `login` \| `test` \| your my-domain host |
| `SF_PRIVATE_KEY` | the JWT private key, full PEM (multi-line ok) |

GitHub cron is best-effort (runs can lag several minutes; skipped after
60 days of repo inactivity). The persistent-worker deployment removes the
lag — this workflow is the interim.

## Gmail provider — operator steps (one-off)

The Gmail provider reuses the platform Google OAuth client
(`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env`). On that client:

1. Add the redirect URI
   `https://<host>/api/integrations/email/google/callback`
   (and `EMAIL_GOOGLE_REDIRECT_URI` in `.env` to match).
2. On the OAuth consent screen add the scopes
   `https://www.googleapis.com/auth/gmail.readonly` and
   `https://www.googleapis.com/auth/gmail.send`.
3. Enable the **Gmail API** for the Google Cloud project.

Until `GOOGLE_CLIENT_ID`/`SECRET` are set, the Gmail radio is disabled and
`GET /api/integrations/email` reports `gmail_available: false`; the IMAP
path works with no server-side config.
