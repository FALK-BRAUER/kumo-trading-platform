"""Which lane owns a protective stop? (#748, the stamping half.)

THE MINT. Under NETTING the execution engine derives a fill's position as
`PositionId(f"{instrument}-{fill.strategy_id}")`, from the ORDER's strategy_id. Protective stops are
built by the display strategy's own `order_factory`, so they carry MANUAL-001 — and on an instrument
MANUAL-001 does not hold, the fill resolves to a position that has never existed. Reduce-only then
applies it to NO position and the position poll fabricates the difference as a synthetic sell at a
price that never traded. Paper's 8 mirror pairs and 262 fabricated shares are that, repeated.

THIS IS THE SMALL CUT, DELIBERATELY. #748's per-lane split changes what is PLANNED — one stop per
lane rather than one per leg — and cannot be activated alone: instrument-level oversize would shrink
the wrong lane's correctly-sized stop, and pro-rata coverage would tell a fully covered lane it is
naked and rest a duplicate that flips it short on trigger. Both fixes are built; neither is wired.

What changes here is only the STAMP. Where exactly one lane holds the instrument, the aggregate stop's
quantity ALREADY equals that lane's quantity, so naming that lane is not an attribution guess — it is
the only answer consistent with the size being placed. No coverage or oversize arithmetic moves.

AND WHERE IT IS AMBIGUOUS, NO STOP IS PLACED AT ALL — changed 2026-09-01, and the previous sentence
here said the opposite for long enough to be worth recording.

It used to read: "those instruments keep today's behaviour and wait for the full split", meaning the
stop was submitted UNSTAMPED. That was wrong twice over. An unstamped stop is not weaker protection:
it carries the SUBMITTING strategy, MANUAL-001, so under NETTING its fill resolves to a position that
never existed, the ExecEngine rejects it, and the shares leave the broker while the cache never
records the sale. And because coverage is judged per INSTRUMENT (`protection.py`), that wrong stop
then permanently blocks a correct one from ever being placed.

Measured on paper 2026-08-31: three lanes refusing every entry on "0 of budget left" while holding
positions the broker had already sold, and GMAB reading 59 in the cache against 0 at the venue.

THE COST OF THE CHANGE IS REAL AND IS NOT A BOOT WINDOW. A multi-holder instrument (HALO: BCTROT-004,
EXTERNAL, MOMENTUM-002), an EXTERNAL-held one, or a SHORT gets NO venue-side stop for as long as it
stays ambiguous — not for one tick. The reconciler retries every 60s, so a TRANSIENT ambiguity costs
one tick; a standing one costs protection until the per-lane split (#748) lands. That trade was taken
deliberately, because the alternative places an order that corrupts the book when it fires.

The refusal is not silent: `stamp_diagnosis` names the holders and the reason on every occurrence,
and it is recorded as standing state rather than only logged.
"""

from __future__ import annotations

#: The bucket for money whose owner is unknown. Not a lane, and never a holder — counting it as one
#: would make every phantom-carrying instrument ambiguous, and those are the ones already damaged.
_UNATTRIBUTED = "EXTERNAL"

#: Quantities come from one source, so a mismatch is a real difference rather than float noise.
_QTY_EPS = 1e-9


