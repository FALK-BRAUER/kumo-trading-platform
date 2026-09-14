"""What the book looks like RIGHT NOW, per lane — the "now" end of #734.

THE GAP THIS FILLS. `_standing_unrealized_total` (engine_node.py:613) sums unrealized across the
ACCOUNT: it asks `portfolio.unrealized_pnl(instrument_id)`, which is per INSTRUMENT and therefore
cannot see that two lanes hold one symbol. Per-lane P&L history needs that split, and Nautilus
already carries it — every `Position` is tagged with the `strategy_id` that opened it, so the
grouping is NATIVE rather than a second attribution scheme layered on top.

WHAT IT DELIBERATELY DOES NOT DO. It computes no window, stores nothing, and reads no database. It
reports an observation; deciding what to persist, under which `capture_kind`, and what the manifest
should say is the caller's job. Keeping those apart is what lets this be tested against a cache
double with no Postgres anywhere near it.

THREE OBSERVATIONS, NOT ONE ANSWER (#734). Each position carries the mark, the basis, the unrealized
DERIVED here, and Nautilus's own figure for the same position. They should agree. The reason to keep
both is that the day they do not is the day something is wrong — #370's WHD divergence (+$263.84
against the broker's +$9.52) was found forensically because nothing was recording the pair.

UNKNOWN NEVER BECOMES ZERO. An unpriced position keeps its quantity — that is a fact — and reports a
None mark and a None unrealized. Its LANE then has an unknown total rather than a partial one, which
is the same choice `_standing_unrealized_total` makes at account level for the same reason: summing
the priced remainder reports part of the book as all of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Nautilus's own enum, imported lazily so this module can be unit-tested without the engine's
#: Cython extensions loaded. The value is what `Cache.price` expects.
_PRICE_TYPE = None


def _last_price_type():
    global _PRICE_TYPE
    if _PRICE_TYPE is None:
        from nautilus_trader.model.enums import PriceType

        _PRICE_TYPE = PriceType.LAST
    return _PRICE_TYPE


def _as_float(value) -> float | None:
    """A Nautilus `Price`/`Money` answers `as_double()`; anything unreadable or non-finite is UNKNOWN.

    NaN is the specific hazard: it survives every comparison written for numbers, which is the family
    that disarmed a daily-loss halt in kumo-strategies and reached this repo again in #588. A NaN mark
    must become None here rather than a NaN that propagates into a stored row and then into a total.
    """
    if value is None:
        return None
    try:
        out = float(value.as_double() if hasattr(value, "as_double") else value)
    except Exception:                                                   # noqa: BLE001
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class PositionObservation:
    """One lane's holding in one instrument, at one instant."""

    strategy_id: str
    instrument_id: str
    qty: float                          #: signed — shorts negative, straight off `Position.signed_qty`
    avg_px_engine: float | None         #: the engine's basis; the venue's is added by the caller
    mark_px: float | None               #: None = no mark, which is not a mark of zero
    unrealized_derived: float | None    #: qty x (mark − basis), computed HERE
    unrealized_engine: float | None     #: Nautilus's own figure for the same position


@dataclass(frozen=True)
class Observation:
    """Every lane, plus what could NOT be described.

    `skipped` is why this is not just a list. A position with no readable quantity or no attributable
    lane is a fact about the capture, and one that vanished silently would be invisible in exactly the
    table built to make gaps visible.
    """

    lanes: tuple
    skipped: tuple

    def __iter__(self):
        """Iterating an Observation yields its LANES — every existing caller reads it that way."""
        return iter(self.lanes)

    def __len__(self):
        return len(self.lanes)

    def __getitem__(self, i):
        return self.lanes[i]


@dataclass(frozen=True)
class LaneObservation:
    """Every position one lane holds, plus what the lane can honestly say about its total."""

    strategy_id: str
    positions: tuple[PositionObservation, ...]
    #: None when ANY leg is unpriced. A lane with an unknown leg has an unknown total, never a
    #: partial one dressed as a complete answer.
    unrealized_total: float | None
    #: How many legs had no usable mark. Zero is a fact; it is not the same as "we did not look".
    unpriced: int
    #: The fraction of this lane's qty whose OPENER is known. REQUIRED, with no default: 1.0 is a
    #: fact for an engine-side observation (Nautilus tags every Position with the strategy that
    #: opened it) and a MEASUREMENT for a reconstruction, where the fill-to-order join is 100% only
    #: from 2026-08-17. A default would let the second case silently inherit the first's certainty —
    #: which is the constant-standing-in-for-a-measurement shape (#734 review, F11).
    attribution_coverage: float


