"""The trigger's SCOPE and its ERROR TEXT (#443).

The behaviour was verified against a real Postgres and the transcript is in the PR: the INSERT is
refused, the two statements the HINT prints both succeed, and WARMUP/HALTED/SHADOW -> TRADING and the
boot upsert all stay legal. That cannot be re-run here — the repo has asyncpg but no pytest-asyncio,
so every `needs_services` test in this suite currently ERRORS rather than runs, and they are deselected
by default so nothing reports it.

What IS testable without a database is the pair of things most likely to rot: the trigger quietly
growing beyond an INSERT rule, and the error message drifting until it no longer tells anyone what to
do. Both are pinned by reading the migration that ships.
"""

from __future__ import annotations

import pathlib
import re

_MIGRATION = pathlib.Path(__file__).parent.parent / "alembic/versions/0016_no_birth_in_trading.py"
_SRC = _MIGRATION.read_text()


def test_the_fixture_can_see_the_migration():
    """Every regex below matches nothing over an empty string and would pass for the wrong reason."""
    assert "CREATE TRIGGER" in _SRC and len(_SRC) > 1000


def test_the_trigger_fires_on_INSERT_ONLY():
    """B, not A. An earlier draft also constrained which state a promotion came FROM, requiring SHADOW.
    The operator rejected SHADOW outright, and the real gate is now kumo-trading-strategies' boot-time dry_run. A
    trigger that creeps back into policing UPDATEs would be enforcing a rejected policy at 3am in psql,
    and would put the engine's boot-time upsert of an already-trading strategy at risk — a failure
    there takes the whole node down on restart."""
    create = re.search(r"CREATE TRIGGER.*?FOR EACH ROW", _SRC, re.DOTALL).group(0)
    assert "BEFORE INSERT" in create
    assert "UPDATE" not in create, "the trigger now fires on UPDATE — that is option A, which was cut"


def test_no_SHADOW_mandate_survives_anywhere_in_the_rule():
    """Aimed at the class rather than the instance: any reintroduction of the dry-run requirement, in
    any wording, fails here."""
    # THE SHIPPED SQL ONLY. Prose that explains why SHADOW was cut is not a SHADOW mandate, and a
    # test that cannot tell a rule from a comment about the rule fails on documentation — which trains
    # whoever hits it to weaken the assertion rather than read it.
    sql = "".join(re.findall(r'op\.execute\(f"""(.*?)"""\)', _SRC, re.DOTALL))
    assert sql.strip(), "no SQL extracted — this test is blind"
    assert "SHADOW" not in sql, "a SHADOW requirement is back in the shipped trigger"


def test_the_error_names_the_STRATEGY_and_both_repair_statements():
    """the operator's requirement, verbatim: "it needs documentation. Best the error from source says how to do
    it right." The message is read by someone bootstrapping an environment who has never opened this
    file, so it carries the fix rather than a rule number."""
    assert "NEW.strategy_id" in _SRC, "the message does not name which strategy failed"
    hint = re.search(r"HINT\s*=(.*?);\s*\n\s*END IF", _SRC, re.DOTALL).group(1)
    assert "INSERT INTO exec_strategy_state" in hint, "the HINT does not show how to create the row"
    assert "UPDATE exec_strategy_state" in hint, "the HINT does not show how to promote it"
    assert "''DISABLED''" in hint, "the HINT's INSERT must use a NON-trading state or it is refused too"
    # THE INSERT SPECIFICALLY. Asserting the id appears anywhere in the HINT survived a mutation that
    # hardcoded it in the INSERT and left it interpolated in the UPDATE — the operator would then paste
    # a statement creating a strategy called SOME-ID.
    insert = hint[hint.index("INSERT INTO"):hint.index("UPDATE exec_strategy_state")]
    assert "NEW.strategy_id" in insert, (
        "the HINT's INSERT hardcodes a strategy id instead of naming the one that failed"
    )


def test_the_HINT_teaches_a_fix_the_TRIGGER_ITSELF_would_accept():
    """Two derivations of one fact: the rule and the advice. An error that prints a repair the same
    trigger refuses is worse than no advice, and nothing else would notice — the INSERT in the HINT
    must not be an INSERT with state TRADING."""
    hint = re.search(r"HINT\s*=(.*?);\s*\n\s*END IF", _SRC, re.DOTALL).group(1)
    insert = hint[hint.index("INSERT INTO"):hint.index("UPDATE exec_strategy_state")]
    assert "TRADING" not in insert, (
        "the HINT tells the operator to INSERT a TRADING row, which this very trigger refuses"
    )


def test_the_error_says_it_is_an_AUDIT_rule_and_not_a_lock():
    """It does not prevent anything — two statements do what one did. A guard sold as prevention that
    only provides evidence is how the next person decides guards are decoration, so the message says
    which one it is."""
    assert "audit rule, not a lock" in _SRC


def test_NOTHING_IN_THE_REPO_CLAIMS_AN_ABSENT_LIFECYCLE_ROW_IS_DISABLED():
    """AIMED AT THE CLASS, because this was the SIXTH copy of one inverted sentence.

    An absent row reads as TRADING (2026-08-19, `momentum._lifecycle`). Six places said the
    opposite — two comments in `engine_node`, a `qc345` docstring, `strategies/README.md`, a
    `momentum` docstring, and a claim in `test_journal_identity` that BCTROT's cross-lane replay "was
    contained only by BCTROT having no lifecycle row". That containment never existed.

    Every one of them promised a second operator gate between the deploy and the first order. There is
    no such gate: the deploy IS the gate. A comment that reads as safety is the most dangerous kind of
    wrong, because it survives review by sounding careful — ibkr-paper-retired's runbook stated this same
    rule exactly inverted and was believed by readers on both sides of it.

    Prose is covered by no other test here, so it drifts freely until something pins it.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    # "no/absent/missing lifecycle row ... DISABLED" within one sentence, in either order.
    pattern = re.compile(
        r"(?:no|absent|missing|without a)\s+lifecycle\s+row[^.]{0,120}?\bDISABLED\b"
        r"|\bDISABLED\b[^.]{0,120}?(?:no|absent|missing)\s+lifecycle\s+row",
        re.IGNORECASE)

    scanned, offenders = 0, []
    for path in root.rglob("*"):
        if path.suffix not in {".py", ".md"} or not path.is_file():
            continue
        if any(p in (".venv", "site-packages", "node_modules", ".git") for p in path.parts):
            continue
        if path.name == pathlib.Path(__file__).name:
            continue
        scanned += 1
        text = path.read_text(errors="ignore")
        for m in pattern.finditer(text):
            line = text[: m.start()].count("\n") + 1
            # QUOTING THE OLD RULE IN ORDER TO CORRECT IT IS NOT CLAIMING IT. Three sites legitimately
            # repeat the wrong sentence: two historical defect tables and the docstring recording the
            # inversion. They carry an explicit marker rather than being matched by a heuristic,
            # because "does this sentence sound like a correction" is exactly the judgement that let
            # six copies survive review in the first place.
            window = "\n".join(text.splitlines()[max(0, line - 5): line + 4])
            if "[absent-row: historical]" in window:
                continue
            offenders.append(f"{path.relative_to(root)}:{line}")

    assert scanned > 100, f"only scanned {scanned} files — this would pass by finding nothing"
    assert not offenders, (
        f"{offenders} claim an absent lifecycle row means DISABLED. It means TRADING. Each of these "
        f"promises an operator gate between the deploy and the first order that does not exist")
