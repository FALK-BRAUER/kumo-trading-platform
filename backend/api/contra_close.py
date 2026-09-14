"""Planning the repair for a mis-attributed book (#437 piece 1).

THE DEFECT. A closing SELL booked to the wrong owner mints, under Nautilus NETTING, an offsetting
SHORT in a sibling lane while the real owner's LONG is left stranded. The netted book still equals
the broker's exactly, which is why all three reconcilers — every one of which sums signed quantity
per SYMBOL — read green for nine days. Eight pairs, $18,217.94 of mis-attributed ownership.

THE REPAIR. For an instrument where one lane holds a LONG, one holds a SHORT, and the cache net
equals the broker's, book offsetting internal legs of `min(|qty|)`: the short closes, the stranded
long gives back exactly what the short claimed, and the net does not move.

IT REALIZES NOTHING. Each leg fills at ITS OWN position's `avg_px_open` — the CARRY_OVER principle,
and per-leg rather than one price because the two positions have different bases. A single price
would realize P&L on whichever leg it did not match: a fictional gain on an event that already
happened, days ago, at a different price.

WHAT IT DOES NOT FIX, SAID PLAINLY SO NOBODY INFERS OTHERWISE. The realized P&L of the original
mis-attributed sale stays with the lane that wrongly booked it. That money moved on a real fill at a
real price, and no book entry can re-attribute it without inventing a trade. This repairs the OPEN
book and the ownership of it. The realized side needs its own answer.

PLANNING IS SEPARATE FROM EXECUTION, AND THE PLAN IS A FIXED LIST. Approval is never of a predicate
re-evaluated later: each pair carries a `fingerprint` over the quantities and bases it was planned
against, and execution refuses a pair that moved rather than resizing it to numbers nobody approved.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

#: Quantities are whole or fractional shares from one source, so a mismatch is never rounding.
QTY_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Leg:
    """One internal leg. Never reaches a venue; the broker's net does not move."""

    instrument_id: str
    strategy_id: str
    #: READ from the live book. See the module docstring — this is the aim, and rebuilding it is the
    #: defect.
    position_id: str
    side: str        # "BUY" closes the phantom short; "SELL" gives back what it claimed
    quantity: float
    #: THIS LEG'S OWN BASIS. A shared price would realize a gain on one lane and an equal loss on the
    #: other, inventing per-lane P&L out of a bookkeeping correction. Each leg closing at its own
    #: `avg_px_open` realizes exactly nothing, which is the point.
    price: float


@dataclass(frozen=True)
class ContraPair:
    instrument_id: str
    long_strategy: str
    short_strategy: str
    #: The id READ from the cache, never `f"{instrument}-{strategy}"` reconstructed. `_apply_leg`
    #: hardcodes that shape, and a fill aimed at an id that is not the real one OPENS A NEW POSITION
    #: instead of closing the short — re-minting the exact defect being repaired. A live cache read
    #: found all sixteen legs matching the reconstructed shape, but only ONE was inspected deeply
    #: enough to confirm the embedded id. Being right by coincidence is not being right.
    long_position_id: str
    short_position_id: str
    quantity: float
    long_px: float
    short_px: float
    long_qty_before: float
    short_qty_before: float
    broker_qty: float
    #: WHEN EACH LEG'S CURRENT CYCLE OPENED. Id stability is NOT cycle identity here: under NETTING
    #: the position id is DERIVED, not allocated — Nautilus's cache even carries a "cleanup for
    #: NETTING reopen" path — so a long that fully closed and re-opened at the same quantity and
    #: basis (a same-level limit re-entry) carries the SAME id and produces an IDENTICAL fingerprint.
    #: The repair would then aim its SELL leg at a genuinely new, real position. This is the only
    #: wrong-aim state that survives every other check, and the stamp is already on the position.
    long_ts_opened: int = 0
    short_ts_opened: int = 0

    @property
    def unbooked_realized(self) -> float:
        """The realized this repair ERASES from the lane ledgers — recorded, never silently dropped.

        MY FIRST READING OF THIS WAS BACKWARDS and the correction inverts it. I documented the
        realized as "staying with the lane that wrongly booked it". It does not: the wrong-lane SELL
        fired FROM FLAT, so under NETTING it OPENED the short, and an opening fill has no prior lots
        to match against — it realizes NOTHING. The sale's realized was never booked to any lane at
        all; it lives only in account cash.

        The repair then closes both legs at their own basis, realizing zero on each. So this amount
        is not mis-attributed — it is erased. Without recording it, sum-of-lane-realized and
        `realized_broker`'s plane disagree forever by exactly this, with the reason thrown away.

        A LEDGER DELTA, NOT ECONOMIC TRUTH. For the EXTERNAL shorts the synthetic flatting fill's
        price may differ from the true sale price. This is still the right number to pin, because it
        is the number the two planes will differ by.
        """
        return (self.short_px - self.long_px) * self.quantity

    @property
    def surviving_long_qty(self) -> float:
        return abs(self.long_qty_before) - self.quantity

    @property
    def is_partial(self) -> bool:
        """Whether a long SURVIVES this repair — and therefore keeps a basis the repair cannot fix.

        A surviving long's `avg_px_open` is a BLEND of the phantom quantity and a real re-entry.
        Closing the phantom part at that blend leaves the remainder still at the blend: the quantity
        becomes broker-true and the basis does not, so the survivor's unrealized is misstated from
        day one. No zero-realizing close can repair a basis — that is arithmetic, not an omission —
        so the pair is flagged instead of the repair being reported as complete.
        """
        return self.surviving_long_qty > QTY_TOLERANCE

    @property
    def fingerprint(self) -> str:
        """Identity of the STATE this pair was planned against. Execution re-derives it and refuses
        on a mismatch — a pair that moved between approval and execution is not the pair approved."""
        # THE POSITION IDS ARE PART OF THE IDENTITY. Execution aims fills at them, and an id that
        # changed between approval and execution is the difference between closing a short and
        # OPENING A NEW POSITION — the exact defect being repaired. Two books with identical
        # quantities under different ids are not the same plan.
        raw = (f"{self.instrument_id}|{self.long_strategy}|{self.short_strategy}|"
               f"{self.long_position_id}|{self.short_position_id}|"
               f"{self.long_qty_before}|{self.short_qty_before}|{self.long_px}|{self.short_px}|"
               f"{self.broker_qty}|{self.long_ts_opened}|{self.short_ts_opened}")
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Refusal:
    instrument_id: str
    reason: str


