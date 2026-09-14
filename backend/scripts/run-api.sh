#!/usr/bin/env bash
# Start the API for one instance. Secrets are resolved by name from the manifest and injected as env;
# values never touch a file.
set -euo pipefail
export KUMO_BIND_HOST="${KUMO_BIND_HOST:-127.0.0.1}"
export KUMO_PORT="${KUMO_PORT:-8000}"
exec uv run uvicorn api.app:app --host "$KUMO_BIND_HOST" --port "$KUMO_PORT"
