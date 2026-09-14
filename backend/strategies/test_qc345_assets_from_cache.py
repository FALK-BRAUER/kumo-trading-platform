"""QC345's ETF/fund filter reads the CACHE, and `assets are required` never kills a session (#647/#624).

THE MEASURED FAILURE. On staging-ibkr — an instance with *"NO business with alpaca whatsoever"*
(instance.env:14) — `_assets_frame` returned None for want of an Alpaca credential, and with the
promoted `fundamental_like` config every QC345 session died at `_decide` with

    ValueError: assets are required for asset_universe_mode='fundamental_like'

as the lane's only journal row. The lane was reaching a vendor for something Nautilus already
carries: `Instrument.info` holds IBKR's `stockType`/`longName` and Alpaca's `name`, in the cache,
before `on_start` (#624).

These tests drive the REAL seam — `QC345SessionGateway._decide` with the real
`QC345ComputedSource` — not a helper. Red evidence (2026-08-29, pre-fix):
  * `test_a_missing_assets_frame_DEGRADES..` failed with the exact production ValueError above;
  * the cache-derived tests failed with `TypeError: unexpected keyword argument 'assets_builder'`
    — the seam did not exist.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pandas as pd
import pytest

from strategies import qc345


# --------------------------------------------------------------------------------------------------
# Fixtures: the promoted config (small windows), a panel, and a cache double built from REAL
# Nautilus instruments — the double must reject what production rejects.
# --------------------------------------------------------------------------------------------------


def _cfg(**over):
    from kumo_strategies.strategies.qc345_rotation import QC345RotationConfig

    base = {
        "lookback_sessions": 2, "liquidity_window": 2, "min_liquidity_history": 2,
        "realized_vol_window": 2, "min_realized_vol_history": 2,
        "liquidity_filter_size": 60, "universe_size": 20, "portfolio_size": 5,
        # The PROMOTED mode — the one that raises without assets. A fixture on "all" cannot reach
        # the failure this file exists for.
        "asset_universe_mode": "fundamental_like", "market_cap_mode": "price_x_dv",
        "momentum_price_field": "close", "corporate_action_window": 252,
        "mania_momentum_threshold": None, "mania_volatility_threshold": None,
    }
    base.update(over)
    return QC345RotationConfig(**base)


def _panel(symbols, *, sessions=8, start="2026-01-26"):
    dates = pd.bdate_range(start, periods=sessions)
    rows = []
    for idx, sym in enumerate(symbols):
        for step, day in enumerate(dates):
            px = 10.0 + idx + step * 0.1
            rows.append({"ticker": sym, "date": day, "open": px, "high": px, "low": px,
                         "close": px, "volume": 1_000_000 + idx * 10_000})
    return pd.DataFrame(rows)


def _equity(symbol: str, mic: str, info):
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Price, Quantity

    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), Venue(mic)),
        raw_symbol=Symbol(symbol), currency=USD, price_precision=2,
        price_increment=Price.from_str("0.01"), lot_size=Quantity.from_int(1),
        ts_event=0, ts_init=0, info=info,
    )


def _strategy_double(instruments):
    """A resolved strategy the way #622 leaves it: `_iids` held, definitions in the cache."""
    by_id = {inst.id: inst for inst in instruments}
    return SimpleNamespace(
        _iids=list(by_id),
        cache=SimpleNamespace(instrument=lambda iid: by_id.get(iid)),
    )


def _instruments(etf_stock_type: str, etf_name: str):
    """Four symbols, three metadata shapes: IBKR common, IBKR classified, Alpaca name-only, and one
    with NO metadata at all (the unclassifiable case)."""
    return [
        _equity("AAA", "XNAS", {"longName": "TRIPLE A INDUSTRIES", "stockType": "COMMON",
                                "contract": {"primaryExchange": "NASDAQ"}}),
        _equity("BBB", "XNAS", {"longName": etf_name, "stockType": etf_stock_type,
                                "contract": {"primaryExchange": "NASDAQ"}}),
        _equity("CCC", "XNAS", {"name": "CCC Corporation Common Stock"}),
        _equity("DDD", "XNAS", {}),
    ]


def _gateway(**kw):
    kw.setdefault("sm", None)
    kw.setdefault("journal", None)
    kw.setdefault("cfg", _cfg())
    kw.setdefault("broker", None)
    kw.setdefault("limits", None)
    return qc345.QC345SessionGateway(**kw)


