"""Protective orders resting at the venue must match the book — reconciled, not assumed (#748).

MEASURED ON PAPER, 2026-08-31 21:40 SGT, at the open:

    SSRM 12 live stops · HALO 8 · AEM 5 · VCTR 4 · RGEN 4 · five more at 2 · 66 working in total

SSRM held ~53 shares. Twelve trailing stops rested on it. Had the trail triggered, all twelve would
have fired: ~636 shares sold against 53 held, leaving a ~583-share naked short in a long-only book.
HALO had already done a smaller version — `order.filled_qty=19, fill.last_qty=18, would result in 37`.

THE LOOP. A protective stop was stamped with the SUBMITTING strategy (MANUAL-001) on instruments
MANUAL-001 does not hold. The fill then resolves to `PositionId('HALO.XNAS-MANUAL-001')`, which has
never existed, so the ExecEngine REJECTS it — and because the fill is rejected the engine still reads
the symbol as unprotected and submits another. Once per protection tick, forever.

SO ONE FIX IS NOT ENOUGH. Refusing to submit an unstampable order stops NEW duplicates; it does
nothing about the 66 already resting. This module is the second half: what is at the venue is
compared against what is held, and the excess is cancelled.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.protective_reconcile import (
    UNKNOWN_BOOK,
    reconcile_protection,
)


def _pos(instrument, strategy, qty, *, is_open=True):
    return SimpleNamespace(
        instrument_id=instrument, strategy_id=strategy, quantity=abs(qty),
        signed_qty=qty, is_open=is_open,
    )


class _Enum:
    """A double that behaves like a Nautilus Cython enum, INCLUDING the part that broke the module.

    `str(OrderType.TRAILING_STOP_MARKET)` is `'8'` in the running container — the ordinal, not the
    name. A double returning the name from `__str__` accepted code that production rejects, and all
    fourteen tests here passed against a reconciler that would have matched nothing live. So this
    double stringifies to a NUMBER and carries the name on `.name`, exactly like the real thing.
    """

    _ORDINALS = {"TRAILING_STOP_MARKET": "8", "ACCEPTED": "6", "FILLED": "9", "LIMIT": "2"}

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self._ORDINALS.get(self.name, "0")


def _order(coid, instrument, strategy, qty, *, side="SELL", ts=0, status="ACCEPTED"):
    return SimpleNamespace(
        client_order_id=coid, instrument_id=instrument, strategy_id=strategy,
        quantity=qty, side=side, ts_init=ts,
        status=_Enum(status), order_type=_Enum("TRAILING_STOP_MARKET"),
    )


# ==================================================================================================
# The guard that has to come first
# ==================================================================================================
def test_an_EMPTY_BOOK_REFUSES_rather_than_cancelling_everything():
    """THE MOST DANGEROUS CASE IN THIS FILE, and the one the repo has shipped wrong before.

    If the positions view is empty or unreadable, every resting stop looks orphaned — so a reconciler
    that trusts it would cancel ALL protection on a book it simply could not see. Empty IS the failure
    mode being guarded against, so empty must never authorise a cancel.
    """
    plan = reconcile_protection([], [_order("c1", "HALO.XNAS", "MANUAL-001", 19)])
    assert plan.refused == UNKNOWN_BOOK, f"an empty book produced a live plan: {plan}"
    assert plan.cancel == (), "an unreadable book authorised cancels"


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the duplicate case below must actually contain duplicates."""
    orders = [_order(f"c{i}", "SSRM.XNAS", "BCTROT-004", 53, ts=i) for i in range(12)]
    assert len({o.client_order_id for o in orders}) == 12


# ==================================================================================================
# Duplicates — the live defect
# ==================================================================================================
def test_TWELVE_STOPS_ON_ONE_POSITION_are_cut_to_one():
    positions = [_pos("SSRM.XNAS", "BCTROT-004", 53)]
    orders = [_order(f"c{i}", "SSRM.XNAS", "BCTROT-004", 53, ts=i) for i in range(12)]
    plan = reconcile_protection(positions, orders)
    assert plan.refused is None
    assert len(plan.cancel) == 11, f"expected 11 cancels, got {len(plan.cancel)}"
    assert len(plan.keep) == 1, "a symbol must not be left with zero protection by a dedupe"


def test_the_INCUMBENT_is_the_one_kept():
    """Keep the OLDEST valid stop, cancel the newer copies.

    Not the newest: the oldest is the one that has been resting and may be partially filled, and
    cancelling a partially-filled stop in favour of a fresh one re-opens the window it was covering.
    """
    positions = [_pos("SSRM.XNAS", "BCTROT-004", 53)]
    orders = [_order("new", "SSRM.XNAS", "BCTROT-004", 53, ts=99),
              _order("old", "SSRM.XNAS", "BCTROT-004", 53, ts=1)]
    plan = reconcile_protection(positions, orders)
    assert plan.keep == ("old",), f"kept {plan.keep}"
    assert [c.client_order_id for c in plan.cancel] == ["new"]


