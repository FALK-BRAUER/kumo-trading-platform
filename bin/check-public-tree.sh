#!/usr/bin/env bash
# check-public-tree — refuse any tree that holds what a public repository must never hold.
#
# Enforce the property, do not enumerate the files: every pattern below is a SHAPE (an account id
# shape, a hostname shape, a data-file shape), never a list of paths. A new file that violates the
# property is caught without anyone editing this script.
#
# Patterns are POSIX ERE (no \b — git grep -E on macOS does not honour it; the first version of this
# script passed a planted account id for exactly that reason). Word edges are spelled out.
#
# Exit non-zero on the first hit and print it. Nothing here prints "clean" it did not check.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

SELF=':!bin/check-public-tree.sh'
NB='[^A-Za-z0-9_]'   # a non-word character, used as a word edge
fail=0
hit() { echo "check-public-tree: $1" >&2; fail=1; }

# scan PATTERN LABEL — record a hit and print the offending lines
scan() {
  if git grep -nE "$1" -- "$SELF" >/dev/null 2>&1; then
    hit "$2"; git grep -nE "$1" -- "$SELF" >&2
  fi
}

# 1. Broker account / login shapes (IB paper "DU…", IB live "U…", Alpaca account uuid in an env line)
scan "(^|$NB)DUP?[0-9]{6,}($NB|$)"                         "IB account id shape found:"
scan "ACCOUNT_ID=[0-9a-f]{8}-[0-9a-f]{4}-"                  "account uuid assigned in a tracked file:"

# 2. Private network names (tailnet hostnames). `.local` is NOT matched: `*.env.local` is a standard
#    file suffix and the first version of this rule refused every .gitignore in the tree.
scan "(^|$NB)[a-z0-9-]+\.tail[0-9a-f]+\.ts\.net($NB|$)"  "private hostname found:"

# 3. Secret-shaped strings (Telegram bot token, Alpaca key ids, private keys)
scan "(^|$NB)[0-9]{8,10}:[A-Za-z0-9_-]{35}($NB|$)"          "telegram bot token shape found:"
scan "(^|$NB)(PK|AK)[A-Z0-9]{16,20}($NB|$)"                 "alpaca key id shape found:"
scan "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"                "private key material found:"

# 4. Market data and operating record (redistribution terms do not cover it; the record is private)
if git ls-files | grep -E '\.(csv|parquet|feather|h5)$' | grep -vE '^tests/fixtures/' >/dev/null; then
  hit "market-data file tracked outside tests/fixtures/:"; git ls-files | grep -E '\.(csv|parquet|feather|h5)$' | grep -vE '^tests/fixtures/' >&2
fi
if git ls-files | grep -E '(^|/)(zz_handoffs|FOR_[A-Z]+\.md|RUNBOOK\.md)' >/dev/null; then
  hit "operating record tracked:"; git ls-files | grep -E '(^|/)(zz_handoffs|FOR_[A-Z]+\.md|RUNBOOK\.md)' >&2
fi

# 5. Personal identifiers — the project's own list, one per line, kept OUTSIDE the public tree.
#    Export sets KUMO_PUBLIC_DENYLIST to a file of literal strings (names, logins, hosts).
if [ -n "${KUMO_PUBLIC_DENYLIST:-}" ] && [ -f "$KUMO_PUBLIC_DENYLIST" ]; then
  if git grep -niFf "$KUMO_PUBLIC_DENYLIST" -- "$SELF" >/dev/null 2>&1; then
    hit "denylist term found:"; git grep -niFf "$KUMO_PUBLIC_DENYLIST" -- "$SELF" >&2
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "check-public-tree: REFUSED" >&2
  exit 1
fi
echo "check-public-tree: 5 checks, 0 hits, $(git ls-files | wc -l | tr -d ' ') tracked files"
