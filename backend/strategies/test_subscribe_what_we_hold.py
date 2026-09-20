"""Every lane must subscribe to what it HOLDS, not only to what it might buy.

`NautilusBroker.submit()` refuses a symbol that is not a subscribed instrument. So a lane holding a
name that is no longer in its buy-list cannot SELL it: the exit is decided every session and refused
every session, and the position is stranded until something else clears it.

MEASURED, on kumo-paper's own journal for 2026-08-21 — TECHIVOL-005's entire exit set:

    SELL 11 XLV:   XLV is not a subscribed instrument
    SELL 26 WPM:   WPM is not a subscribed instrument
    SELL 56 WHD:   WHD is not a subscribed instrument
    SELL 174 CGAU: CGAU is not a subscribed instrument

Eight orders, eight errors, one session.

`build_momentum_strategy` was fixed for exactly this — it subscribes `pool ∪ held` and says why in a
comment. The fix never reached QC27 or QC345, which take `_universe_symbols()` alone. Their universes
are OPERATOR-EDITABLE SETTINGS, so a name leaving the universe while held is one edit away, not an
exotic case.

This file pins the RULE across every lane, by name-independent scan, because a fourth lane added next
month will have the same choice to make and nobody will remember this.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_LANES = {
    "momentum.py": "build_momentum_strategy",
    "qc27.py": "build_qc27_strategy",
    "qc345.py": "build_qc345_strategy",
    "crsi_short.py": "build_crsi_short_strategy",
}
_DIR = Path(__file__).resolve().parent


def _source(name: str) -> str:
    return (_DIR / name).read_text()


def test_the_scan_sees_every_lane_it_claims_to_check():
    """A scan that finds nothing passes vacuously. Prove the files and builders are really there."""
    for filename, builder in _LANES.items():
        src = _source(filename)
        assert f"def {builder}" in src, f"{filename} has no {builder} — this scan is blind"


def test_momentum_is_the_reference_and_still_subscribes_what_it_holds():
    """The lane that HAS the fix. If this stops being true, the rule below is being read off nothing."""
    src = _source("momentum.py")
    assert "_held_claims" in src
    assert "set(await pool.symbols()) | held" in src, (
        "momentum no longer unions held claims into its subscription — the reference for this rule")


def _symbol_resolving_functions(src: str):
    """Every function that RESOLVES the symbol list — whatever it is called.

    Keyed on the resolution, not on a builder name: momentum does it in a nested `_prepare` inside
    `_build_rotation`, qc27 and qc345 do it at builder scope. A scan pinned to builder names finds
    momentum's fix nowhere and reports the lane that HAS it as broken, which is what the first version
    of this test did.
    """
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = {n.func.attr for n in ast.walk(node)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        calls |= {n.func.id for n in ast.walk(node)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "symbols" in calls or "_universe_symbols" in calls:
            out.append((node.name, node))
    return out


def test_the_scan_finds_a_symbol_resolver_in_every_lane():
    """Fixture property: a scan that matches nothing would pass every assertion below."""
    for filename in _LANES:
        found = _symbol_resolving_functions(_source(filename))
        assert found, f"{filename}: no function resolves a symbol list — this scan is blind"


@pytest.mark.parametrize("filename", sorted(_LANES))
def test_every_lane_subscribes_what_it_holds(filename):
    """THE RULE. Whatever resolves the symbol list must union in the claims still held.

    NautilusBroker.submit() refuses an unsubscribed instrument, so a held name outside the universe
    is decided as an exit and refused forever — measured on TECHIVOL-005, 2026-08-21: 8 orders, 8
    "is not a subscribed instrument" errors.
    """
    resolvers = _symbol_resolving_functions(_source(filename))
    ok = []
    for name, node in resolvers:
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        ok.append(any("held" in n for n in names))
    assert any(ok), (
        f"{filename}: {[n for n, _ in resolvers]} resolve a symbol list and none unions the symbols "
        f"the lane HOLDS. Any held name outside the universe becomes unexitable.")


# --------------------------------------------------------------------------------------------------
# THE STRUCTURAL TEST ABOVE DOES NOT BITE, AND THAT IS WHY THIS ONE EXISTS.
#
# `test_every_lane_subscribes_what_it_holds` asserts that a symbol-resolving function MENTIONS a name
# containing "held". Deleting the union itself --
#
#     symbols = sorted(set(symbols) | held)   ->   symbols = sorted(set(symbols))
#
# -- leaves `sm, held = asyncio.run(_prepare())` on the line above, so the word is still there and the
# scan still passes. Measured: with the union removed, that file reported `6 passed`.
#
# A name-matching test cannot see whether the union HAPPENS. These drive the real builder and read the
# instrument ids it actually subscribes.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("module_name,builder_name", [
    ("strategies.qc345", "build_qc345_strategy"),
    ("strategies.qc27", "build_qc27_strategy"),
    ("strategies.crsi_short", "build_crsi_short_strategy"),
])
def test_A_HELD_SYMBOL_OUTSIDE_THE_UNIVERSE_IS_ACTUALLY_SUBSCRIBED(monkeypatch, module_name,
                                                                   builder_name):
    """THE BEHAVIOUR. TECHIVOL-005, 2026-08-21: 8 exits, 8 "is not a subscribed instrument", every
    position stranded. The universes are OPERATOR-EDITABLE SETTINGS, so a held name leaving the
    universe is one edit away — not an exotic case.
    """
    import importlib

    if module_name == "strategies.crsi_short":
        from strategies.test_installed_strategies_carry_crsishort import crsishort_installed

        if not crsishort_installed():
            pytest.skip("CRSISHORT adapter absent from the installed kumo-trading-strategies (pin predates #121)")
    mod = importlib.import_module(module_name)

    captured: dict = {}

    def _spy(symbols):
        captured["symbols"] = list(symbols)
        # RETURNS THE SYMBOLS, not []. `_instrument_ids` used to return resolved ids and [] was a
        # harmless stand-in; `_lane_symbols` returns the pool itself, so [] would empty the lane and
        # this test would pass over a builder that had nothing to build (#622).
        return list(symbols)

    # PATCHED ON `strategies.momentum`, NOT on the lane module. Both builders do
    # `from strategies.momentum import _instrument_ids` INSIDE the function, so the name is looked up
    # on momentum at call time and a patch on the lane module is simply never consulted -- the test
    # would have failed with AttributeError, which is the honest version of a patch that does nothing.
    import strategies.momentum as _mom

    monkeypatch.setattr(_mom, "_lane_symbols", _spy)
    monkeypatch.setattr(mod, "_universe_symbols", lambda: ["AAA", "BBB"])
    # THE GATE RETURNS None BEFORE ANY OF THIS. Without it the builder exits at line 2 and the test
    # captures nothing -- which the "never reached _instrument_ids" assertion caught rather than
    # letting the test pass over a builder that did not run.
    monkeypatch.setattr(mod, "_enabled", lambda: True)

    async def _held(sm, strategy_id):
        return {"ZZZ_HELD_BUT_DELISTED"}

    monkeypatch.setattr(mod, "_held_claims", _held)

    # `_prepare` is a closure, so it cannot be patched — but everything it calls is imported from the
    # store module at call time, and those CAN be. No database: the question is what reaches
    # `_instrument_ids`, and a real engine is not part of it.
    import kumo_strategies.runtime.executor.store as _store

    async def _create_all(_eng):
        return None

    monkeypatch.setattr(_store, "make_engine", lambda *a, **k: object())
    monkeypatch.setattr(_store, "create_all", _create_all)
    monkeypatch.setattr(_store, "make_sessionmaker", lambda *a, **k: object())

    try:
        getattr(mod, builder_name)(feed=None)
    except Exception:
        # The builder goes on to construct a Nautilus strategy, which needs far more than this test
        # provides. The question is only what reached `_instrument_ids`, and that happens first.
        pass

    assert "symbols" in captured, (
        f"{module_name}: the builder never reached _instrument_ids — this test proves nothing")
    assert "ZZZ_HELD_BUT_DELISTED" in captured["symbols"], (
        f"{module_name} subscribed {captured['symbols']} — a held symbol outside the universe was "
        f"NOT subscribed, so every exit for it is refused with 'is not a subscribed instrument'")
    assert {"AAA", "BBB"} <= set(captured["symbols"]), (
        f"the union dropped universe symbols: {captured['symbols']}")


@pytest.mark.parametrize("module_name,builder_name", [
    ("strategies.qc345", "build_qc345_strategy"),
    ("strategies.qc27", "build_qc27_strategy"),
    ("strategies.crsi_short", "build_crsi_short_strategy"),
])
def test_AN_UNREADABLE_CLAIMS_TABLE_STILL_BUILDS_THE_STRATEGY(monkeypatch, module_name,
                                                              builder_name):
    """A BUILD FAILURE IS WORSE THAN THE BUG IT WOULD PREVENT.

    `_held_claims` reads Postgres during SYNCHRONOUS node startup. As first written it let the error
    out, and `kernel.py:1027`'s bare return means a raising builder leaves the node RUNNING with
    fewer strategies and no obvious cause — the lane is simply absent. Trading nothing is a worse
    outcome than being unable to sell one delisted holding.

    So an unreadable table degrades to the PREVIOUS behaviour, universe-only, and logs why. This test
    exists because the mutation that proved it — making the read raise — passed all 294 tests in this
    package: the degradation was written and nothing drove it.
    """
    import importlib

    import kumo_strategies.runtime.executor.store as _store

    import strategies.momentum as _mom

    if module_name == "strategies.crsi_short":
        from strategies.test_installed_strategies_carry_crsishort import crsishort_installed

        if not crsishort_installed():
            pytest.skip("CRSISHORT adapter absent from the installed kumo-trading-strategies (pin predates #121)")
    mod = importlib.import_module(module_name)
    captured: dict = {}

    monkeypatch.setattr(_mom, "_lane_symbols",
                        lambda syms: captured.setdefault("symbols", list(syms)) or [])
    monkeypatch.setattr(mod, "_universe_symbols", lambda: ["AAA", "BBB"])
    monkeypatch.setattr(mod, "_enabled", lambda: True)

    class _Exploding:
        def __call__(self):
            raise RuntimeError("claims table unreadable")

    async def _create_all(_eng):
        return None

    monkeypatch.setattr(_store, "make_engine", lambda *a, **k: object())
    monkeypatch.setattr(_store, "create_all", _create_all)
    monkeypatch.setattr(_store, "make_sessionmaker", lambda *a, **k: _Exploding())

    try:
        getattr(mod, builder_name)(feed=None)
    except Exception:
        pass

    assert "symbols" in captured, (
        f"{module_name}: an unreadable claims table stopped the builder before it could subscribe "
        f"anything — the lane would not register at all")
    assert set(captured["symbols"]) == {"AAA", "BBB"}, (
        f"expected the universe alone, got {captured['symbols']}")
