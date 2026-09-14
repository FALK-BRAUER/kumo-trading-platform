#!/usr/bin/env bash
# Canonical backend test command — the ONE gate. Runs the full suite in two separate processes because a
# BacktestEngine uses native globals that segfault if a second engine is built in the same interpreter
# (see api/test_app.py). The default `pytest api/` excludes `-m engine` (pyproject addopts), so the #76
# replay harness would be silently skipped by a bare `pytest`; this script guarantees it always runs.
#
#   1) default suite         — everything except the engine harness (one engine: test_app's synthetic node)
#   2) engine replay harness — the multi-engine invariant pins (#76), alone in a fresh interpreter
#
# Either pass failing fails the whole run. Use this in CI and before every push.
#
# Usage:  backend/scripts/run-tests.sh [extra pytest args...]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # backend/

# shellcheck disable=SC1091
source "$here/.venv/bin/activate"
cd "$here"

echo "==> [1/4] default suite (pytest api/, engine harness excluded)"
python -m pytest api/ "$@"

echo "==> [2/4] engine replay harness — ONE PROCESS PER FILE (#184)"
# A BacktestEngine cannot be constructed twice in one interpreter (Nautilus caches the logging
# subsystem in `kernel.py.__init__`), and `-m engine` now selects three files — so the single
# `pytest api/ -m engine` this line used to run ABORTED, and had been aborting since 2026-07-29:
# the projection-invariant tests that guard `{instrument}-{strategy}` position ids did not run for
# a month, and nothing said so. CI (tests.yml) already loops per file; this is the same loop, so
# the local gate and the CI gate cannot disagree about what "the engine suite passed" means.
#
# Exit 5 is "no tests collected" — the CORRECT outcome for a file whose engine tests are all
# `needs_services`. Treating it as failure makes a gate that can never go green.
engine_rc=0
# ASSERT THE ANCHOR WAS FOUND (review, 2026-08-29). Under `set -euo pipefail` a grep that matches
# nothing exits 1 — but a command substitution's status in a `for` list is DISCARDED, so the loop
# body never runs, engine_rc stays 0 and this step reports SUCCESS having executed nothing. That is
# the second way this gate can go green empty (the first being the deliberate exit-5 rule), and it
# is the very shape #184 was filed for: a step that ran nothing for a month, invisibly.
engine_files=$(grep -rl "mark.engine\|pytestmark.*engine" api --include='test_*.py' || true)
if [ -z "$engine_files" ]; then
    echo "engine replay harness FOUND NO FILES — discovery is broken, not the suite"; exit 1
fi
for f in $engine_files; do
    echo "    -> $f"
    python -m pytest -m "engine and not needs_services" -q "$f" "$@" && s=0 || s=$?
    if [ "${s:-0}" -ne 0 ] && [ "${s:-0}" -ne 5 ]; then engine_rc=1; fi
done
[ "$engine_rc" -eq 0 ] || { echo "engine replay harness FAILED"; exit 1; }

# Integration passes need a live Redis/Postgres — run only when configured, else skip (the offline suites
# above are the always-on gate). Requires `alembic upgrade head` against KUMO_DATABASE_URL first. The restart
# equality test (engine AND needs_services) gets its OWN process — Nautilus's pyo3 Redis load breaks after a
# prior asyncio.run(), so it can't share the async postgres pass.
if [ -n "${KUMO_DATABASE_URL:-}" ] || [ -n "${KUMO_REDIS_HOST:-}" ]; then
    echo "==> [3/4] integration — async/postgres (pytest -m 'needs_services and not engine')"
    python -m pytest api/ -m "needs_services and not engine" "$@"
    echo "==> [4/4] restart equality — isolated (pytest -m 'needs_services and engine')"
    python -m pytest api/ -m "needs_services and engine" "$@"
else
    echo "==> [3-4/4] integration SKIPPED (no KUMO_DATABASE_URL / KUMO_REDIS_HOST set)"
fi

echo "==> all backend tests passed"
