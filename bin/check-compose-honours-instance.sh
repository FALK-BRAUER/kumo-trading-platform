#!/usr/bin/env bash
# check-compose-honours-instance.sh — asserts that the compose stack reads its ports, volumes and project name from the instance, not from defaults.
# Takes the instance directory as $1. Exits non-zero with a sentence naming what disagreed.
# STUB: the check body is ported from the private instances repo at export time.
set -euo pipefail
inst="${1:?instance dir}"
test -d "$inst" || { echo "check-compose-honours-instance.sh: no such instance dir: $inst" >&2; exit 1; }
echo "check-compose-honours-instance.sh: not implemented in the scaffold" >&2
exit 2