@dataclass(frozen=True)
class Eviction:
    """Every open leg of an instrument the venue has STATED it holds none of (#779).

    A PAIR retires long against short; an EVICTION retires legs against NOTHING, justified by the
    venue saying there is nothing there. It exists because a book that does not net flat at a flat
    venue cannot be expressed as pairs: GMAB's 118 long against 59 short retires at most 59 by
    pairing and STRANDS 59 phantom shares in whichever lane the pairing spared.

    ALL OR NOTHING, at every later stage. `legs` is the COMPLETE open book of the instrument at
    planning time, and execution refuses the whole eviction if any leg moved, vanished, or gained a
    sibling. A partial eviction lands the cache on a NEW wrong net — neither the book that was
    approved nor the venue's zero — and (measured, Nautilus 1.229.0) re-arms the position check's
    `generate_missing_orders` retries the moment the instrument later agrees and then disagrees
    again. A partial eviction is a mint PRECONDITION, not a smaller repair.
    """

    instrument_id: str
    #: EVERY open leg, longs and shorts alike, sorted by (strategy_id, position_id). The sort is
    #: part of the fingerprint's determinism, not cosmetics: without it an identical book could
    #: digest differently across two cache reads and refuse itself.
    legs: tuple[Leg, ...]
    #: `ts_opened` per leg, aligned with `legs`. Same reason `ContraPair` carries them: under NETTING
    #: the position id is DERIVED, not allocated, so a leg that fully closed and re-opened at the
    #: same quantity and basis carries the SAME id — the stamp is the only survivor-detector.
    ts_opened: tuple[int, ...]
    #: The zero the venue STATED. Carried and fingerprinted so the approval names its premise, and a
    #: later non-zero read cannot silently reuse it.
    broker_qty: float

    @property
    def evicted_quantity(self) -> float:
        return sum(l.quantity for l in self.legs)

    @property
    def unbooked_realized(self) -> float:
        """A LEDGER DELTA, NOT ECONOMIC TRUTH — and less so here than for a pair.

        This is the cache book value the eviction writes off: the same quantity
        `ContraPair.unbooked_realized` records, generalized. For a single pair it reduces exactly to
        `(short_px - long_px) * q`.

        DO NOT REPORT IT AS THE MONEY. Part of it is fabricated: an EXTERNAL leg's basis is the
        reconciliation blend, a price that never traded (GMAB's 33.68 is
        `(33.41*59 + 33.94*59)/118`). The ECONOMIC loss belonging to no lane is the real fills the
        cache never booked, and this function cannot see them — the planner is given cache legs and
        a broker quantity, never the fill ledger. That number comes from `realized_broker` over the
        instrument's broker activities and must be recorded separately. Two numbers, two planes.
        """
        return _book_value(self.legs)

    @property
    def fingerprint(self) -> str:
        """Approval is of a FIXED LIST, so every field whose movement changes the repair is in here.

        The `EVICT|` prefix keeps this namespace disjoint from `ContraPair`'s: `ContraLedger` keys on
        the fingerprint alone, so a collision must be structurally impossible rather than improbable.
        """
        lines = "|".join(
            f"{l.strategy_id}|{l.position_id}|"
            f"{l.quantity if l.side == 'SELL' else -l.quantity}|{l.price}|{ts}"
            for l, ts in zip(self.legs, self.ts_opened)
        )
        raw = f"EVICT|{self.instrument_id}|{self.broker_qty}|{lines}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


#: The one lane whose legs are reconciliation fiction by construction: a leg the venue reported and
#: no strategy claimed. The surplus rule below evicts ONLY legs stamped with this id.
EXTERNAL_LANE = "EXTERNAL"


def _book_value(legs) -> float:
    """The cache book value a set of closing legs writes off — ONE formula for `Eviction` and
    `SurplusEviction` (review on #1038: a copied formula passes the value test and drifts)."""
    return (sum(l.price * l.quantity for l in legs if l.side == "BUY")
            - sum(l.price * l.quantity for l in legs if l.side == "SELL"))


