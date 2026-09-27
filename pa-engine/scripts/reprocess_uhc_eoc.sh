#!/usr/bin/env bash
# Run overview extract+judge for the United EOC outside Cursor's proxy.
# Usage (from repo root, in Terminal.app / iTerm — not the agent sandbox):
#   bash pa-engine/scripts/reprocess_uhc_eoc.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
POLICY="${1:-f4322d7a-6218-422f-8e8e-c289c9e25106}"
cd "$ROOT/pa-engine"
set -a
# shellcheck disable=SC1091
[ -f ../.env ] && . ../.env
set +a
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy

export RUN_MAX_SECONDS="${RUN_MAX_SECONDS:-7200}"
export RUN_MAX_MODEL_CALLS="${RUN_MAX_MODEL_CALLS:-800}"
export SECTION_PAGE_CAP="${SECTION_PAGE_CAP:-120}"
export TIMEOUT_EXTRACT_SECONDS="${TIMEOUT_EXTRACT_SECONDS:-180}"
export CONTEXT_GROUP_PAGES="${CONTEXT_GROUP_PAGES:-2}"

echo "Reprocess (full overview) $POLICY …"
curl -sS -X POST "http://127.0.0.1:8000/policies/${POLICY}/reprocess?full=true"
echo
echo "Watch /admin/policies/${POLICY} — rows should gain ACCURATE/Needs a look from extract+judge, not Import."
