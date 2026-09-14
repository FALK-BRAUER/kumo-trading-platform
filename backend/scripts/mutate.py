"""Apply one mutation to one file, or REFUSE and say why.

WHY THIS EXISTS. Mutation testing is the only thing that has reliably caught undiscriminating tests in
this codebase — code review has caught approximately none. But the technique has a silent failure mode
that was hit twice on 2026-08-22 in kumo-strategies and nearly cost two real guards:

    A MUTATION THAT DOES NOT MUTATE READS EXACTLY LIKE A SURVIVING GUARD.

Both cases reported SURVIVED and both were the harness, not the test:

  * an anchor on `self._j(RISK,` — a substring appearing many times, so `replace(a, b, 1)` silently hit
    a DIFFERENT call site than the one being probed. The intended line was never mutated, the suite
    stayed green, and the conclusion "this guard is untested" was wrong.
  * a replacement of `"" or X`, which evaluates to `X`. The file changed; the behaviour did not.

Both are invisible from the result: green is green. So the checks are made mechanical here rather than
remembered, and every refusal names the specific hazard rather than saying "invalid".

Usage:
    python scripts/mutate.py <file> <anchor> <replacement>
    python scripts/mutate.py --restore <file>

Exit 0 = applied. Exit 2 = refused (and NOTHING was written). A refusal is a finding about the
mutation, not about the code under test.
"""

from __future__ import annotations

import pathlib
import shutil
import sys

SUFFIX = ".mutation-backup"


def apply_mutation(path: pathlib.Path, anchor: str, replacement: str) -> str:
    """Return the mutated source, or raise ValueError naming the hazard."""
    source = path.read_text()

    if anchor == replacement:
        raise ValueError(
            "the replacement is IDENTICAL to the anchor — this mutates nothing and would report "
            "SURVIVED for a guard that was never probed"
        )

    count = source.count(anchor)
    if count == 0:
        raise ValueError(
            f"anchor not found in {path.name} — the code moved, so this sweep is measuring nothing"
        )
    if count > 1:
        # THE ONE THAT ACTUALLY BIT. `replace(a, b, 1)` takes the FIRST occurrence, which may not be
        # the line being probed; the intended site stays untouched and the result reads as a survivor.
        raise ValueError(
            f"anchor appears {count} times in {path.name} — it would mutate the FIRST occurrence, "
            f"which may not be the site under test. Extend the anchor until it is unique"
        )

    # NO TEXTUAL CHECK FOLLOWS, AND THAT IS A LIMIT WORTH STATING RATHER THAN FAKING.
    #
    # A `mutated == source` guard here is DEAD CODE: with an identical replacement already refused and
    # the anchor known present, `str.replace` always changes the text. Shipping it would look like
    # protection against the second kumo-strategies case and provide none.
    #
    # That case was `"" or X`, which evaluates to `X`: the FILE changes, the BEHAVIOUR does not. It is
    # semantic, and no string harness can see it. What catches it is the discipline one level up —
    # when a mutation reports SURVIVED, read the mutation before concluding the test is weak. This
    # tool removes the two mechanical lies; the semantic one stays yours.
    return source.replace(anchor, replacement, 1)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--restore":
        path = pathlib.Path(argv[2])
        backup = path.with_suffix(path.suffix + SUFFIX)
        if not backup.exists():
            print(f"REFUSED: no backup at {backup} — nothing to restore", file=sys.stderr)
            return 2
        shutil.move(backup, path)
        return 0

    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2

    path = pathlib.Path(argv[1])
    try:
        mutated = apply_mutation(path, argv[2], argv[3])
    except (ValueError, OSError) as exc:
        # REFUSE LOUDLY AND WRITE NOTHING. A harness that half-applies a mutation leaves the tree in a
        # state the next sweep silently inherits.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    shutil.copy2(path, path.with_suffix(path.suffix + SUFFIX))
    path.write_text(mutated)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
