"""One settings read must yield BOTH the slot name and the time, for every strategy that has a slot.

WHY THIS FILE EXISTS
--------------------
Three occurrences of one shape in two days, in three different files:

  QC345   `_open_offset_from_settings` read the configured slot, kept the MINUTES, threw the NAME away,
          and the gateway was constructed without `slot=`. Setting `QC345-003_SLOTS = ["open+150m"]`
          gave a strategy that DECIDED at 12:00 and JOURNALLED under "open+5m".
  QC27    the same code, copied hours later — with the literal-vs-shipped-offset half correctly fixed
          (`_decision_slot()` derives the default from kumo-strategies) and the settings-vs-journal
          half still open. `TECHIVOL-005_SLOTS = ["open+45m"]` schedules at 45m, journals "open+150m".
  QC27's own docstring names the bug it still had: "the two drifting is exactly the bug that made the
          slot read `open+5m` while the strategy filled at 12:00."

A per-file fix produced a per-file recurrence, so the derivation is shared now and this file asserts
that no strategy module re-derives it privately.

WHY THE SLOT STRING IS NOT A LABEL
----------------------------------
It is part of `uq_exec_one_decision_per_session (strategy_id, session, slot)`, and it is what
`decided_this_session` and `_resume` match on. A wrong name means the record claims a decision at a time
that never happened, and a session the strategy never ran can be refused as already decided — which
presents as a silent skip, the failure this codebase has spent three days removing.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_STRATEGIES = pathlib.Path(__file__).resolve().parent


def _settings(monkeypatch, mapping, registered):
    import sys

    import api
    import api.strategy_registry as reg

    fake = type("m", (), {"resolve": staticmethod(lambda d: dict(mapping))})
    monkeypatch.setitem(sys.modules, "api.settings", fake)
    monkeypatch.setattr(api, "settings", fake, raising=False)
    monkeypatch.setattr(
        reg, "REGISTRY", tuple(type("E", (), {"strategy_id": s})() for s in registered), raising=True
    )


@pytest.mark.parametrize(
    "strategy_id,configured,minutes",
    [("QC345-003", "open+150m", 150), ("TECHIVOL-005", "open+45m", 45)],
)
def test_one_read_yields_both_halves(monkeypatch, strategy_id, configured, minutes):
    """The shared derivation. A pair cannot be edited apart; two accessors can."""
    from strategies.momentum import slot_and_offset_from_settings

    _settings(monkeypatch, {f"{strategy_id}_SLOTS": [configured]}, {strategy_id})
    assert slot_and_offset_from_settings(strategy_id, "open+5m") == (configured, minutes)


def test_an_unusable_setting_falls_back_on_BOTH_halves(monkeypatch):
    """A fallback replacing only the offset would create the divergence it exists to avoid."""
    from strategies.momentum import slot_and_offset_from_settings

    _settings(monkeypatch, {"QC345-003_SLOTS": ["close-20m"]}, {"QC345-003"})
    assert slot_and_offset_from_settings("QC345-003", "open+5m") == ("open+5m", 5)


def test_unreadable_settings_do_not_stop_a_strategy_deciding(monkeypatch):
    """The worse direction. A strategy that never decides announces nothing."""
    import sys

    import api

    def _boom(_d):
        raise RuntimeError("settings unreachable")

    fake = type("m", (), {"resolve": staticmethod(_boom)})
    monkeypatch.setitem(sys.modules, "api.settings", fake)
    monkeypatch.setattr(api, "settings", fake, raising=False)
    from strategies.momentum import slot_and_offset_from_settings

    assert slot_and_offset_from_settings("QC345-003", "open+30m") == ("open+30m", 30)


def _private_offset_parsers() -> list[str]:
    """Any FUNCTION that turns a slot string into minutes on its own."""
    found = []
    for path in sorted(_STRATEGIES.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            # Inside a function only, and not the shared derivation itself. The scan first flagged
            # `slot_and_offset_from_settings` — the thing it exists to protect — and a module-level
            # constant deriving a DEFAULT from its own literal. Neither is the defect: the defect is a
            # function that READS SETTINGS and keeps one half.
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name == "slot_and_offset_from_settings":
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "attr", None) != "removeprefix":
                    continue
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and arg.value == "open+":
                    found.append(f"{path.name}:{fn.name}:{node.lineno}")
    return found


def test_no_strategy_parses_a_slot_into_minutes_privately():
    """AIMED AT THE CLASS, which is the only thing that stops a fourth occurrence.

    Each instance was a correct-looking local parse. The defect is not the arithmetic — it is that a
    module doing its own parse keeps whichever half it needs and silently drops the other. Whoever adds
    the next lane copies the nearest example; this makes the nearest example the shared one.
    """
    offenders = _private_offset_parsers()
    assert offenders == [], (
        f"these functions parse a slot into minutes themselves instead of using "
        f"`slot_and_offset_from_settings`, which is how the name and the time drifted apart three "
        f"times already: {offenders}"
    )


def test_qc345s_gateway_is_given_the_configured_slot():
    """Deriving the pair and then not passing it is the defect unchanged — QC345 shipped exactly that."""
    tree = ast.parse((_STRATEGIES / "qc345.py").read_text())
    call = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "QC345SessionGateway"),
        None,
    )
    assert call is not None, "QC345SessionGateway is no longer constructed — this test is blind"
    kw = next((k for k in call.keywords if k.arg == "slot"), None)
    assert kw is not None and not isinstance(kw.value, ast.Constant), (
        "qc345.py builds its gateway without a settings-derived `slot=`, so it journals its default "
        "while the schedule follows settings"
    )


def test_the_slot_NAME_and_the_FIRE_TIME_are_one_derivation_upstream():
    """CLOSED UPSTREAM 2026-08-22, and this is what replaces the guard rather than deleting it blind.

    The old test held open a gap cockpit could not close: `QC27SessionRunner` took no slot, so the
    OFFSET followed settings and the NAME had nowhere to go. It was re-aimed once when upstream widened
    the signature — and that turned out to be the smaller half. kumo-strategies found the parameter was
    accepted and then DISCARDED: all twelve uses in the body read the module constant, including

        if await self.journal.decided_this_session(session, slot=DECISION_SLOT)

    which is the RESTART GUARD, not a label. `(strategy_id, session, slot)` is unique, so the slot IS
    the idempotency key: a second slot in one session asked "has open+150m decided?" about a slot that
    never ran. A parameter accepted and ignored is worse than a missing one — a missing one is a
    TypeError at the call, loud and immediate, which is what saved us on 2026-08-17.

    Their fix derives the name from the offset actually fired (`f"open+{int(self._open_offset)}m"`),
    which is the inverse of what cockpit does to GET the offset, so name and fire time round-trip and
    cannot disagree. Both lanes pass it now — qc345 had the same defect and no report, because nothing
    had looked.

    So cockpit asserts the PROPERTY it depends on, not the plumbing it no longer owns: the name a lane
    journals must be derivable from the offset cockpit gave it.
    """
    import inspect

    for mod_name in ("qc27_rotation", "qc345_rotation"):
        mod = __import__(f"kumo_strategies.runtime.nautilus.{mod_name}", fromlist=[mod_name])
        src = inspect.getsource(mod)
        calls = [ln.strip() for ln in src.splitlines() if "_runner.run(" in ln]
        assert calls, f"{mod_name}: the runner call moved — this test can no longer see it"
        assert all("slot=" in c for c in calls), (
            f"{mod_name} calls run() without naming a slot ({calls}), so every row of that session is "
            f"filed under the runner's module default"
        )
        assert "self._open_offset" in src, (
            f"{mod_name} no longer derives its slot name from the offset it fires at — name and fire "
            f"time can disagree again, which is the whole defect"
        )
