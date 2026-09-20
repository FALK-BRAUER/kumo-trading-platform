"""A cancel-confirm that never had anything to wait for must not report success (#748 prereq 5).

`_await_reducing_orders_clear` re-derives what it is waiting for, every poll, from a FILTER:

    self._reducing_orders_open(instrument_id, strategy_id, reducing_side)

so it cannot distinguish "the orders I cancelled are gone" from "my filter never matched anything".
Both look like an empty list, and the empty list is read as success — on the step whose absence
caused #245 and #252, where True means "the shares are free, send the exit".

TODAY the filter is `self.id` (MANUAL-001) and protective stops are MANUAL-001's, so it usually
matches. `test_the_lane_the_caller_passes_NEVER_BECOMES_THE_OWNER_FILTER` exists because passing the
lane as the owner once broke exactly that, silently, with a green suite.

THE POINT OF FIXING IT NOW, BEFORE #748 ACTIVATES: when protective stops carry the LANE, the
MANUAL-001 filter matches nothing and this returns True on its first poll having confirmed nothing —
the same defect, arriving from the other side. Changing the filter to the lane is not a fix either;
it re-creates the vacuity during the transition, while aggregate MANUAL-001 stops are still resting.
Regime-dependent correctness is the trap.

So the confirmation is made REGIME-INDEPENDENT instead: an empty filtered set is only success if the
VENUE agrees the leg is clear. The venue knows nothing about our sleeves, which here is the point —
it is the one plane whose answer does not depend on whose name is on the order.
"""

from __future__ import annotations

import asyncio

import pytest


class _Fake:
    """Only the pieces `_await_reducing_orders_clear` touches."""

    _CANCEL_CONFIRM_S = 0.05

    def __init__(self, cache_orders, venue_rows):
        self._cache_orders = cache_orders
        self._venue_rows = venue_rows
        self.said: list[str] = []
        self.log = type("L", (), {
            "error": lambda _s, m, *a, **k: self.said.append(str(m)),
            "warning": lambda _s, m, *a, **k: self.said.append(str(m)),
            "info": lambda _s, m, *a, **k: None,
        })()

    def _reducing_orders_open(self, instrument_id, strategy_id, reducing_side):
        return list(self._cache_orders)

    async def _venue_reducing_orders(self, instrument_id, reducing_side):
        return self._venue_rows


def _run(fake, **kw):
    """Drive the REAL methods against the double.

    Both of them, bound from production: `_await_reducing_orders_clear` and the helper it delegates
    to. Reimplementing the helper on the double would let the two drift, and a double that cannot
    represent what production does is the bug — this file exists because a wait confirmed nothing
    while looking like it had confirmed something.
    """
    from api.engine_node import UiFeedStrategy

    fake._venue_says_leg_is_clear = UiFeedStrategy._venue_says_leg_is_clear.__get__(fake, _Fake)
    bound = UiFeedStrategy._await_reducing_orders_clear.__get__(fake, _Fake)
    return asyncio.run(bound("AEM.XNYS", "MANUAL-001", "SELL", **kw))


def test_an_EMPTY_FILTER_with_ORDERS_STILL_RESTING_at_the_venue_is_NOT_success():
    """The vacuity, named. Nothing matched the filter, so nothing was ever waited on — and the venue
    says the leg is still reserved. Reporting True here is what tells the caller to send an exit into
    shares that are not free, which is `available: 0` on the path built to prevent it."""
    fake = _Fake(cache_orders=[], venue_rows=[{"id": "v1", "client_order_id": "PROT-SELL-AEM-1"}])
    assert _run(fake) is False
    assert any("confirmed nothing" in m.lower() for m in fake.said), fake.said


def test_an_EMPTY_FILTER_with_a_CLEAR_VENUE_is_genuine_success():
    """The common case must stay cheap and must still succeed: nothing resting anywhere means the
    shares really are free. A guard that failed here would block every legitimate exit on a position
    that simply had no protection resting."""
    fake = _Fake(cache_orders=[], venue_rows=[])
    assert _run(fake) is True
    assert not any("confirmed nothing" in m.lower() for m in fake.said), fake.said


def test_an_UNREADABLE_VENUE_is_NOT_success():
    """`_venue_reducing_orders` returns None when the broker cannot be read. Unknown is not clear, and
    the caller treats False as "send nothing" — the position keeps whatever protects it, which is the
    safe outcome. Absence of evidence is a timestamp, not a property."""
    fake = _Fake(cache_orders=[], venue_rows=None)
    assert _run(fake) is False


def test_orders_that_WERE_matched_and_then_CLEARED_do_not_pay_for_a_venue_round_trip():
    """The guard is for the set that was EMPTY AT ENTRY. A wait that genuinely observed orders and
    saw them go is already positive confirmation — it watched something disappear — so it must not
    acquire a second gate, and must not be able to fail because an unrelated lane's stop rests on the
    same leg."""
    seen = {"venue_calls": 0}

    class _Clearing(_Fake):
        def __init__(self):
            super().__init__(cache_orders=["one"], venue_rows=[{"id": "other-lane-stop"}])

        def _reducing_orders_open(self, *a):
            # PRESENT ON THE FIRST POLL, gone on the next: the order really did clear. The first
            # call is what sets `observed_any`, so a fixture that is empty immediately would test
            # the vacuity path instead of this one — which is what my first version did.
            if self._cache_orders:
                self._cache_orders = self._cache_orders[1:]
                return ["resting"]
            return []

        async def _venue_reducing_orders(self, *a):
            seen["venue_calls"] += 1
            return self._venue_rows

    fake = _Clearing()
    assert _run(fake) is True
    assert seen["venue_calls"] == 0, "a wait that observed a clear must not need the venue gate"


def test_the_TIMEOUT_path_is_unchanged():
    """Orders still resting when the deadline passes is a plain timeout — False, and not the vacuity
    message, because something WAS being waited on."""
    fake = _Fake(cache_orders=["still-there"], venue_rows=[])
    assert _run(fake) is False
    assert not any("confirmed nothing" in m.lower() for m in fake.said), fake.said
