"""The engine-side capture: what arms it, what it refuses, and what it must never stamp (#734 step 4b).

THE WHOLE JOB IS TO BE INERT UNLESS ARMED, and to be LOUD when it cannot do its job. A capture that
silently did not run is indistinguishable from a flat day once the table is read — which is the exact
confusion this table was built to end, so the hook must not be able to create it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from api.eod_hook import (
    EOD_TIMER, capture_once, schedule_capture, session_fill_qty,
)

ET = timezone(timedelta(hours=-4))
CLOSE = datetime(2026, 8, 28, 16, 0, tzinfo=ET)
OPEN = CLOSE.replace(hour=9, minute=30)


@dataclass
class _Day:
    session: str
    open_at: datetime
    close_at: datetime


class _Calendar:
    def __init__(self, days, unknown=()):
        self._days, self._unknown = days, set(unknown)

    def day(self, d):
        if d in self._unknown:
            raise RuntimeError(f"the venue has not described {d}")
        return self._days.get(d)


_TRADING = _Calendar({CLOSE.date(): _Day("2026-08-28", OPEN, CLOSE)})


class _Px:
    def __init__(self, v):
        self._v = float(v)

    def as_double(self):
        return self._v


class _Event:
    """A Nautilus position event. `signed_qty` is the position's qty AFTER the event, which is what
    makes the engine's session delta the SAME definition as the backfill's."""

    def __init__(self, ts_event, signed_qty):
        self.ts_event = ts_event
        self.signed_qty = signed_qty


class _Position:
    def __init__(self, lane, instrument, qty, avg_px=100.0, events=()):
        self.strategy_id = lane
        self.instrument_id = instrument
        self.signed_qty = qty
        self.avg_px_open = _Px(avg_px)
        self.events = list(events)

    def unrealized_pnl(self, price):
        if not hasattr(price, "as_double"):
            raise TypeError("Position.unrealized_pnl takes a Price, not a float")
        return _Px(50.0)


class _Account:
    def __init__(self, balances):
        self._b = balances

    def balances_free(self):
        return self._b


class _Cache:
    def __init__(self, positions, balances=None, strategies=()):
        self._p = positions
        self._strategies = list(strategies)
        self._a = [_Account(balances)] if balances is not None else []

    def positions_open(self):
        return list(self._p)

    def strategy_ids(self):
        """Nautilus's own list of REGISTERED strategies — every lane, including the flat ones. The
        first version derived the lane list from `positions_open()`, which cannot name a lane that
        holds nothing."""
        return list(self._strategies)

    def accounts(self):
        return list(self._a)

    def price(self, instrument_id, price_type):
        return _Px(120.0)


class _Store:
    def __init__(self):
        self.calls = []

    async def write(self, rows, manifests):
        self.calls.append((rows, manifests))
        from api.eod_observation_store import WriteResult
        return WriteResult(len(rows), 0, len(manifests), len(manifests))


class _Clock:
    def __init__(self):
        self.timers = {}

    def set_timer(self, name, interval, callback):
        self.timers[name] = (interval, callback)

    def cancel_timer(self, name):
        self.timers.pop(name, None)


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(("info", m))

    def warning(self, m):
        self.lines.append(("warning", m))

    def error(self, m):
        self.lines.append(("error", m))


class _Actor:
    """Shaped from the display actor: a Nautilus clock, a cache, a log, and an event loop the timer
    callback hands async work to — because Nautilus timer callbacks do not run on it."""

    def __init__(self, cache=None, calendar=_TRADING, env=None):
        self.clock = _Clock()
        self.cache = cache or _Cache([])
        self.log = _Log()
        self._venue_calendar = calendar
        self._eod_env = env if env is not None else {"KUMO_EOD_CAPTURE": "true"}
        self._loop = None


# ==================================================================================================
# ARMING — and the three reasons it stays inert
# ==================================================================================================
def test_the_gate_is_OFF_by_default_and_NO_TIMER_is_registered():
    """Every new automation gate in this repo defaults False. Registering a disarmed timer that
    returns early on each tick would still be a behaviour change — and the tick is where a refusal
    would be logged, so a disarmed-but-registered timer produces noise about a job nobody enabled."""
    actor = _Actor(env={})
    armed = schedule_capture(actor)
    assert armed is False
    assert actor.clock.timers == {}, "nothing may be scheduled while the gate is off"


def test_the_gate_being_OFF_is_SAID_OUT_LOUD_rather_than_silently_skipped():
    """"Not armed" and "armed and never fired" look identical in the table months later. The boot log
    is the only place the difference is recoverable, so it is stated there."""
    actor = _Actor(env={})
    schedule_capture(actor)
    assert any("not armed" in m for _, m in actor.log.lines), actor.log.lines


def test_a_provider_with_NO_CALENDAR_refuses_to_arm_and_says_so():
    """The schedule comes from the VENUE, and a provider that cannot supply one has nothing to
    schedule against. Arming anyway would mean falling back to a fixed 16:00 ET — wrong on every half
    day, and this module exists partly to not do that."""
    actor = _Actor(calendar=None)
    assert schedule_capture(actor) is False
    assert any("no trading calendar" in m for _, m in actor.log.lines), actor.log.lines


def test_ARMED_registers_a_timer_on_the_NAUTILUS_clock():
    """Not asyncio, not launchd. `clock.set_timer` is native, already used by every other periodic job
    in this actor, and works in backtest — the rule this repo broke three times before writing it
    down."""
    actor = _Actor()
    assert schedule_capture(actor) is True
    assert EOD_TIMER in actor.clock.timers
    interval, callback = actor.clock.timers[EOD_TIMER]
    assert callable(callback)
    assert interval.total_seconds() > 0


def test_scheduling_is_its_OWN_function_that_a_test_can_DRIVE():
    """The lesson already written into `_schedule_bar_drain`: asserting that `set_timer` appears
    somewhere in `on_start` did not bite when the registration was disabled, because the text survives
    inside a dead branch. So this is driven directly, and the callback registered is the one invoked
    below."""
    actor = _Actor()
    schedule_capture(actor)
    _, callback = actor.clock.timers[EOD_TIMER]
    callback(None)          # must not raise on the clock thread with no loop attached


# ==================================================================================================
# THE SESSION DELTA — the same quantity as the backfill's, by a different route
# ==================================================================================================
def _ns(dt):
    return int(dt.timestamp() * 1e9)


def test_the_session_delta_is_the_position_qty_MINUS_its_qty_before_the_session_opened():
    """`session_fill_qty` must mean the same thing here as in `session_deltas`, or the column holds
    two quantities under one name and the corporate-action invariant is checking neither.

    The backfill computes `book(D) - book(D-1)` from the fill ledger. Nautilus stamps `signed_qty` on
    every position event as the qty AFTER it, so the last event BEFORE the session open is the same
    `qty_{t-1}` — one definition, two routes, which is exactly what the acceptance gate compares at
    T=now."""
    pos = _Position("MOMENTUM-002", "AEM.XNYS", 14.0, events=[
        _Event(_ns(OPEN - timedelta(days=3)), 10.0),          # held 10 coming in
        _Event(_ns(OPEN + timedelta(hours=1)), 14.0),         # bought 4 today
    ])
    assert session_fill_qty(pos, _ns(OPEN)) == 4.0


def test_a_position_OPENED_during_the_session_has_its_WHOLE_size_as_the_delta():
    """No prior event means the lane held none of it before the bell — 0.0 is the correct prior here,
    and it is a MEASURED prior rather than a default, which is the distinction the column exists for."""
    pos = _Position("BCTROT-004", "WPM.XNYS", 7.0, events=[
        _Event(_ns(OPEN + timedelta(hours=2)), 7.0),
    ])
    assert session_fill_qty(pos, _ns(OPEN)) == 7.0


def test_a_position_merely_HELD_through_the_session_has_a_ZERO_delta_that_is_MEASURED():
    """The one case where 0.0 is true. It must be reachable, or the tests above would pass with an
    implementation that always returns the full quantity."""
    pos = _Position("MOMENTUM-002", "AEM.XNYS", 10.0, events=[
        _Event(_ns(OPEN - timedelta(days=5)), 10.0),
    ])
    assert session_fill_qty(pos, _ns(OPEN)) == 0.0


def test_a_SELL_during_the_session_gives_a_NEGATIVE_delta():
    pos = _Position("MOMENTUM-002", "AEM.XNYS", 6.0, events=[
        _Event(_ns(OPEN - timedelta(days=1)), 10.0),
        _Event(_ns(OPEN + timedelta(hours=3)), 6.0),
    ])
    assert session_fill_qty(pos, _ns(OPEN)) == -4.0


# ==================================================================================================
# THE CAPTURE ITSELF
# ==================================================================================================
def _run(actor, store, now=None, **kw):
    return asyncio.run(capture_once(actor, store, now=now or CLOSE + timedelta(minutes=30), **kw))


def test_it_writes_the_session_with_the_currency_the_ACCOUNT_reports():
    """`observation_rows` has no default currency, deliberately — it used to default to "USD" beside a
    column comment saying staging is SGD. The engine knows, so the engine says."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10.0, events=[
        _Event(_ns(OPEN - timedelta(days=1)), 10.0)])], balances={"SGD": 999_215.19})
    actor, store = _Actor(cache), _Store()
    result = _run(actor, store)
    rows, manifests = store.calls[0]
    assert result.written == len(rows) == 1
    assert rows[0]["currency"] == "SGD", "the account's own currency, never a plausible default"
    assert rows[0]["session_date"] == "2026-08-28"
    assert rows[0]["capture_kind"] == "close"


