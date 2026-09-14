"""The config this cockpit actually deploys must be one the live runner can honour.

`ExitConfig` carries four exit rules, but only some are implemented in `PgSessionRunner`; the rest
are silently ignored by the thing holding real positions (kumo-cockpit#197 B12). kumo-strategies has
a coverage test, but it can only assert against a hardcoded copy of these values — which proves
nothing about what ships, because the copy and `live_config()` drift the moment anyone edits here.

This is the gate that actually binds: it imports the real config and asks the real classifier.
"""

from kumo_strategies.strategies.momentum_rotation.config import unsupported_live_exits

from strategies.momentum import live_config


def test_the_deployed_config_asks_for_no_exit_rule_live_ignores():
    """If this fails, DO NOT DEPLOY. The strategy would enter positions on one ruleset and manage
    them with a smaller one — and the runner will suppress entries for every session until it is
    fixed, so the book drifts to cash while the journal explains why to nobody watching."""
    unsupported = unsupported_live_exits(live_config().exits)
    assert unsupported == [], (
        f"backend/strategies/momentum.py configures {unsupported}, which PgSessionRunner does not "
        f"implement. Either implement it live and add it to LIVE_SUPPORTED_EXITS in kumo-strategies, "
        f"or remove it from the deployed config."
    )


def test_the_deployed_config_still_matches_the_validated_research():
    """8/5 with give_back 0.5 is what research/residual-gate/run_verified.py:45 validated. A silent
    drift here means live stops being the thing that was measured — which is the whole subject of
    #197. Change this test deliberately, alongside a new backtest, never to make a build pass."""
    cfg = live_config()
    assert (cfg.portfolio.n_hold, cfg.portfolio.buffer) == (8, 5)
    assert cfg.exits.give_back_frac == 0.5
