"""Do the hand-written test doubles still resemble the things they stand in for?

Four separate defects shipped GREEN tonight (2026-08-11/12) because a double had drifted from
production, and in each case the suite could not see it:

* `_FakeTradeCycles` exposed `.project()`. Production's `strategy._trade_cycles` had become
  `dict[str, TradeCycleProjection]` when multi-strategy landed — a dict has no `.project`. Every
  manager's cycle-drift guard raised `'dict' object has no attribute 'project'` in the LIVE engine, so
  PEAK, STOP-AND-REENTER, PYRAMID and deferred-flatten all silently stopped applying, while the fake
  answered happily and 650 tests passed.
* `_FakeOrder` had no `filled_qty`. Real Nautilus orders do. A re-entry sized itself off a stale
  arm-time param instead of the closing fill (FIG: 424 shares against a 128-share position) and no test
  could tell, because the double had nothing to size from.

The shared shape: **a double that answers a call production would refuse, or omits an attribute
production reads, makes a test that passes for the wrong reason.**

This module is the cheap half of the guard — the half a machine can check. For every double with a
real counterpart, its public surface must be a SUBSET of the real type's. That is exactly what would
have caught `.project()` on a dict.

The other half — a double MISSING an attribute production reads — cannot be settled by inspection
(you would have to know which attributes every code path touches). The mitigation there is the rule in
`docs/` and in review: when a double stands in for a real type, give it the real attribute even when
the test at hand does not read it.
"""

from __future__ import annotations

import pytest


def _public(obj: type) -> set[str]:
    return {n for n in dir(obj) if not n.startswith("_")}


def _double_surface(cls: type) -> set[str]:
    """Public names a double defines itself — class attributes plus whatever `__init__` assigns.

    `dir()` alone misses instance attributes set in `__init__`, which is where most doubles put their
    surface, so the assignments are read out of the source instead.
    """
    import inspect
    import re

    names = {n for n in vars(cls) if not n.startswith("_")}
    try:
        src = inspect.getsource(cls)
    except (OSError, TypeError):  # pragma: no cover — source always available for these
        return names
    names |= set(re.findall(r"^\s+self\.([a-zA-Z][a-zA-Z0-9_]*)\s*=", src, re.MULTILINE))
    return names


def test_fake_order_matches_a_real_nautilus_order():
    """`_FakeOrder` stands in for a Nautilus `Order`. Anything it exposes must exist on the real one —
    otherwise a test can assert against an attribute production could never read."""
    from nautilus_trader.model.orders import Order

    from api.test_stop_reenter import _FakeOrder

    extra = _double_surface(_FakeOrder) - _public(Order)
    assert not extra, f"_FakeOrder exposes names a real Order does not: {sorted(extra)}"


def test_fake_order_carries_the_attributes_the_engine_actually_reads():
    """The `filled_qty` case, pinned. `_StopReenterWatch.apply` sizes a re-entry off the closing
    order's filled quantity; a double without it silently sized off stale params instead."""
    from api.test_stop_reenter import _FakeOrder

    surface = _double_surface(_FakeOrder)
    for attr in ("order_type", "status", "avg_px", "side", "filled_qty", "ts_last", "tags"):
        assert attr in surface, f"_FakeOrder is missing {attr!r} — production reads it"


def test_fake_position_matches_a_real_nautilus_position():
    from nautilus_trader.model.position import Position

    from api.test_peak import _FakePosition

    extra = _double_surface(_FakePosition) - _public(Position)
    assert not extra, f"_FakePosition exposes names a real Position does not: {sorted(extra)}"


@pytest.mark.parametrize("module", ["api.test_peak", "api.test_stop_reenter", "api.test_pyramid"])
def test_trade_cycles_double_is_held_in_a_DICT_like_production(module):
    """The `'dict' object has no attribute 'project'` defect, pinned in the shape that broke.

    `strategy._trade_cycles` is `dict[str, TradeCycleProjection]`, keyed by strategy id. A double that
    hands the strategy a bare projection lets `apply()` call `.project()` on it and pass — while
    production raises, and every manager stops working. So the double must be a MAPPING.
    """
    import importlib

    mod = importlib.import_module(module)
    strategy = mod._FakeStrategy.__init__
    del strategy  # only imported to assert the module exposes the double at all

    fake_cycles = mod._FakeTradeCycles([])
    holder = _build_strategy_with_cycles(mod, fake_cycles)
    assert isinstance(holder._trade_cycles, dict), (
        f"{module}._FakeStrategy must hold _trade_cycles as a DICT keyed by strategy id, "
        "mirroring production — a bare projection is what hid the multi-strategy break"
    )
    assert all(hasattr(v, "project") for v in holder._trade_cycles.values()), (
        "each VALUE in the dict is a projection and must answer .project()"
    )


def _build_strategy_with_cycles(mod, fake_cycles):
    """Each module's `_FakeStrategy` takes a different constructor shape; build whichever it wants."""
    import inspect

    sig = inspect.signature(mod._FakeStrategy.__init__)
    kwargs = {"trade_cycles": fake_cycles}
    if "cache" in sig.parameters:
        kwargs["cache"] = mod._FakeCache()
    if "now_ts_ns" in sig.parameters:
        kwargs["now_ts_ns"] = 0
    return mod._FakeStrategy(**kwargs)