def test_the_MARK_SOURCE_is_live_because_that_is_what_a_cache_price_IS():
    """It runs twenty minutes AFTER the bell and reads `cache.price(LAST)`. Stamping that "close"
    would put a live quote into a close-to-close series indistinguishably — the exact defect review
    found when `mark_source` was derived from `capture_kind`. The KIND says what the row is; the
    SOURCE says where the number came from. They are different columns because they are different
    facts."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10.0, events=[])], balances={"USD": 100.0})
    store = _Store()
    _run(_Actor(cache), store)
    rows, _ = store.calls[0]
    assert rows[0]["capture_kind"] == "close" and rows[0]["mark_source"] == "live"


def test_an_account_whose_CURRENCY_CANNOT_BE_CHOSEN_writes_a_FAILED_manifest_and_NO_rows():
    """Two balance legs and no USD is a GUESS, and `_single_currency_amount_and_ccy` already refuses
    to make it. The capture inherits that refusal rather than picking one — but it still writes the
    manifest row, because a capture that failed and left nothing behind is indistinguishable from a
    day nobody asked about, which is the whole reason the manifest exists."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10.0, events=[])],
                   balances={"SGD": 1.0, "EUR": 2.0})
    actor, store = _Actor(cache), _Store()
    _run(actor, store)
    rows, manifests = store.calls[0]
    assert rows == [], "no row may carry a guessed currency"
    assert manifests and manifests[0]["status"] == "failed"
    assert "currency" in manifests[0]["detail"]


