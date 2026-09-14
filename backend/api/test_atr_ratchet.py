"""ATR RATCHET (#425) — the replacement for hand-set stop distances. Tests precede the module.

WHY. Split of the real IBKR book by HOW the position was exited, 2026-05-27..2026-08-18:

    STOP     57 fills   -3,259.85   17.5% hit rate
    LIMIT    30 fills     +104.75   25.0%
    MARKET  174 fills     +157.01   40.6%

Everything exited by hand nets +$262; the stop orders are -$3,260. Hand-set distances were the whole
loss. ARGX is the clearest single case: bought 1001.53 at 15:19:52Z, stopped 1000.74 at 15:22:06Z —
134 SECONDS later, at -0.08%, a distance inside the spread. PLTR was stopped $0.52 above the day's low.

FOUR PROPERTIES, and three of them exist to stop this being a fixed stop with extra steps:

  1. it ARMS ONLY IN PROFIT. A stop that can fire below entry is a hand-set stop again.
  2. it NEVER RESTS BELOW ENTRY, so an armed position cannot be exited for a loss by this mechanism.
  3. it RATCHETS — the level only ever rises.
  4. it does NOT replace the operator's own exit. Measured: as an addition +$310 (V7); replacing his
     exit with a 5-session horizon, -$11,587 (V1). His fast exits are the thing he already does right.
"""

from __future__ import annotations

import pytest

from api.atr_ratchet import ARM_AT_ATR, TRAIL_ATR, ratchet_stop

ENTRY = 100.0
ATR = 4.0          # 4% of price, close to the measured median of 4.11%


def test_the_fixture_can_express_both_armed_and_unarmed():
    # Fixture property first: +1 ATR from a 100.0 entry is 104.0, and the highs below straddle it.
    assert ENTRY + ARM_AT_ATR * ATR == 104.0


def test_it_does_NOT_arm_before_the_position_is_a_full_ATR_in_profit():
    """The whole failure being replaced. Below the arm threshold there is NO stop from this mechanism —
    not a wide one, not a tight one. A distance chosen while underwater is what produced -0.08% on
    ARGX."""
    assert ratchet_stop(entry=ENTRY, running_high=103.9, atr=ATR, current_stop=None) is None


def test_it_arms_EXACTLY_at_one_ATR_of_profit():
    d = ratchet_stop(entry=ENTRY, running_high=104.0, atr=ATR, current_stop=None)
    assert d is not None
    # 104.0 - 1.5*4.0 = 98.0, which is BELOW entry — so the floor must lift it to entry.
    assert d.price == ENTRY, "an armed stop may never rest below entry"


def test_an_armed_stop_NEVER_rests_below_entry():
    """Property 2. Without the floor this is a hand-set stop again: it would sit 1.5 ATR under the high
    and take a loss on a position that had been in profit."""
    d = ratchet_stop(entry=ENTRY, running_high=105.0, atr=ATR, current_stop=None)
    assert d is not None and d.price == ENTRY  # 105 - 6 = 99 -> floored to 100


def test_once_the_trail_clears_entry_it_follows_the_high():
    d = ratchet_stop(entry=ENTRY, running_high=110.0, atr=ATR, current_stop=None)
    assert d is not None and d.price == pytest.approx(110.0 - TRAIL_ATR * ATR)  # 104.0


def test_it_RATCHETS_and_never_loosens():
    """Property 3, and the silencing direction. A stop that can move DOWN gives back banked protection
    on a pullback — the operator would watch a level they had already earned retreat."""
    held = ratchet_stop(entry=ENTRY, running_high=112.0, atr=ATR, current_stop=106.0)
    assert held is not None and held.price == 106.0, "112 - 6 = 106, unchanged"
    # The high stands but the caller passes a HIGHER existing stop: keep the higher one.
    d = ratchet_stop(entry=ENTRY, running_high=112.0, atr=ATR, current_stop=108.0)
    assert d is not None and d.price == 108.0, "an existing tighter stop must not be loosened"

    # AND THE OTHER DIRECTION, which is the half that makes this a ratchet rather than a latch.
    #
    # THE FIRST VERSION OF THIS TEST COULD NOT FAIL. Both cases above pass `current_stop` values that
    # already sit at or above the trailed price, so `max(trailed, current_stop)` never had to choose
    # the trailed side — and replacing the whole expression with a bare `current_stop` kept all eight
    # tests green. The fixture could not violate the property it was asserting. Caught by the mutation
    # sweep, not by review.
    up = ratchet_stop(entry=ENTRY, running_high=120.0, atr=ATR, current_stop=106.0)
    assert up is not None
    assert up.price == pytest.approx(120.0 - TRAIL_ATR * ATR), (
        "the high rose to 120 so the stop must ratchet UP to 114, not stay at the old 106"
    )


def test_a_non_positive_ATR_decides_NOTHING_rather_than_guessing_a_width():
    # A zero or missing ATR means the instrument has no usable history yet. Fabricating a width here is
    # how #239 rested 11.91% on FIG while PEAK rested 2.5% on the same symbol at the same moment.
    assert ratchet_stop(entry=ENTRY, running_high=120.0, atr=0.0, current_stop=None) is None
    assert ratchet_stop(entry=ENTRY, running_high=120.0, atr=-1.0, current_stop=None) is None


def test_the_measured_constants_are_pinned():
    """+1.0 ATR to arm and 1.5 ATR to trail are the measured pair (V7, +$310 as an ADDITION to his own
    exit). 1.5 ATR is also the engine's existing protective width (protection.py:25), so the ratchet and
    the backstop cannot disagree about how wide a stop on this symbol should be."""
    assert ARM_AT_ATR == 1.0
    assert TRAIL_ATR == 1.5
