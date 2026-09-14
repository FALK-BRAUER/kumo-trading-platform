#!/usr/bin/env bash
# Type-check the cross-repo seams, RATCHETED against a recorded baseline.
#
# WHY A RATCHET AND NOT A CLEAN RUN. Turning mypy on here today reports 77 findings across 12 files.
# Some are real, some are false positives from unannotated context several call-frames away — the
# first one investigated (`protection.py:696`, apparently `set.add(dict)`, which would be an
# unhashable-type TypeError) turned out to be mypy mis-inferring an unannotated dict. A check that
# emits 77 unverified items is a check nobody acts on, and an alarm nobody acts on is worse than
# none: the same reasoning the drift alerts use.
#
# So the baseline records what is true today and this refuses only what is NEW. The number can only
# go down, and each reduction is a deliberate act with a reason rather than a bulk cleanup.
#
# WHAT IT IS FOR. One defect family: a wrong OBJECT reaching a boundary. On 2026-08-25 that was a
# dict passed where something answering `equity()` belonged — three commits, a day of a wrong
# explanation, invisible to 2000 passing tests. The other families each need their own check and none
# of them is typing (see api/seam_types.py).
set -euo pipefail

cd "$(dirname "$0")/.."
baseline="scripts/mypy_seams.baseline"

current=$(.venv/bin/python -m mypy 2>&1 \
  | grep -E "^[^ ]+:[0-9]+: error:" | sed -E 's/:[0-9]+: error:/: error:/' | sort -u || true)

if [ "${1:-}" = "--record" ]; then
  printf '%s\n' "$current" > "$baseline"
  echo "recorded $(printf '%s\n' "$current" | grep -c .) findings as the baseline"
  exit 0
fi

[ -f "$baseline" ] || { echo "REFUSING: no $baseline — run '$0 --record' once, deliberately"; exit 1; }

new=$(comm -13 "$baseline" <(printf '%s\n' "$current") || true)
if [ -n "$new" ]; then
  echo "NEW type findings at the cross-repo seams:"
  printf '%s\n' "$new" | sed 's/^/  /'
  echo ""
  echo "NEW since the baseline. This catches one thing: a wrong OBJECT reaching a boundary."
  exit 1
fi

fixed=$(comm -23 "$baseline" <(printf '%s\n' "$current") | grep -c . || true)
fixed=${fixed:-0}
echo "  seams: no new type findings (${fixed} fixed since the baseline)"
