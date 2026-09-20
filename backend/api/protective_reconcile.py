"""What protective orders REST at the venue, against what the book actually holds (#748).

WHY THIS EXISTS — measured on an Alpaca paper instance, 2026-08-31 at the open:

    SSRM 12 live stops · HALO 8 · AEM 5 · VCTR 4 · RGEN 4 · five more at 2 · 66 working in total

SSRM held ~53 shares. Twelve trailing stops rested on it; had the trail triggered, all twelve fire —
~636 shares sold against 53 held, a ~583-share naked short in a long-only book. HALO had already done
the small version: `order.filled_qty=19, fill.last_qty=18, would result in 37`.

THE LOOP THAT PRODUCED THEM. A protective stop was stamped with the SUBMITTING strategy (MANUAL-001)
on instruments MANUAL-001 does not hold. Nautilus derives the NETTING position id from the ORDER's
strategy_id, so the fill resolved to `HALO.XNAS-MANUAL-001` — a position that never existed — and the
ExecEngine rejected it. A rejected fill means the engine still reads the symbol as unprotected, so it
submits another. Once per protection tick, forever.

TWO HALVES, AND ONE IS NOT ENOUGH. Refusing to submit an unstampable order (see the dispatch in
`engine_node`) stops NEW duplicates. It does nothing about what is already resting, and the danger is
entirely in what is already resting. This module is the other half.

IT PLANS, IT DOES NOT ACT. A pure function over (positions, orders) returning cancels with reasons,
so the decision is testable without a venue and the caller owns the side effects.

WHAT IT REFUSES TO DO. An empty or unreadable position book makes every stop look orphaned, so a
reconciler that trusted it would cancel ALL protection on a book it merely could not see. Empty IS
the failure being guarded against, so empty can never authorise a cancel — it refuses and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: `refused` value when the position book cannot be trusted. Not an empty plan: "I could not look"
#: and "I looked and there is nothing to do" are different facts, and only one of them is safe.
UNKNOWN_BOOK = "position book is empty or unreadable — refusing to judge any resting protection"

#: Order types that PROTECT a long position. A resting LIMIT is a target, not a floor.
#:
#: PINNED AGAINST THE REAL `OrderType` ENUM, member by member, in the conformance test. The first
#: version of this set was copied from the UI and contained `TRAILING_STOP` and `STOP` — NEITHER IS A
#: NAUTILUS ORDER TYPE. They matched nothing, ever, while making the set look more complete than it
#: was. It also OMITTED `TRAILING_STOP_LIMIT`, which is real and protective, so a stop of that type
#: would have been invisible here: never deduped, never cancelled when orphaned.
#:
#: Neither error was findable by reading this line. Both fell out of listing the enum.
PROTECTIVE_TYPES = frozenset({
    "STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP_MARKET", "TRAILING_STOP_LIMIT",
})

#: Statuses in which an order is still RESTING at the venue and can therefore be cancelled. Pinned
#: member by member against the real `OrderStatus` for the same reason as the set above.
#:
#: `EMULATED` and `RELEASED` are excluded on purpose — Nautilus holds those locally and there is
#: nothing at the venue to cancel. `PENDING_CANCEL` is excluded because it is already on its way out.
#: Share-count tolerance. Quantities are venue-rounded, so an exact equality test would
#: read a fully covered position as one share short and place a stop for the remainder.
_QTY_EPS = 1e-9

LIVE_STATUS = frozenset({"ACCEPTED", "PARTIALLY_FILLED", "TRIGGERED", "PENDING_UPDATE", "SUBMITTED"})


@dataclass(frozen=True)
class Cancel:
    client_order_id: str
    instrument_id: str
    #: Why this order is being pulled, in words an operator can act on. A cancel with no stated cause
    #: is indistinguishable from the defect it is fixing.
    reason: str


@dataclass(frozen=True)
class Plan:
    cancel: tuple[Cancel, ...] = ()
    keep: tuple[str, ...] = ()
    #: Held (instrument, lane) pairs with no live protective order after this plan is applied.
    naked: tuple[str, ...] = ()
    refused: str | None = None


def _s(v) -> str:
    return str(v or "")


def _enum_name(v) -> str:
    """The NAME of a Nautilus enum member, never `str()` of it.

    MEASURED IN THE RUNNING CONTAINER: `str(OrderType.TRAILING_STOP_MARKET)` is `'8'` and
    `str(OrderStatus.ACCEPTED)` is `'6'` — these are Cython enums whose `__str__` is the ordinal.
    Comparing that against `"TRAILING_STOP_MARKET"` matches nothing, so an earlier version of this
    module classified EVERY production order as non-protective: it would have found nothing, cancelled
    nothing, and reported a clean book while twelve stops rested on one position.

    All fourteen tests passed, because `SimpleNamespace` doubles hand back the name. That is the
    double-cannot-represent-production trap, and this is where it is closed: `.name` first, since it
    is what the real enum carries and what a conforming double must therefore also carry.
    """
    name = getattr(v, "name", None)
    return _s(name if name is not None else v).upper()


def _is_protective(order) -> bool:
    return _enum_name(getattr(order, "order_type", "")) in PROTECTIVE_TYPES


def _is_live(order) -> bool:
    return _enum_name(getattr(order, "status", "")) in LIVE_STATUS


def reconcile_protection(positions_open, working_orders) -> Plan:
    """Decide which resting protective orders must be cancelled.

    `positions_open` is the engine's open positions; `working_orders` everything live at the venue.

    THE RULE IS OWNERSHIP, NOT COUNT. A protective order can only ever protect the position whose
    NETTING id its own strategy_id derives — so a stop stamped with a lane that holds nothing on that
    instrument is not "extra", it is INERT: every fill it produces is rejected, while it occupies the
    slot that tells the engine the symbol is covered. Those are cancelled even when they are the only
    order on the symbol, because keeping one would leave the book reading protected while naked.

    Among orders that CAN protect, the oldest survives. Not the newest: the incumbent is the one that
    has been resting and may be partially filled, and replacing it with a fresh stop re-opens exactly
    the window it was covering.
    """
    holders: dict[tuple[str, str], object] = {}
    saw_any = False
    for p in positions_open or []:
        # `is_open` absent means we cannot tell whether this position is live — and an unreadable
        # position must not vouch for a stop. Skipped rather than assumed either way.
        if not hasattr(p, "is_open") or not p.is_open:
            continue
        lane, iid = _s(getattr(p, "strategy_id", "")), _s(getattr(p, "instrument_id", ""))
        if not lane or not iid:
            continue
        saw_any = True
        holders[(iid, lane)] = p

    if not saw_any:
        # THE GUARD THAT COMES FIRST. See the module docstring: empty is the bug, so empty cannot
        # authorise a cancel. Refuses loudly instead of returning a plan that happens to be harmless.
        return Plan(refused=UNKNOWN_BOOK)

    by_instrument: dict[str, list] = {}
    for o in working_orders or []:
        if not _is_protective(o) or not _is_live(o):
            continue                       # entries, targets and terminal orders are not our business
        by_instrument.setdefault(_s(getattr(o, "instrument_id", "")), []).append(o)

    cancels: list[Cancel] = []
    keep: list[str] = []
    for iid, orders in by_instrument.items():
        lanes_here = sorted(lane for (i, lane) in holders if i == iid)
        # Oldest first, so the incumbent wins the dedupe below.
        orders = sorted(orders, key=lambda o: (int(getattr(o, "ts_init", 0) or 0), _s(o.client_order_id)))
        # SHARES COVERED PER LANE, not orders seen. Counting orders was wrong and it churned a live
        # book within minutes of shipping: AYA holds 73, a stop covered 65, the reconciler correctly
        # placed 8 more to complete the cover — and this cancelled it as a "duplicate", every tick.
        #
        # Two stops summing to the position are COMPLEMENTARY. The double-sell this rule exists to
        # stop is the OVER-coverage case (GMAB 59 + 59 resting against a 59 holding, both fired,
        # 118 sold), so quantity was always the right test.
        covered: dict[str, float] = {}
        for o in orders:
            coid, lane = _s(o.client_order_id), _s(getattr(o, "strategy_id", ""))
            if (iid, lane) not in holders:
                cancels.append(Cancel(coid, iid, (
                    f"stamped {lane or '<unstamped>'}, which holds no open {iid} position "
                    f"(held by: {', '.join(lanes_here) or 'nobody'}). Every fill it produces is "
                    f"rejected by the ExecEngine, so it protects nothing while the engine reads the "
                    f"symbol as covered"
                )))
                continue
            held = abs(float(getattr(holders[(iid, lane)], "signed_qty", 0) or 0))
            already = covered.get(lane, 0.0)
            if already >= held - _QTY_EPS:
                cancels.append(Cancel(coid, iid, (
                    f"protection on {iid}/{lane} already covers {already:,.0f} of {held:,.0f} held — "
                    f"this stop is EXCESS, and every copy fires on the same trigger, selling more "
                    f"than is held and opening a short"
                )))
                continue
            covered[lane] = already + abs(float(getattr(o, "quantity", 0) or 0))
            keep.append(coid)

    protected = {(_s(getattr(o, "instrument_id", "")), _s(getattr(o, "strategy_id", "")))
                 for orders in by_instrument.values() for o in orders
                 if _s(o.client_order_id) in keep}
    naked = tuple(sorted(f"{i}/{lane}" for (i, lane) in holders if (i, lane) not in protected))
    return Plan(tuple(cancels), tuple(keep), naked, None)
