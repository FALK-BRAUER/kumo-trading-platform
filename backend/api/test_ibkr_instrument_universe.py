"""The IBKR exec client must be told which contracts it may trade.

WHAT HAPPENED, 2026-08-24, on staging-ibkr. BCTROT-004 decided `enter 8` and all 8 died inside the
exec client:

    ExecClient-INTERACTIVE_BROKERS: Error on 'submit_order: ... BUY 62 WPM.XNYS MARKET DAY'
    AttributeError('NoneType' object has no attribute 'is_inverse')

`is_inverse` is an `Instrument` attribute, so the lookup returned None. The IB provider had qualified
exactly 16 instruments and they were EXACTLY the 16 open positions — reconciliation had brought them
in and nothing else ever loaded any. `InteractiveBrokersInstrumentProviderConfig` was constructed with
`convert_exchange_to_mic_venue=True` and NO load directive at all.

So the lane could rank a 98-name pool and submit orders for 16. Any rotation into a name it did not
already hold was unsubmittable, and it failed at the venue where the journal could not see it (#512).

WHY test-alpaca CANNOT SHOW THIS: data and exec are both Alpaca there, so the instrument that priced
the decision is the same object that submits the order. This is the split-broker design (#23,
data=Alpaca / exec=IBKR) failing at its seam, and staging is the only place it is exercised.

`[universe] symbols` was tried first and is the WRONG KNOB — measured: seeding all 98 pool names there
and redeploying still qualified exactly 16, because `[universe]` drives the Alpaca DATA client and
never reaches the IBKR exec client.
"""
from __future__ import annotations

import pytest


def _spec(monkeypatch, symbols, pool=(), exclude=()):
    """The REAL `providers.ibkr.build`, with the account id, the feed universe and THE POOL supplied.

    THE POOL MUST BE STUBBED OR THESE TESTS READ A LIVE TRADING DATABASE. Since #511 `build` unions
    `exec_pool_source` into `load_contracts` via a real asyncpg connect to `KUMO_DATABASE_URL` —
    which on the normal local setup points at the paper database (see conftest.py). Left unstubbed
    these assertions pass only on a machine where port 5432 happens to be CLOSED, because the failed
    read falls back to the declared universe: the test would then be describing the DEGRADED branch
    while claiming to pin the directive, and would fail outright on the operator's own machine.
    """
    from api import feed_config as fc
    from api.providers import ibkr

    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DUTEST001")
    real = fc.load_feed_config

    def _fake(*a, **k):
        cfg = real()
        return cfg.__class__(**{**cfg.__dict__, "symbols": tuple(symbols),
                                "excluded_symbols": tuple(exclude)})

    monkeypatch.setattr(fc, "load_feed_config", _fake)
    monkeypatch.setattr(ibkr, "_pool_symbols", lambda: tuple(pool))
    monkeypatch.setattr(ibkr, "_POOL_CACHE", None, raising=False)
    return ibkr.build({"account_id_env": "IBKR_ACCOUNT_ID", "ibg_client_id": 1})


def test_the_exec_client_is_told_which_contracts_it_may_trade(monkeypatch):
    """Without a load directive the provider holds only what reconciliation brought in, so a lane can
    only ever trade names it ALREADY holds."""
    spec = _spec(monkeypatch, ["AEM", "CGAU", "WPM"])
    provider = spec.config.instrument_provider
    loaded = getattr(provider, "load_contracts", None)
    assert loaded, (
        "the IBKR instrument provider has no load directive — it will hold only the instruments "
        "reconciliation brings in with open positions, and every entry into a new name fails at "
        "submit with AttributeError('NoneType' object has no attribute 'is_inverse')")
    symbols = sorted(c.symbol for c in loaded)
    assert symbols == ["AEM", "CGAU", "WPM"], symbols


def test_every_loaded_contract_is_a_tradeable_US_equity(monkeypatch):
    """`secType` and `exchange` are not decoration: a contract IB cannot resolve is the same None
    lookup by another route, and SMART is what lets it resolve without a per-symbol primaryExchange."""
    spec = _spec(monkeypatch, ["AEM"])
    c = next(iter(spec.config.instrument_provider.load_contracts))
    assert c.secType == "STK"
    assert c.exchange == "SMART"
    assert c.currency == "USD"


def test_the_venue_mapping_that_makes_reconciled_ids_match_is_NOT_dropped(monkeypatch):
    """`convert_exchange_to_mic_venue=True` is what makes IB's reconciled positions carry the same
    TICKER.MIC ids the data provider uses (AAMI.XNYS, INTC.XNAS). Adding a load directive must not
    quietly cost that — the two settings are unrelated and both are required."""
    spec = _spec(monkeypatch, ["AEM"])
    assert spec.config.instrument_provider.convert_exchange_to_mic_venue is True


def test_an_EMPTY_universe_does_not_send_an_empty_load_directive(monkeypatch):
    """An empty frozenset is not "load nothing on purpose", it is the old broken state wearing a
    directive. Fail loudly rather than reproduce the outage."""
    with pytest.raises(Exception):
        _spec(monkeypatch, [])