# --------------------------------------------------------------------------------------------------
# Fixture property first: the promoted config really is the raising one
# --------------------------------------------------------------------------------------------------


def test_FIXTURE_PROPERTY_the_promoted_config_raises_without_assets_in_the_raw_engine():
    """Without this, every degrade assertion below could pass against a config that never needed
    assets — a test that cannot fail carries no information."""
    from kumo_strategies.strategies.qc345_rotation.engine import filter_asset_universe

    with pytest.raises(ValueError, match="assets are required"):
        filter_asset_universe(_panel(["AAA"]), None, _cfg())


def test_FIXTURE_PROPERTY_live_config_still_promotes_fundamental_like():
    """The degrade path is only load-bearing while production actually ships the raising mode."""
    from kumo_strategies.strategies.qc345_rotation import QC345RotationConfig

    assert QC345RotationConfig().asset_universe_mode == "fundamental_like", (
        "the dataclass default moved off fundamental_like — re-check whether this whole file still "
        "tests the shipped config"
    )


# --------------------------------------------------------------------------------------------------
# #647: no assets frame must DEGRADE loudly, never raise — that raise was the staging blocker
# --------------------------------------------------------------------------------------------------


def test_a_missing_assets_frame_DEGRADES_to_venue_only_filtering_and_DOES_NOT_raise(caplog):
    """THE STAGING BLOCKER. Pre-fix this raised the exact production ValueError; the fix must trade
    it for a decided session under venue-only filtering plus an ERROR that says so.

    No assets, no builder — the state a venue with no metadata source leaves the gateway in."""
    gw = _gateway()
    with caplog.at_level(logging.ERROR, logger=qc345._log.name):
        decision, universe_size, terminal = gw._decide(_panel(["AAA", "BBB"]), held=set())
    assert universe_size == 2, "venue-only filtering should rank the full panel"
    assert any("venue-only" in r.message for r in caplog.records), (
        "the degrade happened silently — a fallback that does not announce itself is a silent "
        "wrong answer (CLAUDE.md)"
    )


def test_a_builder_that_RAISES_degrades_the_same_way_and_retries_next_session(caplog):
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("cache exploded")

    gw = _gateway(assets_builder=_boom)
    with caplog.at_level(logging.ERROR, logger=qc345._log.name):
        gw._decide(_panel(["AAA"]), held=set())
        gw._decide(_panel(["AAA"]), held=set())
    assert len(calls) == 2, "a failed build must be retried next session, not cached as None"


# --------------------------------------------------------------------------------------------------
# #624: the classification comes from the cache, and it MOVES the decision
# --------------------------------------------------------------------------------------------------


def test_the_cache_metadata_actually_excludes_the_ETF_verified_by_DISAGREEMENT():
    """Two runs whose caches differ ONLY in BBB's `stockType` must disagree about the universe.
    Agreement at one value proves nothing (a dead frame and a live one look identical); the delta
    of exactly one name is the wire being live.

    THE NAME IS CHOSEN TO PASS THE NAME HEURISTIC — a marker-bearing name like "... DAILY ETF"
    would be excluded by the engine's own predicate whether or not the explicit classification is
    wired, and the first version of this test proved exactly that by staying green under mutation.
    IBKR longNames really do come without markers ("UNITED STS OIL FD LP"-style), which is why
    `stockType` is worth reading at all."""
    from kumo_strategies.strategies.qc345_rotation.engine import is_fundamental_like_asset

    etf_name = "BOGUS CONVEX HOLDINGS INC"
    # FIXTURE PROPERTY: only the explicit classification can exclude this name.
    assert is_fundamental_like_asset(etf_name, "NASDAQ") is True, (
        "the fixture name now trips the name heuristic — the classification wire is masked and "
        "this test can no longer see it"
    )

    symbols = ["AAA", "BBB", "CCC", "DDD"]
    panel = _panel(symbols)

    as_etf = _gateway(assets_builder=lambda: qc345._assets_frame_from_cache(
        _strategy_double(_instruments("ETF", etf_name)), symbols))
    as_stock = _gateway(assets_builder=lambda: qc345._assets_frame_from_cache(
        _strategy_double(_instruments("COMMON", etf_name)), symbols))

    _, n_etf, _ = as_etf._decide(panel, held=set())
    _, n_stock, _ = as_stock._decide(panel, held=set())

    # DDD (no metadata) is excluded in BOTH runs; only BBB's classification differs.
    assert n_stock == 3, f"expected AAA+BBB+CCC ranked, got {n_stock}"
    assert n_etf == 2, f"expected the ETF excluded, got {n_etf}"
    assert n_stock - n_etf == 1, "the venue classification did not travel to the decision"


