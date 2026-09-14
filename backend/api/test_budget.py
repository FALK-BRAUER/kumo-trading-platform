"""Tests for per-strategy sleeves and the transition transfer (#320).

The property that makes a transition safe is a CONSERVATION LAW — total allocated capital does not
change when it moves between sleeves — so it is asserted directly rather than inferred from the
absence of a bug.
"""

from __future__ import annotations

import math

from api.budget import (
    UNALLOCATED,
    Book,
    Sleeve,
    allocated,
    apply_transfer,
    over_committed,
    plan_transfer,
    trading_sleeves,
)


def _book(**kw: tuple[float, float]) -> Book:
    """`_book(MOMENTUM=(target, actual))`."""
    return Book({name: Sleeve(name, target, actual) for name, (target, actual) in kw.items()})


# -- the gap between target and actual is the instruction ----------------------------------------


def test_target_moves_instantly_and_moves_no_capital():
    """Intent is free; reality is not. Setting a target must not itself move a cent, or an operator
    could 'rebalance' a book by typing, without a single share changing hands."""
    book = _book(MOMENTUM=(50_000, 50_000), QC345=(0, 0))
    before = allocated(book)

    book = Book({"MOMENTUM": Sleeve("MOMENTUM", 30_000, 50_000),
                 "QC345": Sleeve("QC345", 20_000, 0)})

    assert allocated(book) == before, "changing targets moved capital"
    assert book.sleeves["MOMENTUM"].must_reduce == 20_000
    assert book.sleeves["QC345"].headroom == 20_000


def test_a_sleeve_at_its_target_is_not_reducing():
    """A strategy that sells while at target is ROTATING, not shrinking — it turned stock into cash
    inside its own sleeve and will buy something else. Taking that cash would break a strategy behaving
    exactly as intended."""
    s = Sleeve("MOMENTUM", target=50_000, actual=50_000)
    assert s.is_reducing is False
    assert plan_transfer(s, proceeds=10_000, recipient=Sleeve("QC345", 20_000, 0)) is None


# -- the transfer -------------------------------------------------------------------------------


def test_capital_moves_only_when_the_donor_actually_sells():
    """The whole safety property. The recipient's target can be raised the instant an operator decides,
    but its ACTUAL only grows when the donor has genuinely freed capital — so the two sleeves can never
    both be deploying the same dollars during the overlap."""
    book = _book(MOMENTUM=(30_000, 50_000), QC345=(20_000, 0))

    assert book.sleeves["QC345"].deployable(currently_deployed=0) == 0, "recipient could spend unfreed capital"

    t = plan_transfer(book.sleeves["MOMENTUM"], proceeds=8_000, recipient=book.sleeves["QC345"])
    assert t == t and t is not None
    assert (t.from_strategy, t.to_strategy, t.amount) == ("MOMENTUM", "QC345", 8_000)

    book = apply_transfer(book, t)
    assert book.sleeves["MOMENTUM"].actual == 42_000
    assert book.sleeves["QC345"].actual == 8_000
    assert book.sleeves["QC345"].deployable(currently_deployed=0) == 8_000


def test_total_allocated_capital_is_invariant_across_a_transfer():
    """The conservation law, stated as an invariant.

    This is WHY the overlap window is safe: while the donor still holds and the recipient is already
    growing, the book's total exposure cannot exceed what it was before the transition began.
    """
    book = _book(MOMENTUM=(30_000, 50_000), QC345=(20_000, 0))
    before = allocated(book)

    for proceeds in (5_000, 7_500, 2_500, 5_000):
        t = plan_transfer(book.sleeves["MOMENTUM"], proceeds, recipient=book.sleeves["QC345"])
        if t is not None:
            book = apply_transfer(book, t)
        assert allocated(book) == before, "capital was created or destroyed by a transfer"


def test_a_transfer_never_overshoots_the_donors_target():
    """One large sale must not leave the donor UNDER target — it would then have to be topped back up,
    and a transition that oscillates is worse than one that takes an extra month."""
    donor = Sleeve("MOMENTUM", target=30_000, actual=50_000)     # must_reduce = 20_000
    t = plan_transfer(donor, proceeds=45_000, recipient=Sleeve("QC345", 25_000, 0))

    assert t is not None
    assert t.amount == 20_000, "moved more than the donor was over by"
    assert apply_transfer(Book({"MOMENTUM": donor, "QC345": Sleeve("QC345", 25_000, 0)}), t).sleeves[
        "MOMENTUM"
    ].actual == 30_000


def test_a_transfer_never_overshoots_the_recipients_target():
    """The mirror. A finished hand-over must stop, or capital keeps arriving in a sleeve that is
    already full and the operator's intent is silently exceeded."""
    donor = Sleeve("MOMENTUM", target=0, actual=50_000)
    recipient = Sleeve("QC345", target=20_000, actual=18_000)    # headroom = 2_000

    t = plan_transfer(donor, proceeds=10_000, recipient=recipient)
    assert t is not None
    assert t.amount == 2_000
    assert t.to_strategy == "QC345"


