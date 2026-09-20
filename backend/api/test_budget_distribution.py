"""Unallocated capital must reach the strategies that are short, without inventing or losing any.

WHY THIS FILE EXISTS
--------------------
Until #373 nothing could move capital INTO a sleeve. `TRANSFER_TO` routes a seller's proceeds to ONE
named recipient, `POST /transfers` moves a position rather than cash, and `set_target` writes intent
without funding anything. So on 2026-08-19 BCTROT-004 and QC345-003 both sat at `target 20000, actual 0`
— fully funded on paper, `deployable` pinned at 0, unable to open a single position — while $45,000.00
sat unallocated and BCTROT's close-20m decision slot was two hours away.

Operator: distribute it "proportionally to how short each strategy is vs its target, not sent to one
recipient."

WHAT THIS PINS
--------------
Two caps, both load-bearing, and the arithmetic between them:

  conservation  the total handed out never exceeds what exists. This is the same law `plan_transfer`
                enforces by capping at `proceeds`, and it is the one a rounding bug breaks silently.
  intent        no sleeve receives past its own target, so a distribution cannot overshoot what an
                operator set.

Proportionality is the point of the feature and the part that is invisible until the sleeves diverge:
equal shares and proportional shares are identical when every sleeve is equally short, which is exactly
the state the system was in when this was written. A test that only used today's book would pass on a
wrong implementation.
"""

from __future__ import annotations

import math

import pytest

from api.budget import Allocation, Sleeve, plan_distribution


def _book(**kw) -> dict[str, Sleeve]:
    """strategy -> (target, actual)."""
    return {sid: Sleeve(sid, t, a) for sid, (t, a) in kw.items()}


def _total(allocs: list[Allocation]) -> float:
    return round(sum(a.amount for a in allocs), 2)


def test_shares_are_proportional_to_shortfall_not_equal():
    """The whole point of the feature, on a book where the two differ.

    Today's live book has every sleeve equally short, so equal-split and proportional-split agree — a
    fixture built from it could not tell a correct implementation from a wrong one.
    """
    book = _book(A=(20000, 5000), B=(20000, 15000))  # A is 15k short, B is 5k short: 3:1
    allocs = {a.to_strategy: a.amount for a in plan_distribution(book, 8000)}
    assert allocs["A"] == pytest.approx(6000.0), f"A should get three quarters, got {allocs}"
    assert allocs["B"] == pytest.approx(2000.0), f"B should get one quarter, got {allocs}"


def test_nothing_exceeds_what_exists():
    """Conservation. A distribution that hands out more than is available is capital from nowhere."""
    book = _book(A=(20000, 0), B=(20000, 0))
    for available in (1.0, 137.77, 9999.99, 40000.0):
        allocs = plan_distribution(book, available)
        assert _total(allocs) <= available + 1e-9, (
            f"handed out {_total(allocs)} from {available} available"
        )


def test_no_sleeve_is_pushed_past_its_own_target():
    """Intent. Overshooting the target an operator set is not a lesser error than underfunding."""
    book = _book(A=(20000, 19000), B=(20000, 0))
    allocs = {a.to_strategy: a.amount for a in plan_distribution(book, 100000)}
    assert allocs["A"] == pytest.approx(1000.0), "A was funded past its target"
    assert allocs["B"] == pytest.approx(20000.0)


def test_surplus_beyond_the_total_shortfall_stays_unallocated():
    """Filling every sleeve is the ceiling; the rest must remain available, not be forced somewhere."""
    book = _book(A=(20000, 15000), B=(20000, 18000))  # 5000 + 2000 = 7000 of room
    allocs = plan_distribution(book, 50000)
    assert _total(allocs) == pytest.approx(7000.0)


def test_the_source_never_receives_its_own_distribution():
    """UNALLOCATED is where the capital comes from. Funding it loops capital back and reports progress
    that never happened."""
    book = _book(A=(20000, 0), UNALLOCATED=(50000, 0))
    assert [a.to_strategy for a in plan_distribution(book, 10000)] == ["A"]


def test_an_over_target_sleeve_receives_nothing():
    """A reducing sleeve has negative shortfall. Handing it capital is the opposite of the intent."""
    book = _book(OVER=(20000, 33735.79), SHORT=(20000, 10000))
    assert [a.to_strategy for a in plan_distribution(book, 5000)] == ["SHORT"]


@pytest.mark.parametrize("available", [0.0, -1.0, 0.004, float("nan"), float("inf")])
def test_nothing_is_distributed_when_there_is_nothing_to_distribute(available):
    """NaN defeats every comparison and cap below it — `NaN <= _EPS` is False — so a non-finite amount
    would sail through and land in a sleeve balance, after which the sleeve can never be corrected.
    Same defect `plan_transfer` rejects at its own boundary."""
    book = _book(A=(20000, 0))
    assert plan_distribution(book, available) == []


