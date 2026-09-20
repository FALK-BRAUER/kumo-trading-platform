#!/usr/bin/env bash
# Claude Code hook. Reads the tool event as JSON on stdin. Exit 0 = proceed, exit 2 = block (stderr
# goes back to the model). Never pipe a gate's exit status through anything.
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
EVENT="$(cat)"
field() { printf '%s' "$EVENT" | python3 -c 'import json,sys; e=json.load(sys.stdin); print(e.get("tool_input",{}).get(sys.argv[1],""))' "$1"; }

FILE="$(field file_path)"
[ -n "$FILE" ] || exit 0
case "$FILE" in
  *.py) ruff format "$FILE" >/dev/null 2>&1 || true; case "$FILE" in */test_*.py) echo 'Reminder: run the test you just wrote and watch it fail before the fix' ;; */api/*.py) echo "Hint: backend API changed — regenerate the TS client (cd ui && npm run gen:api)" ;; esac ;;
  *.test.ts|*.test.tsx) (cd ui && npx --no-install prettier --write "$FILE" >/dev/null 2>&1) || true; echo 'Reminder: run the test you just wrote and watch it fail before the fix' ;;
  *.ts|*.tsx) (cd ui && npx --no-install prettier --write "$FILE" >/dev/null 2>&1) || true ;;
esac
exit 0
