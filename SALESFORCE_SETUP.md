# Salesforce setup (Phase 3)

The `sf_writeback` node and the Chatter "ask human" escalation talk to a
personal **Salesforce Developer Edition** org (free). Everything degrades to
a dry-run when creds are absent, so this is only needed for real writes.

## 1. Credentials → `.env`

Username–password–token flow (`simple-salesforce`):

```
SF_USERNAME=you@example.com
SF_PASSWORD=your-password
SF_SECURITY_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxx
SF_DOMAIN=login          # 'test' for a sandbox
```

Reset the security token in Salesforce: **Settings → My Personal
Information → Reset My Security Token** (it's emailed). `.env` is gitignored.

With these set, `interpreter/salesforce.py` makes real API calls; without
them it logs the intended call and returns `dry_run=True`.

## 2. Custom fields the reference flow expects

The seed flow's `sf_writeback` config (migration `008`) maps:

| triage output | Case field | notes |
|---|---|---|
| `classification.urgency` | `Priority` | standard; mapped `critical/high→High`, `normal→Medium`, `low→Low` |
| `classification.topic` | `Module__c` | **custom** — create it |
| `region` | `Region__c` | **custom** — create it |
| `classification.summary` | `Description` | standard; appended as `[triage] …` |

Create the two custom fields (**Setup → Object Manager → Case → Fields &
Relationships → New**):

- **`Module__c`** — Text(120), or a Picklist if you want controlled values
  (`billing`, `authentication`, `product-usage`, …).
- **`Region__c`** — Text(80), or a Picklist (`AMER`, `EMEA`, `APAC`, …).

If you skip this step the write is **tolerant**: the node drops the unknown
field, still writes `Priority` + `Description`, and lists the dropped field
under `skipped` in the trace.

### Optional: `Account.Tier__c`

`classify` reads the customer tier from `account.customer_type`. When a Case
is pulled with `--sf-case`, that comes from `Account.Tier__c` if it exists,
else the standard `Account.Type` picklist (whose values won't be
`basic/premium/enterprise`, so tier falls back to `basic`). For a faithful
demo add a **`Tier__c`** picklist on Account with values
`basic` / `premium` / `enterprise`.

## 3. Chatter "ask human"

When `confidence_gate` fails for a non-enterprise tier, `ask_human` posts a
Chatter FeedItem on the Case, @mentioning:

- `config.mention_id` on the `ask_human` node, if set (a User Id `005…` or
  Group Id `0F9…`), else
- the running user (`chatter/users/me`).

To @mention a specific person/queue, set `mention_id` in the `ask_human`
node's `config` (edit the `flow_nodes` row, or via the Phase 5 UI later).

## 4. Test data

Either point at existing Cases, or create a few:

```
python scripts/sf_seed_cases.py          # creates 3 Accounts + Contacts + Cases, prints Ids
python -m interpreter.run --flow 11111111-1111-1111-1111-111111111111 --sf-case 500XXXXXXXXXXXXXXX
```
