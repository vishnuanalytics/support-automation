# Salesforce setup (Phase 3)

The `sf_writeback` node and the Chatter "ask human" escalation talk to a
personal **Salesforce Developer Edition** org (free). Everything degrades to
a dry-run when creds are absent, so this is only needed for real writes.

`interpreter/salesforce.py` supports three auth modes and picks one by which
env vars are set, in this order: **JWT** → **OAuth username-password** →
**legacy SOAP**. On new Agentforce/trial orgs only JWT works (the other two
flows are disabled by default and can't be re-enabled reliably) — that's the
one documented first.

`SF_DOMAIN` is a **token, not a URL** — `login` (default), `test` for a
sandbox, or a My Domain like `mycompany-dev-ed.develop.my`. A full URL is
tolerated (scheme and `.salesforce.com` are stripped).

## 1a. Mode A — JWT bearer flow (recommended)

`.env`:
```
SF_USERNAME=you@example.com
SF_CONSUMER_KEY=<Connected App consumer key>
SF_PRIVATE_KEY_FILE=/abs/path/to/support-automation/sf_jwt/server.key
SF_DOMAIN=login          # or your My Domain token
```
No password, secret, or security token. Not subject to MFA.

**Generate the keypair** (already done once; regenerate only if lost):
```
mkdir -p sf_jwt
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
  -keyout sf_jwt/server.key -out sf_jwt/server.crt \
  -subj "/CN=support-automation-jwt/O=support-automation/C=US"
```
`sf_jwt/` is gitignored.

**Connected App:**
1. **Setup → App Manager → New Connected App** (classic Connected App).
2. Name it, contact email. Tick **Enable OAuth Settings**.
3. Callback URL: `http://localhost:1717/OauthRedirect` (required, unused).
4. Tick **Use digital signatures** → upload **`sf_jwt/server.crt`**.
5. OAuth Scopes: **Manage user data via APIs (api)** and **Perform requests
   at any time (refresh_token, offline_access)**.
6. Save, wait ~5 min.
7. **Manage → Edit Policies → OAuth Policies → Permitted Users → "Admin
   approved users are pre-authorized"** → Save.
8. **Manage → Profiles → Manage Profiles →** add your user's profile
   (**System Administrator**). *(JWT requires the subject user be
   pre-authorized — this is the step that yields `invalid_app_access` if
   skipped.)*
9. **Manage Consumer Details** → copy the **Consumer Key** into
   `SF_CONSUMER_KEY`.

## 1b. Mode B — OAuth username-password  ·  1c. Mode C — legacy SOAP

Only if your org allows them (older orgs). B needs `SF_USERNAME` +
`SF_PASSWORD` + `SF_CONSUMER_KEY` + `SF_CONSUMER_SECRET` and the org's
*"Allow OAuth Username-Password Flows"* toggle. C needs `SF_USERNAME` +
`SF_PASSWORD` + `SF_SECURITY_TOKEN` (**Settings → My Personal Information →
Reset My Security Token**, emailed). If C fails with *"SOAP API login() is
disabled"*, or B with `invalid_grant`, use Mode A.

## 2. Custom fields — created for you by a script

```
python scripts/sf_create_fields.py
```
Creates via the Metadata API + grants field-level security on your profile:

| field | type | fed by |
|---|---|---|
| `Case.Module__c` | Text(120) | `sf_writeback` ← `classification.topic` |
| `Case.Region__c` | Text(80) | `sf_writeback` ← `region` |
| `Account.Tier__c` | Text(40) | `classify` tier (`basic`/`premium`/`enterprise`) |

Idempotent. `Priority` and `Description` are standard fields (`Priority` gets
the value-mapped urgency; `Description` gets `[triage] …` appended). If the
custom fields are absent the write is **tolerant** — it drops them and still
writes `Priority` + `Description`, listing the rest under `skipped`.

`get_case` reads tier from `Account.Tier__c` if present, else the standard
`Account.Type` picklist (whose values aren't `basic/premium/enterprise`, so
tier falls back to `basic`).

> Orgs with **State & Country picklists** on: `Account.BillingCountry` must be
> a real country, so `scripts/sf_seed_cases.py` maps `AMER/EMEA/APAC` →
> `United States/United Kingdom/Australia`. `get_case` then reads that country
> straight into `account.region`.

## 3. Chatter "ask human"

When `confidence_gate` fails for a non-enterprise tier, `ask_human` posts a
Chatter FeedItem on the Case, @mentioning `ask_human.config.mention_id` (a
User `005…` or Group `0F9…` Id) if set, else the running user. If the Connect
API mention call fails it falls back to a plain FeedItem (no inline mention).
Set `mention_id` in the `flow_nodes` row (or the Phase 5 UI) to @mention a
specific person or queue.

## 4. Test data + run

```
python scripts/sf_create_fields.py       # once
python scripts/sf_seed_cases.py          # 3 Accounts + Contacts + Cases, prints Ids
python -m interpreter.run --flow 11111111-1111-1111-1111-111111111111 --sf-case 500XXXXXXXXXXXXXXX
```
With creds present the run writes for real; without them it dry-runs.

## Per-tenant credentials (Phase 12)

For multi-tenant use, put a tenant's Salesforce creds in `tenant_integrations`
instead of `.env`:

```sql
insert into tenant_integrations (tenant_id, kind, secret) values
  ('<tenant uuid>', 'salesforce',
   '{"SF_USERNAME":"...","SF_CONSUMER_KEY":"...","SF_PRIVATE_KEY_FILE":"...","SF_DOMAIN":"login"}'::jsonb);
```

`interpreter.salesforce.client_for(tenant_id)` uses that row when present,
else the `.env` client. The interpreter passes the flow's `tenant_id`
through `state` so `sf_writeback` / `ask_human` hit the right org.
