#!/usr/bin/env python3
"""merge_gate — the enforcement point for merging a PR.

    python scripts/merge_gate.py <pr-number> [--merge] [--suite-cmd "..."] [--no-comment] [--skip-pin]

  1. HEAD == @{u}, or refuse and name both shas                (a stale head is unmergeable)
  2. print the resolved kumo_strategies revision and whether it equals the pin in pyproject
  3. run the suite; parse the summary line; refuse on any "failed" or "error"
  3b. run it again against the PIN in a throwaway checkout unless --skip-pin; a difference between two
      green runs is a finding, a red pin run is a refusal
  4. print the counts in the form the PR comment wants, and post them (unless --no-comment)
  4b. before --merge: the head must not have moved since the suite ran
  5. after --merge: assert `git diff <reviewed> origin/main -- <the PR's files>` is empty
  6. exit non-zero at every refusal; the success string is printed on the zero path only

A process's exit code is not a statement about the world: the merge is verified by the PR's state and
by the diff, never by the exit code of the command that asked for it. Every refusal is a sentence
naming what disagreed. Nothing here prints "ok" it did not check.

SCAFFOLD: the body is ported from the private repository at export time.
"""
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    print("merge_gate: not implemented in the scaffold", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