def test_a_capture_with_NO_ACCOUNT_AT_ALL_also_fails_LOUDLY_rather_than_writing_nothing():
    """Absence of an account is not an empty book. Both produce zero observation rows and they must
    not produce the same record."""
    actor, store = _Actor(_Cache([], balances=None)), _Store()
    _run(actor, store)
    _, manifests = store.calls[0]
    assert manifests and manifests[0]["status"] == "failed"


def test_a_day_the_calendar_REFUSES_writes_NOTHING_AT_ALL_not_even_a_manifest():
    """THE ONE PLACE NOTHING IS WRITTEN, and it needs saying why. A refusal has no session date — that
    IS the refusal — and every row in both tables is keyed by one. Writing under the operator's local
    date is the F7 defect this module already deleted once, and inventing a key is worse than leaving
    a gap the backfill can fill later. It is logged instead."""
    actor, store = _Actor(calendar=_Calendar({}, unknown={CLOSE.date()})), _Store()
    result = _run(actor, store)
    assert store.calls == []
    assert result is None
    assert any("refus" in m.lower() for _, m in actor.log.lines), actor.log.lines


def test_it_does_NOT_fire_before_the_offset_has_passed():
    actor, store = _Actor(), _Store()
    assert _run(actor, store, now=CLOSE + timedelta(minutes=1)) is None
    assert store.calls == []


def test_a_SECOND_capture_of_one_session_does_not_re_ask_the_store():
    """The database is the real guard; this is the cheap check in front of it."""
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10.0, events=[])], balances={"USD": 1.0})
    actor, store = _Actor(cache), _Store()
    _run(actor, store)
    _run(actor, store)
    assert len(store.calls) == 1, "one session, one write attempt from this process"