def test_a_SINGLE_correct_stop_is_left_alone():
    """A reconciler that churns a healthy book is one that gets switched off."""
    positions = [_pos("SSRM.XNAS", "BCTROT-004", 53)]
    plan = reconcile_protection(positions, [_order("c1", "SSRM.XNAS", "BCTROT-004", 53)])
    assert plan.cancel == () and plan.keep == ("c1",)


# ==================================================================================================
# Orphans — a stop that cannot protect
# ==================================================================================================
def test_a_stop_stamped_with_a_lane_that_HOLDS_NOTHING_is_cancelled():
    """The HALO case exactly: stamped MANUAL-001, held by BCTROT-004. Its fill is rejected by the
    ExecEngine, so it protects nothing while occupying the slot that says the symbol IS protected."""
    positions = [_pos("HALO.XNAS", "BCTROT-004", 55)]
    plan = reconcile_protection(positions, [_order("bad", "HALO.XNAS", "MANUAL-001", 19)])
    assert [c.client_order_id for c in plan.cancel] == ["bad"]
    assert plan.keep == ()
    assert "MANUAL-001" in plan.cancel[0].reason and "BCTROT-004" in plan.cancel[0].reason


def test_a_CORRECTLY_STAMPED_stop_survives_beside_an_orphan():
    positions = [_pos("HALO.XNAS", "BCTROT-004", 55)]
    orders = [_order("bad", "HALO.XNAS", "MANUAL-001", 19, ts=1),
              _order("good", "HALO.XNAS", "BCTROT-004", 55, ts=2)]
    plan = reconcile_protection(positions, orders)
    assert plan.keep == ("good",)
    assert [c.client_order_id for c in plan.cancel] == ["bad"]


def test_an_ORPHAN_IS_NOT_KEPT_just_because_it_is_the_only_one():
    """A dedupe rule that always leaves one would keep a stop whose every fill the engine rejects,
    and the symbol would read protected forever while being naked."""
    positions = [_pos("HALO.XNAS", "BCTROT-004", 55)]
    plan = reconcile_protection(positions, [_order("bad", "HALO.XNAS", "MANUAL-001", 19)])
    assert plan.keep == (), "an unfillable stop was kept as though it protected something"


def test_a_stop_on_an_instrument_HELD_BY_NOBODY_is_cancelled():
    """GMAB: cache attributed 59 to three lanes, venue held 0. A stop resting on a position that no
    longer exists fires into a naked short."""
    positions = [_pos("HALO.XNAS", "BCTROT-004", 55)]
    plan = reconcile_protection(positions, [_order("g", "GMAB.XNAS", "MOMENTUM-002", 59)])
    assert [c.client_order_id for c in plan.cancel] == ["g"]


# ==================================================================================================
# What it must NOT do
# ==================================================================================================
def test_a_CLOSED_position_does_not_vouch_for_a_stop():
    # A SECOND, OPEN position is required for this fixture to test what it claims. With only the
    # closed row the book has nothing open at all, the UNKNOWN_BOOK guard fires first, and the test
    # would pass on the refusal rather than on the closed-position rule — measuring its neighbour.
    positions = [_pos("HALO.XNAS", "BCTROT-004", 55, is_open=False),
                 _pos("SSRM.XNAS", "BCTROT-004", 53)]
    plan = reconcile_protection(positions, [_order("c", "HALO.XNAS", "BCTROT-004", 55)])
    assert [c.client_order_id for c in plan.cancel] == ["c"]


def test_a_NON_PROTECTIVE_order_is_never_touched():
    """Entries and targets are not this mechanism's business, and cancelling one would be the
    reconciler placing a trade by omission."""
    positions = [_pos("SSRM.XNAS", "BCTROT-004", 53)]
    entry = _order("buy", "SSRM.XNAS", "BCTROT-004", 53, side="BUY")
    entry.order_type = _Enum("LIMIT")
    plan = reconcile_protection(positions, [entry])
    assert plan.cancel == ()


def test_a_TERMINAL_order_is_not_cancelled_again():
    """Cancelling an already-filled order is noise at best and an error at worst."""
    positions = [_pos("SSRM.XNAS", "BCTROT-004", 53)]
    done = _order("f", "SSRM.XNAS", "BCTROT-004", 53, status="FILLED")
    plan = reconcile_protection(positions, [done])
    assert plan.cancel == ()


