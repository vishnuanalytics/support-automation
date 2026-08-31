#!/usr/bin/env bash
# Publish the runtime (worker + cdc + poller) to a Hugging Face Docker Space.
#
#   HF_TOKEN=hf_xxx SPACE=user/space-name ./deploy/push_to_hfspace.sh
#
# Stages the *tracked* files of the current commit (so .env / sf_jwt/ are
# never included), swaps in deploy/Dockerfile as the root Dockerfile and
# deploy/README.hfspace.md as README.md (HF needs the front-matter), and
# force-pushes to the Space's main branch. Set the actual secrets
# afterwards in the Space UI: Settings -> Variables and secrets.
set -euo pipefail

: "${HF_TOKEN:?set HF_TOKEN=<a fresh write token from huggingface.co/settings/tokens>}"
: "${SPACE:?set SPACE=<user>/<space-name>}"

cd "$(dirname "$0")/.."
branch=$(git rev-parse --abbrev-ref HEAD)
echo "→ source: ${branch} @ $(git rev-parse --short HEAD)"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git archive --format=tar HEAD | tar -x -C "$work"

cp deploy/Dockerfile          "$work/Dockerfile"
cp deploy/README.hfspace.md   "$work/README.md"

( cd "$work"
  git init -q
  git checkout -q -b main
  git add -A
  git -c user.email=deploy@local -c user.name=deploy \
      commit -qm "runtime: worker + cdc + poller (deploy/run_all.py) @ ${branch} $(git -C "$OLDPWD" rev-parse --short HEAD)"
  hf_user="${SPACE%%/*}"
  echo "→ pushing to https://huggingface.co/spaces/${SPACE}"
  git push -f "https://${hf_user}:${HF_TOKEN}@huggingface.co/spaces/${SPACE}.git" main
)

cat <<EOF

→ pushed. Next, in the Space (Settings → Variables and secrets), add:
    SUPABASE_URL  SUPABASE_SERVICE_KEY  SUPABASE_ANON_KEY  GROQ_API_KEY
    SF_USERNAME  SF_CONSUMER_KEY  SF_DOMAIN  SF_HOOK_SECRET
    SF_PRIVATE_KEY   <-- the full PEM from sf_jwt/server.key, NOT a path
    EMAIL_*  (whatever your .env has for the mailbox)
    ANTHROPIC_API_KEY  (optional)
The Space rebuilds on save. Health: GET https://<space>.hf.space/  → {"ok": true, ...}
EOF
