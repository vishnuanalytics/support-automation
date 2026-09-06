# Deploy the web editor + public API on an Oracle Always-Free VM

`DEPLOY_ORACLE.md` covers the **outbound-only** stack (worker / cdc / poller /
slackbot). This file adds the two things it treats as optional: the
**FastAPI API reachable over HTTPS** and the **React editor served from a
CDN**, so the whole product runs continuously at **$0**.

> Parked: needs an Oracle account (credit card for identity — Always-Free
> shapes are never charged). Everything below is ready to run when that
> exists.

---

## End state

```
Oracle Always-Free VM (Ubuntu 24.04, Ampere A1 — 2 OCPU / 12 GB)
├── docker compose  →  api (uvicorn :8000) + worker + cdc + poller + slackbot
│                       every service `restart: unless-stopped`   ← always-on
└── Caddy (systemd) →  automatic TLS + reverse proxy
                        api.<domain>  →  localhost:8000
Frontend  →  Cloudflare Pages (free global CDN + TLS)             ← recommended
             or served from the same VM by Caddy
Supabase  ←  every process talks to it over the PostgREST HTTPS API
```

The "continuously running" part is `restart: unless-stopped` in
`docker-compose.yml` + `systemctl enable docker` + Caddy as a systemd unit.
After a crash or an Oracle maintenance reboot the whole thing comes back on
its own; `cdc` resumes from `sf_cdc_state`.

---

## 1. VM + the Docker stack

Do `DEPLOY_ORACLE.md`:

- **Part A** — the account owner creates the VM. Shape: **`VM.Standard.A1.Flex`,
  2 OCPUs / 12 GB** (Always-Free). If it says *"Out of host capacity"*,
  switch the Availability Domain and retry, or wait a few hours.
- **Part B, steps B1–B5** — install Docker, `git clone`, `scp` the `.env`
  and `sf_jwt/` up, `docker compose up -d --build`, pre-warm the embedding
  model.

After B5: `docker compose ps` shows `api / worker / cdc / poller` = `Up`.

---

## 2. A hostname (pick one)

| Option | Cost | Setup |
|---|---|---|
| **`sslip.io`** | $0 | `api.<VM_PUBLIC_IP>.sslip.io` already resolves to the VM. Caddy still issues a real Let's Encrypt cert for it. Nothing to configure. |
| Porkbun / Namecheap | ~$1–10 / yr | Real domain → add an `A` record `api → <VM_PUBLIC_IP>` (and `app → …` if serving the frontend from the VM). |
| Cloudflare (a domain you already own) | $0 extra | Free DNS + proxy. |

The rest uses `api.example.com` — substitute your `sslip.io` host or real
domain.

---

## 3. Open the firewall — two layers on Oracle

**OCI Console → Networking → your VCN → Security Lists → Default Security
List → Add Ingress Rules:**

| Source | Protocol | Dest port |
|---|---|---|
| `0.0.0.0/0` | TCP | `443` |
| `0.0.0.0/0` | TCP | `80` (Let's Encrypt HTTP-01 challenge) |

**On the VM** (Oracle's Ubuntu image ships iptables locked down):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

---

## 4. Caddy — TLS + reverse proxy for the API

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`:

```
api.example.com {
    reverse_proxy localhost:8000
}
```

```bash
sudo systemctl restart caddy        # obtains the cert on first start; auto-renews
curl https://api.example.com/api/health
```

---

## 5. CORS — let the frontend call the API

The API reads `WEB_ORIGINS` (comma-separated, `api/main.py`). On the VM,
add to `~/support-automation/.env`:

```
WEB_ORIGINS=https://app.example.com
```

(the frontend URL from step 6). Then:

```bash
cd ~/support-automation && docker compose up -d api
```

---

## 6. Frontend — build + host

The editor is a **static bundle**; `VITE_API_BASE` is compiled in at build
time (`web/src/api.ts` — empty = dev proxy only).

### Recommended — Cloudflare Pages (free, keeps load off the VM)

1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Connect to
   Git** → this repo.
2. Build config:
   - **Build command:** `cd web && npm ci && npm run build`
   - **Output directory:** `web/dist`
   - **Env var:** `VITE_API_BASE = https://api.example.com`
3. Deploy → `https://<project>.pages.dev` (or bind a custom `app.example.com`).
   Re-builds on every push automatically.
4. Put that URL in `WEB_ORIGINS` (step 5).

### Alternative — serve from the same VM via Caddy

```bash
cd ~/support-automation/web
echo "VITE_API_BASE=https://api.example.com" > .env.production
npm ci && npm run build
sudo mkdir -p /var/www/app && sudo cp -r dist/* /var/www/app/
```

Add to `/etc/caddy/Caddyfile`:

```
app.example.com {
    root * /var/www/app
    try_files {path} /index.html      # SPA history fallback
    file_server
}
```

`sudo systemctl restart caddy`. (Needs an `app` DNS record; port 443 is
already open.)

---

## 7. Update OAuth redirect URIs

Anything that redirects back into the app now points at the public host —
set it in **both** the provider console **and** the matching `.env` var,
then `docker compose up -d api`:

| Provider | Redirect URI | `.env` var |
|---|---|---|
| Google (Drive/Docs/Sheets + KB write-back) | `https://api.example.com/api/integrations/google/callback` | `GOOGLE_REDIRECT_URI` |
| Google (email channel, if used) | `https://api.example.com/api/integrations/email/google/callback` | `EMAIL_GOOGLE_REDIRECT_URI` |
| Slack | `https://api.example.com/api/integrations/slack/callback` | `SLACK_REDIRECT_URI` |
| Salesforce connected app (if using SF OAuth) | `https://api.example.com/api/integrations/salesforce/callback` | `SF_REDIRECT_URI` |

GitHub / Linear / Nolt use API keys (no redirect).

---

## 8. Keep it current (optional)

Auto pull + redeploy on push — a VM cron:

```cron
*/10 * * * * cd /home/ubuntu/support-automation && git pull --ff-only && docker compose up -d --build >> /var/log/sa-deploy.log 2>&1
```

Also run the `DEPLOY_ORACLE.md` "VM cron" block (health check, nightly
`pg_dump` backup to the boot volume, purges).

---

## 9. Verify it's all continuous

```bash
docker compose ps                    # api/worker/cdc/poller = Up, RestartPolicy set
systemctl is-enabled docker caddy    # both -> enabled
sudo reboot
# reconnect after ~1 min:
docker compose ps                    # everything back up on its own
curl https://api.example.com/api/health
```

---

## Cost

| Piece | Where | $ |
|---|---|---|
| API + worker + cdc + poller + slackbot | Oracle Always-Free A1 VM | 0 |
| Caddy TLS | same VM (Let's Encrypt) | 0 |
| Frontend | Cloudflare Pages | 0 |
| DB | Supabase free tier | 0 |
| Hostname | `sslip.io` | 0 |
| Neo4j (optional) | Neo4j Aura Free, or self-host on the same VM | 0 |

Only real cost is a domain if you want a vanity URL (~$1–10/yr). Everything
functional is $0.