def surplus_shape(legs, venue_qty: float):
    """THE ONE PREDICATE for the surplus rule (#1038), read by the planner AND re-derived by the
    executor against its fresh book — two call sites, one inequality.

    Returns `(external_leg, real_lanes_qty)` when the instrument's open legs are: NO short leg,
    exactly ONE EXTERNAL long, and the non-EXTERNAL lanes summing to `venue_qty` within
    `QTY_TOLERANCE` with the EXTERNAL leg carrying exactly the surplus. Returns `(None, reason)`
    with a stated reason for every other over-summing shape, and `(None, None)` when there is no
    surplus at all (nothing to say). `venue_qty` must be a NUMBER the venue stated — the caller
    decides what "stated" means (`broker_complete`, a fresh read); NaN never matches.

    Assumes NETTING (one position per instrument-and-lane, the OMS this node runs): the executor
    re-derives over a `(instrument, lane)`-keyed view, which equals the planner's raw list only
    under NETTING. OUT OF SCOPE, deliberately silent: an EXTERNAL leg with NO real lane under it
    (EXTERNAL 22 over a venue 12) — no real sum to anchor the premise on; #779 owns it when the
    venue is flat, and the banner still shows it otherwise.
    """
    if venue_qty != venue_qty:                            # NaN: the venue did not say
        return None, None
    if any(float(p.signed_qty) < 0 for p in legs):
        return None, None                                 # a short anywhere: #771's book, not this rule
    longs = [p for p in legs if float(p.signed_qty) > 0]
    cache_net = sum(float(p.signed_qty) for p in longs)
    surplus = cache_net - float(venue_qty)
    if surplus <= QTY_TOLERANCE:
        return None, None
    external = [p for p in longs if str(p.strategy_id) == EXTERNAL_LANE]
    real = [p for p in longs if str(p.strategy_id) != EXTERNAL_LANE]
    real_sum = sum(float(p.signed_qty) for p in real)
    if not real:
        return None, None                                 # EXTERNAL alone over an empty book: #779's territory or nothing
    if not external:
        return None, (f"the cache holds {cache_net:g} against a venue quantity of {float(venue_qty):g} and "
                      f"the surplus of {surplus:g} is NOT an EXTERNAL leg — real lanes over-sum the venue "
                      f"and the planner cannot say whose shares are phantom")
    if len(external) > 1:
        return None, (f"{len(external)} EXTERNAL legs sit on this instrument; the surplus rule evicts "
                      f"exactly one and will not choose between them")
    x = external[0]
    if abs(real_sum - float(venue_qty)) > QTY_TOLERANCE or abs(float(x.signed_qty) - surplus) > QTY_TOLERANCE:
        return None, (f"the real lanes hold {real_sum:g} against a venue quantity of {float(venue_qty):g} and "
                      f"the EXTERNAL leg holds {float(x.signed_qty):g}: evicting EXTERNAL would not land the "
                      f"book on the venue, so nothing is planned")
    return x, real_sum


@dataclass(frozen=True)
class SurplusEviction:
    """ONE EXTERNAL leg sitting on top of real lanes that already sum to the venue (#1038).

    THE THIRD SHAPE. A PAIR (#771) retires long against short; an EVICTION (#779) retires every leg
    against a venue that holds NONE. Paper's AEM was neither: BCTROT-004 +12, EXTERNAL +10, venue 12
    — nothing negative, venue not flat — and the planner reported nothing at all while the drift
    banner stood for days with inverted advice ("manage at your broker"; the broker was right).

    ITS OWN KIND, NOT AN `Eviction` OF ONE LEG. `Eviction.legs` is the instrument's COMPLETE open
    book and execution refuses it if any sibling exists — so a one-leg eviction at a venue holding
    12 would be vetoed at execution, correctly. This kind names what it is: the surplus leg, and the
    PREMISE that makes it surplus (the real lanes sum to the venue), both fingerprinted.

    THE PREMISE IS IN THE FINGERPRINT. An approved plan must not execute against a later book where
    BCTROT dropped to 8 and the venue to 8: the same EXTERNAL 10 leg is then only 8 phantom and 2
    real, and evicting all of it walks the book AWAY from the venue. `real_lanes_qty` and
    `broker_qty` are part of the approval, so the executor's fresh read refuses on any move.
    """

    instrument_id: str
    leg: Leg
    ts_opened: int
    broker_qty: float
    real_lanes_qty: float

    @property
    def unbooked_realized(self) -> float:
        """A LEDGER DELTA, NOT ECONOMIC TRUTH — same caveat as `Eviction.unbooked_realized`: the
        EXTERNAL basis is the reconciliation blend, a price that never traded. The book value the
        eviction writes off, in `Eviction`'s sign (BUY − SELL: a long closed by a SELL leg is
        NEGATIVE) — `unbooked_realized_total` sums both kinds, so one sign or the total lies (impl
        review on #1038 caught this inverted)."""
        return _book_value((self.leg,))

    @property
    def fingerprint(self) -> str:
        """`SURPLUS|` keeps the namespace disjoint from `ContraPair` and `Eviction` — the ledger keys
        on the fingerprint alone. The premise travels: real lanes' sum and the venue quantity."""
        raw = (f"SURPLUS|{self.instrument_id}|{self.broker_qty}|{self.real_lanes_qty}|"
               f"{self.leg.strategy_id}|{self.leg.position_id}|"
               f"{self.leg.quantity if self.leg.side == 'SELL' else -self.leg.quantity}|"
               f"{self.leg.price}|{self.ts_opened}")
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ContraPlan:
    pairs: tuple[ContraPair, ...] = field(default_factory=tuple)
    refused: tuple[Refusal, ...] = field(default_factory=tuple)
    #: LAST, so every existing positional construction keeps working.
    evictions: tuple[Eviction, ...] = field(default_factory=tuple)
    #: #1038 — after `evictions`, for the same reason.
    surplus_evictions: tuple[SurplusEviction, ...] = field(default_factory=tuple)

    @property
    def unbooked_realized_total(self) -> float:
        return (sum(p.unbooked_realized for p in self.pairs)
                + sum(e.unbooked_realized for e in self.evictions)
                + sum(e.unbooked_realized for e in self.surplus_evictions))

    @property
    def summary(self) -> str:
        if not self.pairs and not self.refused and not self.evictions and not self.surplus_evictions:
            return "nothing to repair: every instrument's per-lane split agrees with the broker"
        partial = [p for p in self.pairs if p.is_partial]
        note = (f"; {self.unbooked_realized_total:+,.2f} of realized was never booked to any lane "
                f"and this repair does not book it — record it, or the lane sums and the broker "
                f"plane disagree by exactly that, forever, with no reason attached")
        if partial:
            note += (f"; {len(partial)} PARTIAL pair(s) leave a surviving long whose basis stays a "
                     f"blend of phantom and real quantity — the size becomes broker-true, the basis "
                     f"does not, and no zero-realizing close can fix that")
        if self.evictions:
            note += (f"; EVICTING {len(self.evictions)} instrument(s) the venue holds none of "
                     f"({', '.join(e.instrument_id for e in self.evictions)}) — every leg is "
                     f"fiction and closes at its own basis, realizing nothing")
        if self.surplus_evictions:
            note += (f"; EVICTING the EXTERNAL surplus on {len(self.surplus_evictions)} instrument(s) "
                     f"({', '.join(f'{e.instrument_id} {e.leg.quantity:g}' for e in self.surplus_evictions)}) "
                     f"whose real lanes already sum to the venue — the surplus leg closes at its own "
                     f"basis, realizing nothing")
        return (f"{len(self.pairs)} pair(s) to repair, {len(self.evictions)} eviction(s), "
                f"{len(self.surplus_evictions)} surplus eviction(s), "
                f"{len(self.refused)} instrument(s) refused{note}")


