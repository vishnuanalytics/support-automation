---
title: Support Automation Runtime
emoji: "🛠️"
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# support-automation runtime

`worker` + Salesforce CDC subscriber + email poller, supervised in one
container (`deploy/run_all.py`). Health JSON at `/`.

## Deploy

1. Create a **Docker Space** on huggingface.co (free, no card).
2. Push this repo to it: `git remote add space https://huggingface.co/spaces/<you>/<name>` then
   `git push space HEAD:main`. HF builds `deploy/Dockerfile` **only if it
   is the Space's `Dockerfile`** — either copy `deploy/Dockerfile` to the
   Space repo root, or make this GitHub repo public and use a 3-line
   Space that `git clone`s it (see `docs/DEPLOY.md`).
3. **Settings → Variables and secrets** — add every var from `.env`:
   `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_ANON_KEY`,
   `GROQ_API_KEY`, `SF_USERNAME`, `SF_CONSUMER_KEY`, **`SF_PRIVATE_KEY`**
   (paste the full PEM from `sf_jwt/server.key` — not the file path),
   `SF_DOMAIN`, `SF_HOOK_SECRET`, and the email vars. Optionally
   `ANTHROPIC_API_KEY`.
4. Free Spaces pause after **48 h with no HTTP traffic** — add a free
   UptimeRobot / cron-job.org monitor hitting the Space URL every 30 min.

## Health

`GET /` → `{"ok": true, "procs": {"worker": {...}, "cdc": {...}, "poller": {...}}}`
