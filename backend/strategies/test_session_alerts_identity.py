"""BCTROT's degradation alert was keyed and worded as MOMENTUM's (#651 item 1).

`build_session_observer` defaulted `strategy_id="MOMENTUM-002"` and `_build_rotation` called it with
no argument FOR BOTH LANES. So BCTROT-004's "stopped entering" alert named MOMENTUM-002, shared its
12h dedupe key — one lane's alert suppressed the other's — and BCTROT entering cleared MOMENTUM's
alarm. The same defect shape as issue 54 (`PgJournal` defaulting to MOMENTUM-002), already
fixed twenty lines above the call site: a default that is right for exactly one caller is silently
wrong for every other.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

from strategies.session_alerts import build_session_observer

_MOMENTUM = pathlib.Path(__file__).parent / "momentum.py"


def test_the_observer_has_no_default_identity():
    """Any caller must choose. A default identity is what let two lanes share one alarm."""
    param = inspect.signature(build_session_observer).parameters["strategy_id"]
    assert param.default is inspect.Parameter.empty, (
        f"build_session_observer defaults strategy_id={param.default!r} — a default that is right "
        f"for exactly one caller is silently wrong for every other (this is how BCTROT-004's "
        f"degradation alert came to be keyed as MOMENTUM-002's)"
    )


def test_the_rotation_builder_passes_the_lanes_own_identity():
    """The call site, not the helper: `_build_rotation` builds BOTH lanes, so the observer must be
    handed the lane's `strategy_id` the same way `PgJournal` already is."""
    source = _MOMENTUM.read_text()
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "build_session_observer")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "build_session_observer")
        )
    ]
    # Assert the anchor was found — a sweep that finds nothing proves nothing (CLAUDE.md).
    assert calls, "momentum.py no longer calls build_session_observer — this test is blind; rewrite it"
    for call in calls:
        forwarded = {kw.arg for kw in call.keywords} | {
            arg.id for arg in call.args if isinstance(arg, ast.Name)
        }
        assert "strategy_id" in forwarded, (
            f"momentum.py:{call.lineno} calls build_session_observer without the lane's own "
            f"strategy_id — both lanes then alert (and dedupe, and clear) under one identity"
        )


class _CapturingNotifier:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []
        self.cleared: list[str] = []

    async def send(self, key, alert):
        self.sent.append((key, alert))
        return True

    def clear(self, key):
        self.cleared.append(key)


def _degraded_result():
    class R:
        detail = {"suppressed_entries": ["AAPL"], "unsupported_exits": ["atr_trail"]}
        entered = []

    return R()


def test_the_alert_carries_the_identity_it_was_built_with(monkeypatch):
    """BCTROT-004's degradation must be keyed and worded as BCTROT-004's — a value the old default
    could not produce, so agreement with the default cannot mask a severed wire."""
    import api.notify as notify

    captured = _CapturingNotifier()
    monkeypatch.setattr(notify, "Notifier", lambda: captured)

    observe = build_session_observer(strategy_id="BCTROT-004")
    asyncio.run(observe(_degraded_result()))

    assert len(captured.sent) == 1
    key, alert = captured.sent[0]
    assert key == "strategy_degraded:BCTROT-004", (
        f"degradation keyed {key!r} — sharing MOMENTUM's key means one lane's alert suppresses the "
        f"other's for 12h"
    )
    assert "BCTROT-004" in alert.title and "MOMENTUM" not in alert.body


def test_one_lane_entering_does_not_clear_the_others_alarm(monkeypatch):
    """The clear is keyed per lane too: BCTROT entering must not mark MOMENTUM's condition resolved."""
    import api.notify as notify

    captured = _CapturingNotifier()
    monkeypatch.setattr(notify, "Notifier", lambda: captured)

    class Entered:
        detail = {}
        entered = ["AAPL"]

    observe = build_session_observer(strategy_id="BCTROT-004")
    asyncio.run(observe(Entered()))
    assert captured.cleared == ["strategy_degraded:BCTROT-004"]