def _validated_working(working_orders, known_ids=()) -> set[str]:
    """`working_orders` in the SAME vocabulary the position book speaks, or a refusal to plan at all.

    THE GUARD THIS PROTECTS CANNOT BE ALLOWED TO FAIL OPEN. `plan_contra_closes` blocks an instrument
    that has a resting order, because #239 rests protective stops sized to the PHANTOM quantity and a
    reduce-only order left working against a position this repair shrinks OVERSELLS when it triggers
    — turning a book-only correction into a real market event on the live account.

    The check was `if instrument in working`, against a caller-supplied set with no format contract.
    The positions plane speaks instrument ids (`CGAU.XNYS`); a caller reaching for the obvious source
    hands over bare symbols (`CGAU`), and that comparison is then False for every instrument that has
    ever existed. The guard does not misfire, it NEVER fires.

    THE ASYMMETRY IS THE TELL. The `broker` dict is keyed the same way and fails CLOSED on exactly
    the same mismatch — an instrument missing from it is refused, loudly, by name. Two caller-supplied
    mappings, one vocabulary, opposite failure directions, and only the dangerous one was silent.

    NAUTILUS'S OWN PARSER IS THE AUTHORITY, not a regex of ours: `InstrumentId.from_str` is what the
    rest of the system means by "an instrument id", so a value it rejects is one no comparison here
    could ever match. RAISES rather than refusing per-instrument, because a caller in the wrong
    vocabulary has told us nothing about ANY instrument — every guard in the run is equally blind, so
    scoping the complaint to one row would understate it.

    RESIDUAL, STATED RATHER THAN PAPERED OVER: `from_str` accepts `BRK.B`, reading it as symbol BRK on
    venue B, so a bare symbol that itself contains a dot still parses. The live paper book holds both
    `BRK.B` and `BRKB`, so this is not theoretical — it is simply not reachable from here, because
    nothing distinguishes that string from a genuine id without knowing the venue set, and the venue
    set is not knowable from an unrelated resting order (a caller may legitimately name an instrument
    this book has never held). The measured failure was `CGAU` against `CGAU.XNYS`, and that is
    closed.

    AN EMPTY SET IS NOT A MISMATCH. Nothing resting is the common case; it carries no vocabulary to
    disagree with, and refusing it would fire the validator on every clean book.
    """
    items = {str(w) for w in (working_orders or ())}
    if not items:
        return set()
    from nautilus_trader.model.identifiers import InstrumentId

    # THE BOOK'S OWN SYMBOLS CLOSE THE PARSER'S BLIND SPOT. `from_str` accepts `BRK.B` as symbol BRK
    # on venue B, so a bare symbol containing a dot parses and a parser-only check waves it through.
    # That was dismissed here on the grounds that telling the two apart needs the VENUE set, which an
    # unrelated resting order cannot supply — wrong framing: it needs the SYMBOL set, and the symbol
    # set is in the position book already. An item equal to the symbol-part of a held instrument id
    # while equal to NO held id can only be the caller having stripped the venue. It refuses nothing
    # legitimate, because an unrelated instrument matches neither. The live paper book holds both
    # `BRK.B` and `BRKB`, so this shape is real.
    ids = {str(i) for i in known_ids}
    symbols = {i.rsplit(".", 1)[0] for i in ids}

    bad = []
    for item in sorted(items):
        try:
            parsed = InstrumentId.from_str(item)
        except Exception:
            bad.append(item)
            continue
        # CASE IS PART OF THE VOCABULARY. `from_str` does not validate it and the membership test is
        # case-sensitive, so `cgau.xnys` parses, matches nothing, and is the identical silent
        # fail-open the bare symbol was. (Checked on the whole item: the venue is a substring of it,
        # so a separate venue-case clause can never fire alone.)
        if item != item.upper():
            bad.append(item)
            continue
        if item in ids:
            continue
        # BY SYMBOL-PART, NOT BY FULL ID. `CGAU.XNAS` against a book holding `CGAU.XNYS` is
        # well-formed, upper-case, not a held id and not a bare symbol — and it reserves the SAME
        # SHARES. #625 measured 64 of staging's 209 cached instruments carrying two venues, because
        # SMART routing returns multiple listings, so a resting order surfaced under a sibling venue
        # id is ordinary here rather than exotic. Same ticker is the same security on this book.
        #
        # A bare symbol lands here too (`rsplit` on a dotless string returns the string), which is
        # why the earlier explicit bare-symbol branch is gone: one rule, not two that can drift.
        # TWO WAYS TO NAME THE SAME SHARES WITHOUT NAMING THE HELD ID, and each needs its own
        # clause — the venue-variant rule alone misses `BRK.B`, whose symbol-PART is `BRK`, and the
        # bare-symbol rule alone misses `CGAU.XNAS`.
        #
        #   the venue stripped   `CGAU`      == a held symbol
        #   a sibling listing    `CGAU.XNAS` -> symbol-part `CGAU` == a held symbol
        #
        # #625 measured 64 of staging's 209 cached instruments carrying two venues, because SMART
        # routing returns multiple listings, so the second is ordinary here rather than exotic. Same
        # ticker is the same security on this book, and it reserves the same shares.
        if item in symbols or item.rsplit(".", 1)[0] in symbols:
            bad.append(item)
    if bad:
        raise ValueError(
            f"working_orders is not in the instrument id vocabulary: {', '.join(bad)}. The position "
            f"book is keyed by instrument id (e.g. 'CGAU.XNYS'), so these can never match and the "
            f"resting-order guard would silently never fire — which is how a protective stop sized "
            f"to the phantom quantity survives the repair and oversells on trigger (#239). Pass "
            f"fully qualified, upper-case instrument ids (SYMBOL.VENUE) matching the ids the "
            f"position book uses — not bare symbols, and not a sibling venue listing of a held "
            f"symbol, which reserves the same shares."
        )
    return items


