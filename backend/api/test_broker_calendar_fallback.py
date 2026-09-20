"""A provider that supplies no calendar of its own still gets one, if the broker can (#739).

WHY THIS EXISTS. #734's end-of-day capture schedules off the VENUE's close and refuses to arm without
a calendar. Only the IBKR provider declares `supplies_trading_calendar`, so the PAPER instance — the
one holding the trading history the whole feature exists to describe — could not capture at all.

I FIRST WROTE A SECOND ALPACA CALENDAR. That was wrong, and review found it: kumo-trading-strategies already
ships `AlpacaCalendar`, `build_calendar()` already selects it when APCA credentials are present, and
it already does the two things my version got wrong — tz-AWARE ET times, and `session` as a `date`
rather than a string. It also fetches with sync urllib, which dissolves the event-loop problem my
version had: a Nautilus clock callback does not run on the node's loop, and every configuration I
tried raised. ~150 lines deleted for ~10 that use what exists.

THE FALLBACK IS `require_exchange=True`, WHICH REFUSES RATHER THAN DEGRADES. `build_calendar` returns
a HOLIDAY-UNAWARE weekday calendar when nothing better is available, and that must never reach the
capture: it would schedule a session on Thanksgiving and file a phantom close. Refusing gives `None`,
which `schedule_capture` already reads as "do not arm" and SAYS so in the boot log. Three states —
a real calendar, a considered refusal, and never the plausible one.
"""

from __future__ import annotations

import pytest

from api.engine_node import broker_calendar_or_none


def test_a_broker_calendar_is_returned_when_the_credentials_are_there(monkeypatch):
    """On paper this is `AlpacaCalendar` — the one kumo-trading-strategies already ships and
    `build_calendar` already selects. The point of the seam is that cockpit stops being the layer
    that knows which vendor supplies it."""
    monkeypatch.setenv("APCA_API_KEY_ID", "probe-key-not-a-real-credential")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "probe-secret-not-a-real-credential")
    cal = broker_calendar_or_none()
    assert cal is not None
    # All three, because `build_calendar` refuses a calendar missing any: `day` + `is_trading_day`
    # alone satisfy warm/next_slot_fire/elapsed_slots and then raise AttributeError the first time a
    # lane arms — inside arming, which catches and retries, i.e. silently unarmed.
    for name in ("day", "is_trading_day", "next_fire"):
        assert callable(getattr(cal, name, None)), f"missing {name}"


def test_NO_credentials_gives_NONE_and_never_the_HOLIDAY_UNAWARE_fallback(monkeypatch):
    """THE REFUSAL IS THE POINT. `build_calendar` without `require_exchange` hands back a weekday
    calendar that will happily schedule a session on Thanksgiving, and nothing in the logs
    distinguishes it from the real thing. For a capture that STAMPS A SESSION DATE, a plausible
    calendar is worse than none: `None` makes `schedule_capture` decline and say why."""
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert broker_calendar_or_none() is None


def test_the_returned_calendar_speaks_the_CONTRACT_the_capture_reads(monkeypatch):
    """`should_capture` reads `day.session`, `day.open_at`, `day.close_at`, and REFUSES a mixed
    aware/naive comparison. The contract says `open_at` is tz-AWARE ET; my own version returned naive
    and would have hit that refusal on every tick, forever, because `capture_once` compares against
    an AWARE `pd.Timestamp.utcnow()`. Pinned here so a future substitution cannot regress it."""
    monkeypatch.setenv("APCA_API_KEY_ID", "probe-key-not-a-real-credential")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "probe-secret-not-a-real-credential")
    from kumo_strategies.runtime.calendar import TradingDay

    fields = TradingDay.__dataclass_fields__
    assert set(fields) >= {"session", "open_at", "close_at"}
    # FIXTURE PROPERTY: the contract must actually SAY aware, or this test pins nothing.
    import inspect

    src = inspect.getsource(TradingDay)
    assert "tz-aware" in src, f"the contract no longer states awareness; re-read it: {src[:200]}"


def test_the_DISPLAY_ACTOR_actually_uses_the_fallback_when_its_provider_supplies_none():
    """MUTATION SURVIVOR, AND IT IS THIS TICKET'S OWN DEFECT IN MY OWN FIX FOR IT.

    #739 is "the calendar is constructed nowhere in production". I wrote `broker_calendar_or_none`,
    tested it directly, and deleting its use from the actor's constructor left all three tests
    green — a helper that exists, is tested, and is called by nothing. The third time this exact
    shape has appeared today, twice in code I wrote after finding it in someone else's.

    Resolves a real AST node inside the constructor's assignment, not a substring: the name in a
    comment or an import satisfies a text search, which is how the orphan guard misses this.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent / "engine_node.py").read_text())
    inits = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__"]
    # FIXTURE PROPERTY FIRST: find the assignment we mean, or the search below passes vacuously.
    assigns = [
        n for init in inits for n in ast.walk(init)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_venue_calendar" for t in n.targets)
    ]
    assert assigns, "no `self._venue_calendar = ...` found — this test is looking at nothing"

    called = {
        node.func.id for a in assigns for node in ast.walk(a)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "broker_calendar_or_none" in called, (
        f"the actor does not fall back to the broker's calendar, so a provider supplying none "
        f"leaves the capture unarmed — which is #739 itself. Calls found: {sorted(called)}"
    )
    assert "VenueCalendar" in called, "the adapter-supplied path must still be preferred"
