"""Every failure this repair can cause is a failure of AIM (#744).

`contra_execute`'s own docstring states it: a fill aimed at an id that is not the real one does not
close the short — it OPENS A NEW POSITION, re-minting the exact defect being repaired, in a lane that
may hold nothing at all. And a live cache read once found all sixteen legs matching the reconstructed
shape while only ONE was inspected deeply enough to confirm the id embedded in it.

So this file is almost entirely about refusing. The happy path is one test; the rest are the ways a
leg can be pointed somewhere it must not be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from api.contra_book import (
    NOT_ENOUGH,
    UNKNOWN_POSITION,
    WRONG_LANE,
    WRONG_SIDE,
    verify_and_book,
)
from api.contra_ledger import BOOKED, ContraLedger


@dataclass(frozen=True)
class _Leg:
    instrument_id: str = "WHD.XNYS"
    strategy_id: str = "MOMENTUM-002"
    position_id: str = "WHD.XNYS-MOMENTUM-002"
    side: str = "BUY"
    quantity: float = 28.0
    price: float = 70.57


@dataclass(frozen=True)
class _Order:
    instrument_id: str = "WHD.XNYS"
    fingerprint: str = "fp-1"
    legs: tuple = ()
    unbooked_realized: float = 0.0
    is_partial: bool = False


@dataclass(frozen=True)
class _Plan:
    orders: tuple = field(default_factory=tuple)


def _pos(pid, lane, signed):
    return SimpleNamespace(id=pid, strategy_id=lane, signed_qty=signed)


def _pair():
    """The measured WHD shape: MOMENTUM-002 short 28, BCTROT-004 long 28."""
    return _Order(legs=(
        _Leg(side="BUY", strategy_id="MOMENTUM-002", position_id="WHD.XNYS-MOMENTUM-002"),
        _Leg(side="SELL", strategy_id="BCTROT-004", position_id="WHD.XNYS-BCTROT-004"),
    ))


def _book():
    return [_pos("WHD.XNYS-MOMENTUM-002", "MOMENTUM-002", -28.0),
            _pos("WHD.XNYS-BCTROT-004", "BCTROT-004", 28.0)]


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the healthy pair must actually be bookable, or every refusal below is the
    fixture failing rather than a rule firing."""
    plan = verify_and_book(_Plan((_pair(),)), _book(), ContraLedger())
    assert plan.book and not plan.refused


def test_a_leg_aimed_at_a_position_that_DOES_NOT_EXIST_refuses_the_whole_pair():
    """The defect's own re-entry route. Booking against an unknown id opens a position at that id."""
    positions = [p for p in _book() if p.id != "WHD.XNYS-MOMENTUM-002"]
    plan = verify_and_book(_Plan((_pair(),)), positions, ContraLedger())
    assert not plan.book
    assert plan.refused[0].reason == UNKNOWN_POSITION
    assert "OPEN a position" in plan.refused[0].detail


def test_a_leg_aimed_at_ANOTHER_LANES_position_is_refused():
    positions = [_pos("WHD.XNYS-MOMENTUM-002", "QC345-003", -28.0),
                 _pos("WHD.XNYS-BCTROT-004", "BCTROT-004", 28.0)]
    plan = verify_and_book(_Plan((_pair(),)), positions, ContraLedger())
    assert plan.refused[0].reason == WRONG_LANE


@pytest.mark.parametrize("signed,side", [(28.0, "BUY"), (-28.0, "SELL")])
def test_a_leg_that_would_INCREASE_the_position_is_refused(signed, side):
    """A BUY closes a phantom SHORT; a SELL gives back from a real LONG. Either way it must reduce the
    magnitude — the same rule as the exit-ownership guard, on the internal plane."""
    positions = [_pos("WHD.XNYS-MOMENTUM-002", "MOMENTUM-002", signed),
                 _pos("WHD.XNYS-BCTROT-004", "BCTROT-004", 28.0)]
    order = _Order(legs=(_Leg(side=side),))
    plan = verify_and_book(_Plan((order,)), positions, ContraLedger())
    assert plan.refused[0].reason == WRONG_SIDE


def test_a_leg_LARGER_than_the_position_is_refused():
    """Overshooting does not close a position, it flips it — the same defect, opposite sign."""
    positions = [_pos("WHD.XNYS-MOMENTUM-002", "MOMENTUM-002", -10.0),
                 _pos("WHD.XNYS-BCTROT-004", "BCTROT-004", 28.0)]
    plan = verify_and_book(_Plan((_pair(),)), positions, ContraLedger())
    assert plan.refused[0].reason == NOT_ENOUGH


def test_ONE_BAD_LEG_REFUSES_THE_WHOLE_PAIR():
    """THE ONE THAT MATTERS MOST. Half a repair is worse than none: closing the short without giving
    back what it claimed leaves the book FURTHER from the broker than it started."""
    positions = [_pos("WHD.XNYS-MOMENTUM-002", "MOMENTUM-002", -28.0)]   # the SELL leg's target is gone
    plan = verify_and_book(_Plan((_pair(),)), positions, ContraLedger())
    assert not plan.book, "a pair with an unverifiable leg was partially booked"
    assert len(plan.refused) == 1


def test_an_ALREADY_BOOKED_pair_is_skipped_and_reported_as_DONE_not_refused():
    """"Done" and "declined" are different facts. Collapsing them makes a completed repair look like
    a failing one on every subsequent tick, which is how someone concludes the repair is broken and
    runs it again — the second mint."""
    led = ContraLedger()
    led.record("fp-1", "WHD.XNYS", BOOKED, legs=[])
    plan = verify_and_book(_Plan((_pair(),)), _book(), led)
    assert plan.already_done == ("fp-1",)
    assert not plan.book and not plan.refused


def test_a_CLOSED_position_is_still_indexed_so_a_leg_aimed_at_it_is_caught():
    """The position book passed in must include CLOSED positions. A leg aimed at one that has since
    closed must be refused for the right reason, not vanish into UNKNOWN_POSITION and read as a
    missing id — but either way it must NOT be booked."""
    positions = [_pos("WHD.XNYS-MOMENTUM-002", "MOMENTUM-002", 0.0),
                 _pos("WHD.XNYS-BCTROT-004", "BCTROT-004", 28.0)]
    plan = verify_and_book(_Plan((_pair(),)), positions, ContraLedger())
    assert not plan.book
    assert plan.refused[0].reason == WRONG_SIDE


def test_an_EMPTY_plan_books_nothing_and_refuses_nothing():
    plan = verify_and_book(_Plan(()), _book(), ContraLedger())
    assert plan.book == () and plan.refused == () and plan.already_done == ()
