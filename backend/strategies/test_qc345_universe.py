"""Cockpit's universe derivation, checked by DISAGREEMENT (#324, kumo-strategies#43).

The check codex asked for, and the one it explicitly ruled out:

    do NOT compare source output to select_universe(source.panel[date])
    — that is the same derivation

That was the defect in the upstream fidelity test and it is worth naming precisely, because the
shape recurs: both sides came from `source.panel`, computed by `select_universe` under one config.
Whatever it proved, it could not prove the two agreed, because there were never two.

So the comparison here runs across the SEAM cockpit actually owns. Cockpit's job is substrate —
which symbols, which bars, which asset columns. The selection is upstream's. The failure mode is
therefore not "the selection is wrong" but "cockpit handed it different material than the research
had", and every test below perturbs the INPUT and asserts the output moves, or does not.

Run with: PYTHONPATH=~/projects/kumo-strategies/src:.
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

from strategies import qc345_universe as U


def _bars(symbols, *, sessions=8, start="2026-01-26", base=10.0, volume=1_000_000):
    """A panel with a deterministic spread of price and volume across symbols."""
    dates = pd.bdate_range(start, periods=sessions)
    rows = []
    for idx, sym in enumerate(symbols):
        for step, day in enumerate(dates):
            px = base + idx + step * 0.1
            rows.append({"ticker": sym, "date": day, "open": px, "high": px, "low": px,
                         "close": px, "volume": volume + idx * 10_000})
    return pd.DataFrame(rows)


def _assets(symbols, *, names=None, exchange="NASDAQ", status="active"):
    names = names or {}
    return pd.DataFrame([{"symbol": s, "name": names.get(s, f"{s} Inc."),
                          "exchange": exchange, "status": status, "tradable": True}
                         for s in symbols])


def _cfg(**over):
    from kumo_strategies.strategies.qc345_rotation import QC345RotationConfig

    base = {
        "lookback_sessions": 2, "liquidity_window": 2, "min_liquidity_history": 2,
        "realized_vol_window": 2, "min_realized_vol_history": 2,
        "liquidity_filter_size": 60, "universe_size": 20, "portfolio_size": 5,
        "asset_universe_mode": "fundamental_like", "market_cap_mode": "price_x_dv",
        # The PROMOTED live pair, not the dataclass defaults. The upstream fidelity test runs
        # `close_split_dividend`; adjusted prices do not exist live (#319), so a fixture on them
        # would be checking a config we will never run.
        "momentum_price_field": "close", "corporate_action_window": 252,
        "mania_momentum_threshold": None, "mania_volatility_threshold": None,
    }
    base.update(over)
    return QC345RotationConfig(**base)


# --------------------------------------------------------------------------------------------------
# The seam: cockpit's INPUT decides the output, and the test proves it by moving the input
# --------------------------------------------------------------------------------------------------


def test_a_symbol_COCKPIT_NEVER_FETCHED_can_never_be_selected():
    """The failure this whole module exists to prevent, and the one that is invisible downstream.

    A name missing from the fetch does not raise, does not warn, and does not appear as an error. It
    simply never ranks — which reads exactly like a name that was ranked and not selected. That is
    why the fetch is sized by the substrate rather than by the cheap preservation floors.
    """
    cfg = _cfg(universe_size=3)
    symbols = [f"S{i:02d}" for i in range(8)]

    full, _ = U.derive_universe(_bars(symbols), _assets(symbols), cfg)
    # Drop the single strongest name from the FETCH, exactly as an unlucky batch failure would.
    without = [s for s in symbols if s != full[0]] if full else symbols
    partial, _ = U.derive_universe(_bars(without), _assets(without), cfg)

    assert full, "the fixture selected nothing — this test could not detect anything"
    assert full[0] not in partial, (
        "a symbol absent from cockpit's fetch still appeared in the universe — then this test is "
        "not measuring the seam it claims to"
    )
    assert set(partial) - set(full), (
        "dropping the top name changed nothing about the rest — the fixture cannot show that a "
        "missing fetch REPLACES a researched name with a lesser one"
    )


def test_the_selection_is_UPSTREAMS_and_cockpit_does_not_reimplement_it():
    """Cockpit deciding separately what counts as a fund, or which names are liquid enough, would be
    a second derivation of the rule that defines the universe. Two derivations drift."""
    body = inspect.getsource(U.derive_universe)
    assert "QC345ComputedSource(" in body
    for forbidden in ("liquidity_filter_size", "universe_size", "nlargest", "sort_values"):
        assert forbidden not in body, (
            f"derive_universe references {forbidden!r} — the narrowing belongs to the source"
        )
    sub = inspect.getsource(U.substrate)
    assert "is_fundamental_like_asset" in sub, "cockpit re-implements the fund filter"


def test_a_FUND_in_the_substrate_is_removed_by_the_STRATEGYS_filter_not_by_ours():
    """And the fixture asserts its own premise first: that the fund is present before filtering, so
    the assertion below cannot pass because there was nothing to remove."""
    symbols = ["AAA", "QQQ", "BBB"]
    assets = _assets(symbols, names={"QQQ": "Invesco QQQ Trust, Series 1"})

    assert "QQQ" in set(assets["symbol"]), "the fixture has no fund to exclude"
    keep = U.substrate(assets, _cfg())
    assert "QQQ" not in keep and {"AAA", "BBB"} <= set(keep)


def test_an_INACTIVE_asset_is_kept_in_the_reference_data():
    """`terminal_buckets` classifies a stopped holding by reading `status`. Filtering inactive names
    out of the assets frame is exactly how a delisted position stops being visible — the frame would
    report everything as active and `terminal_symbols` would answer {} forever."""
    src = inspect.getsource(U.fetch_assets)
    assert "status=active" not in src, "the asset fetch filters out the names delisting handling needs"
    assert '"status"' in src, "the assets frame carries no status column"


# --------------------------------------------------------------------------------------------------
# The measured floors are a CHECK, not a filter
# --------------------------------------------------------------------------------------------------


def test_the_preservation_floors_are_never_used_to_SIZE_the_fetch():
    """kumo-strategies#43 labels them conformance heuristics, not a law, and the label is binding.

    Prefiltering the fetch on them would be ~an order of magnitude cheaper and would silently become
    wrong the first month a selected name prints below one — undetectably, because the dropped name
    would not be in the panel to be ranked.
    """
    for fn in (U.substrate, U.fetch_daily_bars):
        body = inspect.getsource(fn)
        assert "PRESERVATION_PRICE_FLOOR" not in body and "PRESERVATION_DOLLAR" not in body, (
            f"{fn.__name__} filters on the preservation floors — an 80% cheaper fetch that is right "
            "11 months in 12 is a strategy that occasionally ranks the wrong universe"
        )


def test_a_BREACHED_floor_is_reported_and_NOT_acted_on(caplog):
    """The floors are observed history, and history is the thing that stops being true. Enforcing
    them would make going stale impossible to detect — which was the point of measuring them."""
    import logging

    diag = {"min_selected_price": U.PRESERVATION_PRICE_FLOOR - 1,
            "min_selected_liquidity_proxy": U.PRESERVATION_DOLLAR_VOLUME_FLOOR - 1}
    with caplog.at_level(logging.WARNING, logger=U._log.name):
        U._check_floors(diag)

    assert diag["price_floor_breached"] is True
    assert diag["liquidity_floor_breached"] is True
    assert any("stale" in r.message for r in caplog.records)
    assert "raise" not in inspect.getsource(U._check_floors), (
        "a breached floor aborts the derivation — it is evidence, not a limit"
    )


def test_floors_that_HOLD_produce_no_finding():
    """The other direction. A check that fires either way reports nothing."""
    diag = {"min_selected_price": U.PRESERVATION_PRICE_FLOOR + 1,
            "min_selected_liquidity_proxy": U.PRESERVATION_DOLLAR_VOLUME_FLOOR + 1}
    U._check_floors(diag)
    assert "price_floor_breached" not in diag and "liquidity_floor_breached" not in diag


def test_an_ORDER_OF_MAGNITUDE_substrate_miss_is_surfaced(caplog):
    """~5,523 symbols was measured on local evidence and the listed universe genuinely moves, so
    this is a wide band and a log line, not a gate. But a tenth of it means the asset filter is not
    doing what it did when the research ran, and that shows up downstream only as the strategy
    picking different names for no visible reason."""
    import logging

    symbols = [f"S{i:03d}" for i in range(20)]
    with caplog.at_level(logging.ERROR, logger=U._log.name):
        U.substrate(_assets(symbols), _cfg())
    assert any("outside" in r.message for r in caplog.records), (
        "a 20-symbol substrate against a measured 5,523 was not reported"
    )


# --------------------------------------------------------------------------------------------------
# Fetch mechanics — the failures that are silent rather than loud
# --------------------------------------------------------------------------------------------------


def test_a_FAILED_BATCH_raises_rather_than_being_skipped():
    """A skipped batch is not visible downstream: those symbols merely never rank. The fetch must
    fail loudly instead of quietly narrowing the universe."""
    body = inspect.getsource(U.fetch_daily_bars)
    assert "except" not in body, (
        "the fetch swallows an error somewhere — a dropped batch silently shrinks the universe"
    )
    assert "raise RuntimeError" in body, "an empty fetch does not refuse"


def test_the_fetch_asks_for_SIP_and_RAW_prices():
    """SIP because the selection is a LIQUIDITY ranking and IEX carries a fraction of consolidated
    volume — a median-dollar-volume filter on IEX ranks venue share, not liquidity.

    RAW because adjusted prices are a backtest construct: a live bar is a raw print, and the promoted
    config pairs `momentum_price_field="close"` with a 252-session corporate-action window precisely
    so the strategy handles splits itself (#319).
    """
    body = inspect.getsource(U.fetch_daily_bars)
    assert '"feed": feed' in body and 'feed: str = "sip"' in inspect.getsource(U.fetch_daily_bars)
    assert '"adjustment": "raw"' in body


def test_pagination_is_followed_to_COMPLETION():
    body = inspect.getsource(U.fetch_daily_bars)
    assert "next_page_token" in body and "page_token" in body, "a truncated fetch loses bars silently"


def test_transient_failures_are_RETRIED_with_backoff():
    """This runs at node startup across thousands of symbols. One transient 429 partway through
    would otherwise drop a batch, and a missing batch is indistinguishable from names that were
    ranked and not selected."""
    body = inspect.getsource(U._get)
    assert "429" in body and "delay" in body


@pytest.mark.parametrize("code", [429, 503])
def test_a_RETRYABLE_status_is_retried_and_a_4xx_is_not(code, monkeypatch):
    """Retrying a 401 would turn a wrong credential into a slow wrong credential."""
    import urllib.error

    calls = {"n": 0}

    def _open(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, code, "boom", {}, None)

    monkeypatch.setattr(U.urllib.request, "urlopen", _open)
    monkeypatch.setattr(U.time, "sleep", lambda _s: None)
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")

    with pytest.raises(urllib.error.HTTPError):
        U._get("https://example.test/x", U._headers(), attempts=3)
    assert calls["n"] == 3, f"a {code} was not retried"

    calls["n"] = 0

    def _open401(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 401, "nope", {}, None)

    monkeypatch.setattr(U.urllib.request, "urlopen", _open401)
    with pytest.raises(urllib.error.HTTPError):
        U._get("https://example.test/x", U._headers(), attempts=3)
    assert calls["n"] == 1, "a 401 was retried — a wrong credential became a slow wrong credential"


def test_missing_credentials_RAISE_rather_than_returning_an_empty_universe(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="APCA_API_KEY_ID"):
        U._headers()


def test_derive_universe_CHECKS_THE_FLOORS_by_default():
    """The wiring, not the helper. A mutation that made `derive_universe` skip `_check_floors`
    entirely left the direct-call test above green — it exercises the function, never the call site.
    Same shape as every seam defect in this repo: the unit was right and nothing invoked it."""
    body = inspect.getsource(U.derive_universe)
    assert "_check_floors(diag)" in body, "derive_universe never checks the floors"
    assert "if check_floors:" in body, "the check cannot be disabled for a truncated substrate"

    cfg = _cfg(universe_size=3)
    symbols = [f"S{i:02d}" for i in range(8)]
    # Prices here are ~10-18, far under the 67.08 floor, so a checked run MUST flag it.
    _, diag = U.derive_universe(_bars(symbols), _assets(symbols), cfg)
    assert diag.get("price_floor_breached") is True, (
        "derive_universe did not evaluate the floors on a panel that breaches them by 50 dollars"
    )
    _, skipped = U.derive_universe(_bars(symbols), _assets(symbols), cfg, check_floors=False)
    assert skipped.get("floors_checked") is False
    assert "price_floor_breached" not in skipped, (
        "a truncated run still reported a floor finding it cannot evidence"
    )


def test_the_BUILDER_never_runs_the_derivation():
    """Measured 2026-08-17: a full derivation is 373.8s — 33,431 assets, 1.5M bar rows over 5,499
    symbols. `build_qc345_strategy` runs inside synchronous node startup, so calling this there
    would hold every other strategy AND the UI data feed behind a six-minute REST sweep, for a
    universe that changes monthly.

    Pinned because the shortcut is tempting and its cost is invisible in a unit test: the derivation
    is right, the wiring is right, and the node just takes six minutes to boot.
    """
    from strategies import qc345

    body = inspect.getsource(qc345.build_qc345_strategy)
    for forbidden in ("fetch_daily_bars", "derive_universe", "qc345_universe"):
        assert forbidden not in body, (
            f"build_qc345_strategy calls {forbidden} — a 373.8s fetch on the node's startup path"
        )
    assert "_universe_symbols()" in body, "the builder no longer reads the operator-supplied universe"


def test_the_measured_floors_HELD_on_the_live_run():
    """Recording the outcome, not just the mechanism. Both floors cleared with room on the full
    2026-08-17 run — price 90.20 against 67.08, liquidity 1.118bn against 518.19m. If a future run
    breaches one, that is a real finding rather than an artefact, because this one did not."""
    live = {"min_selected_price": 90.20, "min_selected_liquidity_proxy": 1_118_309_668.14}
    U._check_floors(live)
    assert "price_floor_breached" not in live
    assert "liquidity_floor_breached" not in live
