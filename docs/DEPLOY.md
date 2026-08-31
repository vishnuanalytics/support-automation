# Deploying the runtime

The processing pipeline is **three long-lived processes** plus an optional
HTTP API:

| Process | Command | Serves HTTP? |
|---|---|---|
| `worker` | `python -m api.worker` | no — drains the `jobs` queue |
| `cdc`    | `python -m ingestion.sf_cdc_watch` | no — outbound gRPC to Salesforce Pub/Sub |
| `poller` | `python -m ingestion.email_watch --interval 15` | no — outbound Gmail IMAP |
| `api`    | `uvicorn api.main:app --host 0.0.0.0 --port $PORT` | yes — the flow editor's backend |

`worker` / `cdc` / `poller` make only **outbound** connections, so they
need no public URL and no inbound firewall rules. `api` is only needed by
the web editor (it can also live on Vercel).

`Procfile` and `railway.json` in the repo root declare these for
buildpack/Railway-style platforms. `docker-compose.yml` (Phase 20j) runs
the same four locally or on a VM.

## Secrets that are not files

`.env` and `sf_jwt/` are git-ignored, so a git-based deploy (Railway,
Render, Fly, …) never sees them. Set every var from `.env` in the
platform's dashboard, and for the Salesforce JWT key use the **inline**
form instead of the file:

```
SF_PRIVATE_KEY   = <paste the full contents of sf_jwt/server.key, BEGIN/END lines included>
```

`interpreter/salesforce.py` prefers `SF_PRIVATE_KEY` over
`SF_PRIVATE_KEY_FILE`, so the missing file doesn't matter.

## Railway (paid — ~$5–10/month)

Railway has no free tier since 2023: a one-time $5 trial credit, then the
$5/month Hobby plan (includes $5 usage). Three always-on services land
around $6–12/month total.

1. **New Project → Deploy from GitHub repo.** With `railway.json` present
   Railway builds the `Dockerfile` and starts `python -m api.worker`.
2. **Variables:** paste all of `.env` (+ `SF_PRIVATE_KEY` inline, see
   above). A shared Variable Group can feed all services.
3. **＋ New Service** from the same repo twice more; per service →
   **Settings → Deploy → Custom Start Command**:
   - `python -m ingestion.sf_cdc_watch`
   - `python -m ingestion.email_watch --interval 15`
4. Optionally a fourth service for the API — set start command
   `uvicorn api.main:app --host 0.0.0.0 --port $PORT`, and set
   `WEB_ORIGINS` to the frontend origin. Railway assigns it a public URL;
   point the frontend's `VITE_API_BASE` at it.

The earlier "No start command detected" build error was just the missing
`Procfile` / `railway.json` — fixed by this commit.

## Oracle Cloud Always Free VM ($0)

A real always-on VM. See the step list in the deploy discussion / project
history: create an Ampere A1 (2 OCPU / 12 GB) Ubuntu VM, install Docker,
`git clone`, copy `.env` + `sf_jwt/` up with `scp`, `docker compose up -d`.
No inbound rules needed unless you also serve `api` there.

## Home box / laptop ($0)

`docker compose up -d` on any machine you keep on (Phase 20j). The CDC
replay cursor (`sf_cdc_state`, 72h retention) means an outage just delays
Cases, never drops them.