def test_a_STORE_THAT_RAISES_does_not_take_down_the_actor_but_IS_reported():
    """A display actor must not die on an optional job — and a swallowed exception on every tick is
    indistinguishable from a clean tick, which is the notifier-death pattern already paid for here.
    So it is caught AND logged at error, and the session is NOT marked captured, so the next tick
    retries."""
    class _Boom:
        async def write(self, rows, manifests):
            raise RuntimeError("postgres went away")

    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10.0, events=[])], balances={"USD": 1.0})
    actor = _Actor(cache)
    assert _run(actor, _Boom()) is None
    assert any(lvl == "error" for lvl, _ in actor.log.lines), actor.log.lines

    store = _Store()
    _run(actor, store)
    assert store.calls, "a failed capture must not mark the session done"


def test_the_measured_delta_actually_REACHES_THE_ROW():
    """MUTATION SURVIVOR: `session_fills=None` in the capture left all 17 tests green, because the
    delta was proven in the HELPER and never in a written row. The unit was correct and the wiring
    was untested — the shape that broke production five times in one day and got the rule written
    down. Drive the capture, read the column.

    Two positions with different histories, so a mutant returning one constant for both cannot pass:
    AEM was held 10 coming in and bought 4 more today; WPM was opened today at 7."""
    cache = _Cache([
        _Position("MOMENTUM-002", "AEM.XNYS", 14.0, events=[
            _Event(_ns(OPEN - timedelta(days=3)), 10.0),
            _Event(_ns(OPEN + timedelta(hours=1)), 14.0)]),
        _Position("BCTROT-004", "WPM.XNYS", 7.0, events=[
            _Event(_ns(OPEN + timedelta(hours=2)), 7.0)]),
    ], balances={"USD": 100.0})
    store = _Store()
    _run(_Actor(cache), store)
    rows, _ = store.calls[0]
    by_id = {r["instrument_id"]: r for r in rows}

    # FIXTURE PROPERTY FIRST: the two deltas must DIFFER, or a single-constant mutant survives this
    # test too and it teaches nothing.
    assert by_id["AEM.XNYS"]["session_fill_qty"] != by_id["WPM.XNYS"]["session_fill_qty"]
    assert by_id["AEM.XNYS"]["session_fill_qty"] == 4.0, "held 10, bought 4"
    assert by_id["WPM.XNYS"]["session_fill_qty"] == 7.0, "opened today at 7"
    assert by_id["AEM.XNYS"]["qty"] == 14.0, "and the delta must not be confused with the holding"


# ==================================================================================================
# THE WIRING — because "has a caller somewhere" is not "is armed at boot"
# ==================================================================================================
def test_on_start_ACTUALLY_CALLS_schedule_capture():
    """THE ORPHAN GUARD DOES NOT COVER THIS, and I checked rather than assumed: replacing
    `schedule_capture(self)` with `pass` left it green, because the `from api.eod_hook import
    schedule_capture` line above still counts as a use. A guard satisfied by an import is satisfied by
    dead code.

    This repo's own note on `_schedule_bar_drain` says the same thing from the other direction —
    asserting that `set_timer` appears somewhere in `on_start` did not bite when the registration was
    disabled, because the text survives inside a dead branch.

    So this resolves an actual CALL NODE, not a substring: an `ast.Call` on the name, inside
    `on_start`. A capture that is never armed writes nothing, and nothing is exactly what a flat day
    looks like in this table.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent / "engine_node.py").read_text())
    starts = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "on_start"]
    # FIXTURE PROPERTY FIRST: if `on_start` were renamed or gone, an empty search would pass the
    # `any()` below vacuously.
    assert starts, "no on_start found in engine_node.py — this test is no longer looking at anything"

    called = {
        node.func.id
        for start in starts
        for node in ast.walk(start)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "schedule_capture" in called, (
        "on_start imports schedule_capture but never calls it — the capture is never armed, and an "
        f"unarmed capture is indistinguishable from a flat day. Calls found: {sorted(called)}"
    )


def test_on_stop_CANCELS_the_timer_it_may_have_registered():
    """Symmetry, and cheap. A timer surviving a stop fires against a torn-down cache."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent / "engine_node.py").read_text())
    stops = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "on_stop"]
    assert stops, "no on_stop found — this test is no longer looking at anything"
    names = {
        node.id for stop in stops for node in ast.walk(stop) if isinstance(node, ast.Name)
    }
    assert "EOD_TIMER" in names, "on_stop does not cancel the eod capture timer"


