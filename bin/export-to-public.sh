#!/usr/bin/env bash
# export-to-public — one-way, scrubbed, squashed snapshot of a private ref into the public repo (#1048).
#
#   bin/export-to-public.sh <private-ref> <public-clone-dir> [--push]
#
# LOCAL BY DEFAULT. Nothing reaches a remote unless --push is given (2026-09-14: "we do not push
# anything to any of the new repos before we are ready. clean history. we need to scan everything
# before."). The initial import is ONE commit pushed to a freshly created remote once the exported tree
# has been scanned and read; this script prepares that tree locally and stops.
#
# WHAT IT DOES, in order — and refuses at the first step that fails, printing why:
#   1. Archives <private-ref> (git archive: the COMMITTED tree, never the working tree — an export of
#      uncommitted edits would have no provenance).
#   2. Deletes every path in the private tree's `.public-exclude` from the archive. That file is the
#      ONE list of what the export omits, and it is the same list `bin/check-public-tree.sh` skips when
#      it runs on the private tree — so a private commit that passed the check cannot produce a public
#      tree that fails it (the "one list, two consumers" property; until this script existed the list
#      had one consumer).
#   3. Runs `bin/check-public-tree.sh` on the archive — as a git repo, because the check reads
#      `git ls-files` / `git grep` — with the denylist. Refuses on any hit.
#   4. Syncs the archive over the public clone's working tree (`rsync --delete --checksum`), KEEPING
#      the files the public scaffold owns: LICENSE, README, CONTRIBUTING, CLAUDE.md (rules-only), CI,
#      `.claude/` and `bin/hooks/` — so the PRIVATE gate and Claude config never reach the public tree
#      by design — `instances/` (the public example instance lives only there), and the dependency
#      manifests whose pins name public packages the private tree does not. Every keep entry must
#      EXIST in the public clone: a keep entry naming nothing is an exclusion in disguise (peer
#      review P7). A public clone that carries a `.public-exclude` of its own is refused — it would
#      blind the second check to whatever it names (P1). The keep list is `.export-keep` in the public clone when present, else the
#      default below. Everything else is the private tree's, and a file the private tree no longer has
#      is deleted from the public one.
#   5. Runs the check AGAIN on the public clone (a kept file could carry a hit; the denylist file
#      could have been copied in). Refuses on any hit.
#   6. Commits on branch `export/<private-short-sha>` with the provenance in the body: the private
#      sha, the previous export's sha if the public tree records one, and the counts. Pushes and opens
#      a PR unless --no-push. The PR is squash-merged by a person; this script never merges.
#
# NEVER THE REVERSE. Public→private is a merge a person performs; nothing here reads the public tree
# into the private one. `.public-last-export` in the public tree records the private sha exported, so
# the next export's body can name the range.
set -euo pipefail

REF="${1:?usage: $0 <private-ref> <public-clone-dir> [--no-push]}"
PUB="${2:?usage: $0 <private-ref> <public-clone-dir> [--no-push]}"
PUSH=0; [ "${3:-}" = "--push" ] && PUSH=1

PRIV="$(git rev-parse --show-toplevel)"
cd "$PRIV"
SHA="$(git rev-parse --verify "$REF^{commit}")" || { echo "export: no such ref: $REF" >&2; exit 1; }
SHORT="${SHA:0:7}"
[ -d "$PUB/.git" ] || { echo "export: $PUB is not a git clone" >&2; exit 1; }
[ -x bin/check-public-tree.sh ] || { echo "export: bin/check-public-tree.sh missing or not executable" >&2; exit 1; }
if [ -z "${KUMO_PUBLIC_DENYLIST:-}" ]; then
  echo "export: KUMO_PUBLIC_DENYLIST is not set — the export refuses to run without the literal denylist" >&2; exit 1
fi
[ -f "$KUMO_PUBLIC_DENYLIST" ] || { echo "export: denylist not found: $KUMO_PUBLIC_DENYLIST" >&2; exit 1; }

die() { echo "export: REFUSED — $*" >&2; exit 1; }

# The public clone must be on main and clean: an export onto local edits would fold them in, and an
# export branched from the previous export branch forks the history instead of stacking on main (S1).
( cd "$PUB" && [ -z "$(git status --porcelain)" ] ) || die "$PUB has local changes; export onto a clean clone"
( cd "$PUB" && [ "$(git branch --show-current)" = "main" ] ) || die "$PUB is not on main ($(cd "$PUB" && git branch --show-current)); the export branches from main"
[ ! -e "$PUB/.public-exclude" ] || die "$PUB carries a .public-exclude — the second check would skip whatever it names (P1); the public tree has no exclusions"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
ARCH="$WORK/tree"; mkdir -p "$ARCH"

# 1. the committed tree, exactly. `git archive` honours `export-ignore` and `export-subst` attributes
#    and drops submodules — three undeclared exclusion lists. Refuse them so "export = ref − .public-exclude"
#    holds by construction (P2). Symlinks ship their TARGET string, which git grep never reads — a
#    link to a path under a home directory would pass every rule (M2). Refused here and in the check.
attrs=$(git ls-tree -r --name-only "$SHA" | git check-attr --stdin export-ignore export-subst | grep -vE ': (unspecified|unset)$' || true)
[ -z "$attrs" ] || die "the ref carries export-ignore/export-subst attributes, an exclusion list this script does not read:
$attrs"
subs=$(git ls-tree -r "$SHA" | awk '$1 == "160000" {print $4}')
[ -z "$subs" ] || die "the ref carries submodules, which git archive drops silently: $subs"
links=$(git ls-tree -r "$SHA" | awk '$1 == "120000" {print $4}')
[ -z "$links" ] || die "the ref tracks symlinks, whose targets no check reads: $links"
git archive --format=tar "$SHA" | tar -x -C "$ARCH"