def stamp_for(instrument_id: str, positions) -> str | None:
    """The lane to stamp a protective stop for `instrument_id` with, or None to leave it as today.

    `positions` is the engine's own `cache.positions_open()`.

    Returns None on anything less than certainty: no holder, several holders, a holder this read
    cannot describe, an unreadable quantity, or a SHORT. A short's protection reduces by BUYING and
    this cut does not reason about side, so leaving it unstamped keeps today's behaviour rather than
    aiming a BUY at a position by guess.
    """
    holders: dict[str, float] = {}
    for pos in positions or ():
        if str(getattr(pos, "instrument_id", "")) != str(instrument_id):
            continue
        # NOT `getattr(pos, "is_open", True)`. The durable cache retains CLOSED positions, and a
        # default of True would count them as holders — making almost the whole book ambiguous and
        # leaving the mint in place while looking like it worked. A shape that cannot say is not a
        # confirmation either, so it makes the instrument ambiguous rather than being skipped.
        if not hasattr(pos, "is_open"):
            return None
        if not pos.is_open:
            continue
        lane = str(getattr(pos, "strategy_id", "") or "")
        if not lane or lane == _UNATTRIBUTED:
            continue
        try:
            qty = float(pos.signed_qty)
        except (TypeError, ValueError):
            return None
        # NaN IS DEFENCE IN DEPTH HERE, NOT A LOAD-BEARING GATE, and saying so is the point: a
        # mutation deleting this line survives, because every path a NaN can take ends at the
        # `qty > 0` test below, which is False for NaN. The outcome is None either way. It stays
        # because `abs(nan) <= eps` is also False, so without it a NaN would be COUNTED as a holder
        # and the ambiguity check would read a book it cannot describe — and because the next reader
        # should not have to re-derive that a surviving mutant here is harmless.
        if qty != qty:
            return None
        if abs(qty) <= _QTY_EPS:
            continue
        holders[lane] = holders.get(lane, 0.0) + qty

    if len(holders) != 1:
        return None
    lane, qty = next(iter(holders.items()))
    # LONG ONLY. See the docstring: a short reduces by BUYING and this cut does not reason about side.
    return lane if qty > 0 else None


def stamp_diagnosis(instrument_id: str, positions) -> dict:
    """WHY `stamp_for` answered as it did — the holder set and the refusal reason.

    Measured on paper 2026-08-31: 17 of 23 live protective orders carried MANUAL-001 on instruments
    MANUAL-001 holds nothing of, and correct and incorrect stamps were produced IN THE SAME SECOND.
    Some refusals were explicable (HALO has three holders, RGEN two), and at least one was not (CRM
    has exactly one holder, TECHIVOL-005, and was still stamped MANUAL-001).

    I could not tell those apart from the outside, because `stamp_for` returns None for seven distinct
    reasons and the caller sees one value. This returns the reason and the evidence, so the next
    occurrence explains itself instead of needing another evening.

    PURE, and it re-walks the positions rather than being folded into `stamp_for`: a diagnosis that
    shares state with the decision it describes can drift from it silently, and a decision function
    that also formats messages is one nobody wants to change later.
    """
    holders: dict[str, float] = {}
    flat: list[str] = []
    unreadable = False
    external = 0
    seen = 0

    for pos in positions or ():
        if str(getattr(pos, "instrument_id", "")) != str(instrument_id):
            continue
        seen += 1
        if not hasattr(pos, "is_open"):
            unreadable = True
            continue
        if not pos.is_open:
            continue
        lane = str(getattr(pos, "strategy_id", "") or "")
        if not lane or lane == _UNATTRIBUTED:
            external += 1
            continue
        try:
            qty = float(pos.signed_qty)
        except (TypeError, ValueError):
            unreadable = True
            continue
        if qty != qty:
            unreadable = True
            continue
        if abs(qty) <= _QTY_EPS:
            flat.append(lane)
            continue
        holders[lane] = holders.get(lane, 0.0) + qty

    owner = stamp_for(instrument_id, positions)
    if owner:
        reason = "stamped"
    elif unreadable:
        reason = "a position on this instrument could not be read (no is_open, or a non-numeric qty)"
    elif not seen:
        reason = "no position on this instrument is in the cache at all"
    elif not holders:
        reason = (f"no lane holds an open non-flat position: {external} EXTERNAL/unattributed, "
                  f"{len(flat)} flat ({', '.join(sorted(flat)) or 'none'})")
    elif len(holders) > 1:
        reason = ("several lanes hold it, and the broker cannot adjudicate a split — "
                  f"{', '.join(f'{k}={v:+.0f}' for k, v in sorted(holders.items()))}")
    else:
        lane, qty = next(iter(holders.items()))
        reason = f"the sole holder {lane} is SHORT ({qty:+.0f}); this cut does not reason about side"

    return {
        "instrument_id": str(instrument_id),
        "owner": owner,
        "reason": reason,
        "holders": {k: round(v, 4) for k, v in sorted(holders.items())},
        "flat_lanes": sorted(flat),
        "external_rows": external,
        "unreadable": unreadable,
    }
