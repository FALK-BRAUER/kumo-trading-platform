"""Is the approved repair still true of the book as it is NOW? (#744, the executor's decision half.)

`plan_contra_closes` decides WHAT to repair from a read taken at planning time. Between that read and
the moment a leg is booked, the book can move: a lane can exit, a reconciliation can fire, a fill can
land. This module re-reads the live positions and turns an approved plan into the exact legs to book
— or into a refusal.

PURE. It reads, decides, and writes nothing. Booking the legs is a separate, armed step; keeping the
decision testable without a live cache is the whole reason it is separated, and it is the same shape
`contra_close` already follows.

EVERY FAILURE THIS REPAIR CAN CAUSE IS A FAILURE OF AIM. `_apply_leg` hardcodes
`PositionId(f"{instrument}-{strategy}")`, and a fill aimed at an id that is not the real one does not
close the short — it OPENS A NEW POSITION, re-minting the exact defect being repaired, in a lane that
may hold nothing at all. A live cache read once found all sixteen legs matching the reconstructed
shape, and only ONE was inspected deeply enough to confirm the id embedded in it. Being right by
coincidence is not being right. So every id here is READ from the live book and never rebuilt, and a
pair whose id changed is refused rather than re-aimed.

APPROVAL IS OF A FIXED LIST. The pair's `fingerprint` covers its quantities, bases and position ids;
it is re-derived from the live read and a mismatch is REFUSED, never silently resized. A pair that
moved is not the pair that was approved, and quietly repairing the new one is the repair deciding for
itself how much of a live book to rewrite.

PER PAIR, NOT ALL-OR-NOTHING. One instrument drifting must not block a repair that is still exactly
correct for every other — but neither may a drifting one be carried along. Each pair is decided on
its own evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.contra_close import (QTY_TOLERANCE, ContraPair, Eviction, Leg, Refusal, SurplusEviction,
                              _validated_working, surplus_shape)


@dataclass(frozen=True)
class RepairOrder:
    instrument_id: str
    fingerprint: str
    legs: tuple[Leg, ...]
    #: Realized this repair ERASES. Recorded because it cannot be fixed: the wrong-lane sell fired
    #: from flat, so it opened the short and realized nothing, and closing both legs at their own
    #: basis realizes nothing either. Unrecorded, the lane sums and the broker plane disagree by
    #: exactly this, forever, with the reason discarded.
    unbooked_realized: float
    #: A surviving long keeps a basis blended from phantom and real quantity. The size becomes
    #: broker-true and the basis does not. No zero-realizing close can repair a basis — arithmetic,
    #: not an omission — so it is flagged rather than reported as complete.
    is_partial: bool


@dataclass(frozen=True)
class ExecutionPlan:
    orders: tuple[RepairOrder, ...] = field(default_factory=tuple)
    refused: tuple[Refusal, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        if not self.orders and not self.refused:
            return "nothing to execute"
        partial = sum(1 for o in self.orders if o.is_partial)
        erased = sum(o.unbooked_realized for o in self.orders)
        return (
            f"{len(self.orders)} pair(s) ready, {len(self.refused)} refused as moved; "
            f"{erased:+,.2f} of realized is erased by this repair and belongs to no lane"
            + (f"; {partial} pair(s) leave a blended basis this cannot fix" if partial else "")
        )


def _same_qty(a: float, b: float) -> bool:
    """Equal quantities, with NaN meaning UNKNOWN rather than merely unequal.

    `broker_qty` is NaN when the fresh read does not mention the instrument. Every comparison with
    NaN is False, which happens to give the right answer here — but only by accident, and that is
    exactly the family that disarmed a daily-loss halt elsewhere in this codebase (`nan <= 0` is
    False, so the halt never fired). Saying it explicitly means the next reader does not have to
    rediscover why an accident was safe.
    """
    if a != a or b != b:      # NaN on either side: unknown, never "the same"
        return False
    return abs(a - b) <= QTY_TOLERANCE


def _live_pair(pair: ContraPair, by_key: dict, broker, broker_complete: bool = False) -> ContraPair | None:
    """The same pair, rebuilt from the LIVE read — or None if either leg is gone.

    Keyed by (instrument, strategy), which is what identifies a leg across two reads. The position
    ID is deliberately NOT part of the key: an id that changed must be REFUSED, and a key that
    included it would make that case indistinguishable from a leg that vanished.
    """
    long_pos = by_key.get((pair.instrument_id, pair.long_strategy))
    short_pos = by_key.get((pair.instrument_id, pair.short_strategy))
    if long_pos is None or short_pos is None:
        return None
    long_qty, short_qty = float(long_pos.signed_qty), float(short_pos.signed_qty)
    return ContraPair(
        instrument_id=pair.instrument_id,
        long_strategy=pair.long_strategy,
        short_strategy=pair.short_strategy,
        long_position_id=str(long_pos.id),
        short_position_id=str(short_pos.id),
        quantity=min(abs(long_qty), abs(short_qty)),
        long_px=float(long_pos.avg_px_open),
        short_px=float(short_pos.avg_px_open),
        long_qty_before=long_qty,
        short_qty_before=short_qty,
        # RE-READ, NEVER CARRIED OVER. This previously copied `pair.broker_qty` from the APPROVED
        # pair, so the fingerprint's broker component compared a copy with its own original and could
        # never disagree — the one field describing the VENUE was the one field that was vacuous.
        # `cache net == broker net` is the premise the whole repair rests on, and a real partial sale
        # filling after approval does not touch the cache until reconciliation applies it: legs
        # unchanged, fingerprint unchanged, legs book — and when the sale lands the long lane has
        # given back more than it holds and FLIPS SHORT. Absence is not agreement: a broker read that
        # does not mention the instrument is UNKNOWN, and unknown refuses.
        # SILENCE UNDER A STATED-COMPLETE READ IS ZERO (#771). Unstated, absence stays NaN and
        # every comparison below is False, which refuses — that is the guard, and it survives.
        # Stage 1 gained the same third state; the two stages must not disagree about what they can
        # describe, exactly as they must not for `ts_opened`.
        broker_qty=(float(broker[pair.instrument_id]) if broker is not None
                    and pair.instrument_id in broker
                    else (0.0 if (broker is not None and broker_complete) else float("nan"))),
        long_ts_opened=int(getattr(long_pos, "ts_opened", 0) or 0),
        short_ts_opened=int(getattr(short_pos, "ts_opened", 0) or 0),
    )


def prepare_execution(plan, positions, broker, *, working_orders=(),
                      known_instrument_ids=None, broker_complete: bool = False) -> ExecutionPlan:
    """Turn an approved `ContraPlan` into legs, against a FRESH read of the position book.

    `positions` is the engine's own `cache.positions_open()` and `broker` the venue's signed
    quantities, BOTH read at T0 immediately before booking. `broker` is REQUIRED, not defaulted: a
    default would let a caller omit the one plane that anchors the whole repair and still get orders
    back, which is the shape this codebase keeps paying for. An instrument the broker read does not
    mention is UNKNOWN, never zero, and unknown refuses.
    """
    # OPEN ONLY, and by the position's own answer rather than by a key pattern. The durable cache
    # retains CLOSED positions under the same id shape — a live scan turned up `WHD.XNYS-MANUAL-001`,
    # a flatten that had already run. A closed position is not a smaller one: booking a contra-leg
    # against it OPENS a fresh position in a lane holding nothing, which is strictly worse than the
    # state being repaired. A shape that cannot say whether it is open is not assumed to be.
    by_key: dict = {}
    mute: set = set()
    for p in positions:
        # NOT `getattr(p, "is_open", ...)` IN EITHER DIRECTION. A default of True makes a shape
        # lacking the field count as OPEN — absence reading as permission, where the consequence is
        # a BOOK MUTATION. A default of False is safer but reports the wrong REASON: "this leg is no
        # longer open" is a claim about the book, and from an unreadable shape we do not have one.
        # `contra_close` refuses this by name and the two stages must not disagree about it.
        # BOTH FIELDS, FOR THE SAME REASON. A missing `is_open` makes openness unknowable; a missing
        # `ts_opened` makes the NETTING id-reuse check vacuous — `getattr(..., 0)` yields 0 on BOTH
        # reads, so the pair fingerprints against a copy of itself and agrees by construction. That
        # is character-for-character the defect the broker re-read just fixed, and a default would
        # re-create it one field over. Production's `Position` carries both, so refusing costs
        # nothing live and forces every double to carry what production carries.
        if not hasattr(p, "is_open") or not hasattr(p, "ts_opened"):
            mute.add((str(p.instrument_id), str(p.strategy_id)))
            continue
        if not p.is_open:
            continue
        by_key[(str(p.instrument_id), str(p.strategy_id))] = p

    orders: list[RepairOrder] = []
    refused: list[Refusal] = []

    # THE PLANNER'S GUARDS ARE PLANNING-TIME ONLY, and the protection reconciler re-arms on a 60s
    # timer — so a stop rested between approval and execution was unseen, and the oversell hazard
    # returns through the time gap: a reduce-only order sized to the PHANTOM quantity, left working
    # against a position this repair shrinks, sells shares that are no longer there when it triggers.
    # Same vocabulary rule as the planner, and the same refusal to compare across vocabularies.
    # THE SAME PREDICATE AS THE PLANNER, not a second one that happens to agree today. A held
    # sibling listing is legitimate vocabulary the validator accepts, so full-id membership passes it
    # silently — and without a wider universe the validator instead RAISES and kills every pair,
    # including clean ones, violating this module's per-pair doctrine. #625 measured 64 of staging's
    # 209 cached instruments carrying two venues, so both directions are ordinary here.
    universe = ({str(i) for i in known_instrument_ids} if known_instrument_ids is not None
                else {str(p.instrument_id) for p in positions})
    working = _validated_working(working_orders, universe)
    working_symbols = {w.rsplit(".", 1)[0] for w in working}

    for pair in plan.pairs:
        if pair.instrument_id.rsplit(".", 1)[0] in working_symbols:
            refused.append(Refusal(
                pair.instrument_id,
                "a resting order appeared on this instrument after the plan was approved; it is "
                "sized to the pre-repair quantity and would oversell on trigger — cancel the "
                "protective legs and let the reconciler re-arm before repairing",
            ))
            continue
        unreadable = [
            lane for lane in (pair.long_strategy, pair.short_strategy)
            if (pair.instrument_id, lane) in mute
        ]
        if unreadable:
            refused.append(Refusal(
                pair.instrument_id,
                f"a position in this pair cannot describe itself ({', '.join(unreadable)}): it is "
                f"missing `is_open` or `ts_opened`, so either its openness is unknowable or the "
                f"cycle-reuse check silently fingerprints it as zero. A repair must not be booked "
                f"against a book this read cannot describe",
            ))
            continue
        live = _live_pair(pair, by_key, broker, broker_complete)
        if live is None:
            refused.append(Refusal(
                pair.instrument_id,
                "a leg of this pair is no longer open in the book — it closed or vanished between "
                "approval and execution; booking a contra-leg against it would open a fresh "
                "position rather than repair one",
            ))
            continue
        # THE BROKER PLANE GETS ITS OWN REFUSAL, ahead of the fingerprint, because it is the premise
        # rather than a detail: `cache net == broker net` is what makes a mirror pair a mirror pair.
        # Folding it into "this pair moved" would report the foundation shifting as if a quantity had
        # been nudged, and the operator reading it would look in the wrong plane.
        if not _same_qty(live.broker_qty, pair.broker_qty):
            refused.append(Refusal(
                pair.instrument_id,
                f"the BROKER net moved between approval and execution "
                f"({pair.broker_qty:g} -> {live.broker_qty:g}), or this read does not mention the "
                f"instrument at all. That figure is the premise the repair rests on — cache net "
                f"agreeing with broker net — so it is refused rather than repaired against a "
                f"foundation that has shifted",
            ))
            continue
        if live.fingerprint != pair.fingerprint:
            # DELIBERATELY NOT SAYING WHICH FIELD MOVED. Any difference — quantity, basis, or the
            # position id that the fills are AIMED at — makes this a different pair from the one
            # approved, and ranking them would invite resizing the "small" ones.
            refused.append(Refusal(
                pair.instrument_id,
                f"this pair moved between approval and execution (fingerprint {pair.fingerprint} "
                f"-> {live.fingerprint}); approval was of a fixed list, so it is refused rather "
                f"than resized to numbers nobody approved",
            ))
            continue
        orders.append(RepairOrder(
            instrument_id=live.instrument_id,
            fingerprint=live.fingerprint,
            legs=(
                Leg(live.instrument_id, live.short_strategy, live.short_position_id,
                    "BUY", live.quantity, live.short_px),
                Leg(live.instrument_id, live.long_strategy, live.long_position_id,
                    "SELL", live.quantity, live.long_px),
            ),
            unbooked_realized=live.unbooked_realized,
            is_partial=live.is_partial,
        ))

    # #779 EVICTIONS. All-or-nothing per instrument, and MORE STRICTLY than a pair: a pair ignores
    # unrelated legs on its instrument by design, whereas an eviction CLAIMS TO FLATTEN THE
    # INSTRUMENT — so an extra leg does not merely narrow it, it FALSIFIES ITS PREMISE.
    #
    # `plan.evictions` is read directly, with no getattr fallback. Production always hands a real
    # ContraPlan, and a double that cannot represent one should crash here rather than silently book
    # nothing.
    for ev in plan.evictions:
        if ev.instrument_id.rsplit(".", 1)[0] in working_symbols:
            refused.append(Refusal(ev.instrument_id,
                "a resting order appeared on this instrument after the plan was approved; it is "
                "sized to the pre-eviction quantity and would oversell on trigger"))
            continue
        if any(inst == ev.instrument_id for inst, _lane in mute):
            refused.append(Refusal(ev.instrument_id,
                "a position on this instrument cannot describe itself (missing `is_open` or "
                "`ts_opened`); an eviction must read the WHOLE instrument, so one unreadable shape "
                "refuses all of it"))
            continue
        live_legs = sorted(
            (pos for (inst, _lane), pos in by_key.items() if inst == ev.instrument_id),
            key=lambda pos: (str(pos.strategy_id), str(pos.id)),
        )
        planned_lanes = {l.strategy_id for l in ev.legs}
        live_lanes = {str(pos.strategy_id) for pos in live_legs}
        if live_lanes != planned_lanes:
            # BOTH DIRECTIONS refuse the whole eviction. A vanished leg: booking the rest lands the
            # cache on a NEW wrong net. An appeared leg: the book gained a position after approval,
            # and evicting around it leaves that one standing at a venue that may no longer be flat.
            refused.append(Refusal(ev.instrument_id,
                f"the instrument's lane set moved between approval and execution "
                f"({sorted(planned_lanes)} -> {sorted(live_lanes)}); an eviction is approved for the "
                f"WHOLE book of an instrument and is refused whole, never trimmed"))
            continue
        # THE PREMISE, RE-READ rather than carried over. Copying `ev.broker_qty` forward would compare
        # a value with its own original and agree by construction — character for character the
        # defect `_live_pair` already had to fix.
        live_broker_qty = (float(broker[ev.instrument_id])
                           if broker is not None and ev.instrument_id in broker
                           else (0.0 if (broker is not None and broker_complete)
                                 else float("nan")))
        if not _same_qty(live_broker_qty, 0.0):
            refused.append(Refusal(ev.instrument_id,
                f"the venue no longer states zero for this instrument ({live_broker_qty:g}, or the "
                f"read did not mention it); a venue holding something means a residual must be "
                f"awarded, which is the guess this refuses to make"))
            continue
        live = Eviction(
            instrument_id=ev.instrument_id,
            legs=tuple(
                Leg(ev.instrument_id, str(pos.strategy_id), str(pos.id),
                    "SELL" if float(pos.signed_qty) > 0 else "BUY",
                    abs(float(pos.signed_qty)), float(pos.avg_px_open))
                for pos in live_legs
            ),
            ts_opened=tuple(int(pos.ts_opened or 0) for pos in live_legs),
            broker_qty=live_broker_qty,
        )
        if live.fingerprint != ev.fingerprint:
            refused.append(Refusal(ev.instrument_id,
                f"this eviction moved between approval and execution (fingerprint {ev.fingerprint} "
                f"-> {live.fingerprint}); approval was of a fixed list, so it is refused rather than "
                f"resized to numbers nobody approved"))
            continue
        orders.append(RepairOrder(
            instrument_id=live.instrument_id,
            fingerprint=live.fingerprint,
            legs=live.legs,
            unbooked_realized=live.unbooked_realized,
            # NOT partial: an eviction leaves no survivor, so no blended basis outlives it.
            is_partial=False,
        ))

    for se in getattr(plan, "surplus_evictions", ()):
        # THE SURPLUS EVICTION (#1038), re-derived against the FRESH book with the planner's OWN
        # predicate: one inequality, two call sites. The venue quantity comes from the fresh read and
        # is NaN when that read did not mention the instrument (`_same_qty` semantics: unknown is
        # never "the same"); the premise — real lanes summing to the venue, the EXTERNAL leg carrying
        # exactly the surplus — must hold NOW, and the rebuilt fingerprint must equal the approved
        # one. A real lane that moved (BCTROT 12 -> 8), a venue that now holds the surplus, a sibling
        # that appeared: each refuses by name. Approval was of a fixed premise, never resized.
        if se.instrument_id.rsplit(".", 1)[0] in working_symbols:
            refused.append(Refusal(se.instrument_id,
                "a resting order appeared on this instrument after the plan was approved; it is "
                "sized to the pre-eviction quantity and would oversell on trigger"))
            continue
        if any(inst == se.instrument_id for inst, _lane in mute):
            refused.append(Refusal(se.instrument_id,
                "a position on this instrument cannot describe itself (missing `is_open` or "
                "`ts_opened`); the surplus premise must read the WHOLE instrument, so one unreadable "
                "shape refuses it"))
            continue
        live_legs = [pos for (inst, _lane), pos in by_key.items() if inst == se.instrument_id]
        live_broker_qty = (float(broker[se.instrument_id])
                           if broker is not None and se.instrument_id in broker
                           else float("nan"))
        if not _same_qty(live_broker_qty, se.broker_qty):
            refused.append(Refusal(se.instrument_id,
                f"the venue quantity moved between approval and execution ({se.broker_qty:g} -> "
                f"{live_broker_qty:g}, or the read did not mention it); the surplus premise no longer "
                f"holds and the approved eviction is refused rather than resized"))
            continue
        live_leg, live_premise = surplus_shape(live_legs, live_broker_qty)
        if live_leg is None:
            refused.append(Refusal(se.instrument_id,
                f"the surplus shape moved between approval and execution: "
                f"{live_premise or 'no EXTERNAL surplus over lanes that sum to the venue any more'}"))
            continue
        live = SurplusEviction(
            instrument_id=se.instrument_id,
            leg=Leg(se.instrument_id, str(live_leg.strategy_id), str(live_leg.id), "SELL",
                    abs(float(live_leg.signed_qty)), float(live_leg.avg_px_open)),
            ts_opened=int(live_leg.ts_opened or 0),
            broker_qty=live_broker_qty,
            real_lanes_qty=float(live_premise),
        )
        if live.fingerprint != se.fingerprint:
            refused.append(Refusal(se.instrument_id,
                f"this surplus eviction moved between approval and execution (fingerprint "
                f"{se.fingerprint} -> {live.fingerprint}); approval was of a fixed premise, so it is "
                f"refused rather than resized to numbers nobody approved"))
            continue
        orders.append(RepairOrder(
            instrument_id=live.instrument_id,
            fingerprint=live.fingerprint,
            legs=(live.leg,),
            unbooked_realized=live.unbooked_realized,
            is_partial=False,
        ))
    return ExecutionPlan(orders=tuple(orders), refused=tuple(refused))