# ==================================================================================================
# A FLAT LANE IS NOT AN ABSENT LANE
# ==================================================================================================
def test_a_lane_holding_NOTHING_still_gets_its_manifest_row():
    """THE TABLE'S STATED PURPOSE, and the live hook could not deliver it. `known_lanes` was derived
    from `cache.positions_open()`, which by construction cannot name a lane that holds nothing — so
    `observed, 0 positions` was UNREACHABLE, and a flat lane was indistinguishable from a lane the
    capture never asked about. That is precisely the distinction migration 0017 exists to make.

    Nautilus already knows: `cache.strategy_ids()` is every REGISTERED strategy, flat or not."""
    cache = _Cache(
        [_Position("MOMENTUM-002", "AEM.XNYS", 10.0, events=[])],
        balances={"USD": 100.0},
        strategies=["MOMENTUM-002", "BCTROT-004", "TECHIVOL-005"],
    )
    store = _Store()
    _run(_Actor(cache), store)
    _, manifests = store.calls[0]
    by_lane = {m["strategy_id"]: m for m in manifests}

    # FIXTURE PROPERTY FIRST: two of the three lanes really hold nothing.
    assert len(cache.positions_open()) == 1 and len(cache.strategy_ids()) == 3
    assert set(by_lane) == {"MOMENTUM-002", "BCTROT-004", "TECHIVOL-005"}
    assert by_lane["TECHIVOL-005"]["instrument_count"] == 0
    assert by_lane["TECHIVOL-005"]["status"] == "observed", "flat is OBSERVED, not absent"


def test_a_session_whose_OPEN_TIME_is_unknown_REFUSES_rather_than_treating_it_as_the_epoch():
    """A FALLBACK THAT REPORTS THE WHOLE BOOK AS TRADED TODAY. `session_open_ns` fell back to 0 when
    the calendar day carried no `open_at`, and every position event is after the epoch — so every
    prior read as 0.0 and `session_fill_qty` became the ENTIRE position. Every lane would have looked
    like it opened its whole book that session, and the corporate-action invariant would have been
    violated on every row.

    Unreachable while `VenueCalendar.day()` returns a day with `open_at`, which is exactly the kind of
    'unreachable' this repo keeps being wrong about. Refuse."""
    class _NoOpen:
        session = "2026-08-28"
        open_at = None
        close_at = CLOSE

    cal = _Calendar({CLOSE.date(): _Day("2026-08-28", OPEN, CLOSE)})
    cal._days[CLOSE.date()] = _NoOpen()
    actor, store = _Actor(_Cache([], balances={"USD": 1.0}), calendar=cal), _Store()
    _run(actor, store)
    # The capture must not write observation rows it cannot date the deltas against.
    rows = store.calls[0][0] if store.calls else []
    assert rows == [], "no row may carry a delta measured from the epoch"
    if store.calls:
        assert store.calls[0][1][0]["status"] == "failed"
    assert any("open" in m.lower() for _, m in actor.log.lines), actor.log.lines


# ==================================================================================================
# THE TABLE RECORDS TRANSITIONS; THE LOG RECORDS ATTEMPTS
# ==================================================================================================
class _BrokenStore(_Store):
    """A store whose write of OBSERVATIONS fails, so the capture takes its failure path every tick
    while the manifest write still succeeds and can be counted."""

    def __init__(self, fail_with=None):
        super().__init__()
        self._fail_with = fail_with

    async def write(self, rows, manifests):
        if rows and self._fail_with is not None:
            raise self._fail_with()
        return await super().write(rows, manifests)


def _failing_actor():
    cache = _Cache([_Position("MOMENTUM-002", "AEM.XNYS", 10.0, events=[])],
                   balances={"SGD": 1.0, "EUR": 2.0},          # currency cannot be chosen
                   strategies=["MOMENTUM-002"])
    return _Actor(cache)


