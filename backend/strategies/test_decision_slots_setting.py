"""A strategy's decision time must be changeable by hand, without a rebuild.

WHY THIS FILE EXISTS
--------------------
The operator asked for this repeatedly and it kept coming back as a ticket instead of code. The slots were
Python constants — `qc345.py:49`, and the literals in `momentum.py`'s builders — so moving a decision by
ten minutes meant editing source, rebuilding an image, and restarting a live engine. That restart is the
riskiest operation this system performs and it crash-looped the engine on 2026-08-19. Every other
operational knob here is already settings-driven; the schedule was the gap, and it is the one that
mattered on the day a decision slot was missed and could not be re-run (#359).

WHAT THIS PINS
--------------
Not "a setting exists" — a setting that reaches nothing is the defect category this repo has hit seven
times in a week. It pins that the value REACHES the strategy constructor, that a malformed value cannot
silently move a live decision, and that an unreadable settings store cannot stop a strategy deciding at
all. Both failure directions matter: a typo that moves a decision is bad; a typo that stops the strategy
ever running is worse, because nothing announces it.

#360.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import textwrap

import pytest

_SCHEMA = pathlib.Path(__file__).parent.parent / "config" / "settings" / "strategies.schema.json"


def _settings(monkeypatch, mapping):
    monkeypatch.setitem(
        __import__("sys").modules, "api.settings",
        type("m", (), {"resolve": staticmethod(lambda d: mapping)}),
    )


# -- the operator-facing half ------------------------------------------------------------------


@pytest.mark.parametrize("key", ["MOMENTUM-002_SLOTS", "BCTROT-004_SLOTS", "QC345-003_SLOTS"])
def test_the_setting_is_declared_so_the_ui_renders_it(key):
    """The settings screen is generated from schema — no schema entry, no control, nothing to change.

    That is exactly why this was invisible: the mechanism worked and had nothing to render.
    """
    props = json.loads(_SCHEMA.read_text())["properties"]
    assert key in props, f"{key} is not declared, so the settings screen cannot show it"
    assert props[key]["items"].get("pattern"), (
        f"{key} accepts any string — an operator typo like '5m' or 'open +5m' would be stored and then "
        f"silently ignored at load, which reads as a setting that does not work"
    )


def test_the_declared_pattern_accepts_real_slots_and_rejects_plausible_typos():
    """Assert the fixture can go both ways, or the pattern assertion above proves nothing."""
    import re

    pat = re.compile(json.loads(_SCHEMA.read_text())["properties"]["MOMENTUM-002_SLOTS"]["items"]["pattern"])
    for good in ("open+5m", "open+150m", "close-20m", "close+0m"):
        assert pat.match(good), f"{good} is a real slot and the pattern rejects it"
    for bad in ("5m", "open +5m", "open+5", "openplus5m", "", "noon+5m"):
        assert not pat.match(bad), f"{bad} is not a slot and the pattern accepts it"


# -- the half that actually changes behaviour --------------------------------------------------


def test_a_configured_slot_reaches_the_strategy(monkeypatch):
    from strategies.momentum import decision_slots_from_settings

    _settings(monkeypatch, {"MOMENTUM-002_SLOTS": ["open+45m"]})
    assert decision_slots_from_settings("MOMENTUM-002", ("open+5m",)) == ("open+45m",)


def test_two_slots_survive_in_order(monkeypatch):
    """BCTROT decides twice. Order is the schedule, so it must not be normalised away."""
    from strategies.momentum import decision_slots_from_settings

    _settings(monkeypatch, {"BCTROT-004_SLOTS": ["open+150m", "close-20m"]})
    assert decision_slots_from_settings("BCTROT-004", ("open+5m",)) == ("open+150m", "close-20m")


@pytest.mark.parametrize("raw", [[], ["nonsense"], ["open+5m", "half past two"], [""], ["5m"]])
def test_nothing_unusable_can_move_a_live_decision(monkeypatch, raw):
    """Fail safe. A malformed slot must not partially apply — that is a decision at a time nobody chose."""
    from strategies.momentum import decision_slots_from_settings

    _settings(monkeypatch, {"MOMENTUM-002_SLOTS": raw})
    assert decision_slots_from_settings("MOMENTUM-002", ("open+5m",)) == ("open+5m",)


def test_an_unreadable_settings_store_does_not_stop_the_strategy_deciding(monkeypatch):
    """The other direction, and the worse one. A strategy that never decides announces nothing."""
    from strategies.momentum import decision_slots_from_settings

    def _boom(_domain):
        raise RuntimeError("settings unreachable")

    monkeypatch.setitem(__import__("sys").modules, "api.settings",
                        type("m", (), {"resolve": staticmethod(_boom)}))
    assert decision_slots_from_settings("MOMENTUM-002", ("open+5m",)) == ("open+5m",)


# -- the seam: the value has to be handed to the constructor -----------------------------------


@pytest.mark.parametrize("builder", ["build_momentum_strategy", "build_bctrot_strategy"])
def test_the_builders_pass_the_configured_slots_not_a_literal(builder):
    """Aimed at the class. A hardcoded tuple here is the whole bug, and it is easy to reintroduce.

    `momentum_rotation.py:196` takes `decision_slots` verbatim when passed, so this constructor argument
    is what makes the setting effective at all.
    """
    from strategies import momentum

    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(momentum, builder))))
    calls = {
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "decision_slots_from_settings" in calls, (
        f"{builder} does not read the configured slots, so the setting is stored and never applied — a "
        f"control that visibly does nothing is worse than no control"
    )


def test_qc345_converts_to_the_open_offset_it_actually_schedules_on(monkeypatch):
    """QC345 schedules ONE alert from an offset in minutes (`qc345_rotation.py:165`), not from a slot."""
    from strategies.qc345 import _slot_and_offset_from_settings

    _settings(monkeypatch, {"QC345-003_SLOTS": ["open+30m"]})
    # A PAIR now, not just the offset: the journalled slot name and the scheduled time are two spellings
    # of one fact and were derivable apart, so an operator moving the time left the journal naming a
    # slot the strategy never used.
    assert _slot_and_offset_from_settings() == ("open+30m", 30)


@pytest.mark.parametrize("raw", [["close-20m"], ["open+5m", "close-20m"]])
def test_qc345_refuses_a_slot_it_cannot_express(monkeypatch, raw):
    """`close-20m` has nowhere to go on this strategy. Refused, never reinterpreted.

    Quietly turning a close-relative slot into an open offset would put a live decision at a time the
    operator did not ask for — which is the failure this whole change exists to make impossible.
    """
    from strategies.qc345 import (
        _DEFAULT_OPEN_OFFSET_MIN,
        DECISION_SLOT,
        _slot_and_offset_from_settings,
    )

    _settings(monkeypatch, {"QC345-003_SLOTS": raw})
    # BOTH halves fall back together — a fallback that replaced only the offset would create the
    # divergence it exists to avoid.
    assert _slot_and_offset_from_settings() == (DECISION_SLOT, _DEFAULT_OPEN_OFFSET_MIN)


def test_qc345_hands_the_offset_to_its_constructor():
    """Same seam as the momentum builders — the conversion must actually be passed."""
    from strategies import qc345

    src = inspect.getsource(qc345)
    tree = ast.parse(src)
    call = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "QC345RotationStrategy"),
        None,
    )
    assert call is not None, "QC345RotationStrategy is no longer constructed here — this test is blind"
    kw = next((k for k in call.keywords if k.arg == "open_offset_minutes"), None)
    assert kw is not None, "the configured offset never reaches the strategy"
    assert not isinstance(kw.value, ast.Constant), (
        "the offset is a literal again — the setting would be stored and ignored"
    )


def test_the_default_and_the_constant_cannot_drift():
    """`_DEFAULT_OPEN_OFFSET_MIN` is the parsed form of `DECISION_SLOT`; two spellings of one fact."""
    from strategies.qc345 import _DEFAULT_OPEN_OFFSET_MIN, DECISION_SLOT

    assert DECISION_SLOT == f"open+{_DEFAULT_OPEN_OFFSET_MIN}m"