def test_capital_returns_to_UNALLOCATED_when_the_recipient_is_full():
    """It must go SOMEWHERE, and it must not go to a sleeve that did not ask for it.

    Routing loose capital to 'whoever still has headroom' would make the outcome depend on iteration
    order over the book, which is not a property anyone should have to reason about.
    """
    donor = Sleeve("MOMENTUM", target=0, actual=50_000)
    full = Sleeve("QC345", target=20_000, actual=20_000)

    t = plan_transfer(donor, proceeds=10_000, recipient=full)
    assert t is not None
    assert t.to_strategy == UNALLOCATED
    assert t.amount == 10_000

    book = apply_transfer(Book({"MOMENTUM": donor, "QC345": full}), t)
    assert book.sleeves["MOMENTUM"].actual == 40_000
    assert book.sleeves["QC345"].actual == 20_000, "an unrequested sleeve received capital"
    # CORRECTED (codex review, Critical). The first version asserted 60_000 with a comment claiming
    # UNALLOCATED was "still counted" — it was not. The book went 70k -> 60k and the conservation law
    # silently failed on exactly the path it exists to cover. UNALLOCATED is now a real sleeve.
    assert book.sleeves[UNALLOCATED].actual == 10_000
    assert allocated(book) == 70_000, "capital vanished when parked in UNALLOCATED"
    assert UNALLOCATED not in trading_sleeves(book), "the holding pen is being treated as a strategy"


def test_no_named_recipient_also_returns_to_UNALLOCATED():
    donor = Sleeve("MOMENTUM", target=0, actual=10_000)
    t = plan_transfer(donor, proceeds=4_000, recipient=None)
    assert t is not None and t.to_strategy == UNALLOCATED and t.amount == 4_000


# -- P&L accrues to the strategy that earned it ---------------------------------------------------


def test_a_profitable_sale_leaves_the_gain_in_the_selling_sleeve():
    """`actual` is a NET ASSET VALUE, not a cost basis.

    Sell 100 shares for $12,000 against a $10,000 basis and the sleeve is worth $2,000 more than
    before. Only the amount it was OVER TARGET by leaves; the gain stays. Under a basis definition the
    sleeve would hand over the full proceeds and a profitable strategy would shrink toward zero while
    performing well — the arithmetic would punish exactly the thing it is meant to measure.
    """
    donor = Sleeve("MOMENTUM", target=45_000, actual=50_000)     # over by 5_000
    t = plan_transfer(donor, proceeds=12_000, recipient=Sleeve("QC345", 10_000, 0))

    assert t is not None
    assert t.amount == 5_000, "handed over sale proceeds rather than the excess"
    assert apply_transfer(Book({"MOMENTUM": donor, "QC345": Sleeve("QC345", 10_000, 0)}), t).sleeves[
        "MOMENTUM"
    ].actual == 45_000


# -- deployment is bounded by BOTH numbers --------------------------------------------------------


def test_deployable_is_bounded_by_actual_while_funding_arrives():
    """Mid-transition the binding constraint is what the sleeve HAS, not what it was granted."""
    s = Sleeve("QC345", target=20_000, actual=8_000)
    assert s.deployable(currently_deployed=0) == 8_000


def test_deployable_is_bounded_by_target_once_funded():
    """Fully funded, the binding constraint is the operator's intent. A sleeve holding more than its
    target — because its positions appreciated — must not keep buying."""
    s = Sleeve("QC345", target=20_000, actual=26_000)
    assert s.deployable(currently_deployed=0) == 20_000


def test_deployable_accounts_for_what_is_already_in_the_market():
    s = Sleeve("QC345", target=20_000, actual=20_000)
    assert s.deployable(currently_deployed=15_000) == 5_000
    assert s.deployable(currently_deployed=20_000) == 0
    assert s.deployable(currently_deployed=25_000) == 0, "negative headroom must clamp, not invert"


# -- over-commitment is visible rather than dangerous ---------------------------------------------


def test_targets_summing_past_the_account_are_reported_not_enforced():
    """Nothing stops an operator granting more than the account holds, and nothing needs to — every
    sleeve is bounded by `actual` too, so no strategy can spend money that does not exist. What must
    not happen is silence: a plan that cannot be met should be visible, not discovered months later
    when a sleeve never fills."""
    book = _book(MANUAL=(40_000, 20_000), MOMENTUM=(50_000, 50_000), QC345=(30_000, 0))
    assert over_committed(book, net_liquidation=100_000) == 20_000
    assert over_committed(book, net_liquidation=120_000) == 0


def test_an_inactive_sleeves_capital_still_counts():
    """the operator's model answers this by construction: a paused strategy's `actual` does not move until it
    sells, so its capital remains allocated and cannot be double-granted. Pinned because the tempting
    'free up an inactive strategy's budget' shortcut would over-commit the book the moment one pauses.
    """
    book = _book(MOMENTUM=(0, 50_000), QC345=(20_000, 0))        # MOMENTUM paused, target 0
    assert allocated(book) == 50_000
    assert book.sleeves["QC345"].deployable(currently_deployed=0) == 0, "spent a paused sleeve's capital"