def plan_operator_pair(positions, broker: dict[str, float], *, instrument_id: str,
                       long_strategy: str, short_strategy: str, quantity: float,
                       working_orders=(), known_instrument_ids=None,
                       broker_complete: bool = False) -> ContraPlan:
    """One contra pair the OPERATOR named, for a book the automatic planner refuses to guess (#784).

    WHY THIS EXISTS. `plan_contra_closes` refuses an instrument whose residual cannot be attributed
    from the position book — HALO is BCTROT-004 +55, MOMENTUM-002 +1, EXTERNAL -37 against a venue
    holding 19, and which lane keeps the 19 is not derivable. That refusal is correct and unchanged:
    the SYSTEM must not guess.

    AN OPERATOR NAMING THE PAIRING IS NOT A GUESS. It is the decision a claim control exists to let a
    human make. So the pairing is an INPUT here, and everything downstream — the aim checks, the
    fingerprint, the all-or-nothing booking — is the path already proven on CGAU and GMAB.

    THE OPERATOR MAY CHOOSE THE SPLIT, NEVER THE TOTAL. Closing `quantity` from a long and the same
    from a short preserves the instrument's net by construction, so the broker is untouched whatever
    is chosen. Everything that could move the net is still refused, by name: a leg that is not on the
    side named, a quantity that does not fit either leg, an unread venue, a resting order (#239).

    NOT CLAMPED. A quantity that does not fit is refused rather than reduced — resizing to a number
    nobody approved is exactly what the fingerprint downstream exists to prevent.
    """
    universe = ({str(i) for i in known_instrument_ids} if known_instrument_ids is not None
                else {str(p.instrument_id) for p in positions})
    working = _validated_working(working_orders, universe)
    working_symbols = {w.rsplit(".", 1)[0] for w in working}

    iid = str(instrument_id)
    refused: list[Refusal] = []

    if iid.rsplit(".", 1)[0] in working_symbols:
        return ContraPlan(refused=(Refusal(iid, "a resting order is working on this instrument; "
                                                "cancel protective legs and let the reconciler re-arm"),))

    legs = [p for p in positions
            if str(p.instrument_id) == iid and hasattr(p, "is_open") and p.is_open
            and hasattr(p, "ts_opened")]
    if not legs:
        return ContraPlan(refused=(Refusal(iid, "no open, readable position on this instrument"),))

    if iid not in broker and not broker_complete:
        # Same rule as the automatic path (#771): silence is not zero, and an operator choosing a
        # split has not waived knowing what the venue holds.
        return ContraPlan(refused=(Refusal(iid, "the broker payload did not mention this instrument, "
                                                "which is not the same as reporting zero"),))
    broker_qty = float(broker.get(iid, 0.0))
    if broker_qty != broker_qty:
        return ContraPlan(refused=(Refusal(iid, "the broker quantity for this instrument is not a "
                                                "number, so nothing here can be checked against it"),))

    long_leg = next((p for p in legs if str(p.strategy_id) == str(long_strategy)
                     and float(p.signed_qty) > 0), None)
    short_leg = next((p for p in legs if str(p.strategy_id) == str(short_strategy)
                      and float(p.signed_qty) < 0), None)
    if long_leg is None:
        refused.append(Refusal(iid, f"{long_strategy} does not hold a LONG on this instrument, so it "
                                    f"cannot give anything back"))
    if short_leg is None:
        refused.append(Refusal(iid, f"{short_strategy} does not hold a SHORT on this instrument, so "
                                    f"there is nothing to close against"))
    if refused:
        return ContraPlan(refused=tuple(refused))

    qty = float(quantity)
    if qty <= QTY_TOLERANCE:
        return ContraPlan(refused=(Refusal(iid, "the quantity must be positive"),))
    if qty > abs(float(long_leg.signed_qty)) + QTY_TOLERANCE or \
            qty > abs(float(short_leg.signed_qty)) + QTY_TOLERANCE:
        return ContraPlan(refused=(Refusal(
            iid, f"{qty:g} does not fit both legs ({long_strategy} holds "
                 f"{float(long_leg.signed_qty):+g}, {short_strategy} holds "
                 f"{float(short_leg.signed_qty):+g}); it is refused rather than clamped, because a "
                 f"resized repair is one nobody approved"),))

    return ContraPlan(pairs=(ContraPair(
        instrument_id=iid,
        long_strategy=str(long_leg.strategy_id),
        short_strategy=str(short_leg.strategy_id),
        long_position_id=str(long_leg.id),
        short_position_id=str(short_leg.id),
        quantity=qty,
        long_px=float(long_leg.avg_px_open),
        short_px=float(short_leg.avg_px_open),
        long_qty_before=float(long_leg.signed_qty),
        short_qty_before=float(short_leg.signed_qty),
        broker_qty=broker_qty,
        long_ts_opened=int(long_leg.ts_opened or 0),
        short_ts_opened=int(short_leg.ts_opened or 0),
    ),))


