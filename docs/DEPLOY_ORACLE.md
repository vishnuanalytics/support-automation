# Deploy the pipeline on an Oracle Cloud Always‑Free VM

Two parts. **Part A** needs the account owner (your friend, whose card is
on the account). **Part B** is the deploy — anyone with SSH access can do
it.

End state: one small always‑on VM running `docker compose up -d` →
`cdc` + `worker` + `poller` (+ optional `api`). Cost: **$0** (Always‑Free
shapes). No inbound firewall rules needed — the pipeline only makes
outbound connections.

---

## Part A — your friend (account + VM)

### A1. Create the account
1. https://www.oracle.com/cloud/free/ → **Start for free**.
2. Use **your friend's own name, address, and card** — Oracle rejects a
   card whose name doesn't match the account holder. Card is for identity;
   Always‑Free resources are never charged.
3. Pick a **home region** in your friend's country that isn't saturated
   (e.g. `ap-mumbai-1`, `ap-hyderabad-1`, `ap-singapore-1`). Permanent.

### A2. (Recommended) Upgrade to "Pay As You Go"
Billing → **Upgrade**. Still $0 for Always‑Free shapes, but the account is
then **exempt from the 7‑day idle reclamation** that deletes inactive
Free‑Tier VMs.

### A3. Create the VM
**Compute → Instances → Create instance**

| Field | Value |
|---|---|
| Name | `support-automation` |
| Image | **Canonical Ubuntu 24.04** |
| Shape | **Ampere** → `VM.Standard.A1.Flex` → **2 OCPUs / 12 GB** (Always‑Free) |
| Networking | new VCN + subnet, **Assign a public IPv4 address** ✔ |
| SSH keys | **Paste public key** → paste the key the deployer sends you (see B0) |

**Create.** If it says *"Out of host capacity"* for Ampere: change the
**Availability Domain** in the dropdown and retry, or try again a few
hours later. This is the only common snag.

### A4. Hand off
Send the deployer:
- the instance's **Public IP address** (Instance details page)
- confirm the login user is **`ubuntu`**

That's it for Part A. Nothing else needs the account owner.

---

## Part B — the deploy (you)

### B0. One‑time: send your SSH public key to your friend
On your machine:
```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C "support-automation"
cat ~/.ssh/id_ed25519.pub        # paste this to your friend for step A3
```

### B1. SSH in
```bash
ssh ubuntu@<PUBLIC_IP>
```

### B2. Install Docker
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
sudo systemctl enable --now docker
exit
```
Reconnect (`ssh ubuntu@<PUBLIC_IP>`) so the `docker` group applies. Check:
`docker run --rm hello-world`.

### B3. Get the code
Private repo → use a read‑only GitHub token (Settings → Developer settings
→ Fine‑grained tokens, this repo, Contents: read):
```bash
git clone https://<GH_TOKEN>@github.com/<you>/support-automation.git
cd support-automation
git checkout phase-20h-wire-remaining-flows      # or main once merged
```

### B4. Copy the secrets up (from your laptop, not the VM)
`.env` and `sf_jwt/` are git‑ignored, so copy them:
```bash
scp .env ubuntu@<PUBLIC_IP>:~/support-automation/.env
scp -r sf_jwt ubuntu@<PUBLIC_IP>:~/support-automation/sf_jwt
```
`docker-compose.yml` bind‑mounts `sf_jwt/` and overrides
`SF_PRIVATE_KEY_FILE` to the in‑container path, so the file just needs to
sit next to `docker-compose.yml`. No inline‑key trick needed here.

### B5. Start it
```bash
cd ~/support-automation
docker compose up -d --build
```
First build: a few minutes (arm64 wheels for `grpcio` / `onnxruntime`,
one‑time fastembed model download into a named volume). ~1.2 GB image.

**Optional — attachment OCR / video (Phase 25).** Off by default to keep the
build light. If your Cases carry screenshots or screen recordings and you
want the bot to read them, add `MEDIA=1` to `.env` before `up -d --build`:

```bash
echo "MEDIA=1" >> .env
docker compose build --no-cache        # pulls RapidOCR + faster-whisper + ffmpeg
docker compose up -d
```

That adds ~10 min to the first build and ~1.5 GB to the image (fine on the
2 OCPU / 12 GB A1; don't try it on the 1 GB AMD micro). The OCR / Whisper
models download once into the `model-cache` volume. Without `MEDIA=1` the
`attachments` node still runs — it just skips text‑in‑image and video.

### B6. Verify
```bash
docker compose ps                         # cdc / worker / poller / api = Up
docker compose logs -f cdc                # "subscribed /data/CaseChangeEvent (resume)"
docker compose logs -f worker
```
Then create or reassign a Case in Salesforce → `cdc` logs
`-> job … (case_created)` and `worker` processes it.

---

## Keep it running

Already handled: every service is `restart: unless-stopped` and Docker is
`systemctl enable`d, so after a crash or an Oracle maintenance reboot the
stack comes back and `cdc` resumes from `sf_cdc_state`. Docker log growth is
capped in `docker-compose.yml` (`json-file`, 10 MB × 3 per service).

No Postgres connection pooling needed — every service talks to Supabase over
the PostgREST HTTP API (`supabase-py`), not a direct `postgresql://` socket,
so the free-tier direct-connection cap doesn't apply.

## VM cron

The scheduled jobs that don't run inside the compose stack:

```cron
# health: alert to SLACK_ALERT_WEBHOOK if a component is stale / failing
*/5 * * * *  cd /opt/support-automation && venv/bin/python -m scripts.health_check --slack "$SLACK_ALERT_WEBHOOK" >> /var/log/sa-health.log 2>&1

# nightly: refresh the Case-resolution memory + trim old jobs/runs
30 3 * * *   cd /opt/support-automation && venv/bin/python -m ingestion.case_memory_sync --once >> /var/log/sa-sync.log 2>&1
45 3 * * *   cd /opt/support-automation && venv/bin/python -m scripts.purge_old >> /var/log/sa-sync.log 2>&1
```

(`daily-sync.yml` on GitHub Actions already runs the docs scrape + Neo4j
sync + `case_memory_sync` + `purge_old`; the cron above is the belt-and-braces
copy if you'd rather not depend on Actions, plus `health_check` which only
makes sense next to the running stack.)

## Optional — also serve the API from the VM

Only if you don't want the API on Vercel:
1. **VCN → Security Lists → Add Ingress Rule**: source `0.0.0.0/0`, TCP,
   port `8000`.
2. Open the host firewall too:
   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
   sudo netfilter-persistent save
   ```
3. Point the frontend at `http://<PUBLIC_IP>:8000` (`VITE_API_BASE`) and
   set `WEB_ORIGINS` in `.env`.

The pipeline itself needs none of this.

## Prerequisites already done

- Supabase migrations `042` (`flows.sf_entry`) and `043` (`sf_cdc_state`)
  are applied.
- `Case` + `EmailMessage` are in Setup → Change Data Capture.
- The `sf_entry` flag is currently on **"Email L0/L1 — inbound to
  Salesforce"**; move it to the router flow in the editor if that's not
  what you want the hook to run.
