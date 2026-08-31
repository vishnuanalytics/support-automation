# Local runtime — running the pipeline without a cloud host

The email → Salesforce pipeline needs three long-running processes. There is
no paid always-on host (no credit card), so they run on your machine in
Docker. `docker-compose.yml` defines all three from one image.

| service  | command                                    | role |
|----------|--------------------------------------------|------|
| `poller` | `python -m ingestion.email_watch --interval 15` | inbound Gmail → `jobs` table |
| `worker` | `python -m api.worker`                      | drains `jobs` → runs the flow → writes the Case → **sends the reply over SMTP** |
| `api`    | `uvicorn api.main:app --port 8000`          | receives the Salesforce → automation push (`POST /api/hooks/salesforce/case`) |

## Prerequisites

- Docker Desktop (Windows) with WSL2 integration enabled, **or** Docker
  Engine inside WSL. Set Docker Desktop to *start on login* so the stack
  comes back after a reboot.
- `.env` in the repo root — `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
  `GROQ_API_KEY`, the `SF_*` block, `SF_HOOK_SECRET`, optional
  `ANTHROPIC_API_KEY`.
- `sf_jwt/server.key` present (the Salesforce JWT bearer key). It is
  bind-mounted read-only at `/app/sf_jwt/server.key`; compose overrides
  `SF_PRIVATE_KEY_FILE` to that path because the `.env` value is a host path.
- The **Gmail app-password is not in `.env`** — it is in Supabase Vault
  (`tenant_integrations` + `integration_secret_*` RPCs). The containers
  fetch it with the `SUPABASE_*` creds.

## Run it

```bash
docker compose up -d --build     # build the image, start all three
docker compose logs -f           # watch every service
docker compose logs -f worker    # just the worker (flow runs + sends)
docker compose ps                # health / restart counts
docker compose down              # stop
```

After a code change: `docker compose up -d --build` again (only the changed
layer rebuilds).

The first `worker` run downloads the fastembed ONNX model (~80 MB) into the
`model-cache` named volume; it is reused on every later start.

## The Salesforce push webhook

`api` listens on `localhost:8000`. Salesforce needs a public URL to call it.
Use a Cloudflare quick tunnel (no account, no card):

```bash
cloudflared tunnel --url http://localhost:8000
```

Take the `https://<random>.trycloudflare.com` URL it prints and point the
`SupportAutomation` Named Credential at it (`scripts/dev_serve.sh` automates
both). The URL rotates every time `cloudflared` restarts — re-point the
Named Credential when it does.

## Outbound replies

The `worker` sends the reply **over SMTP from the support mailbox**
(`emailer.send_reply`) — that is what reaches the customer. A Salesforce API
send (`emailSimple`) needs org-wide *Deliverability = All email* plus an
Org-Wide Email Address, and silently drops the message otherwise. After a
successful SMTP send the reply is also mirrored onto the Case as an outbound
`EmailMessage` (best-effort — a failure there never blocks or retries the
send). Salesforce stays the source of truth for the Case; Gmail just carries
the message.

## Caveats / next step

- The stack only runs while your PC is on. Fine for demos; not a substitute
  for a real deployment.
- The poller **polls** every 15 s. The zero-latency upgrade is **IMAP IDLE**
  — one long-lived connection, Gmail pushes new-mail in ~1–2 s. That is a
  change to `ingestion/email_watch.py` / `interpreter/mailbox.py`, not to
  this compose file.