def plan_contra_closes(positions, broker: dict[str, float], working_orders=(),
                       known_instrument_ids=None, broker_complete: bool = False) -> ContraPlan:
    """Plan the repair. PURE — reads, decides, writes nothing.

    `positions` is the engine's own `cache.positions_open()`. `broker` maps instrument id to the
    venue's signed quantity. `working_orders` is the set of instruments with resting orders.

    SELECTION IS ON `is_open`, NEVER ON A KEY PATTERN. The durable cache retains CLOSED positions
    under the same id shape — a live scan turned up `WHD.XNYS-MANUAL-001`, a flatten that already
    ran, alongside the real legs. A pattern-driven sweep would book contra-legs against those and
    OPEN BRAND-NEW POSITIONS in lanes currently holding nothing: worse than the defect being
    repaired, and one dry-run away from being obvious.
    """
    # THE FULL INSTRUMENT UNIVERSE, not just the ids in this book. A dual-listed name has a
    # legitimate sibling id the position book may not mention, and refusing it as a stripped-venue
    # symbol would block repairs on every such name — #625 measured 64 of staging's 209 cached
    # instruments carrying two venues. Defaults to the book's own ids, which is the honest answer
    # when the caller has nothing wider to offer.
    universe = ({str(i) for i in known_instrument_ids} if known_instrument_ids is not None
                else {str(p.instrument_id) for p in positions})
    working = _validated_working(working_orders, universe)
    # BY SYMBOL-PART, because the VALIDATOR CANNOT CLOSE THIS ONE. A resting order under a held
    # sibling listing is legitimate vocabulary — it raises nothing — and a full-id membership test
    # then reads `"CGAU.XNYS" in {"CGAU.XNAS"}` as False and plans the pair with that order resting.
    # Same shares reserved, #239 guard silent again, no vocabulary error to catch. On this book the
    # same ticker is the same security.
    working_symbols = {w.rsplit(".", 1)[0] for w in working}
    by_instrument: dict[str, list] = {}
    unreadable: list[Refusal] = []
    for p in positions:
        # NOT `getattr(p, "is_open", True)`. A default of True made a shape lacking the field count
        # as OPEN — eligible for a plan that books fills against it. Absence reading as permission,
        # in the one direction where the consequence is a BOOK MUTATION rather than a missing row.
        # A Nautilus Position always has it, which is exactly the reasoning that makes the default
        # look safe; the durable cache also always had ids matching the reconstructed shape, and
        # refusing to rely on that is this module's whole subject.
        if not hasattr(p, "is_open"):
            unreadable.append(Refusal(str(p.instrument_id),
                                      "a position cannot say whether it is open; a repair must not "
                                      "be planned against a book this read cannot describe"))
            continue
        if not p.is_open:
            continue
        by_instrument.setdefault(str(p.instrument_id), []).append(p)

    pairs: list[ContraPair] = []
    evictions: list[Eviction] = []
    surplus_evictions: list[SurplusEviction] = []
    refused: list[Refusal] = list(unreadable)
    blocked = {r.instrument_id for r in unreadable}

    for instrument, legs in sorted(by_instrument.items()):
        if instrument in blocked:
            # One unreadable leg makes the whole instrument's split unknowable, not partially known.
            continue
        # A NaN QUANTITY FALLS OUT OF BOTH SIGN BUCKETS, so the instrument read as clean and was
        # skipped without a word — while unreadable OPENNESS one loop above is refused by name. The
        # same read failing loudly in one place and silently in the other is where a naked position
        # hides, and NaN is the family that disarmed a daily-loss halt elsewhere here.
        # THE EXECUTOR REFUSES A SHAPE WITHOUT `ts_opened`, so the planner must not fingerprint one
        # into an approval list an operator signs off and the executor is then guaranteed to bounce.
        # The two stages must not disagree about what they can describe.
        no_stamp = [p for p in legs if not hasattr(p, "ts_opened")]
        if no_stamp:
            refused.append(Refusal(instrument,
                                   "a position on this instrument cannot say when its current cycle "
                                   "opened, so the id-reuse check would fingerprint it as zero and "
                                   "silently agree with itself"))
            continue
        unreadable_qty = [p for p in legs if float(p.signed_qty) != float(p.signed_qty)]
        if unreadable_qty:
            refused.append(Refusal(instrument,
                                   "a leg on this instrument has a quantity that is not a number, "
                                   "so it belongs to neither side and the split cannot be read"))
            continue
        longs = [p for p in legs if float(p.signed_qty) > 0]
        shorts = [p for p in legs if float(p.signed_qty) < 0]
        # #779: THE VENUE STATED ZERO AND THE CACHE DOES NOT NET FLAT — every leg is fiction.
        # Pairing cannot express this: it retires min(longs, shorts) and strands the residual in
        # whichever lane it spared, and awarding that residual would be a guess. When the venue holds
        # NOTHING there is no residual to award, so the only correct end state is every leg flat,
        # each at its own basis, realizing nothing.
        #
        # BOTH conditions, deliberately:
        #   `broker_complete` — silence alone is never zero. #771's rule survives untouched; a caller
        #     that did not vouch for its read still gets the existing refusals.
        #   `cache_net != 0` — a book that nets flat at a flat venue is already fully expressible as
        #     pairs (#770) and KEEPS producing pairs, so the ledger's existing pair fingerprints stay
        #     meaningful.
        # A NaN broker quantity fails the `<=` comparison and falls through to the NaN refusal below.
        #
        # PLACED BEFORE the `not shorts` skip on purpose: the rule is about the VENUE, not the leg
        # composition. A longs-only phantom book at a stated-flat venue is exactly the residue that
        # skip would hide forever.
        cache_net = sum(float(p.signed_qty) for p in legs)
        stated_flat = (broker_complete
                       and abs(float(broker.get(instrument, 0.0))) <= QTY_TOLERANCE)
        if stated_flat and abs(cache_net) > QTY_TOLERANCE:
            if instrument.rsplit(".", 1)[0] in working_symbols:
                # The same #239 hazard as pairs: a resting reduce-only order sized to the PHANTOM
                # quantity oversells on trigger once the book beneath it is evicted.
                refused.append(Refusal(instrument, "a resting order is working on this instrument; "
                                                   "cancel protective legs and let the reconciler re-arm"))
                continue
            ordered = sorted(legs, key=lambda p: (str(p.strategy_id), str(p.id)))
            evictions.append(Eviction(
                instrument_id=instrument,
                legs=tuple(
                    Leg(instrument, str(p.strategy_id), str(p.id),
                        "SELL" if float(p.signed_qty) > 0 else "BUY",
                        abs(float(p.signed_qty)), float(p.avg_px_open))
                    for p in ordered
                ),
                ts_opened=tuple(int(p.ts_opened or 0) for p in ordered),
                broker_qty=float(broker.get(instrument, 0.0)),
            ))
            continue
        # THE SURPLUS RULE (#1038), before the pair rule can decline this shape as "clean": the real
        # book already agrees with the venue and one EXTERNAL long is reconciliation fiction on top.
        # It is evicted ALONE, at its own basis. `surplus_shape` is the one predicate; the executor
        # re-derives it against a fresh read. Any other over-summing shape is NAMED, never guessed.
        # A venue that did not state this instrument is NaN here: absent-with-`broker_complete` is
        # the #779 branch above (venue flat), absent-without is silence, and silence is not 12. A
        # PARTIAL read is NaN even when the instrument is present — the same gate #779 applies to a
        # present zero; a partial snapshot must not plan (or name) what the complete read would not.
        stated_qty = (float(broker[instrument]) if broker_complete and instrument in broker
                      else float("nan"))
        surplus_leg, premise = surplus_shape(legs, stated_qty)
        if surplus_leg is not None:
            if instrument.rsplit(".", 1)[0] in working_symbols:
                refused.append(Refusal(instrument, "a resting order is working on this instrument; "
                                                   "cancel protective legs and let the reconciler re-arm"))
                continue
            surplus_evictions.append(SurplusEviction(
                instrument_id=instrument,
                leg=Leg(instrument, str(surplus_leg.strategy_id), str(surplus_leg.id), "SELL",
                        abs(float(surplus_leg.signed_qty)), float(surplus_leg.avg_px_open)),
                ts_opened=int(surplus_leg.ts_opened or 0),
                broker_qty=stated_qty,
                real_lanes_qty=float(premise),
            ))
            continue
        if premise is not None:                           # an over-summing shape with a stated reason
            refused.append(Refusal(instrument, premise))
            continue
        if not shorts:
            continue                                    # a clean instrument, silently
        if instrument.rsplit(".", 1)[0] in working_symbols:
            # #239 rests protective stops sized to the PHANTOM quantities. A reduce-only order
            # against a position about to shrink oversells when it triggers, turning a book repair
            # into a real market event.
            refused.append(Refusal(instrument, "a resting order is working on this instrument; "
                                               "cancel protective legs and let the reconciler re-arm"))
            continue
        if len(legs) > 2:
            # A RESIDUAL is what makes the pairing a guess. When the venue holds NOTHING and the
            # cache nets to nothing, every leg must close and there is no residual to award — so
            # there is no choice being made (#770). CGAU on paper: +168 / -88 / -80, and
            # 168 = 88 + 80 is the only outcome. Each leg still closes at ITS OWN basis, so no
            # pairing realizes anything either; the order legs are matched in is arithmetic, not
            # attribution.
            #
            # BOTH CONDITIONS, and the broker one is not redundant. A cache that nets flat while the
            # venue still holds shares is a NET disagreement, and closing every leg there would walk
            # the book AWAY from the venue — the opposite of a repair.
            #
            # IF ONLY SOME PAIRS BOOK, the residue is self-healing rather than corrupt: dropping one
            # pair of a flatten leaves a book that still nets to zero, with fewer legs, which the
            # ordinary two-leg path finishes on the next run.
            cache_net = sum(float(p.signed_qty) for p in legs)
            flat_at_venue = (instrument in broker or broker_complete) and \
                abs(float(broker.get(instrument, 0.0))) <= QTY_TOLERANCE
            if not (flat_at_venue and abs(cache_net) <= QTY_TOLERANCE):
                refused.append(Refusal(instrument, f"more than two open legs ({len(legs)}); the correct "
                                                   f"pairing is not knowable from the position book"))
                continue
            # Greedy match: every long against every short until both sides are exhausted. The
            # result is identical whatever order they are taken in, because everything reaches zero.
            remaining_long = [[p, abs(float(p.signed_qty))] for p in sorted(
                longs, key=lambda x: str(x.id))]
            remaining_short = [[p, abs(float(p.signed_qty))] for p in sorted(
                shorts, key=lambda x: str(x.id))]
            li = si = 0
            while li < len(remaining_long) and si < len(remaining_short):
                lp, lq = remaining_long[li]
                sp, sq = remaining_short[si]
                take = min(lq, sq)
                if take > QTY_TOLERANCE:
                    pairs.append(ContraPair(
                        instrument_id=instrument,
                        long_strategy=str(lp.strategy_id),
                        short_strategy=str(sp.strategy_id),
                        long_position_id=str(lp.id),
                        short_position_id=str(sp.id),
                        quantity=take,
                        long_px=float(lp.avg_px_open),
                        short_px=float(sp.avg_px_open),
                        long_qty_before=float(lp.signed_qty),
                        short_qty_before=float(sp.signed_qty),
                        broker_qty=float(broker.get(instrument, 0.0)),
                        long_ts_opened=int(lp.ts_opened or 0),
                        short_ts_opened=int(sp.ts_opened or 0),
                    ))
                remaining_long[li][1] -= take
                remaining_short[si][1] -= take
                if remaining_long[li][1] <= QTY_TOLERANCE:
                    li += 1
                if remaining_short[si][1] <= QTY_TOLERANCE:
                    si += 1
            continue
        if not longs:
            refused.append(Refusal(instrument, "a short with no offsetting long — either the long "
                                               "is somewhere this read cannot see, or the short is "
                                               "real; booking a contra-leg alone moves the net"))
            continue
        if instrument not in broker and not broker_complete:
            # ABSENCE IS NOT ZERO. Missing from the payload might be genuinely flat or a truncated
            # read, and the two are indistinguishable from here.
            #
            # UNLESS THE CALLER STATES THE READ WAS A COMPLETE SNAPSHOT (#771). Then it is a third
            # state, not a relaxation of this one: "asked, and told nothing is held" rather than
            # "never told us". A venue that returns only NON-ZERO positions says zero by omission,
            # and discarding that made the repair blind to its own subject — a fully-offset pair
            # NETS TO ZERO, so every clean minted pair on paper (MRVL, RBRK, TOST, VEEV) was refused
            # by the one condition that identifies it. A detector that recognises its subject by a
            # property the defect destroys can never fire.
            #
            # The flag defaults FALSE so no existing caller silently changes meaning, and it is the
            # CALLER's assertion because only the caller knows whether its read succeeded in full.
            # Passing it on a truncated or errored payload reintroduces exactly the defect the
            # refusal above exists to prevent.
            refused.append(Refusal(instrument, "the broker payload did not mention this instrument, "
                                               "which is not the same as reporting zero"))
            continue

        # Silence under a stated-complete read IS zero; absence under an unstated read never
        # reaches here, because the refusal above already continued.
        broker_qty = float(broker.get(instrument, 0.0))
        if broker_qty != broker_qty:
            # NaN survives every comparison written for numbers: `nan < 0` is False at the check
            # below and `abs(net - nan) > tol` is False at the agreement check, so an unusable value
            # slipped BOTH and reached the approved list an operator signs off. The executor refuses
            # it later, but an approval step showing unusable numbers is worse than one showing fewer.
            refused.append(Refusal(instrument,
                                   "the broker quantity for this instrument is not a number, so "
                                   "neither the broker-short check nor the net agreement check "
                                   "means anything for it"))
            continue
        if broker_qty < 0:
            # THE PREMISE FAILS HERE. The repair assumes the account is flat or long and only the
            # split is wrong. A genuinely short broker position is a crisis of its own, and the
            # mirror arithmetic quietly accommodates it: long +20 / short -100 against a broker at
            # -80 nets correctly, so a 20-share pair plans and a REAL net short is left standing,
            # unremarked, on a book that now looks repaired.
            refused.append(Refusal(instrument, f"the broker itself holds a SHORT "
                                               f"({broker_qty:g}) in a long-only "
                                               f"book; that is its own incident, not a split to fix"))
            continue

        long_leg, short_leg = longs[0], shorts[0]
        net = float(long_leg.signed_qty) + float(short_leg.signed_qty)
        if abs(net - broker_qty) > QTY_TOLERANCE:
            # The premise of the repair is that the account is FLAT and only the split is wrong.
            refused.append(Refusal(instrument, f"the cache net ({net:g}) does not agree with the "
                                               f"broker ({broker_qty:g}); a "
                                               f"contra-close here would move a real position"))
            continue

        pairs.append(ContraPair(
            instrument_id=instrument,
            long_strategy=str(long_leg.strategy_id),
            short_strategy=str(short_leg.strategy_id),
            long_position_id=str(long_leg.id),
            short_position_id=str(short_leg.id),
            quantity=min(abs(float(long_leg.signed_qty)), abs(float(short_leg.signed_qty))),
            long_px=float(long_leg.avg_px_open),
            short_px=float(short_leg.avg_px_open),
            long_qty_before=float(long_leg.signed_qty),
            short_qty_before=float(short_leg.signed_qty),
            broker_qty=broker_qty,
            long_ts_opened=int(long_leg.ts_opened or 0),
            short_ts_opened=int(short_leg.ts_opened or 0),
        ))

    return ContraPlan(pairs=tuple(pairs), refused=tuple(refused), evictions=tuple(evictions),
                      surplus_evictions=tuple(surplus_evictions))