def test_an_UNCLASSIFIABLE_instrument_is_NAMED_at_ERROR_and_never_silently_ranked(caplog):
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    with caplog.at_level(logging.ERROR, logger=qc345._log.name):
        frame = qc345._assets_frame_from_cache(
            _strategy_double(_instruments("COMMON", "BOGUS INDUSTRIES")), symbols)
    assert frame is not None
    named = [r.message + str(r.args) for r in caplog.records]
    assert any("DDD" in m for m in named), (
        f"the metadata-less symbol was not NAMED at ERROR — records: {named}"
    )
    # Missing name means EXCLUDE (kumo-strategies 4d46290): the row travels with name=None so the
    # engine's own predicate refuses it — reported AND excluded, never silently ranked.
    ddd = frame.loc[frame["symbol"] == "DDD", "name"]
    assert len(ddd) == 1 and ddd.isna().all()


def test_a_WHOLLY_unresolved_strategy_yields_None_not_an_empty_frame(caplog):
    """Empty IS the bug (2026-08-29): a strategy with no resolved instruments must not produce an
    empty frame that quietly excludes the whole universe — it must return None so `_decide` takes
    the LOUD venue-only degrade instead."""
    with caplog.at_level(logging.ERROR, logger=qc345._log.name):
        assert qc345._assets_frame_from_cache(_strategy_double([]), ["AAA"]) is None
        assert qc345._assets_frame_from_cache(None, ["AAA"]) is None


# --------------------------------------------------------------------------------------------------
# #647 half 2: the delisting/status ledger is a PROVIDER capability, three states not two
# --------------------------------------------------------------------------------------------------


def test_NO_reference_ledger_is_reported_as_delisting_detection_OFF(caplog):
    with caplog.at_level(logging.ERROR, logger=qc345._log.name):
        assert qc345._reference_status(None) is None
    assert any("delisting detection" in r.message and "OFF" in r.message for r in caplog.records), (
        "a venue with no reference ledger must say 'delisting detection OFF' loudly, not just "
        "return None"
    )


def test_an_UNREADABLE_ledger_is_its_own_loud_state_not_a_quiet_absence(caplog):
    def _boom():
        raise RuntimeError("venue 503")

    with caplog.at_level(logging.ERROR, logger=qc345._log.name):
        assert qc345._reference_status(_boom) is None
    assert any("unreadable" in r.message for r in caplog.records)


def test_the_ledger_status_travels_into_the_frame_so_terminal_buckets_can_fire():
    """`terminal_buckets` (kumo-strategies engine.py) reads the `status` column by NAME and answers
    'everything is active' without it — wired, tested and dead. 'inactive' is a value the default
    could not produce, so its arrival proves the wire."""
    symbols = ["AAA", "CCC"]
    status = qc345._reference_status(lambda: [
        {"symbol": "AAA", "status": "inactive", "name": "x"},
        {"symbol": "CCC", "status": "active", "name": "y"},
    ])
    frame = qc345._assets_frame_from_cache(
        _strategy_double(_instruments("COMMON", "BOGUS")[:1] + _instruments("COMMON", "B")[2:3]),
        symbols, status_by_symbol=status)
    assert frame is not None and "status" in frame.columns
    assert frame.set_index("symbol")["status"]["AAA"] == "inactive"


def test_WITHOUT_a_ledger_the_frame_carries_NO_status_column_rather_than_a_fake_active():
    """Three states: a fabricated status='active' column would be absence rendered as an answer —
    the engine's own 'no status column -> best-effort active' branch is the honest degrade, and it
    only stays honest if we do not counterfeit the column."""
    frame = qc345._assets_frame_from_cache(
        _strategy_double(_instruments("COMMON", "BOGUS")), ["AAA", "BBB", "CCC", "DDD"],
        status_by_symbol=None)
    assert frame is not None and "status" not in frame.columns
