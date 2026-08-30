#!/usr/bin/env bash
# One command for a live demo with NO cloud account / credit card:
#   API + worker locally, a Cloudflare quick tunnel for a public HTTPS URL,
#   and the Salesforce Named Credential re-pointed at it.
#
#   ./scripts/dev_serve.sh
#
# A Case created in Salesforce (Status=New, Origin != Email) then routes
# through the flow in ~15 s — real-time, no polling. Ctrl-C stops everything.
# The trycloudflare URL changes each run, so this re-points Salesforce every
# time (needs SF creds + SF_HOOK_SECRET in .env).
set -euo pipefail
cd "$(dirname "$0")/.."
source venv/bin/activate 2>/dev/null || true

pids=()
cleanup() { echo; echo "stopping…"; kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "→ API on :8000"
uvicorn api.main:app --port 8000 --log-level warning & pids+=($!)
echo "→ worker"
python -m api.worker & pids+=($!)

echo "→ cloudflare tunnel"
tlog=$(mktemp)
cloudflared tunnel --url http://localhost:8000 --no-autoupdate > "$tlog" 2>&1 & pids+=($!)
url=""
for _ in $(seq 1 20); do
  url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$tlog" | head -1 || true)
  [ -n "$url" ] && break; sleep 2
done
[ -z "$url" ] && { echo "tunnel URL not found — see $tlog"; exit 1; }
echo "→ public URL: $url"

echo "→ pointing Salesforce at it"
python scripts/sf_deploy_case_hook.py "$url" | tail -1

echo
echo "LIVE.  Create a Case in Salesforce (Origin=Web, Status=New) and watch it route."
echo "Ctrl-C to stop."
wait
