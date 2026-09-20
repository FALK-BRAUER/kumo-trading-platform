"""Per-strategy capital sleeves, and the transfer that moves capital between them (#320).

2026-08-15, on how a transition works:

    "We set a target budget and have an actual budget. When a ramping-down strategy sells something it
     is deducted and given to the target strategy. The ramping-down strategy target is set right away
     to reduce; the target gets the budget gradually."

That is a conservation law, and it is why this is safe: the recipient can only ever deploy capital the
donor has ACTUALLY freed. Total allocated capital is invariant across a transition, so the overlap
window — donor still holding, recipient already growing — cannot double the book's exposure. A model
where the recipient's target were raised immediately would allow exactly that.

Two numbers per strategy, and the gap between them is the instruction:

    target   OPERATOR INTENT. Changes instantly, costs nothing, moves no capital.
    actual   REALITY. The sleeve's net asset value: its cash plus the market value of what it holds.

    actual > target  ->  reduce; freed cash leaves the sleeve on each sell
    actual < target  ->  grow into it as capital arrives
    actual == target ->  steady

`actual` is a NET ASSET VALUE, not a cost basis, and that choice does the work:

  * A buy or a sell INSIDE a sleeve is an asset swap — cash becomes stock or stock becomes cash — so
    it must not move `actual`. Only transfers and P&L do.
  * Realized and unrealized P&L therefore accrue to the strategy that earned them, automatically. A
    sleeve that sells 100 shares for $12,000 against a $10,000 basis is worth $2,000 more than before,
    and any definition where it is not would shrink a profitable strategy's sleeve toward zero.

LIFECYCLE AND BUDGET ARE ORTHOGONAL, and conflating them is the mistake this module exists to avoid:

    lifecycle (kumo-trading-strategies `runtime/executor/lifecycle.py`)   MAY it act
    budget    (here)                                              HOW MUCH

"MOMENTUM goes from $50k to $30k" is not `LIQUIDATING`. That strategy stays `TRADING` and keeps
rotating, just inside a smaller sleeve. Encoding size into the state machine would need a state per
degree of reduction; leaving it here needs none. `LIQUIDATING` is then simply `target = 0` plus "do not
come back".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

#: Sub-cent movements are rounding, not intent. Comparing floats exactly here would make a sleeve
#: "reducing" by $0.0000001 forever and transfer dust on every fill.
_EPS = 0.005

#: Where capital goes when a donor frees it and no recipient is named. Not a trading strategy — but it
#: IS a sleeve, and that matters: an earlier version left it out of the book entirely, so capital
#: routed here simply vanished from `allocated()` and the conservation law quietly failed on exactly
#: the path it was meant to cover (codex review, Critical). Use `trading_sleeves()` to iterate only
#: the real strategies.
UNALLOCATED = "UNALLOCATED"


@dataclass(frozen=True)
class Sleeve:
    """One strategy's capital allocation.

    CURRENCY, written down because no field carries it (#986): `target` is the operator's number from
    the `strategies` settings domain (declared in USD); `actual` is a net asset value set by
    `budget_store.mark_to_market` (no production caller as of 2026-09-11) and otherwise moved only by
    transfers. IB's SGD net liquidation never reaches either field. A tenant in another base currency
    must convert BEFORE writing here; nothing downstream can tell.
    """

    strategy_id: str
    target: float
    actual: float

    @property
    def must_reduce(self) -> float:
        """Capital this sleeve is over its target by, and must give back as it sells."""
        return max(0.0, self.actual - self.target)

    @property
    def headroom(self) -> float:
        """Capital this sleeve could still receive before it reaches its target."""
        return max(0.0, self.target - self.actual)

    @property
    def is_reducing(self) -> bool:
        return self.must_reduce > _EPS

    def deployable(self, currently_deployed: float) -> float:
        """How much MORE this sleeve may put into positions right now.

        Bounded by BOTH numbers, and the two bounds mean different things. `target` is the operator's
        intent and stops a sleeve growing past what it was granted. `actual` is what it physically has
        and stops it spending capital that is still sitting in the donor — the whole point of the
        gradual hand-over. A sleeve mid-transition is limited by `actual`; a fully funded one by
        `target`.
        """
        return max(0.0, min(self.actual, self.target) - max(0.0, currently_deployed))


@dataclass(frozen=True)
class Transfer:
    """Capital moving from one sleeve to another because the donor sold while over target.

    `fill_id` is what makes application idempotent. Fills are not delivered exactly once — a
    reconciliation pass re-reports them, a reconnect replays them, and this codebase already carries
    `_seen_orders` and a command ledger for precisely that. Without an identity, a replayed fill
    transfers twice: the donor drops below its target and the recipient exceeds its headroom, and
    neither cap can catch it because each application looked individually correct (codex review,
    Critical).
    """

    from_strategy: str
    to_strategy: str
    amount: float
    reason: str
    fill_id: str = ""


def plan_transfer(
    seller: Sleeve,
    proceeds: float,
    *,
    recipient: Sleeve | None,
    fill_id: str = "",
) -> Transfer | None:
    """What moves when `seller` sells for `proceeds`. Pure — computes, never mutates.

    Only the EXCESS moves. A strategy at or under its target that sells is simply rotating: it has
    turned stock into cash inside its own sleeve and will buy something else. Taking that cash away
    would break a strategy that is behaving exactly as intended — which is why `is_reducing` gates
    this rather than "did a sell happen".

    The amount is capped at `must_reduce` so a single large sale cannot overshoot and leave the donor
    UNDER its target, and capped at `proceeds` because capital that has not actually been freed cannot
    be handed over. That second cap is the conservation law, and it is what makes the overlap window
    safe.

    A recipient at or above its own target does not receive — capital returns to UNALLOCATED instead.
    Otherwise a finished hand-over would keep pushing capital into a sleeve that has already been
    filled, silently overshooting the operator's intent.
    """
    # NaN defeats every comparison below — `NaN <= _EPS` is False, so it would sail through the gate,
    # survive both `min()` caps, and land in a sleeve balance. Once there it is permanent: every
    # subsequent comparison against it is False, so the sleeve can never reduce, never receive, and
    # never be detected as wrong. Reject non-finite input at the boundary (codex review, Critical).
    if not math.isfinite(proceeds) or proceeds <= _EPS or not seller.is_reducing:
        return None
    amount = min(proceeds, seller.must_reduce)
    if recipient is None or recipient.headroom <= _EPS:
        return Transfer(seller.strategy_id, UNALLOCATED, amount,
                        "donor is over target; no recipient with headroom", fill_id)
    amount = min(amount, recipient.headroom)
    if amount <= _EPS:
        return None
    return Transfer(seller.strategy_id, recipient.strategy_id, amount,
                    "donor over target, recipient under target", fill_id)


@dataclass(frozen=True)
class Book:
    """Every sleeve, plus the fills already applied to it.

    The applied-set lives WITH the balances rather than beside them, because the two are only correct
    together: a book restored from one snapshot and an applied-set from another would re-apply or skip
    transfers silently.
    """

    sleeves: dict[str, Sleeve]
    applied: frozenset[str] = field(default_factory=frozenset)


def apply_transfer(book: Book, transfer: Transfer) -> Book:
    """Apply a transfer, returning a NEW book. Pure, and IDEMPOTENT on `fill_id`.

    A transfer whose `fill_id` has already been applied is a no-op, because the second delivery of a
    fill carries no new information. A transfer with an EMPTY `fill_id` is applied unconditionally —
    callers that cannot identify their fill get no protection, and that is deliberate rather than
    silent: the alternative is treating every anonymous transfer as a duplicate of the last one.

    Capital routed to UNALLOCATED is CREDITED there rather than dropped. It has left the strategies but
    not the account, and a total that shrinks when capital is parked would make the conservation law
    fail exactly where it is needed.
    """
    if transfer.fill_id and transfer.fill_id in book.applied:
        return book
    out = dict(book.sleeves)
    donor = out[transfer.from_strategy]
    out[transfer.from_strategy] = replace(donor, actual=donor.actual - transfer.amount)
    target = out.get(transfer.to_strategy) or Sleeve(transfer.to_strategy, target=0.0, actual=0.0)
    out[transfer.to_strategy] = replace(target, actual=target.actual + transfer.amount)
    applied = book.applied | {transfer.fill_id} if transfer.fill_id else book.applied
    return Book(out, applied)


def trading_sleeves(book: Book) -> dict[str, Sleeve]:
    """The real strategies, excluding the UNALLOCATED holding pen."""
    return {k: v for k, v in book.sleeves.items() if k != UNALLOCATED}


def allocated(book: Book) -> float:
    """Total capital across every sleeve INCLUDING UNALLOCATED. Invariant under any transfer."""
    return sum(s.actual for s in book.sleeves.values())


def over_committed(book: Book, net_liquidation: float) -> float:
    """How much the sum of TARGETS exceeds what the account actually has.

    Targets are intent and nothing stops an operator setting three of them that add to more than the
    account holds. That is not immediately dangerous — `deployable` is bounded by `actual` too, so
    nothing can spend money that does not exist — but it means the stated plan is unreachable, and a
    plan that cannot be met should be visible rather than discovered when a sleeve never fills.
    """
    return max(0.0, sum(s.target for s in trading_sleeves(book).values()) - net_liquidation)

@dataclass(frozen=True)
class Allocation:
    """One sleeve's share of a distribution. Pure data — computing it mutates nothing."""

    to_strategy: str
    amount: float


def plan_distribution(
    sleeves: dict[str, Sleeve], available: float, *, exclude: frozenset[str] = frozenset({UNALLOCATED})
) -> list[Allocation]:
    """Split `available` across under-target sleeves in proportion to how short each one is.

    THE GAP THIS FILLS (#373). Until now nothing could move capital INTO a sleeve. `TRANSFER_TO` routes
    a seller's proceeds to ONE named recipient, `POST /transfers` moves a position rather than cash, and
    Setting a target writes intent without funding anything. So a strategy could be given a 20,000 target and
    still hold `actual = 0` forever, with `deployable` pinned at 0 — which is exactly where BCTROT-004
    and QC345-003 sat on 2026-08-19 while 45,000.00 sat unallocated.

    PROPORTIONAL TO SHORTFALL, not equal shares. `headroom` is already the shortfall (`target - actual`,
    floored at zero), so a sleeve that is 15k short receives three times what a sleeve 5k short receives.
    Equal shares would overfund the nearly-full sleeve and underfund the empty one, and the difference
    only shows once the sleeves diverge — which is precisely when it matters and precisely when nobody
    is looking for it.

    TWO CAPS, AND BOTH ARE LOAD-BEARING:

      conservation   the total handed out never exceeds `available`. Capital that does not exist cannot
                     be allocated, and this is the same law `plan_transfer` enforces by capping at
                     `proceeds`.
      intent         no sleeve receives more than its own headroom, so a distribution cannot push a
                     sleeve past the target its operator set. If `available` exceeds the total shortfall,
                     every sleeve is simply filled and the remainder stays unallocated.

    `UNALLOCATED` is excluded by default: it is the source, and a source that receives its own
    distribution would loop capital back into itself and report progress that never happened.

    Returns an empty list rather than raising when there is nothing to do — no capital, no shortfall, or
    a non-finite input. A distribution that cannot be computed must move nothing, not guess.
    """
    # NaN and infinity defeat every comparison and cap below, exactly as they do in `plan_transfer`:
    # `NaN <= _EPS` is False, so a non-finite amount would sail through the gate and land in a sleeve
    # balance, after which every comparison against it is False and the sleeve can never be corrected.
    if not math.isfinite(available) or available <= _EPS:
        return []

    short = {
        sid: sleeve.headroom
        for sid, sleeve in sorted(sleeves.items())
        if sid not in exclude and sleeve.headroom > _EPS and math.isfinite(sleeve.headroom)
    }
    total_short = sum(short.values())
    if total_short <= _EPS:
        return []

    pot = min(available, total_short)

    # INTEGER CENTS FROM HERE DOWN. Two float attempts got this wrong in opposite directions: rounding
    # the remainder to 2dp handed out MORE than existed (available=100.005 produced 100.01), and
    # flooring it silently dropped a legitimate cent, because `1000 - 999.99` is 0.00999999... in binary
    # and floors to zero. Money in binary floating point cannot express the invariant this function is
    # built on, so the split is computed in whole cents and converted back once.
    pot_c = int(math.floor(pot * 100))
    short_c = {sid: int(math.floor(h * 100)) for sid, h in short.items()}
    total_c = sum(short_c.values())
    if pot_c <= 0 or total_c <= 0:
        return []
    pot_c = min(pot_c, total_c)

    # Largest-remainder apportionment: floor each share, then hand the leftover cents to the largest
    # fractional parts. Exact by construction — the floors plus the leftovers sum to `pot_c`.
    exact = {sid: pot_c * c for sid, c in short_c.items()}          # numerator, denominator total_c
    cents = {sid: num // total_c for sid, num in exact.items()}
    leftover = pot_c - sum(cents.values())
    if leftover > 0:
        # Deterministic: largest remainder first, then strategy id, so the same book always produces the
        # same split and a test can pin it. Capped at each sleeve's own headroom, so the last cents
        # cannot push a sleeve past the target its operator set.
        order = sorted(short_c, key=lambda sid: (-(exact[sid] % total_c), sid))
        for sid in order:
            if leftover <= 0:
                break
            # UNREACHABLE BY CONSTRUCTION today, and kept deliberately rather than tested. `pot_c` is
            # capped at `total_c`, so `cents[sid] = pot_c*c // total_c < c` strictly whenever there is
            # any leftover at all — a sleeve can never already be at its cap here. A test for this
            # branch could not fail, and this repo has shipped several assertions that could not fail;
            # saying so is more honest than inventing a fixture that pretends otherwise. The guard
            # stays because it is the invariant the loop depends on, and the arithmetic above it has
            # already been rewritten twice.
            if cents[sid] < short_c[sid]:
                cents[sid] += 1
                leftover -= 1

    amounts = {sid: c / 100 for sid, c in cents.items()}
    return [Allocation(sid, amt) for sid, amt in sorted(amounts.items()) if amt > 0]
