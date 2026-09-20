#!/usr/bin/env bash
# Launch the kumo-trading-platform UI process — the FastAPI/WebSocket server (issue #20).
# This process holds NO Nautilus node and NO vendor secrets: it only reads the engine's Redis `ui:stream`
# (see api/consumer.py) and serves the browser. Crash/restart it freely — the engine (run-engine.sh) is
# untouched. Requires Redis up (docker-compose up -d redis); the engine process supplies the data.
#
# Usage:  backend/scripts/run-api.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # backend/

# shellcheck disable=SC1091
source "$here/.venv/bin/activate"
exec python -m api
