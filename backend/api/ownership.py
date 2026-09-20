"""Per-strategy ownership invariants (#437).

The broker has ONE net position per symbol and no opinion about whose it is. CLAUDE.md states it
plainly: *broker net is the only hard reconciliation anchor; the per-strategy split is unverified by
the broker.* So the split cannot be audited at read time — `_report_reconcile_drift` sums signed_qty
per SYMBOL, and for WHD on 2026-08-21 that was +28 - 28 == 0 against a broker holding 0. It reported
no drift and it was RIGHT. The account was flat. The split underneath it was wrong.

What is left is invariants that need no anchor because they are wrong on their face. This module holds
the cheapest one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: Strategies allowed to carry a short. ONE, and that is the contract, not an oversight.
#:
#: CRSISHORT-006 is the short book (issue 123, platform issue 858) and the only lane that opens
#: a SELL against a flat book. Every other lane is long: MANUAL is discretionary long equity,
#: MOMENTUM/BCTROT/QC345 are long rotations, TECHIVOL is a long inverse-vol sleeve. A negative
#: quantity in any of THEM is a defect by construction — most likely one strategy selling shares
#: another owns, which is what produced WHD. Per the standing rule that new gates default to False,
#: adding a name here is a deliberate, reviewed edit (this one: #858), never a default that grows.
SHORT_PERMITTED: frozenset[str] = frozenset({"CRSISHORT-006"})

#: The two sides a lane can hold. Strings on purpose: `kumo_strategies.runtime.nautilus.sides` spells
#: its own (`SHORT == "short"`), and a cockpit reader comparing the two vocabularies by string would
#: agree by accident on one side and disagree on the other. Compare BOOLEANS across that seam
#: (`test_the_two_side_declarations_agree`), never these values.
LONG = "LONG"
SHORT = "SHORT"


def lane_side(strategy_id) -> str:
    """The side a lane is DECLARED to hold — the one fact every ownership question keys on (#1030).

    READ AT CALL TIME from `SHORT_PERMITTED`, not bound at import: a module-level capture would
    freeze whichever set existed then, so a later change to the permission (a test's monkeypatch
    today, a settings-driven set tomorrow) would be silently not used while reporting green — the
    same reason the budget gate imports its rule per call.

    `strategy_id` may be a Nautilus `StrategyId` or a str; the set holds strings, and an object
    compared against it would read LONG for the short lane and refuse its every entry — the exact
    defect this function exists to end, one type away.

    UNKNOWN IS LONG. A lane not in the set gets the long-only rule, which REFUSES shorts — the
    fail-safe direction: a new short lane that nobody added here cannot open a position until
    someone does, and says so at its first order rather than at its first fill.
    """
    return SHORT if str(strategy_id) in SHORT_PERMITTED else LONG


@dataclass(frozen=True)
class ShortViolation:
    strategy_id: str
    instrument_id: str
    signed_qty: float

    def __str__(self) -> str:
        return f"{self.strategy_id} holds {self.signed_qty:+g} {self.instrument_id}"


def display_signed_qty(side, quantity) -> float:
    """The DISPLAY reading of a quantity: magnitude first, then the side (#855).

    Two questions, two predicates — and they must answer `LONG / -28` DIFFERENTLY:
      * `signed_qty_of` (below) applies the side to the RAW value. It is the WHD detector's reading:
        a LONG carrying a negative quantity is a VIOLATION, and abs-first would silence exactly what
        `short_violations` exists to see.
      * this one treats `side` as the authority and the number as a size. It is what a mark, a
        market value or a P&L percentage needs, and it RETURNS on every input — it runs inside
        `_publish_trades`'s try, where a raise blanks the whole book (the NameError that showed an
        empty book while eight positions were held).
    FLAT is 0. An unreadable side reads as LONG with the magnitude — the conservative display, and
    the detector beside it is what reports the shape.
    """
    try:
        mag = abs(float(quantity or 0.0))
    except (TypeError, ValueError):
        return 0.0
    s = str(side or "").upper()
    if s == "FLAT" or mag == 0.0:
        return 0.0
    return -mag if s == "SHORT" else mag


def signed_qty_of(position) -> float:
    """Signed quantity from either shape this book is served in.

    Nautilus's `Position` carries `signed_qty` directly. The `PositionDTO` that `/positions` serves
    carries an UNSIGNED `quantity` with the direction in a separate `side` field — which is precisely
    why WHD was invisible: the two legs read 28 and 28, both positive, and only `side` differed.

    RAISES on a shape it cannot read. Returning 0.0 would report "no shorts" for a book it failed to
    understand — a silent all-clear, the exact failure mode this ticket exists to remove. A renamed
    DTO field must break this loudly rather than quietly disarm it.
    """
    # BOTH ACCESS SHAPES, because both are production. Nautilus's `Position` is an object with
    # attributes; `/trades` and `/positions` serve plain dicts over JSON. An attribute-only reader
    # raises on every row the API returns, which would have made this check dead on arrival at the one
    # seam it is wired into.
    def field(name):
        if isinstance(position, Mapping):
            return position.get(name)
        return getattr(position, name, None)

    signed = field("signed_qty")
    if signed is not None:
        return float(signed)
    side = field("side")
    qty = field("quantity")
    if side is not None and qty is not None:
        s = str(side).upper()
        if s == "LONG":
            return float(qty)
        if s == "SHORT":
            return -float(qty)
        if s == "FLAT":
            return 0.0
    raise ValueError(
        f"cannot determine signed quantity from {type(position).__name__}: "
        f"no signed_qty, and side={side!r} quantity={qty!r}"
    )


def short_violations(positions, *, permitted: frozenset[str] | None = None) -> list[ShortViolation]:
    """Every strategy holding a negative quantity it is not permitted to hold.

    Reports ONLY the negative leg. Flagging both sides of a netted pair would name BCTROT — whose +28
    was a legitimate holding — alongside MOMENTUM, and an alarm that indicts the victim is one an
    operator learns to skip.
    """
    allowed = SHORT_PERMITTED if permitted is None else permitted
    out = []
    for p in positions or ():
        q = signed_qty_of(p)
        sid = p.get("strategy_id") if isinstance(p, Mapping) else getattr(p, "strategy_id", "")
        iid = p.get("instrument_id") if isinstance(p, Mapping) else getattr(p, "instrument_id", "")
        if q < 0 and str(sid) not in allowed:
            out.append(ShortViolation(str(sid), str(iid), q))
    return out
