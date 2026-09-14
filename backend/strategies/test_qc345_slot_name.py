"""The slot QC345 journals must be the slot it actually decides at.

WHY THIS FILE EXISTS
--------------------
Two derivations of one fact, and #360 is what made them able to disagree.

`_open_offset_from_settings()` reads the configured slot — `decision_slots_from_settings(STRATEGY_ID,
(DECISION_SLOT,))` — and then throws the NAME away, returning only the minutes:

    int(slots[0].removeprefix("open+").removesuffix("m"))

The minutes schedule the alert. The name falls back to a literal, because the gateway is constructed
without `slot=` (qc345.py:565) and so keeps its default `DECISION_SLOT = "open+5m"`.

So an operator setting `QC345-003_SLOTS = ["open+150m"]` — a form field, two clicks, exactly what #360
built — gets a strategy that DECIDES at 12:00 and JOURNALS every row under `"open+5m"`.

WHY THAT IS NOT COSMETIC
------------------------
The slot string is not a label. It is part of the idempotency key:

    uq_exec_one_decision_per_session UNIQUE (strategy_id, session, slot) WHERE kind='decision'

and it is what `decided_this_session` and `_resume` match on. A wrong name means the record says the
strategy decided at a time it did not, and a session it never ran can be refused as already decided —
which presents as a strategy that silently skips, the failure this codebase has spent two days removing.

Found by the kumo-strategies peer, who hit the identical defect on QC27: a `DECISION_SLOT` literal of
`"open+5m"` against a shipped offset of 150 minutes. Same class, independently.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _settings(monkeypatch, slots):
    import sys

    import api

    fake = type("m", (), {"resolve": staticmethod(lambda d: {"QC345-003_SLOTS": list(slots)})})
    monkeypatch.setitem(sys.modules, "api.settings", fake)
    monkeypatch.setattr(api, "settings", fake, raising=False)
    import api.strategy_registry as reg

    monkeypatch.setattr(
        reg, "REGISTRY",
        tuple(type("E", (), {"strategy_id": s})() for s in ("QC345-003",)),
        raising=True,
    )


@pytest.mark.parametrize("configured,minutes", [("open+150m", 150), ("open+5m", 5), ("open+45m", 45)])
def test_the_journalled_slot_and_the_scheduled_time_come_from_ONE_read(monkeypatch, configured, minutes):
    """The whole point. Both halves must be derivable together, or they can disagree.

    A pair is the fix: whatever schedules the alert and whatever names the row are the same decision,
    so they cannot be edited apart.
    """
    from strategies.qc345 import _slot_and_offset_from_settings

    _settings(monkeypatch, [configured])
    slot, offset = _slot_and_offset_from_settings()
    assert (slot, offset) == (configured, minutes), (
        f"configured {configured} produced slot={slot!r} offset={offset} — the name and the time "
        f"disagree, so the journal would file rows under a slot the strategy never used"
    )


def test_an_unusable_setting_falls_back_on_BOTH_halves_together(monkeypatch):
    """Fail-safe, and it must be safe in the same direction for both.

    `close-20m` cannot be honoured — QC345 schedules a single alert as an offset from the OPEN — so the
    built-in is used. If only the offset fell back and the name did not, the fallback itself would
    create the divergence it exists to avoid.
    """
    from strategies.qc345 import (
        _DEFAULT_OPEN_OFFSET_MIN,
        DECISION_SLOT,
        _slot_and_offset_from_settings,
    )

    _settings(monkeypatch, ["close-20m"])
    assert _slot_and_offset_from_settings() == (DECISION_SLOT, _DEFAULT_OPEN_OFFSET_MIN)


def test_the_gateway_is_given_the_configured_slot_not_the_literal_default():
    """The seam. Deriving the pair correctly and then not passing it is the defect unchanged.

    `QC345SessionGateway.__init__` defaults `slot=DECISION_SLOT`, so a construction that omits `slot=`
    silently keeps the literal however the settings read turns out.
    """
    from strategies import qc345

    tree = ast.parse(textwrap.dedent(inspect.getsource(qc345)))
    call = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "QC345SessionGateway"),
        None,
    )
    assert call is not None, "QC345SessionGateway is no longer constructed here — this test is blind"
    kw = next((k for k in call.keywords if k.arg == "slot"), None)
    assert kw is not None, (
        "the gateway is built without `slot=`, so it journals the literal DECISION_SLOT while "
        "`open_offset_minutes` schedules from settings — set QC345-003_SLOTS to open+150m and it "
        "decides at 12:00 under a row named open+5m"
    )
    assert not isinstance(kw.value, ast.Constant), (
        "`slot=` is a literal again — it must come from the same settings read as the offset"
    )
