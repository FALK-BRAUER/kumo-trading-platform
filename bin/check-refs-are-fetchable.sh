#!/usr/bin/env bash
# check-refs-are-fetchable.sh — asserts that every ref in versions.lock resolves at its remote.
# Takes the instance directory as $1. Exits non-zero with a sentence naming what disagreed.
# STUB: the check body is ported from the private instances repo at export time.
set -euo pipefail
inst="${1:?instance dir}"
test -d "$inst" || { echo "check-refs-are-fetchable.sh: no such instance dir: $inst" >&2; exit 1; }
echo "check-refs-are-fetchable.sh: not implemented in the scaffold" >&2
exit 2
