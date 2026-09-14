"""The gate, and what happens on either side of it."""

from __future__ import annotations

import pytest

from strategies.momentum import _enabled, build_momentum_strategy


def test_off_by_default(monkeypatch):
    """CLAUDE.md: all new automation gates default False. An unset env var must not trade."""
    monkeypatch.delenv("KUMO_MOMENTUM_ENABLED", raising=False)
    assert _enabled() is False
    assert build_momentum_strategy() is None


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_only_an_explicit_yes_enables_it(monkeypatch, value):
    monkeypatch.setenv("KUMO_MOMENTUM_ENABLED", value)
    assert _enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_explicit_values_enable_it(monkeypatch, value):
    monkeypatch.setenv("KUMO_MOMENTUM_ENABLED", value)
    assert _enabled() is True


def test_enabled_but_unwired_raises_instead_of_returning_none(monkeypatch):
    """A strategy that is switched ON and silently absent is the worst outcome: the flag is set, the
    node boots clean, and nothing ever trades or explains why."""
    monkeypatch.setenv("KUMO_MOMENTUM_ENABLED", "1")
    with pytest.raises(Exception) as e:
        build_momentum_strategy()
    assert not isinstance(e.value, AssertionError)


# -- venue resolution -----------------------------------------------------------------------------
#
# SIX TESTS LIVED HERE AND HAVE MOVED (#622). They drove `_instrument_ids`, which built TICKER.MIC
# ids from ALPACA's asset list on every venue and is now deleted — it is why an IBKR-only node could
# not boot at all (`build_node()` raised; 13 restarts; /positions served [] against 22 held
# positions).
#
# The properties did not go away, they moved to where resolution now happens — the strategy's
# `on_start`, from the instruments the attached adapter loaded:
#
#   kumo-strategies  tests/runtime/nautilus/test_symbols_resolve_through_nautilus.py
#     test_a_NYSE_name_does_not_become_XNAS          (was: venue comes from the symbol, not a default)
#     test_an_UNRESOLVABLE_symbol_is_DROPPED_and_NAMED  (was: dropped and NEVER defaulted / all named)
#     test_on_start_resolves_the_venue_from_the_CACHE
#
# ONE PROPERTY DELIBERATELY CHANGED, and it is the point of the ticket. "A pool that resolves to
# NOTHING still RAISES" was correct when resolution happened inside `build_node`; it is wrong now.
# Raising there killed the node and with it every other lane and the whole UI feed — the JEPO
# outage. A lane that can buy nothing is a broken LANE. It now reports and keeps the node up:
#
#   kumo-strategies  test_resolving_NOTHING_does_not_take_the_node_down
#
# What remains in COCKPIT is the compass's own resolver, which reads the same Cache and is pinned in
# `api/test_rotation_resolves_in_one_fetch.py`.













def test_bctrot_registers_inert_and_claims_nothing(monkeypatch):
    """BCTROT-004 alongside MOMENTUM-002 (kumo-strategies#32, the operator 2026-08-16).

    Two properties that must hold before it goes near a node:

      IT CLAIMS NOTHING. `external_order_claims` are EXCLUSIVE node-wide and collide at
      `Trader.add_strategy`, which raises InvalidConfiguration rather than degrading. BCTROT and
      MOMENTUM trade the SAME pool, so any instrument given to both stops the node booting.

      ITS TAG IS 004, PASSED EXPLICITLY. The adapter DEFAULTS to 003, which QC345 holds.
    """
    from kumo_strategies.runtime.nautilus.bctrot_rotation import BCTRotationStrategy
    from kumo_strategies.strategies.momentum_rotation.candidates import StaticList

    from strategies.momentum import live_config

    s = BCTRotationStrategy(
        cfg=live_config(), source=StaticList(["AAA"]), instrument_ids=[],
        strategy_name="BCTROT", order_id_tag="004",
        decision_slots=("open+150m", "close-20m"),
    )
    assert str(s.id) == "BCTROT-004", "BCTROT would be booked under the wrong strategy"
    assert s.claimed_instruments == [], "BCTROT claims instruments MOMENTUM may also claim"


def test_the_node_registers_bctrot_next_to_momentum():
    """The call site. A builder nothing calls registers nothing — the shape that has hidden four
    defects in this session alone."""
    import inspect

    from api import engine_node

    source = inspect.getsource(engine_node)
    assert "build_bctrot_strategy" in source, "the node never builds BCTROT"
    assert "add_strategy(bctrot)" in source, "BCTROT is built and never registered"


def test_momentum_still_claims_its_own_held_positions():
    """The refactor must not have widened or dropped MOMENTUM's claims — they are what stop a
    reconciliation-generated flatting order opening a phantom position under EXTERNAL (#197 B8)."""
    import inspect

    from strategies import momentum

    source = inspect.getsource(momentum._build_rotation)
    assert "claims_from" in source
    assert "PositionState.strategy_id == claims_from" in source, (
        "claims are no longer scoped to the strategy that holds them"
    )


def test_bctrots_builder_asks_for_NO_claims_at_all():
    """The hazard is not an empty query, it is a WRONG one.

    Removing the `claims_from is None` guard changes nothing — the query then filters on None and
    returns no rows either way. What would actually stop the node booting is BCTROT being given
    MOMENTUM's claims, since the two trade the same pool and `external_order_claims` collide at
    `Trader.add_strategy`. So pin the argument rather than the query.
    """
    import inspect

    from strategies import momentum

    source = inspect.getsource(momentum.build_bctrot_strategy)
    assert "claims_from=None" in source, "BCTROT is being given claims it must not have"

    momentum_source = inspect.getsource(momentum.build_momentum_strategy)
    assert 'claims_from="MOMENTUM-002"' in momentum_source, "MOMENTUM lost its own claims"
