"""What each lane held on a PAST day, rebuilt from the broker's own fills (#734 backfill).

WHY THIS EXISTS. The engine can only observe today, so a forward-only table leaves 1W/1M/3M dark for
weeks after it ships — and #699's headline renders an em dash for exactly that long. The history is
recoverable: the broker's fill ledger reaches account inception, and this repo already owns a FIFO
lot matcher that is production-hardened for shorts, partial fills, flips through flat, and per-lot
opening tags. Truncate the fills at a past instant and the matcher's residue IS that day's book.

SO NOTHING HERE MATCHES ANYTHING. It truncates, calls `open_lots_detail`, and shapes the result into
the same objects the live observer produces. A second matcher would drift from the one whose
leftovers production reconciles against the broker's real positions — the check that found the PENG
phantom lot — and then the reconciliation would be checking the second walk rather than the number
anyone reports.

WHAT IT REFUSES. It never guesses an opener. The venue-order-cache join was believed to be "100% from 2026-08-17,
about 1% before" — MEASURED WRONG on 2026-08-31: it is 98.5% across the ENTIRE ledger and 100% on
every date back to inception (2026-07-13) except 08-12 and 08-13, and reconstructed UNCLAIMED
open-lot quantity is 0.0 on EVERY session. The durable order cache reaches inception. That figure
steered a decision about which windows were fillable, so it is corrected here rather than left; #292 is explicit that such money must not
be credited to whoever closed the position. Untagged lots become an UNCLAIMED row — first-class, not
dropped — which is also what keeps the per-lane cells summing to the account total.

AND IT DOES NOT INVENT THE ENGINE'S OPINION. `unrealized_engine` is Nautilus's own reading of a LIVE
position; there is none for a past day. Copying the derived figure into that column would manufacture
agreement on the very detector the table exists to provide — two derivations that are secretly one.
"""

from __future__ import annotations

import math
import re

from api.eod_observer import LaneObservation, Observation, PositionObservation
from api.realized_broker import open_lots_detail

#: The bucket for money whose opener is unknowable. Same name the realized sweep uses, deliberately:
#: two names for one concept is how a panel's cells stop adding up (#596).
UNCLAIMED = "UNCLAIMED"

