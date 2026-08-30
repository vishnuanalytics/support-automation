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

## 2b. Routing queues + Case picklists — `scripts/sf_support_setup.py` (Phase 20g)

```
python scripts/sf_support_setup.py            # everything (idempotent)
python scripts/sf_support_setup.py --only queues
python scripts/sf_support_setup.py --dry-run
```

Via the Metadata API (needs "Modify Metadata" — System Administrator has it):

**Queues** (Case assigned, you're added as the sole member — add real
agents in Setup): per-team `Team_Email` / `Team_Support` / `Team_CSM` /
`Team_Sales` / `Team_Offboarding`, and per-reason `Support_L0L1` /
`Billing_Escalations` / `Enterprise_Support` / `Support_Tier2`. The
`handover` node's `config.queue` and `ask_human`'s `config.queue` /
`config.escalate_queue` (the latter used when the gate forced the
escalation on topic) name one of these; the node resolves it by
DeveloperName or Name and sets `Case.OwnerId` (migration `038` wires the
seeded flows).

**Case picklists**: `Case.Type` → `Question / How-to / Problem·Bug /
Billing / Account·Login / Feature Request / Other`; `Case.Status` gains
`Waiting on Customer`. `Case.Module__c` / `Case.Region__c` become
**restricted picklists** (Text → Picklist is a drop-and-recreate); new
`Case.SubModule__c` (picklist dependent on `Module__c`) and
`Case.Topic__c` Text(255). `sf_writeback` fills them via
`salesforce.map_case_fields(topic, country)` — `Topic__c` always gets the
classifier's raw slug; `Module__c` / `SubModule__c` / `Region__c` are a
best-effort keyword mapping and left blank when nothing matches.

## 2c. Case-router workflow + team roster (Phase 20i)

```
python scripts/sf_seed_teams.py        # 2 Users + 13 Contacts, Team__c / TeamRole__c
python scripts/seed_router_flow.py     # the router flow -> published
```

**Router flow** ("Case router — team routing + tag manager", team `router`):
`identify → sf_case → retrieve → classify → team_route → sf_writeback →
draft → confidence_gate → {auto_reply | ask_human | handover}`.
`team_route` picks **support / csm / sales / offboarding** from keyword
rules over the case (renewal/expansion → csm, pricing/pre-sales → sales,
cancellation/data-export → offboarding, else support); `ask_human` /
`handover` resolve the Salesforce queue from `routed_team`
(`queue_by_team` config) — a routed team keeps its own case, `support`
billing cases go to `Billing_Escalations`, enterprise → `Enterprise_Support`.

**Team roster** — Dev Edition caps Salesforce Users at 4, so: 2 real
Users (`Sam Rivera` = Support manager, `Casey Lin` = CSM manager, each in
their `Team_*` queue) + 13 Contacts on the *"Internal — Support Teams"*
Account tagged `Contact.Team__c` + `Contact.TeamRole__c` (each team = 1
Manager + 2 Members).

## 2d. Salesforce → automation **push** (Phase 20i)

The automation exposes `POST /api/hooks/salesforce/case` — body
`{"case_id": "..."}`, header `X-SF-Hook-Secret: <SF_HOOK_SECRET from .env>`.
It pulls the Case, resolves the published `router` flow, and queues a
`run_flow` job (deduped on the Case Id). 401 without the secret.

Wire it from Salesforce (no Apex), once the API has a public URL
(a deploy, or `cloudflared tunnel --url http://localhost:8000`):

1. **Setup → Named Credentials → New** — Label `SupportAutomation`, URL =
   the public API base, Identity Type *Anonymous* (the shared secret is
   the auth). Optionally add a Custom Header `X-SF-Hook-Secret` = the
   secret so the Flow doesn't carry it.
2. **Setup → Flows → New → Record-Triggered Flow** on **Case**, *After
   Save*, **A record is created**, entry condition `Status Equals New`
   (optionally `AND Origin Not Equal Email` to skip bot-created email
   Cases).
3. Add an **HTTP Callout** action → `POST` to
   `callout:SupportAutomation/api/hooks/salesforce/case`, body
   `{ "case_id": "{!$Record.Id}" }`, header `X-SF-Hook-Secret: <secret>`
   (skip the header if you set it on the Named Credential).
4. Activate the Flow.

Now a Case created in Salesforce triggers the router in seconds. Until the
URL is public, test with `curl` against `localhost:8000` (see above).

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
