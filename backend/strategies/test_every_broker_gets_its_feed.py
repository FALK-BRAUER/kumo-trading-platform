"""Every gateway that builds a `NautilusBroker` must wire `broker.feed`, or its exits are refused.

WHY THIS FILE EXISTS
--------------------
`NautilusBroker.exit()` is the ONLY path that releases the shares a resting protective stop reserves —
it calls `feed.release_for_exit(...)` to cancel the stop before selling. `submit()` releases nothing.
And `exit()` refuses outright when the feed is absent:

    if self.feed is None:
        ... "broker.feed is not wired — cannot release the shares ..."

`broker.feed` was assigned in exactly ONE place, `momentum.py`. QC345-003 and TECHIVOL-005 build their
own `NautilusBroker` and never assigned it, so the moment kumo-trading-strategies routed their exits through
`exit()` (53e2ff5) both would refuse every sell.

MEASURED AT THE VENUE, so the cost is not hypothetical. Every held symbol had `qty_available = 0` —
100% of shares reserved by resting trailing stops:

    SYM     HELD  AVAIL  STOPS            SYM     HELD  AVAIL  STOPS
    AEM       18      0      2            BETA      79      0      1
    AMGN       8      0      2            CGAU     174      0      2
    BDX       65      0      2            WPM       26      0      2

`available: 0` means UNRESERVED is 0, not that the position is gone. A sell that does not cancel the
stop first gets `403 insufficient qty available` — every time, structurally, not conditionally.

WHY IT IS ASSERTED OVER EVERY BUILDER RATHER THAN OVER THE TWO THAT WERE BROKEN
------------------------------------------------------------------------------
The question is never "is THIS gateway wired", it is "could ANY of them not be". Two sibling defects
this same day were hardcoded lists that went stale by omission — cockpit's `test_broker_equity_seam`
named two strategies of four, and kumo-trading-strategies' discovery floor named three of five. Both passed
while a live strategy was broken. So this scans the directory: a gateway added tomorrow is covered
without anyone remembering.

kumo-trading-platform issue 459, second half. The routing half is kumo-trading-strategies 53e2ff5.
"""

from __future__ import annotations

import ast
import pathlib

_STRATEGIES = pathlib.Path(__file__).parent


def _modules_building_a_broker() -> dict[str, ast.Module]:
    """Every non-test module under `strategies/` that constructs a `NautilusBroker`."""
    out: dict[str, ast.Module] = {}
    for path in sorted(_STRATEGIES.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text())
        builds = any(
            isinstance(n, ast.Call)
            and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "NautilusBroker"
            for n in ast.walk(tree)
        )
        if builds:
            out[path.name] = tree
    return out


def _assigns_feed(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "feed" for t in n.targets)
        for n in ast.walk(tree)
    )


def test_the_scan_sees_the_builders_it_judges():
    """An invariant asserted over an empty set passes for the wrong reason.

    Named explicitly rather than counted, because the failure this guards against is a gateway going
    MISSING from the scan — which a bare `assert found` would not notice.
    """
    found = _modules_building_a_broker()
    assert found, "no module under strategies/ constructs a NautilusBroker — this file is vacuous"
    for expected in ("momentum.py", "qc345.py", "qc27.py"):
        assert expected in found, (
            f"{expected} builds a strategy that trades but the scan did not see it construct a "
            f"NautilusBroker — the guarantee below does not cover it. Found: {sorted(found)}"
        )


def test_every_broker_built_here_is_given_its_feed():
    """THE DEFECT, stated as an assertion over all of them.

    Without `feed`, `exit()` refuses and the strategy cannot release the shares its own protective stop
    is holding — so every exit is refused at the venue while the position sits there.
    """
    missing = [name for name, tree in _modules_building_a_broker().items() if not _assigns_feed(tree)]
    assert not missing, (
        f"{missing} construct a NautilusBroker and never assign `broker.feed`. `exit()` is the only "
        f"path that releases the shares a resting protective stop reserves, and it refuses outright "
        f"when the feed is None — so every sell is refused at the venue with `403 insufficient qty "
        f"available` while the position is still held"
    )


