"""The exit release WAITS through the venue's cancel ack and the share-availability lag, then sells (#1058).

THE SHAPE THE PAPER LOG MEASURED on 2026-09-09 (BCTROT-004 exiting ARKK at 16:00Z): the cancel went
to the venue at +0.0 s, the wait gave up at +1.05 s, the venue's cancel ack landed at +4.4 s, the stop
was re-armed at +21 s and the exit never went. LFXX +7.1 s, PAGP +4.3 s, VCTR +1.6 s — every one
refused. At the 15:40 slot the same path polled 6-7 s and every exit filled. Twenty refusals in six
sessions, and every one the branch `exit release on <iid>: nothing matched the cache filter, so this
wait CONFIRMED NOTHING`. The cause was #840 (the wait filtered the cache by MANUAL-001 while #748
stamps the stop with the lane); the fix `a1aab47` carries both identities. `test_exit_owner_filter_840`
pins the FILTER; this file pins the TIMING the filter has to survive — the lead's shape: cancel ack at
~1.2 s, shares available at ~5 s, and the release must come back True having sold nothing early.

REAL TIME, DELIBERATELY. The waits use `time.monotonic()` and `asyncio.sleep(0.25)`; a fake clock
would test a different function. ~5.5 s of wall time is the cost of measuring the thing that broke.

Every test here was seen red with `owner` reduced to `{MANUAL-001}` (the pre-#840 filter), for the
reason its docstring names.
"""

from __future__ import annotations

import asyncio
import time
import types
from decimal import Decimal

import pytest
from nautilus_trader.model.enums import OrderSide

from api.test_exit_release import _Clock, _Log, _protection

#: Real wall time (~7 s across the file) by design — the waits use `time.monotonic()`. Marked so a
#: quick local run can deselect it (`-m 'not slow'`); the merge gate runs everything.
pytestmark = pytest.mark.slow

LANE = "BCTROT-004"
IID = "ARKK.BATS"
NEEDED = 23

#: The measured shape, in seconds after the cancel left: the venue acks the cancel (the cache's
#: OrderCanceled lands, the stop leaves `orders_open()`), then the shares become available.
CANCEL_ACK_S = 1.2
SHARES_FREE_S = 5.0


class _TimedHost:
    """`release_for_exit` over a venue that answers on a CLOCK, not on a poll count.

    The REAL `_await_reducing_orders_clear`, `_venue_says_leg_is_clear`, `_reducing_orders_open` and
    `_await_shares_available` are bound to this host; `_cancel_reducing_leg` is the only stub, and it
    records the instant the cancel left so every later answer is relative to it.
    """

    _CANCEL_CONFIRM_S = 20.0            # production's default (KUMO_CANCEL_CONFIRM_TIMEOUT_S)
    _SHARES_FREE_S = 30.0               # production's default (KUMO_SHARES_FREE_TIMEOUT_S)
    _exec_reserves_shares = True        # Alpaca reserves shares against a resting stop

    def __init__(self, stamp: str = LANE):
        from api.engine_node import UiFeedStrategy as U

        self.id = "MANUAL-001"
        self.log, self.clock = _Log(), _Clock()
        self._exit_suppressed: dict[str, int] = {}
        self._exit_windows: dict[str, dict] = {}
        self._stop = types.SimpleNamespace(instrument_id=IID, strategy_id=stamp, side=OrderSide.SELL,
                                           client_order_id="PROT-SELL-ARKK-BATS-2d9ffb5d")
        self.cache = types.SimpleNamespace(orders_open=self._orders_open)
        self.cancelled_at: float | None = None
        self.owner_seen = None
        self.venue_reads = 0
        self.available_reads: list[tuple[float, int]] = []
        self._EXIT_SUPPRESSION_S = U._EXIT_SUPPRESSION_S
        self._EXIT_SUPPRESSION_MAX_S = U._EXIT_SUPPRESSION_MAX_S
        for name in ("_extend_standoff", "_close_exit_window", "_await_reducing_orders_clear",
                     "_venue_says_leg_is_clear", "_reducing_orders_open", "_await_shares_available",
                     "release_for_exit"):
            setattr(self, name, getattr(U, name).__get__(self, _TimedHost))

    def _since_cancel(self) -> float:
        return float("inf") if self.cancelled_at is None else time.monotonic() - self.cancelled_at

    def _orders_open(self):
        return [] if self._since_cancel() >= CANCEL_ACK_S else [self._stop]

    async def _venue_reducing_orders(self, instrument_id, side):
        self.venue_reads += 1
        return [] if self._since_cancel() >= CANCEL_ACK_S else [{"client_order_id": self._stop.client_order_id, "id": "v1"}]

    async def _venue_shares_available(self, instrument_id):
        avail = NEEDED if self._since_cancel() >= SHARES_FREE_S else 0
        self.available_reads.append((round(self._since_cancel(), 2), avail))
        return Decimal(avail)

    async def _cancel_reducing_leg(self, instrument_id, strategy_id, reducing_side, proxy=False, canceller=None):
        self.owner_seen = strategy_id
        self.cancelled_at = time.monotonic()


def _release(host, **kw):
    return asyncio.run(host.release_for_exit(IID, NEEDED, strategy_id=LANE, **kw))


# ------------------------------------------------------------------ fixture properties ---------