def test_a_sleeve_at_target_selling_with_NO_recipient_emits_nothing():
    """The gap a mutation found: `is_reducing` looked redundant and is not.

    On the normal path a sleeve at target is already stopped by `min(proceeds, must_reduce) == 0`. But
    the no-recipient branch returns a `Transfer` BEFORE the epsilon check, so without the
    `is_reducing` guard a strategy that is merely rotating — at its target, selling to buy something
    else — would emit a zero-amount transfer to UNALLOCATED on every single sell.

    Nothing would move, so no test that checks balances could see it. It would just fill the transfer
    ledger with entries for strategies behaving exactly as intended, which is how a real transfer
    later gets scrolled past.
    """
    at_target = Sleeve("MOMENTUM", target=50_000, actual=50_000)
    assert plan_transfer(at_target, proceeds=10_000, recipient=None) is None


def test_a_zero_proceeds_sell_emits_nothing_even_while_reducing():
    """The other side of the same edge: a fill that frees nothing hands over nothing."""
    reducing = Sleeve("MOMENTUM", target=30_000, actual=50_000)
    assert plan_transfer(reducing, proceeds=0.0, recipient=None) is None
    assert plan_transfer(reducing, proceeds=0.001, recipient=None) is None, "sub-cent dust moved"


# -- the three Criticals codex found ---------------------------------------------------------------


def test_a_replayed_fill_transfers_only_once():
    """Fills are not delivered exactly once.

    A reconciliation pass re-reports them and a reconnect replays them — this codebase already carries
    `_seen_orders` and a command ledger for exactly that. Without an identity, the second delivery
    transfers again: the donor drops BELOW its target and the recipient rises ABOVE its headroom, and
    neither cap catches it because each application looked individually correct.
    """
    book = _book(MOMENTUM=(30_000, 50_000), QC345=(20_000, 0))
    t = plan_transfer(book.sleeves["MOMENTUM"], 8_000, recipient=book.sleeves["QC345"],
                      fill_id="fill-abc")
    assert t is not None

    once = apply_transfer(book, t)
    twice = apply_transfer(once, t)          # the same fill, redelivered

    assert twice.sleeves["MOMENTUM"].actual == 42_000, "a replayed fill transferred twice"
    assert twice.sleeves["QC345"].actual == 8_000
    assert allocated(twice) == allocated(book)


def test_distinct_fills_still_each_transfer():
    """The counter-case, so idempotency is not silently swallowing real fills."""
    book = _book(MOMENTUM=(30_000, 50_000), QC345=(20_000, 0))
    for fid, proceeds in (("f1", 5_000), ("f2", 3_000)):
        t = plan_transfer(book.sleeves["MOMENTUM"], proceeds, recipient=book.sleeves["QC345"],
                          fill_id=fid)
        assert t is not None
        book = apply_transfer(book, t)
    assert book.sleeves["QC345"].actual == 8_000, "a distinct fill was mistaken for a duplicate"


def test_capital_parked_in_UNALLOCATED_is_still_in_the_book():
    """The conservation law on the path it was failing.

    Capital routed to UNALLOCATED has left the STRATEGIES but not the ACCOUNT. A total that shrinks
    when capital is parked would make the invariant fail precisely where a transition needs it.
    """
    book = _book(MOMENTUM=(0, 50_000), QC345=(20_000, 20_000))
    before = allocated(book)
    t = plan_transfer(book.sleeves["MOMENTUM"], 10_000, recipient=book.sleeves["QC345"], fill_id="f")
    assert t is not None and t.to_strategy == UNALLOCATED

    book = apply_transfer(book, t)
    assert allocated(book) == before, "capital vanished when parked"
    assert book.sleeves[UNALLOCATED].actual == 10_000


def test_a_non_finite_proceeds_is_refused_at_the_boundary():
    """NaN defeats every comparison, so it must never reach a balance.

    `NaN <= _EPS` is False, so it sails through the gate and survives both `min()` caps. Once in a
    sleeve it is PERMANENT: every later comparison against it is False, so that sleeve can never
    reduce, never receive, and never be detected as wrong.
    """
    donor = Sleeve("MOMENTUM", target=30_000, actual=50_000)
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert plan_transfer(donor, bad, recipient=None) is None, f"{bad} produced a transfer"


def test_a_NaN_never_reaches_a_sleeve_balance():
    """The property that actually matters, asserted on the balance rather than on the plan."""
    book = _book(MOMENTUM=(30_000, 50_000), QC345=(20_000, 0))
    t = plan_transfer(book.sleeves["MOMENTUM"], float("nan"), recipient=book.sleeves["QC345"])
    assert t is None
    assert all(math.isfinite(s.actual) for s in book.sleeves.values())
