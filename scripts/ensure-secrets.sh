#!/usr/bin/env bash
# Adds the secrets needed by the LiteLLM admin UI + database to the existing "lab-secrets",
# ONLY if they are missing. Existing keys (Anthropic key, gateway URL, master key) are never touched.
# Safe to run any number of times; deploy.sh runs it automatically.
set -euo pipefail
NS=ai-defense-lab
KUBECTL="${KUBECTL:-kubectl}"

if ! $KUBECTL -n "$NS" get secret lab-secrets >/dev/null 2>&1; then
  echo "ERROR: secret 'lab-secrets' not found in namespace $NS." >&2
  echo "Create it first (README step 5), then run this again." >&2
  exit 1
fi

has() { [[ -n "$($KUBECTL -n "$NS" get secret lab-secrets -o jsonpath="{.data.$1}" 2>/dev/null)" ]]; }
add() { $KUBECTL -n "$NS" patch secret lab-secrets --type merge -p "{\"stringData\":{\"$1\":\"$2\"}}" >/dev/null; }
rand() { openssl rand -hex "$1"; }

# the three keys the app already needs must exist
for k in ANTHROPIC_API_KEY AIDEFENSE_GATEWAY_URL LITELLM_MASTER_KEY; do
  has "$k" || { echo "ERROR: lab-secrets is missing $k (README step 5)." >&2; exit 1; }
done

NEWPW=""
has LITELLM_DB_PASSWORD || { add LITELLM_DB_PASSWORD "$(rand 24)"; echo ">> generated LITELLM_DB_PASSWORD"; }
has LITELLM_SALT_KEY    || { add LITELLM_SALT_KEY "sk-$(rand 32)"; echo ">> generated LITELLM_SALT_KEY (never change it)"; }
if ! has UI_PASSWORD; then
  NEWPW="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 16)"
  add UI_PASSWORD "$NEWPW"
  echo ">> generated UI_PASSWORD"
fi

if [[ -n "$NEWPW" ]]; then
  echo
  echo "   LiteLLM admin UI login   user: admin    password: $NEWPW"
  echo "   (show it again any time:"
  echo "    $KUBECTL -n $NS get secret lab-secrets -o jsonpath='{.data.UI_PASSWORD}' | base64 -d; echo )"
  echo
fi
