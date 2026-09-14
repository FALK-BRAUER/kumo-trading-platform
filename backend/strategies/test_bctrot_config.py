"""BCTROT-004 no longer shares MOMENTUM-002's config, so both halves of that need guarding.

The lanes were deliberately identical except for schedule — "one change, one number". Splitting them
was the operator's call on 2026-09-04, and it creates two new ways to be wrong: BCTROT could fail to get the
changes, or MOMENTUM could silently receive them. The second is worse, because MOMENTUM-002 is the
reference the comparison is measured against.
"""

import ast
import inspect
import pathlib
from dataclasses import fields

import pytest

from kumo_strategies.strategies.momentum_rotation.config import unsupported_live_exits

from strategies.momentum import bctrot_config, live_config


def test_MOMENTUM_did_not_move():
    """The reference lane. If this fails the live A/B is comparing two changed things."""
    m = live_config()
    assert m.score.lookback == 20, "MOMENTUM's ranking lookback must stay at the shipped value"
    assert m.execution.min_abs_gap_pct is None, "MOMENTUM must not acquire BCTROT's entry filter"



def test_BCTROT_gets_the_ranking_and_entry_changes():
    b = bctrot_config()
    assert b.score.lookback == 40
    assert b.execution.min_abs_gap_pct == 0.015


def test_NEITHER_lane_sizes_to_the_book():
    """`size_to_book` measured +2.72pp but sizes at ENTRY and never rebalances, so weights end up
    decided by arrival order. The fix is target-based sizing, which needs partial sells — and
    `NautilusBroker.exit` cancels the position's protective stop before selling, so a partial sell
    would leave the remainder unprotected with nothing re-arming it.

    Shipping it would put a known structural defect into a live book for a benefit measured only in
    a regime (mean 3.40 names against n_hold=8) where that defect cannot surface."""
    assert bctrot_config().portfolio.size_to_book is False
    assert live_config().portfolio.size_to_book is False


def test_the_gap_rule_carries_NO_slot_list():
    """The gap is `today's open / yesterday's close`, fixed once the market opens, so all three of
    BCTROT's decisions reach the same verdict. A slot list would mean the rule had been rebuilt on
    the fill price again — a quantity that changes through the day."""
    assert not hasattr(bctrot_config().execution, "min_abs_gap_slots"), (
        "min_abs_gap_slots is back; the gap does not vary within a session")


def test_the_two_configs_differ_ONLY_where_intended():
    """Catches a change smuggled in by `replace` on a nested dataclass — the whole config is rebuilt,
    so an unintended field is as easy to move as an intended one."""
    m, b = live_config(), bctrot_config()
    changed = {f.name for f in fields(m)
               if getattr(m, f.name) != getattr(b, f.name)}
    assert changed == {"score", "execution"}, f"unexpected top-level divergence: {changed}"

    score_changed = {f.name for f in fields(m.score)
                     if getattr(m.score, f.name) != getattr(b.score, f.name)}
    assert score_changed == {"lookback"}, f"unexpected ranking divergence: {score_changed}"

    exec_changed = {f.name for f in fields(m.execution)
                    if getattr(m.execution, f.name) != getattr(b.execution, f.name)}
    assert exec_changed == {"min_abs_gap_pct"}, f"unexpected execution divergence: {exec_changed}"


@pytest.mark.parametrize("factory", [live_config, bctrot_config], ids=["momentum", "bctrot"])
def test_neither_config_asks_for_an_exit_rule_the_LIVE_runner_ignores(factory):
    """The same gate `test_live_config.py` applies to MOMENTUM. BCTROT now has its own config and
    would otherwise have no such gate at all — `max_hold_days=15` is recommended by the same
    research these changes come from and is NOT implemented live, so this is a live tripwire rather
    than a hypothetical."""
    assert unsupported_live_exits(factory().exits) == []


def test_the_BCTROT_BUILDER_actually_passes_bctrot_config():
    """`bctrot_config()` being correct proves nothing about what boots. `_build_rotation` defaults to
    `live_config`, so dropping the keyword leaves BCTROT running MOMENTUM's config with every test
    above still green — a config that exists, typechecks, deploys and is never read (#26).

    Bound to the AST of the call rather than to a substring: a grep assertion is satisfied by a
    comment mentioning the name and broken by deleting one.
    """
    import strategies.momentum as mom

    tree = ast.parse(inspect.getsource(mom.build_bctrot_strategy))
    passed = [kw.value.id for call in ast.walk(tree) if isinstance(call, ast.Call)
              for kw in call.keywords
              if kw.arg == "config_factory" and isinstance(kw.value, ast.Name)]
    assert passed == ["bctrot_config"], (
        f"build_bctrot_strategy passes config_factory={passed or 'nothing'} — BCTROT would boot on "
        f"MOMENTUM's config and neither lookback=40 nor the gap filter would exist live")
