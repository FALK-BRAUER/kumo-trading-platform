"""MOMENTUM's live parameters become operator-changeable WITHOUT changing what they currently are.

WHY THE DEFAULT IS THE WHOLE TEST. The obvious version of this change MOVES `n_hold=8, buffer=5` into
the settings domain and reads them back. That is what codex flagged and it is a live-money defect:
`PortfolioConfig`'s dataclass defaults are `n_hold=5, buffer=3`, and `settings.resolve` never raises —
it fills SCHEMA defaults. So an instance with no `MOMENTUM_*` keys would size 5 names instead of 8, on
the two lanes holding the most capital, and nothing anywhere would report it.

So the researched values stay in this module and settings supplies only an OVERRIDE.

kumo-cockpit#486 step 6.
"""

from __future__ import annotations

import pytest

from strategies.momentum import _RESEARCHED, _live_overrides, live_config


def _domain(monkeypatch, values: dict):
    """Stub `api.settings.resolve` as the ATTRIBUTE the caller resolves.

    `momentum.py` does `from api import settings` then `settings.resolve(...)`, so patching
    `sys.modules["api.settings"]` would not be seen — the package attribute is what is read. That
    exact mistake made an earlier settings test in this repo pass while asserting nothing.
    """
    from api import settings as real

    monkeypatch.setattr(real, "resolve", lambda domain: dict(values), raising=True)


def test_with_NO_settings_the_researched_values_are_used():
    """The landable-today property, and the one a "move it to settings" version would break."""
    assert _live_overrides() == (8, 5, 0.5, None)
    cfg = live_config()
    assert (cfg.portfolio.n_hold, cfg.portfolio.buffer) == (8, 5)
    assert cfg.exits.give_back_frac == 0.5


def test_the_researched_values_are_NOT_the_dataclass_defaults():
    """The reason the default cannot simply be delegated.

    If these ever coincide, the distinction this module makes is invisible and the next person will
    "simplify" it away — so the difference is asserted rather than assumed.
    """
    from kumo_strategies.strategies.momentum_rotation.config import PortfolioConfig

    d = PortfolioConfig()
    assert (d.n_hold, d.buffer) != (_RESEARCHED["n_hold"], _RESEARCHED["buffer"]), (
        f"dataclass defaults are now {(d.n_hold, d.buffer)}, identical to the researched values — "
        f"delegating to them would look safe and this test would stop protecting anything"
    )


def test_an_operator_override_reaches_the_live_config(monkeypatch):
    """The point of the change: no rebuild, no engine restart."""
    _domain(monkeypatch, {"MOMENTUM_N_HOLD": 3, "MOMENTUM_BUFFER": 2,
                          "MOMENTUM_GIVE_BACK_FRAC": 0.25})
    assert _live_overrides() == (3, 2, 0.25, None)
    cfg = live_config()
    assert (cfg.portfolio.n_hold, cfg.portfolio.buffer) == (3, 2)
    assert cfg.exits.give_back_frac == 0.25


def test_a_PARTIAL_override_leaves_the_others_researched(monkeypatch):
    """Overriding one knob must not silently reset the rest to anything."""
    _domain(monkeypatch, {"MOMENTUM_N_HOLD": 6})
    assert _live_overrides() == (6, 5, 0.5, None)


@pytest.mark.parametrize("bad", ["", None, "eight", [], {}])
def test_a_MALFORMED_override_is_ignored_not_obeyed(monkeypatch, bad):
    """A hand-edited settings file is the expected input, not the exception.

    An unparseable value must leave sizing alone. Coercing it to something plausible, or letting it
    raise inside a live session, are both worse than ignoring it.
    """
    _domain(monkeypatch, {"MOMENTUM_N_HOLD": bad})
    assert _live_overrides()[0] == 8


def test_an_UNREADABLE_domain_does_not_change_sizing(monkeypatch):
    """Fail-safe direction. A settings outage must not resize a live portfolio."""
    from api import settings as real

    def _boom(_domain):
        raise RuntimeError("settings unreachable")

    monkeypatch.setattr(real, "resolve", _boom, raising=True)
    assert _live_overrides() == (8, 5, 0.5, None)


def test_BCTROT_shares_the_config_and_the_source_says_so():
    """Both lanes read `live_config()`, deliberately — "so the live comparison is ONE change rather
    than two". An override therefore moves BOTH, and that must be stated where someone will read it
    before assuming they are independent."""
    import inspect

    from strategies import momentum

    src = inspect.getsource(momentum.live_config)
    assert "BCTROT-004 SHARES THIS CONFIG" in src, (
        "the shared-config warning is gone — an operator overriding MOMENTUM would move BCTROT too "
        "with nothing saying so"
    )