def test_a_book_with_no_shortfall_distributes_nothing():
    book = _book(A=(20000, 20000), B=(20000, 25000))
    assert plan_distribution(book, 50000) == []


def test_rounding_neither_invents_nor_loses_a_cent():
    """Three-way splits do not divide evenly, and a conservation law broken by rounding is not one.

    1000/3 is 333.333…; naive rounding gives 333.33 x 3 = 999.99 (a cent lost) or 333.34 x 3 = 1000.02
    (two cents invented, which is capital from nowhere).
    """
    book = _book(A=(20000, 0), B=(20000, 0), C=(20000, 0))
    allocs = plan_distribution(book, 1000)
    assert _total(allocs) == pytest.approx(1000.00, abs=1e-9), f"{[(a.to_strategy, a.amount) for a in allocs]}"
    for a in allocs:
        assert round(a.amount * 100) == pytest.approx(a.amount * 100, abs=1e-6), (
            f"{a.to_strategy} got {a.amount}, which is not a whole number of cents"
        )


def test_the_split_is_deterministic():
    """The same book must always produce the same split, or the remainder cent wanders between runs and
    no test can pin it."""
    book = _book(A=(20000, 0), B=(20000, 0), C=(20000, 0))
    first = plan_distribution(book, 1000)
    for _ in range(5):
        assert plan_distribution(book, 1000) == first


def test_it_computes_and_mutates_nothing():
    """Pure, like `plan_transfer`. Applying is a separate, auditable step."""
    book = _book(A=(20000, 5000))
    before = (book["A"].target, book["A"].actual)
    plan_distribution(book, 5000)
    assert (book["A"].target, book["A"].actual) == before


def test_todays_live_book_funds_both_starved_strategies():
    """The case that motivated it, with the real numbers from 2026-08-19."""
    book = _book(
        **{
            "MANUAL-001": (20000, 10000),
            "MOMENTUM-002": (20000, 33735.79),
            "BCTROT-004": (20000, 10000),
            "QC345-003": (20000, 10000),
            "UNALLOCATED": (0, 39504.35),
        }
    )
    allocs = {a.to_strategy: a.amount for a in plan_distribution(book, 39504.35)}
    assert set(allocs) == {"MANUAL-001", "BCTROT-004", "QC345-003"}, (
        f"MOMENTUM is over target and UNALLOCATED is the source; neither may receive: {allocs}"
    )
    assert all(v == pytest.approx(10000.0) for v in allocs.values()), (
        f"each is 10k short and there is more than enough to fill them all: {allocs}"
    )
    assert _total(list(plan_distribution(book, 39504.35))) == pytest.approx(30000.0), (
        "the 9,504.35 beyond the total shortfall must stay unallocated"
    )


def test_the_remainder_cent_cannot_push_a_sleeve_past_its_target():
    """The cap on the remainder loop, which every other test here was blind to.

    Removing `if amounts[sid] + 0.01 <= short[sid] + _EPS` kept the whole suite green, because no
    fixture had a shortfall that ended in a fraction of a cent. It needs one: three sleeves short by
    33.333, 33.333 and 33.334 total exactly 100.00, so flooring to whole cents leaves 99.99 and one cent
    to place — and placing it anywhere overshoots that sleeve's target by 0.007.

    The correct behaviour is to leave the cent unallocated. Underfunding by a cent is invisible;
    overshooting the number an operator set is the thing the cap exists to prevent, and "it was only a
    cent" is how a cap stops being a cap.
    """
    book = _book(A=(20000, 20000 - 33.333), B=(20000, 20000 - 33.333), C=(20000, 20000 - 33.334))
    allocs = plan_distribution(book, 100.00)
    by_id = {a.to_strategy: a.amount for a in allocs}
    headroom = {sid: s.headroom for sid, s in book.items()}

    for sid, amount in by_id.items():
        # Production's own cap admits at most _EPS (0.005) of slack; an uncapped remainder overshoots
        # by 0.007, which is why the tolerance is stated rather than rounded away.
        assert amount <= headroom[sid] + 0.005 + 1e-9, (
            f"{sid} received {amount} against a headroom of {headroom[sid]} — the remainder cent was "
            f"placed past the operator's target"
        )
    assert _total(allocs) <= 100.00 + 1e-9, "conservation broken while placing the remainder"


