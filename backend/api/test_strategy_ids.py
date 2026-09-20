"""Tests for the cockpit strategy-id contract (#68 Phase-2 Step 0).

The strategy_id is the cycle-attribution key (NETTING position id = {instrument}-{strategy_id}), so these
guard the *stability* of that contract: MANUAL must stay `MANUAL-001`, and the name must parse back out.
"""

from __future__ import annotations

import pytest
from nautilus_trader.model.identifiers import StrategyId

from api.strategy_ids import (
    ETF_AUTO,
    ETF_AUTO_NAME,
    ETF_AUTO_TAG,
    MANUAL,
    MANUAL_NAME,
    MANUAL_TAG,
    strategy_name,
)


def test_manual_is_stable_two_part_id() -> None:
    # Do not "fix" this to a bare "MANUAL" — Nautilus StrategyId requires a NAME-tag hyphen, and cycles/positions
    # key off this exact value. A change here silently re-homes every MANUAL position.
    assert MANUAL == StrategyId("MANUAL-001")
    assert str(MANUAL) == "MANUAL-001"


def test_etf_auto_is_stable_two_part_id() -> None:
    """`ETF_AUTO-001` is the value out-of-tree backtest artifacts (kumo-trading-strategies) already write into their
    `strategy_id` field. Changing it orphans every artifact produced before the change."""
    assert ETF_AUTO == StrategyId("ETF_AUTO-001")
    assert str(ETF_AUTO) == "ETF_AUTO-001"
    # The parts must stay separable: Nautilus builds the runtime id from
    # `StrategyConfig(strategy_id=NAME, order_id_tag=TAG)`, so the constants are what a future
    # ETF_AUTO strategy config is built from — not a hand-written string.
    assert (ETF_AUTO_NAME, ETF_AUTO_TAG) == ("ETF_AUTO", "001")
    assert str(ETF_AUTO) == f"{ETF_AUTO_NAME}-{ETF_AUTO_TAG}"


def test_strategy_ids_are_distinct() -> None:
    """MANUAL and ETF_AUTO share the `001` tag but must never collide — the NAME is what separates the
    NETTING position ids (`{instrument}-{strategy_id}`) and therefore the per-strategy P&L split."""
    assert ETF_AUTO != MANUAL
    assert strategy_name(ETF_AUTO) != strategy_name(MANUAL)


def test_gem_vt_is_not_a_strategy_id() -> None:
    """Provenance guard (#8 / kumo-trading-strategies momentum-rotation plan): `GEM-VT` names the rotation MODEL,
    not the cockpit strategy. If a future edit ever re-homes ETF_AUTO onto the model name, the persisted
    strategy field stops matching the cockpit contract and the managed book loses attribution."""
    assert ETF_AUTO_NAME == "ETF_AUTO"
    assert "GEM" not in str(ETF_AUTO)


@pytest.mark.parametrize(
    ("strategy_id", "expected"),
    [
        (MANUAL, "MANUAL"),
        (StrategyId("MANUAL-001"), "MANUAL"),
        (StrategyId("MOMENTUM-002"), "MOMENTUM"),
        (StrategyId("ETF_AUTO-001"), "ETF_AUTO"),
        ("MANUAL-001", "MANUAL"),  # accepts raw str too
    ],
)
def test_strategy_name_recovers_the_cockpit_name(strategy_id: StrategyId | str, expected: str) -> None:
    assert strategy_name(strategy_id) == expected


def test_uifeed_strategy_carries_the_manual_id() -> None:
    """The manual-order-owning strategy must construct with the stable MANUAL id (via change_id), not the
    class-name default `UiFeedStrategy-000`. This is the actual Step-0 contract the projection depends on."""
    from nautilus_trader.model.identifiers import ClientId

    from api.engine_node import UiFeedStrategy
    from api.feed_config import load_feed_config

    strat = UiFeedStrategy(load_feed_config(), ClientId("ALPACA"), "")
    assert strat.id == MANUAL


def test_manual_id_is_stable_through_registration() -> None:
    """Guards the id against Nautilus's registration-time rewrite. `Trader.add_strategy` only rewrites a
    strategy to `{name}-{auto_tag}` when its config `order_id_tag is None` — which would silently turn ours
    into `MANUAL-000`. Binding the tag EXPLICITLY (`order_id_tag="001"`) is the exact condition that prevents
    that, so we assert the config carries it. (Verified end-to-end against a live BacktestEngine registration
    out-of-band; that path can't run in-suite — two Nautilus engines per process abort natively.)"""
    from nautilus_trader.model.identifiers import ClientId

    from api.engine_node import UiFeedStrategy
    from api.feed_config import load_feed_config

    strat = UiFeedStrategy(load_feed_config(), ClientId("ALPACA"), "")
    assert strat.config.strategy_id == MANUAL_NAME
    assert strat.config.order_id_tag == MANUAL_TAG, "tag must be explicit or registration rewrites to -000"
    assert strat.config.order_id_tag is not None
