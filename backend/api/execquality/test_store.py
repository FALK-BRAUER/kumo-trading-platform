"""The daily execution-quality job. Offline: fake clock, fake HTTP, fake recorder.

These assert WHEN it runs and what it must never do, not SQL.
"""

from __future__ import annotations

import asyncio

from api.execquality.measure import Leg
from api.execquality.store import ExecutionQualityJob

OPEN = {"is_open": True, "timestamp": "2026-08-10T14:00:00-04:00"}
CLOSED = {"is_open": False, "timestamp": "2026-08-10T16:45:00-04:00"}
NEXT_DAY = {"is_open": False, "timestamp": "2026-08-11T16:45:00-04:00"}

FILL = {"transaction_time": "2026-08-10T13:35:00Z", "symbol": "SU", "side": "buy",
        "qty": "10", "price": "101.0"}
AUCTIONS = {"auctions": {"SU": [{"d": "2026-08-10", "o": [
    {"c": "O", "p": 100.0, "s": 5000, "x": "N", "t": "2026-08-10T13:30:01.1Z"}]}]}}


class FakeHttp:
    def __init__(self, clocks, acts=None, auctions=None):
        self._clocks = list(clocks)
        self._acts = acts if acts is not None else [FILL]
        self._auctions = auctions if auctions is not None else AUCTIONS
        self._trading = "trading"
        self._data = "data"
        self.calls = 0

    async def get_clock(self):
        return self._clocks.pop(0) if len(self._clocks) > 1 else self._clocks[0]

    async def list_activities(self, activity_type="FILL", after=None, page_size=100):
        self.calls += 1
        return self._acts

    async def _get(self, base, path, params=None):
        self.calls += 1
        return self._auctions


class FakeRow:
    def __init__(self, session, symbol):
        self.session, self.symbol = session, symbol


class FakeDb:
    """Serves the journal lookup that restricts the sample to this strategy's own orders."""

    def __init__(self, pairs):
        self._pairs = pairs

    async def execute(self, *a, **k):
        return [FakeRow(s, y) for s, y in self._pairs]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


OURS = {("2026-08-10", "SU")}


def job(http, recorded, pairs=None):
    async def fake_record(_sf, legs):
        recorded.append(legs)
        return len(legs)

    pairs = OURS if pairs is None else pairs
    return ExecutionQualityJob(session_factory=lambda: FakeDb(pairs), http=http,
                               recorder=fake_record)


def test_it_does_not_run_while_the_market_is_open():
    """Fills mid-session are incomplete; recording then would store a partial picture as the session."""
    recorded: list = []
    http = FakeHttp([OPEN])
    assert asyncio.run(job(http, recorded).tick()) == -1
    assert recorded == []
    assert http.calls == 0, "hit the activities endpoint during the session"


def _after_close(recorded, http=None, pairs=None):
    """Drive one open→closed transition and return the tick result."""
    j = job(http or FakeHttp([OPEN, CLOSED]), recorded, pairs)

    async def go():
        await j.tick()          # observes OPEN — establishes the baseline
        return await j.tick()   # observes CLOSED — the transition

    return asyncio.run(go()), j


def test_it_records_once_the_market_has_closed():
    recorded: list = []
    written, _ = _after_close(recorded)
    assert written == 1
    assert recorded and recorded[0][0].symbol == "SU"


def test_a_restart_while_the_market_is_shut_records_nothing():
    """THE guard bug codex found. `is_open == false` is true all weekend and every premarket, so a
    process that merely starts up at 20:00 — or on Monday premarket — must not manufacture a session.
    Recording requires an observed OPEN→CLOSED transition."""
    recorded: list = []
    j = job(FakeHttp([CLOSED]), recorded)
    assert asyncio.run(j.tick()) == -1, "recorded without ever seeing the market open"
    assert asyncio.run(j.tick()) == -1
    assert recorded == []


def test_a_manual_trade_in_another_symbol_is_not_attributed_to_the_strategy():
    """The sample must contain only what this strategy submitted; the original study had to exclude
    manual trades by hand."""
    recorded: list = []
    manual = {**FILL, "symbol": "FIG"}
    written, _ = _after_close(recorded, http=FakeHttp([OPEN, CLOSED], acts=[manual]))
    assert written == 0


def test_it_does_not_record_the_same_day_twice():
    """The loop ticks every few minutes all evening; only the transition should do work."""
    recorded: list = []
    written, j = _after_close(recorded)
    assert written == 1
    assert asyncio.run(j.tick()) == -1
    assert asyncio.run(j.tick()) == -1
    assert len(recorded) == 1


def test_a_new_session_is_recorded_after_a_previous_one():
    recorded: list = []
    j = job(FakeHttp([OPEN, CLOSED, OPEN, NEXT_DAY]), recorded,
            pairs={("2026-08-10", "SU"), ("2026-08-11", "SU")})

    async def go():
        await j.tick()
        first = await j.tick()
        await j.tick()                       # market opens again
        return first, await j.tick()         # next close

    first, second = asyncio.run(go())
    assert first == 1 and second == 1, "a new day must not be suppressed by the previous marker"


def test_a_day_with_no_fills_is_not_an_error():
    recorded: list = []
    written, _ = _after_close(recorded, http=FakeHttp([OPEN, CLOSED], acts=[]))
    assert written == 0


def test_the_loop_never_raises_out():
    """This sits in the API process. A research job must never take down the operator's API."""
    class Broken(FakeHttp):
        async def get_clock(self):
            raise RuntimeError("venue clock down")

    async def noop(_sf, legs):
        return 0

    j = ExecutionQualityJob(session_factory=None, http=Broken([CLOSED]), recorder=noop)

    async def one_pass():
        task = asyncio.create_task(j.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(one_pass())          # must complete without propagating RuntimeError


def test_legs_carry_through_unchanged():
    """The recorder must not reshape what `compare` produced — the drift sign convention is load
    bearing and a silent transformation here would poison months of accumulated sample."""
    recorded: list = []
    _after_close(recorded)
    leg: Leg = recorded[0][0]
    assert leg.drift_bps == 100.0 and leg.cost_usd == 10.0
    assert leg.lag_minutes > 0