def test_that_fixture_really_does_leave_a_remainder():
    """Assert the fixture's own property first — otherwise the test above passes on a book that never
    exercises the branch, which is exactly how the mutation escaped the first time."""
    book = _book(A=(20000, 20000 - 33.333), B=(20000, 20000 - 33.333), C=(20000, 20000 - 33.334))
    total_short = sum(s.headroom for s in book.values())
    assert total_short == pytest.approx(100.00, abs=1e-9)
    floored = sum(math.floor(s.headroom * 100) / 100 for s in book.values())
    assert round(total_short - floored, 4) > 0, "no remainder to place — the branch is unreachable"


# -- findings from the 2026-08-19 review ------------------------------------------------------


@pytest.mark.parametrize("available", [100.005, 1000.008, 0.019, 33.333, 1e-3])
def test_conservation_holds_for_amounts_that_are_not_whole_cents(available):
    """The headline law was false for any sub-cent input, and the suite could not see it.

    `test_nothing_exceeds_what_exists` only tried whole-cent amounts (1.0, 137.77, 9999.99, 40000.0), so
    rounding the remainder UP — which handed out 100.01 against 100.005 available — stayed green. An
    operator passing broker cash supplies exactly such a number, and the docstring recommends it.
    """
    book = _book(A=(20000, 0), B=(20000, 0), C=(20000, 0))
    allocs = plan_distribution(book, available)
    assert _total(allocs) <= available + 1e-12, (
        f"handed out {_total(allocs)} against {available} available"
    )


def test_every_allocation_is_a_whole_number_of_cents():
    """Money in binary floating point cannot express this function's invariant, so the split is
    computed in integer cents. A fractional cent is evidence that arithmetic leaked back into floats."""
    book = _book(A=(20000, 0), B=(20000, 0), C=(20000, 0))
    for a in plan_distribution(book, 1000.0):
        assert abs(a.amount * 100 - round(a.amount * 100)) < 1e-9, f"{a.to_strategy} got {a.amount}"


def test_a_three_way_split_loses_nothing():
    """1000/3 does not divide. Flooring the remainder dropped a cent because `1000 - 999.99` is
    0.00999999... in binary; rounding it invented two. Integer cents gives exactly 1000.00."""
    book = _book(A=(20000, 0), B=(20000, 0), C=(20000, 0))
    assert _total(plan_distribution(book, 1000.0)) == pytest.approx(1000.00, abs=1e-9)


def test_an_infinite_headroom_cannot_poison_the_split():
    """A settings value of `inf` parses as a float and becomes a target.

    Deleting the `math.isfinite(sleeve.headroom)` guard kept the whole suite green: `raw` then becomes
    NaN and `math.floor(nan)` raises, taking the endpoint down rather than refusing the input.
    """
    book = _book(A=(float("inf"), 0), B=(20000, 0))
    allocs = plan_distribution(book, 5000)
    assert [a.to_strategy for a in allocs] == ["B"], f"an infinite target was funded: {allocs}"
    assert _total(allocs) <= 5000 + 1e-9


def test_a_zero_allocation_is_never_emitted():
    """`if amt > 0` — relaxing it to `>= 0` stayed green and would write a zero-value SleeveTransfer row
    for every sleeve, consuming the run's idempotency keys and logging a distribution that moved
    nothing."""
    # The fixture must produce a sleeve that IS in the split and receives ZERO cents. The first draft
    # used a headroom of 0.001, which the `> _EPS` filter drops before the split — so no sleeve could
    # ever hold a zero amount and the mutation walked straight through.
    #
    # A is 10,000 short and B is 2 cents short, against 1 cent to give: the whole cent goes to A on
    # largest-remainder, and B lands at exactly 0.
    book = _book(A=(20000, 10000), B=(20000, 19999.98))
    allocs = plan_distribution(book, 0.01)
    assert {a.to_strategy for a in allocs} == {"A"}, (
        f"a sleeve that received nothing was still emitted: {[(a.to_strategy, a.amount) for a in allocs]}"
    )
    for a in allocs:
        assert a.amount > 0, f"{a.to_strategy} emitted a zero allocation"


def test_the_tie_break_is_largest_remainder_not_merely_stable():
    """`test_the_split_is_deterministic` pins stability, never WHICH sleeve gets the odd cent — so
    reversing the documented largest-remainder ordering stayed green.

    A gets 2/3 of a cent and B gets 1/3, so the odd cent belongs to A.
    """
    book = _book(A=(20000, 0), B=(20000, 10000))  # 20000 vs 10000 short: 2:1
    allocs = {a.to_strategy: a.amount for a in plan_distribution(book, 0.01)}
    assert allocs.get("A") == pytest.approx(0.01), (
        f"the odd cent went to the smaller remainder: {allocs}"
    )
