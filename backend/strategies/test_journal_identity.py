"""Every journal must be constructed with the identity of the strategy that owns it.

WHY THIS FILE EXISTS
--------------------
`_build_rotation` takes `strategy_id` as a parameter and uses it for the runner, the claims and the
lifecycle — and constructed the journal as `PgJournal(sm)`, with no identity at all. `PgJournal` defaults
to `MOMENTUM-002`, so **BCTROT-004's entire durable record was written under MOMENTUM-002's identity.**

That falsified something three independent reviews and both repos asserted with confidence: that
BCTROT-004 had never written a row. It had. Misattributed.

The live risk, before kumo-trading-strategies fixed their half: MOMENTUM-002 decides at `open+5m`; BCTROT-004's
session starts later, reads MOMENTUM's decision as its own because the journal is misattributed *and* the
slot check defaulted to the same value; `_resume` then replays MOMENTUM's unfilled orders through
BCTROT's broker under BCTROT's `order_id_tag`. It was NOT contained by BCTROT lacking a lifecycle row: absent
reads as TRADING (2026-08-19), so the containment described here never existed. **Arming BCTROT-004 removes that containment as step one**, which is why this
blocks #320.

WHY THIS TEST IS AIMED AT THE CLASS, NOT THE INSTANCE
-----------------------------------------------------
"BCTROT's journal is fixed" would pass while the next strategy added repeats it. The question is "what
would have caught this *and* its siblings", so this pins the rule: **no `PgJournal` is constructed
anywhere in `strategies/` without an explicit identity.** A default that is right for one caller and
silently wrong for every other is the defect.

issue 54.
"""

from __future__ import annotations

import ast
import pathlib

_STRATEGIES = pathlib.Path(__file__).parent


def _journal_constructions() -> list[tuple[str, int, bool]]:
    """Every `PgJournal(...)` in production code: (file, line, passes an explicit strategy_id)."""
    found: list[tuple[str, int, bool]] = []
    for path in _STRATEGIES.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "PgJournal":
                continue
            explicit = any(kw.arg == "strategy_id" for kw in node.keywords) or len(node.args) >= 2
            found.append((str(path.relative_to(_STRATEGIES)), node.lineno, explicit))
    return found


def test_no_journal_is_built_without_an_explicit_identity():
    """Reintroduce `PgJournal(sm)` and this goes red naming the file and line.

    Deliberately not "BCTROT's journal has the right id": that phrasing passes while the next strategy
    added repeats the defect, which is how a default that suits exactly one caller survives.
    """
    anonymous = [(f, ln) for f, ln, explicit in _journal_constructions() if not explicit]
    assert not anonymous, (
        f"PgJournal constructed without an explicit strategy_id at {anonymous} — it defaults to "
        f"MOMENTUM-002, so every other strategy's durable record is written under MOMENTUM's identity"
    )


def test_the_fixture_can_actually_see_a_construction():
    """A test asserting an invariant over an empty set passes for the wrong reason.

    Proves the scanner finds the constructions it is judging, so the assertion above cannot be green
    merely because it looked at nothing — the same discipline as asserting a fixture's own property
    before asserting invariance over it.
    """
    assert _journal_constructions(), (
        "the scanner found no PgJournal construction at all — the guarantee above is vacuous and the "
        "constructor was probably renamed or moved out of strategies/"
    )
