"""#840 — the exit release must wait on the stop that is actually resting, whoever's name is on it.

Measured 2026-09-09 16:00:02 UTC: BCTROT-004 exits ARKK; the protective stop in the cache is stamped
`BCTROT-004` (#748 builds stops with the lane's own OrderFactory); `release_for_exit` filtered the
cache by `MANUAL-001`; the filter matched nothing from the first poll; the venue still showed the stop
1 s after the cancel; "confirmed nothing" → no exit. 60 refused exits in 6 sessions.

The REAL `_await_reducing_orders_clear`, `_venue_says_leg_is_clear` and `_reducing_orders_open` are
driven over a cache double whose stop carries the LANE's stamp — the fixture asserts that property
first, because a MANUAL-stamped double would pass under today's filter and prove nothing.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from nautilus_trader.model.enums import OrderSide

LANE = "BCTROT-004"
IID = "ARKK.BATS"


def _stop(stamp: str):
    return types.SimpleNamespace(instrument_id=IID, strategy_id=stamp, side=OrderSide.SELL,
                                 client_order_id="PROT-SELL-ARKK-BATS-2d9ffb5d")


class _Fake:
    """A cache whose stop disappears after `confirm_after` polls — the venue confirming the cancel."""

    _CANCEL_CONFIRM_S = 1.0

    def __init__(self, stop, confirm_after=2):
        self._polls, self._confirm_after, self._stop = 0, confirm_after, stop
        self.id = "MANUAL-001"                      # the protection strategy, as in production
        self.said: list[str] = []
        self.log = types.SimpleNamespace(error=lambda m, *a, **k: self.said.append(str(m)),
                                         warning=lambda m, *a, **k: self.said.append(str(m)),
                                         info=lambda m, *a, **k: None)
        self.cache = types.SimpleNamespace(orders_open=self._orders_open)

    def _orders_open(self):
        self._polls += 1
        return [] if self._polls > self._confirm_after else [self._stop]

    async def _venue_reducing_orders(self, instrument_id, side):
        # the venue still shows the stop until the cancel is confirmed — exactly what was measured
        return [] if self._polls > self._confirm_after else [{"client_order_id": self._stop.client_order_id}]


def _wait(fake, owner):
    from api.engine_node import UiFeedStrategy

    fake._reducing_orders_open = UiFeedStrategy._reducing_orders_open.__get__(fake, _Fake)
    fake._venue_says_leg_is_clear = UiFeedStrategy._venue_says_leg_is_clear.__get__(fake, _Fake)
    bound = UiFeedStrategy._await_reducing_orders_clear.__get__(fake, _Fake)
    return asyncio.run(bound(IID, owner, OrderSide.SELL))


def test_fixture_property__the_stop_is_stamped_with_the_LANE_not_MANUAL():
    assert _stop(LANE).strategy_id == LANE != "MANUAL-001"


def test_fixture_property__todays_filter_confirms_nothing_over_a_lane_stamped_stop():
    """The double reproduces the measured refusal: MANUAL-only filter → the #748 line → False."""
    fake = _Fake(_stop(LANE))
    assert _wait(fake, "MANUAL-001") is False
    assert any("#748" in s for s in fake.said), fake.said


def test_a_filter_carrying_the_lane_WATCHES_the_stop_disappear_and_confirms_the_release():
    fake = _Fake(_stop(LANE))
    assert _wait(fake, {"MANUAL-001", LANE}) is True
    assert not any("CONFIRMED NOTHING" in s for s in fake.said), fake.said


def test_the_same_filter_still_watches_an_aggregate_MANUAL_stop():
    """Regime-independent (engine_node ~:5290): a pre-#748 stop stamped MANUAL-001 is still matched."""
    fake = _Fake(_stop("MANUAL-001"))
    assert _wait(fake, {"MANUAL-001", LANE}) is True


def test_another_lanes_stop_is_NOT_matched():
    """AEM: MOMENTUM 10 + BCTROT 12, one stop each. BCTROT's exit must not wait on — or cancel —
    MOMENTUM's stop; with only that stop in the cache the filter is empty and the venue decides."""
    fake = _Fake(_stop("MOMENTUM-002"), confirm_after=0)
    assert _wait(fake, {"MANUAL-001", LANE}) is True      # venue says clear → nothing of ours rests
    assert fake._polls >= 1


def test_release_for_exit_hands_BOTH_identities_to_the_cancel_and_the_wait(monkeypatch):
    """The wiring: the owner filter that reaches `_cancel_reducing_leg` and
    `_await_reducing_orders_clear` carries MANUAL-001 AND the exiting lane."""
    from api.test_exit_release import _bind, _protection

    _protection(monkeypatch)
    host = _bind()
    ok = asyncio.run(host.release_for_exit(IID, 23, strategy_id=LANE))
    assert ok is True
    seen = host.owner_seen
    assert "MANUAL-001" in seen and LANE in seen, (
        f"owner filter reached the wait as {seen!r}; the stop resting for this exit is stamped {LANE}")


def test_a_lane_less_release_still_hands_may_cancel_order_a_STRING_canceller(monkeypatch):
    """Review finding on #840: `canceller or strategy_id` fell back to the FILTER, which is now a
    set — `may_cancel_order(canceller={...})` compares a set to `PROTECTION_OWNER` and the proxy
    concession silently dies on every lane-less (manual) exit. The REAL `_cancel_reducing_leg` over
    a venue row, `strategy_id` a set, no canceller."""
    import api.engine_node as en

    seen = {}
    monkeypatch.setattr(en, "may_cancel_order", lambda **k: seen.update(k) or False)
    fake = _Fake(_stop("MANUAL-001"), confirm_after=0)          # nothing native; one venue row
    fake._reducing_orders_open = en.UiFeedStrategy._reducing_orders_open.__get__(fake, _Fake)
    fake._owner_of = lambda coid, vid: "MANUAL-001"
    fake._venue_reducing_orders = lambda iid, side: _ret([{"client_order_id": "PROT-SELL-X", "id": "v1"}])
    bound = en.UiFeedStrategy._cancel_reducing_leg.__get__(fake, _Fake)
    asyncio.run(bound(IID, {"MANUAL-001"}, OrderSide.SELL, proxy=True, canceller=None))
    assert isinstance(seen.get("canceller"), str) and seen["canceller"] == "MANUAL-001", seen


async def _ret(v):
    return v
