"""Realized P&L per period, from the BROKER's own fill record (#322).

`api.realized` answers for the current session from `cache.positions_closed()`, and that is the right
source for today — native, no parallel ledger. It cannot answer for a WEEK: the Nautilus cache holds
positions closed during THIS PROCESS's life, and reconciliation restores open positions on startup,
not closed ones. So a mid-session restart zeroes the figure and it climbs again from the next close.

The broker's fill activities survive everything. They are also the record an operator would reconcile
against, which makes them the right authority for a number that answers "what did this week make".

WHY FIFO MATCHING AND NOT A P&L FIELD. Alpaca's activities carry price and quantity per fill, not
realized P&L per round trip — the broker has no opinion about which buy a sell closed. So lots are
matched first-in-first-out per symbol, which is the convention every statement uses.

ATTRIBUTION (#292) is per LOT, through `strategy_of(fill)` — the caller resolves a fill's opening order
to a strategy from the cache, and a fill whose opener the cache has forgotten lands in `unclaimed`
rather than being handed to the closer. That is honest for an account total. It is NOT the per-strategy
figure the cockpit renders: that comes from Nautilus's own persisted position legs
(`realized.realized_windows`, #846), which survive restarts and reopens. This module's split is
published beside it (`realized_periods_swept`) so the two derivations can be compared, not averaged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PeriodRealized:
    """Realized P&L over a window, and how much of the window the fills actually cover."""

    total: float
    #: Round trips that closed inside the window.
    closed: int
    #: Sells with no matching buy in the window — the position was opened BEFORE it began. Their P&L
    #: is unknowable from these fills alone, so they are counted rather than guessed at, and a non-zero
    #: value means `total` understates the period.
    unmatched: int

    @property
    def is_partial(self) -> bool:
        return self.unmatched > 0


def _match(
    activities: list[dict],
    lots: dict[str, list[list]],
    direction: dict[str, int],
    events: list[tuple[str, float]] | None = None,
    strategy_of: callable | None = None,
    attrib: list[tuple[str, float, str | None]] | None = None,
) -> tuple[float, int, int]:
    """The matcher. ONE implementation, mutating the caller's book so the residue can be inspected.

    `realized_from_fills` wants the P&L; `open_lots_after` wants the leftovers. Both come from the same
    pass deliberately — two walks over one set of fills would drift, and the reconciliation would then
    be checking the second walk rather than the number anyone reports.

    Ordering is by `transaction_time`, not by the order the API returned them: FIFO on an unsorted list
    is not FIFO, and the error would be silent and small enough to look plausible.
    """
    total = 0.0
    closed = 0
    unmatched = 0

    for fill in sorted(activities, key=lambda a: str(a.get("transaction_time") or "")):
        symbol = str(fill.get("symbol") or "")
        try:
            qty = float(fill.get("qty") or 0)
            price = float(fill.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or qty <= 0:
            continue

        side = str(fill.get("side") or "")
        # THE LOT'S OPENING TAG (#292, decided by the operator 2026-08-14): "P&L is attributed to the strategy
        # tag that OPENED the position", "tagging is at LOT level, not position level", disposal FIFO.
        # The matcher was already FIFO by lot, so attribution is a third field on the lot rather than a
        # second pass. None means the opener is unknown — a fill from before the cache's horizon, or
        # activity this cockpit did not originate. It is carried as None and reported as UNCLAIMED, never
        # guessed at: attributing an unknown opener to whoever closed it is the exact leak #292 exists to
        # close ("MOMENTUM does the work, a human clicks sell, and the manual book gets the credit").
        tag = strategy_of(fill) if strategy_of is not None else None
        book = lots.setdefault(symbol, [])
        # WHICH WAY THIS FILL POINTS, from Alpaca's OWN label rather than inferred from the book.
        #
        # `sell_short` OPENS a short; a plain `sell` CLOSES a long. Inferring it from "is a long lot
        # open" gets that backwards at a window edge, and the cost was measured, not theorised: PENG was
        # sold short on 2026-07-28 and later covered. Read as a plain sell it matched nothing, and the
        # covering BUY then opened a phantom LONG lot of 23 shares that never closes — $1,235.10 of cost
        # basis the broker does not have, and the short's entire P&L silently dropped.
        opening_short = side == "sell_short"
        want = 1 if side.startswith("buy") else -1

        if book and direction.get(symbol) == want:
            book.append([qty, price, tag])  # adds to the position; closes nothing
            continue
        if not book and (side.startswith("buy") or opening_short):
            book.append([qty, price, tag])
            direction[symbol] = want
            continue

        remaining = qty
        matched_any = False
        fill_realized = 0.0
        was_short = direction.get(symbol) == -1
        while remaining > 0 and book:
            lot_qty, lot_px, lot_tag = book[0]
            take = min(remaining, lot_qty)
            # Closing a SHORT profits when the price FALLS, so the sign flips. Getting this wrong does
            # not look wrong — it reports a losing short as a winner of the same size.
            gain = take * ((lot_px - price) if was_short else (price - lot_px))
            total += gain
            fill_realized += gain
            # PER LOT SLICE, not per closing fill: one sale can consume lots opened by different
            # strategies, and collapsing them would hand the whole gain to whichever opened first.
            if attrib is not None:
                attrib.append((str(fill.get("transaction_time") or ""), gain, lot_tag))
            remaining -= take
            book[0][0] -= take
            matched_any = True
            if book[0][0] <= 0:
                book.pop(0)
        if matched_any:
            closed += 1
            # Record WHEN this P&L was realized (#336). Realized gain is recognised at the SALE, with the
            # lot's true basis however far back it was bought — which is what a broker statement, and a
            # tax lot, both do. Emitting it per closing fill lets `realized_by_period` bucket one
            # full-history match into windows instead of re-matching inside each window with a truncated
            # book. See that function for what the old approach got wrong.
            if events is not None:
                events.append((str(fill.get("transaction_time") or ""), fill_realized))
        if remaining > 0:
            if opening_short:
                # Flipped through flat into a new short. What is left OPENS, and is not a gap — and it
                # is opened by THIS fill, so it carries this fill's tag, not the closed lot's.
                book.append([remaining, price, tag])
                direction[symbol] = -1
            else:
                # Sold more than the window's buys account for: opened BEFORE the window started. Its
                # basis is unknowable from these fills, so it is counted rather than guessed at.
                unmatched += 1

    return total, closed, unmatched


def realized_from_fills(activities: list[dict]) -> PeriodRealized:
    """FIFO-match fills into round trips. Pure — takes what the REST call returned."""
    total, closed, unmatched = _match(activities, {}, {})
    return PeriodRealized(total=total, closed=closed, unmatched=unmatched)


#: Period key -> lookback in ET calendar days. `all` is inception-to-date, hence None.
#:
#: The vocabulary is Alpaca's and the UI's `PERIODS` — one list, because a period the selector offers and
#: the backend does not compute renders a confident-looking dash.
PERIOD_DAYS: dict[str, int | None] = {"1D": 0, "1W": 7, "1M": 30, "3M": 90, "all": None}


def realized_by_period(
    activities: list[dict],
    *,
    day_start_ns: int,
    ns_of: callable,
    adjustments: list[dict] | None = None,
    strategy_of: callable | None = None,
    floor_of: callable,
) -> dict[str, dict]:
    """Every period at once, from ONE fetch of the account's fills.

    `floor_of(day_start_ns, days=N) -> ns` names a window's start (#846) and is REQUIRED: an optional
    one is the invisible-missing-argument fallback, and the fallback was `day_start - N * 86,400s` —
    23:00 or 01:00 ET across a DST change while the legs-derived windows (`realized.realized_windows`)
    start at ET midnight, two derivations of one fact disagreeing BY CONSTRUCTION on a boundary leg.
    The engine passes the legs' own predicate (`et_window_start_ns`) so both agree.

    One fetch rather than one per period because the periods NEST: 1D's fills are a subset of 1W's are a
    subset of all. Asking the broker five times for overlapping windows costs five round trips to learn
    the same thing, and the five answers can disagree if a fill lands mid-sweep.

    MATCHED ONCE OVER ALL HISTORY, THEN BUCKETED BY WHEN THE SALE HAPPENED (#336).

    This function used to re-run FIFO INSIDE each window, and defended it here: "a lot opened before the
    window has no basis inside it, so 1W's matching is genuinely a different calculation from All's".
    That reasoning is wrong, and it produced numbers the operator could not use. Measured live 2026-08-18:

        1W  $2,226.12*    1M  $825.52*    3M  $138.50

    A shorter window reading HIGHER than a longer one containing it, with an asterisk and "+19 opened
    earlier" admitting the figure was understated. Three numbers under one label, none comparable with
    another, because each used a different cost basis: any lot bought before the window contributed its
    PROCEEDS with no basis at all.

    Realized P&L is recognised AT THE SALE, with the lot's true basis however far back it was bought.
    That is what a brokerage statement does, what a tax lot does, and what anyone reading "realized this
    week" means. So: match once across the whole fill record, record what each closing fill realized, and
    sum the ones that fall inside each window. The windows nest again, the asterisk disappears, and 3M
    genuinely contains 1W.

    `unmatched` survives but now means something ELSE, and something more useful: a sale whose opening
    buy is outside the FETCH horizon entirely — the broker record we pulled does not go back far enough.
    That is a property of the data we hold, not of the window being displayed, so it no longer varies
    from period to period. Non-zero still means the totals understate, and it is still surfaced.
    """
    events: list[tuple[str, float]] = []
    attrib: list[tuple[str, float, str | None]] = []
    _, _, unmatched = _match(activities, {}, {}, events, strategy_of=strategy_of, attrib=attrib)

    # NON-FILL CASH, BUCKETED THE SAME WAY (#345 item 1). Fees and withholdings are recognised on the
    # day they post, so they bucket by date exactly as a closing fill buckets by its sale. Stamped at
    # noon UTC — 07:00-08:00 ET depending on DST, which is inside the ET day the row is dated to, and
    # therefore lands in the same window the day's fills do. Using `created_at` instead would put a fee
    # booked overnight into the NEXT ET day from the trades that caused it.
    adj_events: list[tuple[str, float]] = []
    for a in adjustments or []:
        if str(a.get("activity_type") or "") == FILL_TYPE:
            continue
        raw = a.get("net_amount", a.get("amount"))
        day = str(a.get("date") or "")[:10]
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            continue
        if not day:
            continue
        adj_events.append((f"{day}T12:00:00Z", amount))

    out: dict[str, dict] = {}
    day_ns = 86_400 * 1_000_000_000
    for key, days in PERIOD_DAYS.items():
        floor_ns = None if days is None else floor_of(day_start_ns, days=days)
        in_window = [
            realized for ts, realized in events if floor_ns is None or ns_of(ts) >= floor_ns
        ]
        adj_in_window = [
            amount for ts, amount in adj_events if floor_ns is None or ns_of(ts) >= floor_ns
        ]
        gross = sum(in_window)
        adj = sum(adj_in_window)
        # PER STRATEGY, bucketed the same way and by the rule #292 fixed (#345 item 3).
        #
        # `unclaimed` is not a rounding bucket, it is the honest half of the answer. Measured against
        # the live account 2026-08-21: fills join to a cached order 100% of the time from 2026-08-17
        # and essentially 0% before it — the Nautilus cache does not reach further back, so an all-time
        # per-strategy figure CANNOT be derived and must not pretend to be. The invariant that makes the
        # number checkable is the one #345 asked for:
        #
        #     sum(by_strategy.values()) + unclaimed == total
        #
        # Two derivations of one fact, so a disagreement is the detector rather than a silent drift.
        by_strategy: dict[str, float] = {}
        unclaimed = 0.0
        for ts, gain, tag in attrib:
            if floor_ns is not None and ns_of(ts) < floor_ns:
                continue
            if tag is None:
                unclaimed += gain
            else:
                by_strategy[tag] = by_strategy.get(tag, 0.0) + gain
        out[key] = {
            # `total` stays FILL-ONLY and keeps its meaning. Changing what an existing key means is how
            # #336 shipped three incomparable numbers under one label; the new fact gets a new name.
            "total": gross,
            "closed_count": len(in_window),
            #: Cash that moved without a fill in this window — fees, withholdings. Reported separately
            #: rather than folded in, because "what my trades made" and "what left the account" are
            #: different questions and the panel is allowed to show both.
            "adjustments": adj,
            #: What actually hit the account. This is the one that reconciles against equity.
            "net": gross + adj,
            #: Realized in this window per OPENING strategy tag (#292). Excludes adjustments — a fee is
            #: an account-level cost, not one strategy's trading result, and splitting it would be an
            #: allocation decision nobody has made.
            "by_strategy": by_strategy,
            #: Realized whose opening lot has no known strategy — before the cache horizon, or activity
            #: this cockpit did not originate. Reported, never distributed: handing it to whoever closed
            #: the position is the exact leak #292 exists to close.
            "unclaimed": unclaimed,
            # The same horizon caveat for every window — it describes the fetch, not the period.
            "unmatched": unmatched,
            "is_partial": unmatched > 0,
        }
    return out


#: Activity types that move CASH without being a fill. Measured on the paper account 2026-08-20 rather
#: than enumerated from docs: of 362 activities, 323 were FILL, 38 were FEE and one was WH.
#:
#: This is deliberately a NEGATIVE definition — everything that is not a FILL — instead of a list of
#: known types. A list would silently drop the next type Alpaca introduces, and dropping is exactly the
#: failure being fixed: the one WH row was worth $990.46 and nobody knew it existed.
FILL_TYPE = "FILL"


def cash_adjustments(activities: list[dict]) -> tuple[float, dict[str, float]]:
    """Cash that moved WITHOUT a fill: total, and the breakdown by activity type.

    THE MISSING $999.09 (#345 item 1). Realized-from-fills reported +$3,574.04 with `unmatched: 0` while
    the broker's own arithmetic implied +$2,574.95. Both were right about what they measured, and the
    entire gap was cash the fill sweep cannot see:

        WH / SLWH   "PTP Withholding", symbol PAA, 2026-08-06     -990.46
        FEE x38     CAT / TAF / REG                                  -8.63
                                                                 ---------
                                                                   -999.09

    exactly the disagreement, to the cent. PAA is a publicly traded partnership; Alpaca withholds on
    PTP proceeds, and that withholding is a cash movement with no fill behind it.

    WHY `unmatched` COULD NEVER HAVE CAUGHT THIS. It counts sales whose opening buy is missing from the
    fetch. A withholding is not a sale and a fee is not a sale, so the flag reads clean — and reading
    clean is what made the number trustworthy. The issue's own hypotheses (a sweep truncated at the new
    end, the short-page-is-terminal assumption, dividend credits) were all measured and all ruled out:
    the sweep returned 323 fills spanning 2026-07-13 to four minutes before the probe ran, terminating
    on a genuinely short page, and the account has no DIV, INT or JNL activity at all.
    """
    by_type: dict[str, float] = {}
    total = 0.0
    for a in activities:
        kind = str(a.get("activity_type") or "")
        if kind == FILL_TYPE:
            continue
        raw = a.get("net_amount", a.get("amount"))
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            continue
        by_type[kind] = by_type.get(kind, 0.0) + amount
        total += amount
    return total, by_type


def fill_cashflow(activities: list[dict]) -> float:
    """Net cash the FILLS moved: sells credit, buys debit. The exact half of the reconciliation.

    Measured 2026-08-23 on the live paper account: baseline 100,000.00 + this (−25,605.79) +
    `cash_adjustments` (−1,000.88) = 73,393.33, which is Alpaca's `cash` TO THE CENT. That equality is
    the strongest statement available about the record, and it is stronger than the equity-implied one
    for a specific reason: it carries no marks. Equity-implied realized subtracts `unrealized`, and
    `unrealized` is the broker's marks against the broker's cost basis — two more things that can be
    wrong. Cash is cash.

    Non-fill rows are skipped: their money is the `adjustments` term, and counting a fee here and there
    would double it.

    `sell_short` credits cash exactly as `sell` does, which is why the test is `startswith("sell")` and
    not equality — the PENG short of 2026-07-28 is in this record.
    """
    total = 0.0
    for a in activities:
        if str(a.get("activity_type") or "") != FILL_TYPE:
            continue
        try:
            qty = float(a["qty"])
            price = float(a["price"])
        except (KeyError, TypeError, ValueError):
            continue
        total += qty * price if str(a.get("side") or "").startswith("sell") else -qty * price
    return total


def reconcile(
    *,
    reported_realized: float,
    adjustments: float,
    equity: float,
    baseline: float,
    unrealized: float,
    tolerance: float = 1.0,
    cash: float | None = None,
    fill_cashflow: float | None = None,
    cash_tolerance: float = 0.01,
    lots_diverge: bool | None = None,
) -> dict:
    """Two derivations of one fact, compared — and the residue reported rather than assumed away.

    CLAUDE.md: verification by disagreement, in both directions. `reported_realized` comes from FIFO
    matching the fill record; `equity - baseline - unrealized` comes from the broker's own books and
    shares no code with it. When they agree, the fill record is complete AND the matcher is right, which
    no single derivation can establish about itself. When they disagree, the residue is the finding.

    This is the guard #345 asked for: an assertion about the record that is INDEPENDENT of the matcher,
    because the matcher's own completeness flag is structurally blind (see `cash_adjustments`).

    `tolerance` is $1: fee rows post overnight, so an intraday read can legitimately be a few cents out
    on the day's not-yet-booked CAT/TAF. A dollar is far below the $999.09 this was built to catch and
    far above the rounding.

    `lots_diverge` is the share-side arm: cash answers what MOVED, and nothing about cash can see a
    quantity change that moved no money. Pass the result of comparing `open_lots_after` against the
    account's own positions — the verdict is decided in ONE place because the alternative is two
    mechanisms answering one question without consulting each other.

    `cash` and `fill_cashflow` add a THIRD derivation and a `verdict`, because "they disagree" turned
    out not to be actionable on its own — see the comment in the body. `cash_tolerance` is a cent, not a
    dollar: cash is exact, and the moment it is not, money moved that the record does not show.
    """
    implied = equity - baseline - unrealized
    residual = (reported_realized + adjustments) - implied
    ok = abs(residual) <= tolerance

    # THE THIRD DERIVATION, and the one that decides what a residual MEANS (2026-08-23). Equity-implied
    # realized disagreed by $33.53 for hours while the record was complete to the cent: BETA went flat
    # at 15:35:22 on 2026-08-20 and reopened at 16:00:10, and Alpaca's `avg_entry_price` did not reset,
    # so its `cost_basis` still blended a lot that had been sold in full. `unrealized` inherits that,
    # and `implied` inherits it from `unrealized`. Cash inherits nothing.
    cash_residual: float | None = None
    verdict = "OK" if ok else "UNKNOWN"
    if cash is not None and fill_cashflow is not None:
        cash_residual = (baseline + fill_cashflow + adjustments) - cash
        cash_ok = abs(cash_residual) <= cash_tolerance
        if lots_diverge:
            # VALUE LEAVES WITHOUT CASH MOVING (59sh1zl1, reviewing this change). A split, reverse
            # split, merger-with-exchange, spinoff or symbol change alters QUANTITY with no cash
            # activity at all — so the cash arm reconciles to the cent while every lot basis derived
            # from the fills is stale. Saying "the record is COMPLETE" there is worse than saying
            # nothing. The lot check already knew; it simply was not asked.
            verdict = "LOTS_DIVERGE"
        elif ok and cash_ok:
            verdict = "OK"
        elif cash_ok:
            # Every dollar that entered or left is accounted for. Whatever the equity arm disagrees
            # about is a valuation, not a movement — and telling an operator that money moved
            # unexplained when it did not is how an alarm gets ignored before the day it matters.
            verdict = "BROKER_BASIS"
        else:
            verdict = "RECORD_INCOMPLETE"

    return {
        "implied": implied,
        "reported": reported_realized,
        "adjustments": adjustments,
        "residual": residual,
        "reconciled": ok,
        "cash_residual": cash_residual,
        "verdict": verdict,
    }


@dataclass(frozen=True)
class OpenLot:
    """One surviving lot: what is still held, what it cost, and which strategy OPENED it."""

    symbol: str
    qty: float                    #: signed — shorts negative, matching `open_lots_after`
    basis: float                  #: the lot's own fill price, never an average across lots
    strategy_id: str | None       #: the OPENER (#292). None = unknown, reported UNCLAIMED, never guessed.


@dataclass(frozen=True)
class OpenBook:
    """A symbol's surviving lots, plus the two summaries every caller would otherwise recompute."""

    symbol: str
    qty: float                    #: signed net, and it MUST equal `open_lots_after`'s figure
    basis: float                  #: quantity-weighted mean of the surviving lots
    lots: tuple[OpenLot, ...]


def open_lots_detail(
    activities: list[dict],
    strategy_of: callable | None = None,
) -> dict[str, OpenBook]:
    """The open book with its BASIS and its OPENER intact — the input `unrealized_at(T, lane)` needs.

    `open_lots_after` answers "how much is still held" and discards the two fields per-lane P&L
    history is made of: what each lot cost, and who opened it. This is the same residue, read rather
    than recomputed.

    A SECOND MATCHER WAS NOT AN OPTION. `_match`'s own docstring says its two existing callers share
    one pass "deliberately — two walks over one set of fills would drift, and the reconciliation would
    then be checking the second walk rather than the number anyone reports". So this is a third VIEW,
    and `test_it_agrees_with_open_lots_after_on_NET_QTY_for_the_live_week` pins that it cannot drift
    from the figure production already reconciles against the broker's real positions.

    Flat symbols are ABSENT, not zero-qty rows: a zero would make every consumer decide whether it
    means flat or unknown, and that distinction is the whole point of the table this feeds (#734).
    """
    lots: dict[str, list[list]] = {}
    direction: dict[str, int] = {}
    _match(activities, lots, direction, strategy_of=strategy_of)

    out: dict[str, OpenBook] = {}
    for symbol, book in lots.items():
        held = sum(lot[0] for lot in book)
        if held <= 0:
            continue
        sign = direction.get(symbol, 1)
        surviving = tuple(
            OpenLot(symbol=symbol, qty=lot[0] * sign, basis=lot[1],
                    strategy_id=(lot[2] if len(lot) > 2 else None))
            for lot in book
        )
        out[symbol] = OpenBook(
            symbol=symbol,
            qty=held * sign,
            # Weighted by quantity, not a mean of prices: two lots of 10@100 and 5@120 are 106.67,
            # not 110. The unweighted form is wrong in the direction that looks plausible.
            basis=sum(lot[0] * lot[1] for lot in book) / held,
            lots=surviving,
        )
    return out


def open_lots_after(activities: list[dict]) -> dict[str, float]:
    """Net open quantity per symbol implied by these fills — signed, shorts negative.

    Exists to be RECONCILED against the broker's positions, which is how the `sell_short` defect was
    found: the matcher's leftovers held 23 PENG that the account did not. Realized P&L cannot be checked
    by inspection — every plausible implementation returns a plausible number — but its residue can be,
    and a phantom lot means realized is wrong by exactly that lot's basis.
    """
    lots: dict[str, list[list[float]]] = {}
    direction: dict[str, int] = {}
    _match(activities, lots, direction)
    return {
        sym: sum(l[0] for l in book) * direction.get(sym, 1)
        for sym, book in lots.items()
        if sum(l[0] for l in book) > 0
    }