def test_a_failure_that_PERSISTS_writes_ONE_row_and_keeps_LOGGING_every_tick():
    """The capture asks every five minutes, so an unresolved condition would write a `failed` row per
    tick — around forty in a day. Read as forty failures, that is a false picture of one.

    THE SPLIT: the TABLE records transitions, the LOG records attempts. Two questions, two artifacts.
    The per-tick log line is therefore written UNCONDITIONALLY and is not redundant — it is the only
    thing that bounds an unresolved failure's duration once the rows are collapsed. Do not delete it.
    """
    actor, store = _failing_actor(), _Store()
    for _ in range(4):
        _run(actor, store)
    failed = [m for _, mans in store.calls for m in mans if m["status"] == "failed"]
    assert len(failed) == 1, f"one condition, one row; got {len(failed)}"
    assert sum(1 for lvl, _ in actor.log.lines if lvl == "error") == 4, (
        "every attempt must still appear in the log — that is what bounds the failure interval"
    )


def test_two_reasons_differing_ONLY_IN_AN_EMBEDDED_VALUE_still_collapse():
    """THE CONDITION THAT MAKES THE COLLAPSE REAL. Deduplicating on the FORMATTED message fails the
    moment a reason embeds a value — a quantity, a timestamp, a connection address with a port. Every
    tick then looks like a new reason, forty rows come back, and the collapse reads as working.

    So the key is NORMALIZED: the exception type plus a stable prefix, never the message."""
    from api.eod_hook import reason_key

    a = reason_key(ConnectionError("could not connect to 192.0.2.4:54312 after 3.02s"))
    b = reason_key(ConnectionError("could not connect to 192.0.2.4:61887 after 7.45s"))
    # FIXTURE PROPERTY FIRST: the two messages really are different, or this proves nothing.
    assert str(a) is not None and "192.0.2.4:54312" != "192.0.2.4:61887"
    assert a == b, "two ticks of one condition must key the same"


def test_a_DIFFERENT_class_of_failure_writes_its_OWN_row():
    """The collapse must not swallow a genuinely new problem. A database that went away and then an
    unreadable currency are two facts, and the table is where transitions live."""
    from api.eod_hook import reason_key

    assert reason_key(ConnectionError("x")) != reason_key(TimeoutError("x"))
    assert reason_key("currency") != reason_key("no open time")


def test_the_SAME_failure_on_the_NEXT_SESSION_writes_its_own_row():
    """Keyed PER SESSION DATE. Day N+1's identical failure is a different day's fact, and a table
    that skipped it would report the second day as never captured.

    THE FIXTURE HAD TO GROW A SECOND TRADING DAY. The first version reused `_TRADING`, which knows
    only 2026-08-28, so the next day was "not a trading day", the capture never reached its failure
    path, and the test failed for a reason that had nothing to do with the collapse. A double that
    cannot represent the situation cannot test it."""
    nxt = CLOSE + timedelta(days=1)
    cal = _Calendar({
        CLOSE.date(): _Day("2026-08-28", OPEN, CLOSE),
        nxt.date(): _Day("2026-08-29", nxt.replace(hour=9, minute=30), nxt),
    })
    actor = _Actor(_failing_actor().cache, calendar=cal)
    store = _Store()
    _run(actor, store)
    _run(actor, store, now=nxt + timedelta(minutes=30))

    # FIXTURE PROPERTY FIRST: both days really are trading days, or the second tick never fires.
    assert cal.day(CLOSE.date()) is not None and cal.day(nxt.date()) is not None
    failed = [m for _, mans in store.calls for m in mans if m["status"] == "failed"]
    assert len(failed) == 2, f"two sessions, two rows; got {[m['session_date'] for m in failed]}"


def test_a_RECOVERY_after_a_collapsed_failure_still_writes_and_marks_the_session_done():
    """The resolved case is where the interval is recoverable anyway: the first `failed` row and the
    `observed` rows bracket it. What must not happen is the collapse suppressing the recovery."""
    actor = _failing_actor()
    store = _Store()
    _run(actor, store)                                          # fails: currency unchoosable
    actor.cache._a = [_Account({"USD": 100.0})]                 # operator fixes it
    _run(actor, store)
    rows = [r for rws, _ in store.calls for r in rws]
    assert rows, "the recovery must write observation rows"
    _run(actor, store)
    assert len([r for rws, _ in store.calls for r in rws]) == len(rows), (
        "and the session is captured, so a later tick writes nothing more"
    )