def observe_lanes(cache) -> Observation:
    """Group the open book by the strategy that holds it, and price what can be priced.

    RAISES if the book cannot be read. That is deliberate and it is the opposite of what
    `_standing_unrealized_total` does — it returns None because it is feeding a display that must
    keep rendering. This feeds a WRITER, and a writer that turned an unreadable cache into an empty
    observation would make a failed capture indistinguishable from a genuinely flat account. Absence
    readable as flatness is this repo's most-repeated defect class; the caller catches this and
    records `failed` in the manifest.
    """
    positions = cache.positions_open()            # deliberately unguarded — see the docstring

    by_lane: dict[str, list[PositionObservation]] = {}
    #: Positions this function could not describe. Returned rather than logged: the caller writes the
    #: manifest, and a skipped position that only appears in a log is a hole nobody can audit later.
    skipped: list[str] = []
    for position in positions or ():
        lane = str(getattr(position, "strategy_id", "") or "")
        instrument_id = getattr(position, "instrument_id", None)
        if not lane or instrument_id is None:
            # Neither is inferable, and guessing which lane owns a position is the attribution leak
            # #292 exists to close. COUNTED, not merely skipped: the comment here used to promise the
            # caller would count these, and the return type carried no way to — a mechanism promised
            # in prose and absent from the code.
            skipped.append(f"unattributable position: lane={lane!r} instrument={instrument_id!r}")
            continue

        # ONE READ, and the SAME object passed to both derivations. Reading the price twice — once to
        # float it for `mark_px`, once to hand Nautilus a Price — lets a tick land between them, and
        # the row then records a mark that is not the price the engine figure was computed at. That
        # manufactures a disagreement on the exact derived-vs-engine detector this table exists to
        # provide, at the close capture where the close row is the one that ALARMS. Demonstrated in
        # review with a ticking cache: mark 110.0, derived 90.0, engine 99.0, no defect present.
        try:
            price = cache.price(instrument_id, _last_price_type())
        except Exception:                                               # noqa: BLE001
            price = None
        mark = _as_float(price)

        # GUARDED, because `getattr(obj, name, default)` does NOT swallow an exception raised by a
        # property — it only covers a missing attribute. An unreadable quantity on ONE position would
        # otherwise propagate and fail the entire capture, turning a single bad leg into a whole
        # missing day. Skipping and counting it is right; failing everything is not.
        try:
            qty = _as_float(position.signed_qty)
        except Exception:                                               # noqa: BLE001
            qty = None
        try:
            basis = _as_float(position.avg_px_open)
        except Exception:                                               # noqa: BLE001
            basis = None

        derived = None
        if qty is not None and basis is not None and mark is not None:
            derived = qty * (mark - basis)

        # NAUTILUS'S OWN ANSWER TO THE SAME QUESTION, asked separately so the two can disagree. It
        # needs a Price object, not the float above — passing the float raises, which is why the test
        # double refuses one.
        engine_pnl = None
        if mark is not None and price is not None:
            try:
                engine_pnl = _as_float(position.unrealized_pnl(price))
            except Exception:                                           # noqa: BLE001
                engine_pnl = None

        # AN UNREADABLE QUANTITY IS NOT A QUANTITY OF ZERO. This line used to coerce it, directly
        # under a docstring headed "UNKNOWN NEVER BECOMES ZERO" — and a mutation proved the wire
        # untested: replacing the 0.0 with 12345.0 left the entire suite green. A stored qty of 0
        # reads as "this lane held nothing", which is the one thing the manifest exists to
        # distinguish from "we could not tell".
        if qty is None:
            skipped.append(f"{lane}/{instrument_id}: quantity unreadable")
            continue

        by_lane.setdefault(lane, []).append(
            PositionObservation(
                strategy_id=lane,
                instrument_id=str(instrument_id),
                qty=qty,
                avg_px_engine=basis,
                mark_px=mark,
                unrealized_derived=derived,
                unrealized_engine=engine_pnl,
            )
        )

    out: list[LaneObservation] = []
    for lane, observations in by_lane.items():
        unpriced = sum(1 for o in observations if o.unrealized_derived is None)
        out.append(
            LaneObservation(
                strategy_id=lane,
                positions=tuple(observations),
                # One unpriced leg makes the LANE's total unknown, exactly as one unpriced position
                # makes the account's total unknown in `_standing_unrealized_total`.
                unrealized_total=(
                    None if unpriced else sum(o.unrealized_derived or 0.0 for o in observations)
                ),
                unpriced=unpriced,
                # A FACT here, not a default: every Nautilus Position carries the strategy that
                # opened it, so an engine-side observation is fully attributed by construction.
                attribution_coverage=1.0,
            )
        )
    return Observation(lanes=tuple(out), skipped=tuple(skipped))