def test_FIXTURE_the_stop_is_stamped_with_the_LANE_and_the_venue_answers_on_the_clock():
    """The double must be able to represent the measured world: a lane-stamped stop, a cancel ack
    that arrives LATER than the first poll, and shares that free LATER than the ack. A double that
    answered immediately would pass under the pre-#840 filter and prove nothing."""
    host = _TimedHost()
    assert host._stop.strategy_id == LANE != "MANUAL-001"
    host.cancelled_at = time.monotonic()
    assert host._orders_open() == [host._stop], "the stop must still rest right after the cancel"
    assert asyncio.run(host._venue_shares_available(IID)) == 0, "shares must still be reserved right after the cancel"
    assert CANCEL_ACK_S < SHARES_FREE_S < host._CANCEL_CONFIRM_S


def test_FIXTURE_the_pre_840_filter_gives_up_before_the_ack_and_refuses(monkeypatch):
    """The RED half, kept as a fixture property: with the wait filtered by MANUAL-001 alone the
    cache shows nothing, the venue is read once ~immediately and still shows the stop, and the
    release refuses — the 09-09 16:00Z sequence, reproduced. If this ever passes, the harness can no
    longer represent the defect and the test below is vacuous."""
    _protection(monkeypatch)
    host = _TimedHost()
    host.cancelled_at = time.monotonic()
    cleared = asyncio.run(host._await_reducing_orders_clear(IID, {"MANUAL-001"}, OrderSide.SELL))
    assert cleared is False
    assert host._since_cancel() < CANCEL_ACK_S, "the pre-#840 wait must have given up BEFORE the ack"
    assert any("CONFIRMED NOTHING" in e for e in host.log.errors), host.log.errors


# ------------------------------------------------------------------ the seam -------------------

def test_the_release_waits_through_the_ack_and_the_availability_lag_and_then_SELLS(monkeypatch):
    """THE TIMING SHAPE, driven through the real `release_for_exit`: the cancel leaves at 0 s, the
    ack lands at 1.2 s, shares free at 5 s. The release must (1) watch the lane-stamped stop leave
    the cache — never "confirm nothing"; (2) keep polling availability past the ack until the venue
    frees the shares; (3) come back True only AFTER that, having reported nothing early."""
    _protection(monkeypatch)
    host = _TimedHost()
    t0 = time.monotonic()

    ok = _release(host)

    elapsed = time.monotonic() - t0
    assert ok is True, f"refused: errors={host.log.errors} warnings={host.log.warnings}"
    assert "MANUAL-001" in host.owner_seen and LANE in host.owner_seen
    assert not any("CONFIRMED NOTHING" in e for e in host.log.errors), host.log.errors
    assert not any("not released" in e for e in host.log.errors), host.log.errors
    assert elapsed >= SHARES_FREE_S, f"came back True at {elapsed:.2f}s, before the venue freed the shares at {SHARES_FREE_S}s"
    # TWO-SIDED (review): a wait that always ran to `_SHARES_FREE_S` (30 s in production) and read
    # availability once at the end would also satisfy `>=` on a 5 s fixture. The release must return
    # PROMPTLY once the venue frees the shares — the ks runner sits on this per exit per slot.
    assert elapsed < SHARES_FREE_S + 1.0, f"came back True at {elapsed:.2f}s — the release ran to a bound instead of returning when the shares freed"
    assert host.available_reads and host.available_reads[0][1] == 0, "the first availability read must have seen the reservation"
    assert host.available_reads[-1][1] == NEEDED
    assert min(t for t, _ in host.available_reads) >= CANCEL_ACK_S - 0.3, (
        "availability was polled before the cancel-confirm wait had cleared — the ordering #245 requires")
    assert host.venue_reads == 0, "the cache saw the stop, so the venue fallback must not have been consulted"


def test_the_ack_lands_but_the_shares_NEVER_free_refuses_at_the_second_bound_and_keeps_the_stop(monkeypatch):
    """The third case (review): the cancel acks at 1.2 s, the shares stay reserved past
    `_SHARES_FREE_S`. The release must refuse at THAT bound — never sell, keep the stop — and the
    availability poll must have run (all zeros), not been skipped. This is the shape ks#224 now
    holds entries on: the release landed and the shares did not."""
    _protection(monkeypatch)
    host = _TimedHost()
    host._SHARES_FREE_S = 1.0                         # bounded, so the test is too

    async def _never_free(instrument_id):
        host.available_reads.append((round(host._since_cancel(), 2), 0))
        return Decimal(0)

    host._venue_shares_available = _never_free
    t0 = time.monotonic()

    ok = _release(host)

    elapsed = time.monotonic() - t0
    assert ok is False
    assert CANCEL_ACK_S + 1.0 <= elapsed < CANCEL_ACK_S + 3.0, f"refused at {elapsed:.2f}s"
    assert host.available_reads and all(a == 0 for _, a in host.available_reads), host.available_reads
    assert any("still not available" in e or "not released" in e for e in host.log.errors), host.log.errors


def test_a_cancel_ack_that_NEVER_lands_refuses_at_the_timeout_and_keeps_the_stop(monkeypatch):
    """The other side of the same clock: the venue never acks. The wait must run to its bounded
    timeout and refuse — never sell into reserved shares, never report released."""
    _protection(monkeypatch)
    host = _TimedHost()
    host._CANCEL_CONFIRM_S = 1.0                      # bounded, so the test is too
    host._orders_open = lambda: [host._stop]         # the ack never lands
    t0 = time.monotonic()

    ok = _release(host)

    assert ok is False
    assert 1.0 <= time.monotonic() - t0 < 3.0
    assert any("not released" in e for e in host.log.errors), host.log.errors
    assert host.available_reads == [], "availability must not be polled while a reducing order still rests"
