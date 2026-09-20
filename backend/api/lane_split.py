"""Who holds the shares the broker says we hold? (#748, the per-lane source.)

WHY THIS EXISTS. Protective stops are stamped with the strategy that BUILT them, and under NETTING a
fill's position is derived as `PositionId(f"{instrument}-{fill.strategy_id}")`. A stop built by the
display strategy resolves to a position that has never existed, so its reduce-only fill is applied to
NO position and the position poll fabricates the difference — the phantom mint. Fixing it means
resting one stop per HOLDING LANE, and that needs a per-lane view of the book. There is not one.

NEITHER PLANE CAN DO THIS ALONE, WHICH IS THE WHOLE DESIGN.

  - The BROKER knows the truth and not the split. `broker_rows` sets `strategy_id: ""` and says why:
    the broker does not know about our sleeves.
  - The CACHE knows the split and is the thing that has been wrong. The protection sweep plans from
    broker state deliberately (#285): on 2026-08-14 the engine held three orders as REJECTED that
    Alpaca reported `new`, and a cache-based coverage check answers "nothing resting", so the next
    pass places a DUPLICATE stop.

So the broker anchors the TOTAL and the cache supplies the SPLIT — which is this repo's stated
architecture rather than a new idea: broker net is the only hard reconciliation anchor, and the
per-strategy split is unverified by the broker.

AND WHERE THEY DISAGREE, NOTHING IS SPLIT. A cache net that differs from the broker's means the
per-lane numbers do not describe the shares the venue is holding, and splitting on them would
attribute real shares to a lane that does not hold them — the defect this work exists to end, arriving
through its own fix. The instrument falls back to ONE aggregate row: exactly today's behaviour, plus
a named divergence.

THAT FALLBACK IS UNCOMFORTABLE AND IT IS DELIBERATE. The per-lane split is corrupted by precisely the
phantoms this is meant to stop, so a symbol carrying a phantom leg keeps the old aggregate behaviour
until #744 repairs it. Correct ordering, not a workaround — and it is a LOUD degrade: every fallback
is reported as its own condition, never as the healthy one, because a silent fallback here would make
"we could not attribute this" indistinguishable from "one lane holds it all".

AND SAY PLAINLY WHAT THE FALLBACK IS, because "it keeps today's behaviour" is a comfortable way to
describe something uncomfortable. Today's behaviour IS the #748 defect: an aggregate row carries
`strategy_id: ""`, so its stop is stamped by whichever strategy submits it, and that fill resolves to
`{instrument}-MANUAL-001` — a position that may not exist on precisely the instruments messy enough
to have diverged. The fallback therefore preserves the mint, scoped to the worst names. It is still
the right choice, because the alternative is attributing real shares to a lane that does not hold
them; but it is a REFUSAL TO MAKE THINGS WORSE, not a safe harbour, and whoever wires this must
decide what an aggregate stop resolves to rather than inheriting that sentence as reassurance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Quantities come from one source per plane, so a mismatch is a real disagreement, not float noise.
#: Matches `contra_close`'s tolerance deliberately: two thresholds for one question drift apart.
QTY_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Divergence:
    """An instrument whose per-lane split could not be trusted, and why. Reported, never swallowed."""

    instrument_id: str
    #: `None` MEANS THE PLANE COULD NOT SAY, and that is not zero. A broker read that does not
    #: mention an instrument has not reported it flat, and a cache net that is unreadable is not a
    #: flat leg. 0.0 was defensible while this was only a report, but the moment a consumer does
    #: arithmetic on it — a /health sum of |broker - cache| gaps, a `broker_net == 0` filter — the
    #: two states collapse, in the module whose whole purpose is keeping them apart.
    broker_net: float | None
    cache_net: float | None
    reason: str


@dataclass(frozen=True)
class LaneSplit:
    rows: tuple[dict, ...] = field(default_factory=tuple)
    divergent: tuple[Divergence, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        if not self.rows:
            return "no positions"
        split = sum(1 for r in self.rows if r["strategy_id"])
        return (
            f"{split} lane row(s) across {len({r['instrument_id'] for r in self.rows})} instrument(s)"
            + (f"; {len(self.divergent)} instrument(s) NOT split — "
               f"{', '.join(d.instrument_id for d in self.divergent)}" if self.divergent else "")
        )


def _signed(row: dict) -> float:
    qty = abs(float(row.get("quantity") or 0))
    return -qty if str(row.get("side") or "").upper() == "SHORT" else qty


def split_by_lane(broker_rows: list[dict], cache_positions) -> LaneSplit:
    """Broker rows -> one row per HOLDING LANE, where the cache can be trusted to say who holds what.

    `broker_rows` is what `broker_rows()`/`position_rows_from_reports()` already produce.
    `cache_positions` is the engine's own `cache.positions_open()`.

    A row's `market_value` is APPORTIONED across the lanes, not copied: notional drives refusal
    reporting and exposure totals, so copying it to each lane would report the same exposure twice.
    """
    legs: dict[str, list] = {}
    mute: set[str] = set()
    for p in cache_positions or ():
        iid = str(p.instrument_id)
        # NOT `getattr(p, "is_open", True)`. A default of True counts a CLOSED position as a holder,
        # and the durable cache retains closed positions under the same id shape — which would make
        # the cache net disagree with the broker on nearly every symbol and silently send the whole
        # book down the aggregate fallback, disabling the split while looking like it worked.
        # A default of False is the mirror error: it silently drops a leg this read cannot describe,
        # so an unreadable book would split as if the missing leg held nothing.
        if not hasattr(p, "is_open"):
            mute.add(iid)
            continue
        if not p.is_open:
            continue
        legs.setdefault(iid, []).append(p)

    rows: list[dict] = []
    divergent: list[Divergence] = []

    for row in broker_rows or ():
        iid = str(row.get("instrument_id"))
        broker_net = _signed(row)
        mine = legs.get(iid, [])
        cache_net = sum(float(p.signed_qty) for p in mine)

        def fall_back(reason: str) -> None:
            # THE AGGREGATE ROW IS THE INPUT ROW, UNCHANGED. Reconstructing it would be a second
            # derivation of the same fact, and this file's whole subject is what happens when two
            # derivations of one thing drift.
            rows.append(dict(row))
            divergent.append(Divergence(iid, broker_net, cache_net, reason))

        if iid in mute:
            fall_back("a position on this instrument cannot say whether it is open, so the split "
                      "for it is unknown rather than absent")
            continue
        if not mine:
            # Absence is a timestamp, not a property: a seeding frame, a stale read and a genuinely
            # unattributed holding are indistinguishable from here, and emitting the aggregate row
            # silently would make a blind read look like a healthy one.
            fall_back("the cache knows no open position on this instrument, so who holds it is "
                      "unknown — not nobody")
            continue
        # NaN IS NOT A DISAGREEMENT, IT IS AN UNREADABLE NUMBER — and it must be caught BEFORE the
        # comparison, because `abs(nan - x) > tol` is False, so the net check PASSES and the
        # instrument SPLITS, emitting a row with `quantity=nan` that the per-row zero-skip is equally
        # blind to. Every comparison written for numbers is False against NaN; this is the family
        # that silently disarmed a daily-loss halt, and it was fixed in `contra_close` on the same
        # day this module was written without being fixed here.
        if broker_net != broker_net or cache_net != cache_net:
            fall_back("a quantity on this instrument is not a number, so neither the net comparison "
                      "nor the per-lane sizing means anything for it")
            continue
        if abs(cache_net - broker_net) > QTY_TOLERANCE:
            fall_back(f"the per-lane book nets {cache_net:g} against the broker's {broker_net:g}, so "
                      f"the split does not describe the shares the venue is holding")
            continue

        # The price the broker's own row implies, so the apportioned notionals sum back to it exactly
        # rather than to a separately-sourced mark.
        px = (float(row.get("market_value") or 0.0) / broker_net) if broker_net else 0.0
        for pos in mine:
            qty = float(pos.signed_qty)
            if abs(qty) <= QTY_TOLERANCE:
                # An open position at zero is not a holder. A stop for zero shares is not a stop.
                continue
            rows.append({
                **row,
                "strategy_id": str(pos.strategy_id),
                "quantity": abs(qty),
                "side": "SHORT" if qty < 0 else "LONG",
                "market_value": qty * px,
            })

    # INSTRUMENTS THE CACHE HOLDS AND THE BROKER DOES NOT MENTION. No rows — the venue holds nothing
    # to protect, so there is nothing to size a stop against — but this is the STARKEST cache/broker
    # disagreement there is, and leaving the divergence channel empty for it is absence reading as
    # agreement in the one module built to keep those apart. A broker-flat mirrored pair (WHD +28
    # against -28) is the core #744 shape and produced silence.
    seen = {str(r.get("instrument_id")) for r in (broker_rows or ())}
    for iid in sorted(set(legs) | mute):
        if iid in seen:
            continue
        cache_net = sum(float(p.signed_qty) for p in legs.get(iid, []))
        divergent.append(Divergence(
            iid,
            # UNKNOWN, not zero: the broker read did not mention this instrument at all.
            None,
            # And an unreadable leg is unknown too, rather than rewritten to flat inside the report.
            None if cache_net != cache_net else cache_net,
            "the cache holds an open position on this instrument and the broker read does not "
            "mention it at all — the venue holds nothing to protect here, so no stop is sized, but "
            "the two planes disagree and that is this channel's subject",
        ))

    return LaneSplit(rows=tuple(rows), divergent=tuple(divergent))
