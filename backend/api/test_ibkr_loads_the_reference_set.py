"""The IBKR provider must load the compass REFERENCE set, not only the tradeable one (#606).

THE DEFECT, measured on staging-ibkr 2026-08-27. The data client declared:

    load_contracts=frozenset(IBContract(...symbol=s...) for s in _tradeable_symbols())

`_tradeable_symbols()` is the deployment's TRADEABLE universe. The market compass grades rotation
from 27 REFERENCE ETFs — SPY, TLT, GLD, the sector XLs — which are never held, therefore not
tradeable, therefore never declared, therefore never loaded:

    compass tickers                       27
    compass tickers in tradeable universe  0

So every bar request for them was refused, all session:

    Cannot request XLU.ARCX-1-DAY-LAST-EXTERNAL bars: instrument not found   (x13)
    ... 21 of the 27 refused ...
    rotation: seeding, 27 of 27 instruments still short                      (every 5 minutes)

and the compass published ZERO axes for the life of the process. SPY, GLD, IWM, XLV and XLY loaded
only by luck, through the EXEC client's provider config, which carries no `load_contracts` and
resolves on demand.

THE VENUE WAS NEVER THE PROBLEM. IBKR serves all of these. We did not ask for the contracts.

WHY THIS IS THE FIX AND A FILTER IS NOT. The first proposal was to detect unservable instruments via
`cache.instrument(iid) is None` and skip them. Codex attacked that before any code existed: an
instrument is legitimately absent while the provider bootstraps, during async contract
qualification, and after a reconnect. Given the real cause, that filter would have permanently
blacklisted 21 instruments IBKR serves perfectly well and published a compass graded on six axes,
looking healthy throughout — "silent universe drift". The filter survives as a backstop (#606) with
a persistence condition; it is not the fix.
"""

from __future__ import annotations

import pytest


def _compass_tickers() -> set[str]:
    from strategies.rotation_from_cache import rotation_tickers

    return set(rotation_tickers())


def test_the_fixture_can_express_the_defect():
    """Fixture property first: the compass set must be non-empty and must NOT already be a subset of
    the tradeable universe, or this file asserts nothing about the bug that shipped."""
    compass = _compass_tickers()
    assert len(compass) >= 20, f"compass universe collapsed to {len(compass)} — check rotation_tickers()"
    assert {"SPY", "TLT", "GLD"} <= compass, "the reference ETFs are not in the compass set any more"


def test_the_reference_set_is_declared_to_the_provider(monkeypatch):
    """THE DEFECT. Every compass ticker must reach `load_contracts`, or its bars are refused."""
    import api.providers.ibkr as mod

    monkeypatch.setattr(mod, "_tradeable_symbols", lambda: ("AAPL", "MSFT"))
    declared = {c.symbol for c in mod._data_contracts()}
    missing = sorted(_compass_tickers() - declared)
    assert not missing, (
        f"{len(missing)} compass reference tickers are not declared to the IBKR instrument provider "
        f"and their bars will be refused with 'instrument not found': {', '.join(missing[:8])}"
    )


def test_the_tradeable_set_is_still_declared(monkeypatch):
    """The reference set is ADDITIVE. Dropping a tradeable symbol here means every new entry fails at
    submit with a None instrument — the outage `_tradeable_symbols` raises to prevent."""
    import api.providers.ibkr as mod

    monkeypatch.setattr(mod, "_tradeable_symbols", lambda: ("AAPL", "MSFT"))
    declared = {c.symbol for c in mod._data_contracts()}
    assert {"AAPL", "MSFT"} <= declared


def test_an_overlap_is_declared_ONCE(monkeypatch):
    """A ticker that is both tradeable and a compass reference must not produce two contracts. IB
    charges a market-data line per contract, and a duplicate is a wasted one."""
    import api.providers.ibkr as mod

    monkeypatch.setattr(mod, "_tradeable_symbols", lambda: ("SPY", "AAPL"))
    symbols = [c.symbol for c in mod._data_contracts()]
    assert symbols.count("SPY") == 1, f"SPY declared {symbols.count('SPY')} times"


def test_every_contract_is_shaped_the_way_IB_resolves_it(monkeypatch):
    """SMART routing and USD, the same shape the tradeable set already uses. A wrong
    `primaryExchange` is the same None lookup by another route — the comment in the module says so,
    and the reference set must not quietly adopt a different convention."""
    import api.providers.ibkr as mod

    monkeypatch.setattr(mod, "_tradeable_symbols", lambda: ("AAPL",))
    for c in mod._data_contracts():
        assert c.secType == "STK"
        assert c.exchange == "SMART"
        assert c.currency == "USD"


def test_an_EMPTY_tradeable_set_still_raises(monkeypatch):
    """The reference set must not paper over the empty-universe failure. An IBKR deployment with no
    tradeable symbols can only trade what reconciliation drags in, and `_tradeable_symbols` raises
    for that reason. Adding reference contracts must not make that error unreachable."""
    import api.providers.ibkr as mod

    def _boom():
        raise RuntimeError("feed config declares no universe symbols")

    monkeypatch.setattr(mod, "_tradeable_symbols", _boom)
    with pytest.raises(RuntimeError, match="no universe symbols"):
        mod._data_contracts()


def test_the_DATA_client_declares_them_too(monkeypatch):
    """THE SEAM, and I got this wrong first.

    The refusals come from `DataClient-INTERACTIVE_BROKERS`, and the data client's provider is a
    DIFFERENT object from the exec client's — it never sees an order and the exec provider never sees
    a bar request. My first version wired the union into `build()` alone, which would have shipped a
    fix that changed nothing observable: the compass would still have had no history.

    `build_data`'s provider carried NO `load_contracts` at all.
    """
    import inspect

    import api.providers.ibkr as mod

    src = inspect.getsource(mod.build_data)
    assert "load_contracts=_data_contracts()" in src, (
        "the DATA client does not declare the contracts — it is the one that requests bars, so the "
        "compass would still be refused with 'instrument not found' (#606)"
    )


def test_the_EXEC_client_is_NOT_given_the_reference_set():
    """REFERENCE INSTRUMENTS MUST NOT BECOME TRADEABLE, and my first version made them so.

    `load_contracts` on the exec client is the set it may TRADE. Putting the union there widened the
    tradeable surface by 27 ETFs to fix a display plane — caught by the existing tripwire
    `test_ibkr_instrument_universe.py::test_the_exec_client_is_told_which_contracts_it_may_trade`,
    which pins that set exactly. The data client is where bars are requested and where the reference
    set belongs.
    """
    import inspect

    import api.providers.ibkr as mod

    src = inspect.getsource(mod.build)
    assert "_data_contracts()" not in src, (
        "the exec client was given the compass reference set — those are graded, never held, and "
        "this makes 27 ETFs tradeable to fix the market tab (#606)"
    )
    assert "_tradeable_symbols()" in src
