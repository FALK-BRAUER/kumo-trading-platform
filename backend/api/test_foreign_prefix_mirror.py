"""kumo-trading-strategies must recognise every client-order-id prefix THIS repo mints (#748).

THE ANCHOR I SAID DID NOT EXIST. `order_provenance.FOREIGN_ORDER_PREFIXES` over there mirrors the
prefixes cockpit stamps on orders it places, and I wrote in that file that the constant "cannot be
imported across the seam" — so the mirror was maintained by hand and immediately went stale.

It is importable. Cockpit's venv carries kumo_strategies as an editable, pinned install, so BOTH
constants are in scope HERE, and this is the one place a compiler sits between them. Third time this
session I asserted an impossibility without checking; a capability claim is a measurement.

WHY THE MIRROR MATTERS. Nautilus routes order events by the ORDER's strategy_id, so any order cockpit
stamps with a lane's id delivers its events to that lane's strategy. Those handlers move book state.
A prefix cockpit mints and kumo-trading-strategies does not recognise is a fill that lane will treat as its
own: `TR-` was exactly that, and reaches those handlers TODAY rather than after #748 — transfer legs
are built stamped with the lane and emitted as synthetic `OrderFilled` events.

VERSION SKEW IS THE POINT, NOT A NUISANCE. Adding a prefix here goes RED against the pinned
kumo-trading-strategies until that pin bumps. That is the coupling surfacing, which is what a mirror across a
seam is supposed to do instead of drifting quietly.
"""

from __future__ import annotations

import pytest


def _their_prefixes():
    """The prefixes kumo-trading-strategies recognises — or a decision about WHY it cannot say.

    TWO ABSENCES, AND ONLY ONE OF THEM IS A SKIP. This function skipped on any ImportError, and that
    made the whole mirror green-by-absence in CI: the pin was `becd2bd`, which predates the guard
    entirely and carries no `order_provenance` module at all, so the test that exists to catch a
    mismatch skipped on the largest mismatch there is. "Not asked" reading as "asked and clean", in
    a test written against that exact failure — which is why the two cases are now separated:

      kumo_strategies ABSENT        -> skip. A genuine environment without the dependency, which is
                                       a deliberate CI job here (it proves the suite runs without it).
      kumo_strategies PRESENT but
      order_provenance MISSING      -> FAIL. That is a strategies revision with no foreign-event
                                       guards while cockpit mints lane-stamped orders. It IS the
                                       mismatch this test hunts, and skipping it is the test refusing
                                       to look at its own subject.
    """
    import importlib

    if importlib.util.find_spec("kumo_strategies") is None:
        pytest.skip("kumo_strategies is not installed in this environment")

    try:
        from kumo_strategies.runtime.nautilus.order_provenance import FOREIGN_ORDER_PREFIXES
    except ImportError as exc:                                    # pragma: no cover
        pytest.fail(
            f"kumo_strategies IS installed but has no `order_provenance` ({exc}). That revision has "
            f"no foreign-order-event guards at all, while this repo mints orders stamped with a "
            f"lane's strategy_id — whose events Nautilus delivers to that lane's handlers, where "
            f"they move book state. Bump the `kumo-trading-strategies` pin in pyproject.toml"
        )
    return set(FOREIGN_ORDER_PREFIXES)


#: Every prefix cockpit mints onto an order it places. Enumerated from the minting sites, NOT copied
#: from `OUR_STOP_PREFIXES` — that constant answers "is this one of our STOPS", which is a different
#: question, and copying its answer is precisely how `TR-` went missing.
#:
#:   PROT-  engine_node.py, protective backstop stops
#:   PKW-   engine_node.py, PEAK trailing work orders
#:   PK-    engine_node.py, PEAK
#:   FL-    engine_node.py, flatten
#:   TR-    transfers.py,   transfer legs — stamped with the LANE, so their synthetic fills already
#:          land on that lane's strategy today
_MINTED_BY_COCKPIT = {"PROT-", "PKW-", "PK-", "FL-", "TR-"}


def test_the_fixture_matches_what_this_repo_actually_mints():
    """FIXTURE PROPERTY FIRST: the enumerated set must still be found in the source, or the mirror
    check below is comparing two lists nobody is keeping true to the code."""
    import ast
    import pathlib

    # BOUND TO STRING LITERALS IN THE AST, not to source text. A substring check here is satisfied by
    # writing the prefix in a COMMENT and broken by deleting one — the shape kumo-trading-strategies has a
    # dedicated test against, and the shape a stubbed-out scan used to slip past in this repo.
    literals: set[str] = set()
    for path in pathlib.Path(__file__).parent.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)

    for prefix in sorted(_MINTED_BY_COCKPIT):
        assert any(lit.startswith(prefix) or prefix in lit for lit in literals), (
            f"{prefix} appears in no string literal in this repo's source — it is no longer minted, "
            f"so remove it from the fixture rather than guarding a prefix that does not exist"
        )


def test_kumo_strategies_recognises_EVERY_prefix_cockpit_mints():
    """The mirror. A prefix we mint and they do not know is a fill a lane will treat as its own."""
    missing = _MINTED_BY_COCKPIT - _their_prefixes()
    assert not missing, (
        f"kumo-trading-strategies does not recognise {sorted(missing)} as foreign. Cockpit stamps these "
        f"onto orders it places, and any such order carrying a lane's strategy_id delivers its "
        f"events to that lane's handlers, where they move book state — a SELL read as an entry "
        f"completing, or a cancel clearing a pending intent for an order still working. Add the "
        f"prefix in kumo-trading-strategies' `order_provenance.FOREIGN_ORDER_PREFIXES` and bump the pin"
    )


def test_our_own_stop_prefixes_are_a_SUBSET_of_what_they_recognise():
    """The narrower half, stated separately because the two constants answer different questions and
    conflating them is what opened the gap: `OUR_STOP_PREFIXES` is about stops, the mirror is about
    everything cockpit places."""
    from api.cancel_attribution import OUR_STOP_PREFIXES

    missing = set(OUR_STOP_PREFIXES) - _their_prefixes()
    assert not missing, f"stop prefixes unknown to kumo-trading-strategies: {sorted(missing)}"
