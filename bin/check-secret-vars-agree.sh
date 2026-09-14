#!/usr/bin/env bash
# check-secret-vars-agree.sh — asserts that every env var the compose file reads is declared in secrets.manifest, and nothing more.
# Takes the instance directory as $1. Exits non-zero with a sentence naming what disagreed.
# STUB: the check body is ported from the private instances repo at export time.
set -euo pipefail
inst="${1:?instance dir}"
test -d "$inst" || { echo "check-secret-vars-agree.sh: no such instance dir: $inst" >&2; exit 1; }
echo "check-secret-vars-agree.sh: not implemented in the scaffold" >&2
exit 2