def test_the_PLAN_IS_IDEMPOTENT():
    """Running it twice must not keep cancelling — a reconciler that never converges is a loop of
    its own, which is the thing being fixed."""
    positions = [_pos("SSRM.XNAS", "BCTROT-004", 53)]
    orders = [_order(f"c{i}", "SSRM.XNAS", "BCTROT-004", 53, ts=i) for i in range(12)]
    first = reconcile_protection(positions, orders)
    survivors = [o for o in orders if o.client_order_id in first.keep]
    second = reconcile_protection(positions, survivors)
    assert second.cancel == (), f"second pass still cancels: {second.cancel}"


def test_EVERY_CANCEL_CARRIES_A_REASON():
    """An operator reading the log must see WHY a protective order was pulled. A cancel with no
    stated cause is indistinguishable from the bug it is fixing."""
    positions = [_pos("SSRM.XNAS", "BCTROT-004", 53)]
    orders = [_order(f"c{i}", "SSRM.XNAS", "BCTROT-004", 53, ts=i) for i in range(3)]
    plan = reconcile_protection(positions, orders)
    assert plan.cancel
    for c in plan.cancel:
        assert c.reason and len(c.reason) > 20, f"bare reason: {c.reason!r}"


# ==================================================================================================
# Complementary coverage is NOT a duplicate — measured live, 2026-09-01 01:22 SGT
# ==================================================================================================
def test_TWO_STOPS_THAT_SUM_TO_THE_POSITION_are_both_kept():
    """The defect this file's own dedupe rule introduced, caught on paper within minutes.

        17:22:55  rested SELL 8.0 AYA  -> accepted
        17:23:55  cancelled as "duplicate protection on AYA.XNAS/MOMENTUM-002"
        17:25:56  rested SELL 8.0 AYA  -> accepted
        17:26:55  cancelled as "duplicate"

    AYA's position is 73. A stop already covered 65, so the reconciler correctly placed 8 more to
    reach full coverage — and this module cancelled it as a duplicate, because it counted ORDERS per
    (instrument, lane) and never looked at quantity. The result was a position permanently 8 shares
    short of protection and two real venue orders churned every three minutes.

    Two stops summing to the position are COMPLEMENTARY. The genuine double-sell this rule exists to
    stop — HALO twice, GMAB 59+59 against a 59 holding — is the OVER-coverage case, so quantity was
    always the right test.
    """
    positions = [_pos("AYA.XNAS", "MOMENTUM-002", 73)]
    orders = [_order("old", "AYA.XNAS", "MOMENTUM-002", 65, ts=1),
              _order("top-up", "AYA.XNAS", "MOMENTUM-002", 8, ts=2)]
    plan = reconcile_protection(positions, orders)
    assert plan.cancel == (), f"complementary coverage was cancelled: {[c.client_order_id for c in plan.cancel]}"
    assert sorted(plan.keep) == ["old", "top-up"]


def test_STOPS_THAT_EXCEED_THE_POSITION_are_cut_back_to_it():
    """The GMAB shape: 59 + 59 resting against a 59 holding. Both fire on one trigger and sell 118.
    The excess is cancelled, the incumbent kept."""
    positions = [_pos("GMAB.XNAS", "MOMENTUM-002", 59)]
    orders = [_order("first", "GMAB.XNAS", "MOMENTUM-002", 59, ts=1),
              _order("second", "GMAB.XNAS", "MOMENTUM-002", 59, ts=2)]
    plan = reconcile_protection(positions, orders)
    assert [c.client_order_id for c in plan.cancel] == ["second"]
    assert plan.keep == ("first",)
    assert "59" in plan.cancel[0].reason or "exceed" in plan.cancel[0].reason.lower()


def test_a_PARTIAL_stop_alone_is_kept_and_the_shortfall_is_not_a_cancel():
    """Under-coverage is a reason to place MORE, never to cancel what is there."""
    positions = [_pos("AYA.XNAS", "MOMENTUM-002", 73)]
    plan = reconcile_protection(positions, [_order("part", "AYA.XNAS", "MOMENTUM-002", 65)])
    assert plan.cancel == () and plan.keep == ("part",)


def test_the_EXCESS_is_cut_oldest_first_so_the_incumbent_survives():
    """Three stops, 40 + 40 + 40 on a 73 position: the first two cover it, the third is excess."""
    positions = [_pos("X.XNAS", "BCTROT-004", 73)]
    orders = [_order(f"s{i}", "X.XNAS", "BCTROT-004", 40, ts=i) for i in range(3)]
    plan = reconcile_protection(positions, orders)
    assert sorted(plan.keep) == ["s0", "s1"]
    assert [c.client_order_id for c in plan.cancel] == ["s2"]
