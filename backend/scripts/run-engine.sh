#!/usr/bin/env bash
# Launch the kumo-trading-platform ENGINE process (issue #20) — the standalone Nautilus trading node.
# Runs independently of the UI: it keeps trading even if the UI (run-api.sh) is restarted.
# Secrets injected from the macOS keychain (never in the repo). Requires Redis up (docker-compose up -d redis)
# and — for execution — the IB Gateway running (backend/scripts/ib_gateway.py).
#
# Usage:  backend/scripts/run-engine.sh
#
# Adds:   APCA_API_KEY_ID / APCA_API_SECRET_KEY — Alpaca market-data keys (keychain services:
#                                                 alpaca-paper-key / alpaca-secret-paper) — the active
#                                                 [data].provider (#23).
#         DATABENTO_API_KEY  — Databento key (keychain: databento-kumo-trading-platform) — only if that provider
#                              is re-selected; optional otherwise.
#         IBKR_ACCOUNT_ID    — IBKR paper account id     (keychain service: ibkr-account-paper)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # backend/

read_secret() {  # service -> stdout, or fail loudly
  security find-generic-password -s "$1" -w 2>/dev/null \
    || { echo "missing keychain secret: $1 (add with: security add-generic-password -s $1 -a api_key -w <value>)" >&2; exit 1; }
}
read_secret_opt() {  # service -> stdout, empty if absent (for providers not currently selected)
  security find-generic-password -s "$1" -w 2>/dev/null || true
}

# Alpaca is the active data provider — keys are required.
export APCA_API_KEY_ID="$(read_secret alpaca-paper-key)"
export APCA_API_SECRET_KEY="$(read_secret alpaca-secret-paper)"
# Databento only if that provider is re-selected — don't block boot when it isn't.
export DATABENTO_API_KEY="$(read_secret_opt databento-kumo-trading-platform)"
# IBKR account only needed if execution is switched back to ibkr — optional now that exec=alpaca
# (the Alpaca exec client uses the APCA_* keys above).
export IBKR_ACCOUNT_ID="$(read_secret_opt ibkr-account-paper)"

# shellcheck disable=SC1091
source "$here/.venv/bin/activate"
exec python -m api.engine_node