def test_the_builders_can_actually_RECEIVE_a_feed():
    """The half that makes the assignment possible, and the half that was actually missing.

    `build_qc345_strategy()` and `build_qc27_strategy()` took NO arguments, so there was nothing to
    assign — `build_momentum_strategy(feed=feed)` was the only one the engine could hand the feed to.
    Asserting only that `broker.feed` is assigned would let someone satisfy this file by assigning
    `None`, which is exactly the value that breaks it.
    """
    import inspect

    from strategies import momentum, qc27, qc345

    for mod, fn_name in ((momentum, "build_momentum_strategy"),
                         (qc345, "build_qc345_strategy"),
                         (qc27, "build_qc27_strategy")):
        fn = getattr(mod, fn_name)
        params = inspect.signature(fn).parameters
        assert "feed" in params, (
            f"{fn_name} takes no `feed` parameter, so the engine has no way to hand it one — "
            f"`broker.feed` can only ever be None and every exit refuses. Params: {list(params)}"
        )


def _builder_names() -> set[str]:
    """Every `build_*_strategy` in `strategies/`, DERIVED — the floor under the call-site check.

    A SECOND INDEPENDENT DERIVATION, not a list. Borrowed from kumo-trading-strategies, which hit the same
    failure today: their discovery floor named three strategies while five existed, so discovery could
    narrow and the guard would still pass. Two derivations that must agree cannot go stale by omission,
    because neither is written down.
    """
    names: set[str] = set()
    for path in sorted(_STRATEGIES.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.FunctionDef) and n.name.startswith("build_") \
                    and n.name.endswith("_strategy"):
                names.add(n.name)
    return names


def test_the_engine_ACTUALLY_PASSES_a_feed_to_every_builder():
    """The seam, and without it everything above passes while `broker.feed` is None.

    `feed=None` is a keyword DEFAULT. A builder can accept the parameter, assign `broker.feed = feed`,
    and still be handed nothing — byte-for-byte the broken state this file forbids. The default is
    deliberate (the builders are called directly by tests and tooling), so the guarantee has to be
    asserted at the CALL SITE.

    THIS TEST'S FIRST VERSION HAD A MUTATION ESCAPE, and it is worth recording because it is the third
    instance of one shape today. It collected `{builder_name: was_fed}` by walking calls in
    `engine_node.py` and asserted none were unfed. Deleting a call ENTIRELY — which is exactly what a
    refactor does — removed the key rather than setting it False, so the set was empty and the
    assertion passed. An invariant asserted over an empty set passes for the wrong reason: the same
    error as `test_broker_equity_seam`'s hardcoded list and kumo-trading-strategies' three-name floor, in the
    test written to guard against them.

    Now the expected set is DERIVED from the builders that exist, so a call that disappears is a
    failure rather than a gap.
    """
    import pathlib

    src = pathlib.Path(__file__).parent.parent.joinpath("api", "engine_node.py").read_text()
    tree = ast.parse(src)

    fed: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name is None or not (name.startswith("build_") and name.endswith("_strategy")):
            continue
        called.add(name)
        if any(kw.arg == "feed" for kw in node.keywords):
            fed.add(name)

    builders = _builder_names()
    assert builders, "no `build_*_strategy` found under strategies/ — this test is blind"

    uncalled = sorted(builders - called)
    assert not uncalled, (
        f"{uncalled} exist as builders but `engine_node.py` never calls them — either the strategy is "
        f"no longer wired, or the call moved and this guarantee no longer covers it. A builder that "
        f"vanishes from the call graph must fail here, not silently drop out of the check"
    )
    unfed = sorted(builders - fed)
    assert not unfed, (
        f"engine_node.py calls {unfed} without `feed=`, so the builder takes its `feed=None` default "
        f"and `broker.feed` is None — `exit()` refuses, and every sell is refused at the venue on "
        f"shares the strategy's own protective stop is reserving"
    )