#: A whole trading date. See `reconstruct_lanes` for why nothing finer is accepted.
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def reconstruct_lanes(
    activities: list[dict],
    *,
    as_of: str,
    marks: dict[str, float],
    strategy_of=None,
    instrument_of=None,
) -> Observation:
    """The per-lane book as of `as_of`, from fills at or before it.

    `as_of` IS A WHOLE TRADING DATE, `YYYY-MM-DD`, and that is enforced rather than documented.

    THE OBVIOUS GENERALISATION IS WRONG. An earlier version claimed "lexical order is chronological
    order" for ISO-8601 and accepted a full timestamp. It is false wherever fractional precision
    varies, because the 'Z' terminator (0x5A) sorts above every digit:

        "…13:35:53.9Z" <= "…13:35:53.95Z"   lexical False, chronological True
        "…13:35:00Z"   <= "…13:35:00.5Z"    lexical False, chronological True

    (`_match` sorts on the same key, so it shares that ordering — the two AGREE, which is not the
    same as either being right. That is pre-existing and out of scope here.)

    A malformed cutoff was worse than wrong, it was silently catastrophic: `as_of="2026-08-1"`
    selected everything through 08-19 via `startswith` AND everything before 08-10 via `<=`. So the
    date is validated, and a caller wanting sub-second truncation must be refused rather than served
    something plausible.

    `marks` is symbol -> that day's close, supplied by the caller from historical bars. A symbol the
    caller could not price is UNKNOWN, never zero: the holding is a fact and the valuation is not.

    `instrument_of` MAPS THE BROKER'S BARE SYMBOL INTO THE ENGINE'S NAMESPACE, and this is the one
    seam where that happens. The two sides genuinely differ: `str(position.instrument_id)` renders
    `AEM.XNYS` while the fill ledger carries `AEM`. Every `instrument_id` column in the live database
    — `trade_cycle`, `position_claim_event`, `manager`, `watchlist_item` — holds the qualified form,
    and #699's read path keys on it, so the reconstruction resolves UPWARD and the engine stays
    native. A symbol the resolver cannot place is SKIPPED and named, never stored bare: `AEM` in a
    column every other table fills with `AEM.XNYS` is a value that joins to nothing forever while
    looking perfectly fine in the row.

    DO NOT IMPLEMENT THE RESOLVER BY SPLITTING ON ".". The live paper database holds `BRK.B` and
    `BRKB` as symbols, so `instrument_id.split(".")[0]` yields `BRK` for `BRK.B.XNYS` — a different
    company. The inverse (`f"{symbol}.{venue}"`) is safe; the parse is not, and the parse is exactly
    what a hurried fix reaches for the morning this first matters. Nautilus's own `InstrumentId.symbol`
    is the correct downward direction if one is ever needed.

    MEASURED COVERAGE (paper, 2026-08-30): 59 distinct instrument ids in `trade_cycle`, 60 distinct
    symbols in `exec_action_log`, and 15 symbols with no id — ALL FIFTEEN with zero positions and zero
    decisions, i.e. pool candidates that never became fills. Every symbol that has ever held a
    position resolves, so the skip path is currently theoretical rather than routine. Re-measure
    before trusting that on another instance.

    RESOLUTION IS TIME-DEPENDENT AND THIS ONE IS NOT. A ticker can be recycled, so resolving a fill
    from 2026-07 with TODAY's mapping can attach it to a different instrument entirely. Negligible
    for this account — its whole history is six weeks — and stated here so nobody generalises the
    resolver over a longer ledger without knowing that it needs `as_of` to be correct there.
    """
    cutoff = str(as_of)
    if not _DATE.fullmatch(cutoff):
        raise ValueError(
            f"as_of must be a whole trading date, YYYY-MM-DD, not {cutoff!r}. A partial date silently "
            f"selects the wrong fills — '2026-08-1' takes everything through 08-19 via the prefix "
            f"match AND everything before 08-10 via the comparison — and a sub-second cutoff cannot "
            f"be honoured, because the string ordering these timestamps are compared under is not "
            f"chronological once fractional precision varies."
        )
    # ONE COMPARISON, ON DATES, which is the actual rule: a fill belongs to this reconstruction if
    # the DAY it happened is on or before the cutoff day.
    #
    # The earlier form was `t <= cutoff or t.startswith(cutoff)`, and its two halves were
    # indistinguishable under mutation — `<` passes every test `<=` does, because a same-day fill is
    # caught by the prefix either way and an earlier one is strictly less. Two clauses where one
    # decides is how a boundary ends up pinned by nothing. Slicing the date off makes the rule the
    # comparison, so `<` genuinely changes the answer and a test can say so.
    in_scope = [f for f in activities if str(f.get("transaction_time") or "")[:10] <= cutoff]

    book = open_lots_detail(in_scope, strategy_of=strategy_of)

    # (lane, symbol) -> [qty, qty*px] so the basis is quantity-weighted rather than a mean of prices.
    grouped: dict[tuple[str, str], list[float]] = {}
    for symbol, open_book in book.items():
        for lot in open_book.lots:
            # An untagged lot is UNCLAIMED. Not dropped — dropping it would make the lane cells stop
            # summing to the account, which is #596 — and not guessed at, which is #292.
            lane = lot.strategy_id or UNCLAIMED
            slot = grouped.setdefault((lane, symbol), [0.0, 0.0])
            slot[0] += lot.qty
            slot[1] += lot.qty * lot.basis

    by_lane: dict[str, list[PositionObservation]] = {}
    skipped: list[str] = []
    for (lane, symbol), (qty, notional) in grouped.items():
        # NOT a `qty == 0` skip. `open_lots_detail`'s books are direction-homogeneous, so per-lane
        # lot sums cannot cancel — the guard described a state the matcher cannot produce, and it was
        # a float `==` besides. Asserted instead, so if that invariant ever changes this says so
        # rather than silently dropping a row.
        assert qty != 0, f"direction-homogeneous books cannot cancel: {lane}/{symbol}"
        # RESOLVE OR REFUSE. Not `instrument_of(symbol) or symbol` — that fallback would silently
        # write the bare symbol on exactly the rows where the mapping failed, which is the one case
        # it must not. Without a resolver at all the caller is stating the two namespaces are already
        # one, which is true only in tests.
        instrument_id = symbol if instrument_of is None else instrument_of(symbol)
        if not instrument_id:
            skipped.append(f"{lane}/{symbol}: could not be resolved to an instrument id")
            continue
        mark = marks.get(symbol)
        if mark is not None and not math.isfinite(float(mark)):
            # NaN survives every comparison written for numbers — the family that disarmed a
            # daily-loss halt in kumo-trading-strategies and reached this repo again in #588.
            mark = None
        basis = notional / qty
        by_lane.setdefault(lane, []).append(
            PositionObservation(
                strategy_id=lane,
                instrument_id=str(instrument_id),
                qty=qty,
                avg_px_engine=basis,
                mark_px=None if mark is None else float(mark),
                unrealized_derived=None if mark is None else qty * (float(mark) - basis),
                # NO ENGINE READING FOR A PAST DAY. See the module docstring: filling this in from the
                # derived figure would make the two columns agree by construction and kill the
                # detector.
                unrealized_engine=None,
            )
        )

    lanes = []
    for lane, positions in by_lane.items():
        unpriced = sum(1 for p in positions if p.unrealized_derived is None)
        lanes.append(
            LaneObservation(
                strategy_id=lane,
                positions=tuple(positions),
                # One unpriced leg makes the LANE's total unknown, not partial — the same rule
                # `observe_lanes` and `_standing_unrealized_total` both follow.
                unrealized_total=None if unpriced else sum(p.unrealized_derived or 0.0 for p in positions),
                unpriced=unpriced,
                # MEASURED FROM THE TAGS, not declared. A tagged row's opener is known, so 1.0 is a
                # fact about that row; the UNCLAIMED row's opener is unknown, so it is 0.0. Review's
                # F11: a constant standing in for a measurement is the QC27-allocated-equity shape.
                attribution_coverage=0.0 if lane == UNCLAIMED else 1.0,
            )
        )
    return Observation(lanes=tuple(lanes), skipped=tuple(skipped))