# 2. drop the excluded paths — read from the ARCHIVED .public-exclude, not the working tree's
EXCL="$ARCH/.public-exclude"
[ -f "$EXCL" ] || die "the ref has no .public-exclude; nothing says what the export omits"
n_excl=0
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  case "$line" in /*|*..*) die ".public-exclude entry is absolute or parent-relative: $line" ;; esac
  rm -rf "${ARCH:?}/$line"; n_excl=$((n_excl+1))
done < "$EXCL"
rm -f "$EXCL"

# 3. the check, on the archive as a git repo (it reads git ls-files / git grep)
( cd "$ARCH" && git init -q && git add -A && git -c user.email=export@invalid -c user.name=export commit -qm snapshot ) \
  || die "could not stage the archive for the check"
( cd "$ARCH" && bash "$PRIV/bin/check-public-tree.sh" ) || die "the private ref's export set fails check-public-tree (see above)"
rm -rf "$ARCH/.git"

# 4. keep list: the public scaffold's own files
KEEP_DEFAULT='LICENSE
LICENSE.GPL-3.0
README.md
CONTRIBUTING.md
CLAUDE.md
.gitignore
.editorconfig
.pre-commit-config.yaml
.github/
.claude/
bin/hooks/
instances/
backend/pyproject.toml
.export-keep
.public-last-export'
if [ -f "$PUB/.export-keep" ]; then KEEP="$(grep -vE '^\s*(#|$)' "$PUB/.export-keep")"; else KEEP="$KEEP_DEFAULT"; fi
RSYNC_EXCL=(--exclude '.git/' --exclude '/.export-keep' --exclude '/.public-last-export')
# The clone's IGNORED files (node_modules, a venv) are not the export's to delete (P5). rsync's
# `:- .gitignore` filter reads the SENDER's ignore files, so it is asked of git instead: every ignored
# entry in the clone becomes an exclude.
while IFS= read -r ig; do [ -n "$ig" ] && RSYNC_EXCL+=(--exclude "/$ig"); done < <(cd "$PUB" && git ls-files --others --ignored --exclude-standard --directory)
missing=""
while IFS= read -r k; do
  [ -n "$k" ] || continue
  case "$k" in .export-keep|.public-last-export) ;;            # may not exist yet, by design
    .public-exclude) die ".public-exclude may not be a keep entry (P1)" ;;
    *) [ -e "$PUB/$k" ] || missing="$missing $k" ;;
  esac
  RSYNC_EXCL+=(--exclude "/$k")
done <<< "$KEEP"
[ -z "$missing" ] || die "keep entries that do not exist in the public clone — an exclusion in disguise (P7):$missing"
# --checksum, not the size+mtime quick check: git archive stamps every file with the commit time, and
# a public file of the same size stamped within the same second is skipped by `-a` alone — the test
# for "the private content wins" passed once and then failed three times running, on a one-byte edit.
rsync -a --delete --checksum "${RSYNC_EXCL[@]}" "$ARCH/" "$PUB/"

# 5. the check again, on the public clone with everything in place
if ! ( cd "$PUB" && git add -A && bash "$PRIV/bin/check-public-tree.sh" ); then
  ( cd "$PUB" && git reset -q --hard && git clean -qfd )      # leave the clone as it was found (S2)
  die "the public tree fails check-public-tree after sync (see above); clone restored"
fi

# 6. commit with provenance
cd "$PUB"
PREV="$(cat .public-last-export 2>/dev/null || true)"
echo "$SHA" > .public-last-export; git add .public-last-export
if git diff --cached --quiet; then echo "export: nothing to export — public tree already at $SHORT"; exit 0; fi
n_files=$(git diff --cached --name-only | wc -l | tr -d ' ')
BR="export/$SHORT"
git checkout -q -B "$BR"
git -c user.name="${GIT_AUTHOR_NAME:-$(git config user.name)}" commit -q -F - <<MSG
chore(export): snapshot of the private ref $SHORT

One-way export from the private platform repository (kumo-trading-platform issue 1048).
  private ref:      $SHA
  previous export:  ${PREV:-none — first export}
  files changed:    $n_files
  excluded paths:   $n_excl (the private tree's .public-exclude)
  kept (public):    $(printf '%s' "$KEEP" | tr '\n' ' ')

Scrubbed and checked twice: on the export set before sync and on this tree after. Squash-merge
this PR; do not rebase or cherry-pick from it. Public→private changes are merged by hand, never by
this script.
MSG
echo "export: committed $BR ($n_files files) in $PUB"
if [ "$PUSH" -eq 1 ]; then
  git push -q -u origin "$BR"
  gh pr create --base main --head "$BR" --title "chore(export): snapshot of the private ref $SHORT" \
    --body "$(git log -1 --format=%b)" 2>&1 | tail -1
fi
