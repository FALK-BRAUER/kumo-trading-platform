"""Writing history — and the gate that must pass before any of it is trusted (#734).

THE GATE IS THE POINT OF THIS FILE. Reconstruction derives a lane's book from the broker's fills;
the engine derives it from Nautilus's own cache. At `T = now` those two must produce the SAME per-lane
book, because they are describing the same instant by different routes. If they disagree today, every
historical row the reconstruction writes is wrong in the same way — and nobody would find out, because
history has nothing to be checked against.

So `verify_at_now` runs FIRST and the backfill refuses to write when it fails. That is the issue's own
acceptance condition, and it is the only test of the reconstruction that can run against live data.

WHY PER-LANE AND NOT PER-ACCOUNT. An account-level check cancels exactly the error most worth
catching: a lot attributed to the wrong lane leaves the account total untouched. The equality has to
be lane by lane or it is not testing attribution at all.

QUANTITY IS THE GATE. BASIS IS NOT, AND CANNOT BE — the two sides use different, both-correct
conventions. Nautilus's `avg_px_open` is the weighted average of the OPENING fills and does not move
when part of the position is closed; the FIFO residue drops the oldest lots. Measured against the
installed package: buy 10@100, buy 10@120, sell 10 gives `avg_px_open` 110.0 and a FIFO residue of
120.0 — same fills, both right, guaranteed to differ. Any position with a partial close after
multi-price entries would fail a basis equality with NO defect present, and the live book contains
exactly those. The divergence is structural and unbounded, so it is RECORDED as a #370-style
observation rather than tolerance-fudged into a pass.

Unrealized is not compared either: it is qty, basis and mark multiplied back together, so comparing
it only adds the mark-source difference as noise.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from api.eod_observer import observe_lanes
from api.eod_reconstruct import UNCLAIMED, reconstruct_lanes

#: How far two bases may differ and still count as agreeing. Money is rounded at several hops (venue
#: reports, Nautilus's Decimal, our float), so exact equality would fail on arithmetic rather than on
#: substance. A cent per share is far below anything that would matter and far above rounding.
BASIS_TOLERANCE = 0.01

#: Quantities are whole or fractional shares and both sides count the same fills — a mismatch here is
#: never rounding, it is a different book.
QTY_TOLERANCE = 1e-6


def fingerprint(activities) -> str:
    """Identifies the LEDGER a gate run was certified against.

    The gate's deepest value is that it validates the ledger's REACH: a fill history that does not
    reach account inception makes every reconstructed day wrong — missing opening lots shrink
    positions or invent phantom shorts — invisibly and unboundedly, and at T=now the engine holds the
    real book so a truncated ledger cannot agree with it.

    That value evaporates entirely if the certified ledger and the written-from ledger may differ.
    Verify against fetch A, backfill against fetch B truncated by a lookback window, and `agrees` is
    HONESTLY true while every row is wrong — no mistake visible anywhere in the call. So the report
    carries this and the backfill re-derives it.

    THE WHOLE ACTIVITY, NOT THE FIELDS THE MATCHER READS. The first version hashed
    transaction_time / symbol / side / qty / price — every field `_match` consumes, and NOTHING the
    ATTRIBUTION consumes. Strip the order-id field and the fingerprints are EQUAL (measured: True),
    so a gate certified against an id-bearing fetch would pass a backfill run against an id-stripped
    one, honestly, while every lot landed UNCLAIMED at coverage 0.0.

    The repair would then DOUBLE-COUNT rather than correct: UNCLAIMED rows and per-lane rows differ
    in `strategy_id`, so `uq_eod_observation_base` keeps BOTH and any per-instrument sum across lanes
    counts the position twice — and base rows are append-only, so it is not undoable. Hashing
    everything makes a spurious mismatch fail CLOSED and loud; the narrow version failed OPEN and
    silent, and this repo picks the first direction every time.

    ORDER-INDEPENDENT in both senses — across activities, because the same fills fetched in a
    different order are the same ledger, and within one activity, because a dict built by a different
    code path with the same content is the same fill. Refusing either would be a false alarm the
    operator learns to route around, which is how a real detector gets muted. Content-addressed
    rather than a count: a count cannot tell a truncated ledger from one with a fill swapped in.
    """
    h = hashlib.sha256()
    for line in sorted(
        "|".join(f"{k}={a[k]!r}" for k in sorted(a)) for a in activities
    ):
        h.update(line.encode())
    return h.hexdigest()[:32]


@dataclass(frozen=True)
class Disagreement:
    lane: str
    instrument_id: str
    what: str
    engine: float | None
    reconstructed: float | None


@dataclass(frozen=True)
class BasisDivergence:
    """The two conventions, side by side. NOT a failure — a fact worth keeping.

    `avg_px_open` (opening-weighted, unchanged by partial closes) and the FIFO residue basis answer
    different questions and both are correct. Recording the pair is the same move as storing both
    bases in the observation row: a stored conclusion could not disagree with anything, and the
    divergence is only interesting because it can be seen.
    """

    lane: str
    instrument_id: str
    engine_avg_px_open: float
    fifo_residue_basis: float


@dataclass(frozen=True)
class AgreementReport:
    """Whether the reconstruction may be trusted to write history.

    THREE STATES. `agrees` is not a bool the caller can shrug at: `compared == 0` means the gate never
    actually ran — an empty book, or a cache that returned nothing — and that is NOT a pass. A gate
    that reports success because it examined nothing is the vacuity this repo keeps finding.
    """

    compared: int
    #: How many (lane, instrument) pairs the ENGINE holds. `compared` must equal it, or the gate
    #: examined only part of the book — non-empty is not complete.
    engine_pairs: int
    #: WHICH LEDGER THIS CERTIFIES. None means "not produced by a gate run" — a hand-built report,
    #: which is a third state and not a variant of pass. See `fingerprint`.
    activities_fingerprint: str | None = None
    disagreements: tuple[Disagreement, ...] = field(default_factory=tuple)
    #: Observed, never gated on. See `BasisDivergence`.
    basis_divergences: tuple[BasisDivergence, ...] = field(default_factory=tuple)

    @property
    def agrees(self) -> bool:
        # `compared >= engine_pairs` is UNREACHABLE through `verify_at_now`, which asserts the
        # stronger invariant directly — mutation proved both this conjunct and `engine_pairs` itself
        # could be deleted with the whole suite green. It is kept because this dataclass is public
        # and a future caller could build one by hand, and it is now tested DIRECTLY rather than
        # through a path that cannot reach it.
        return self.compared > 0 and self.compared >= self.engine_pairs and not self.disagreements

    @property
    def verdict(self) -> str:
        if self.compared == 0:
            return "INCONCLUSIVE: nothing was compared, so this is not a pass"
        if self.compared < self.engine_pairs:
            return (f"INCOMPLETE: compared {self.compared} of the engine's {self.engine_pairs} "
                    f"lane-positions — non-empty is not complete")
        if self.disagreements:
            return f"DISAGREES on {len(self.disagreements)} of {self.compared} lane-positions"
        return f"agrees on all {self.compared} lane-positions"


def _by_key(observation) -> dict[tuple[str, str], object]:
    """Keyed by (lane, instrument). A dict comprehension would silently DROP a duplicate and compare
    the survivor, so the collision is asserted instead — unreachable under NETTING, where each
    strategy-instrument pair has exactly one net position, and one line to close forever."""
    out: dict[tuple[str, str], object] = {}
    for lane in observation:
        for p in lane.positions:
            key = (p.strategy_id, p.instrument_id)
            assert key not in out, f"{key} appears twice — NETTING gives one net position per pair"
            out[key] = p
    return out


def verify_at_now(cache, activities, *, as_of: str, strategy_of=None, instrument_of=None) -> AgreementReport:
    # `as_of` is a whole trading DATE (YYYY-MM-DD) — `reconstruct_lanes` enforces it, and for "now"
    # that means today's date, which its prefix match takes to include every fill so far today.
    """THE ACCEPTANCE GATE. Do the two derivations agree about the book right now?

    `cache` is the engine's Nautilus cache; `activities` the broker's full fill history. Both describe
    this instant, by routes that share nothing — which is what makes agreement meaningful and
    disagreement diagnostic.

    `instrument_of` maps the broker's bare `symbol` into the engine's `AEM.XNYS` namespace — see
    `reconstruct_lanes`. Without it the two key sets are DISJOINT and this gate can never pass.

    THERE IS NO UNCLAIMED EXEMPTION. This docstring used to promise one, and described the code as it
    stood before the G2 fix: "excluded from the comparison, deliberately". That is now false in every
    clause, and it survived the fix because the change touched the code and half the prose — the
    comment-that-reads-as-safety class, in the function whose whole job is to be trusted. An
    unattributed lot at T=now is a DISAGREEMENT: the live book postdates 2026-08-17, where the
    fill-to-order join measures 100%, so a lot the reconstruction cannot place means the join broke.

    KNOWN BLIND SPOT — A SYMMETRIC LANE SWAP PASSES. Quantity-only gating cannot see an EQUAL-quantity
    mis-attribution: engine {MOM: 10 AEM, BCT: 10 AEM} against a reconstruction that swaps them has
    the same keys and the same quantities, so it agrees. The 10/5 case below is caught; the 10/10 case
    is not, and no quantity comparison could catch it. A basis divergence often shows it, which is one
    more reason those are recorded rather than discarded — but they are not gated on, so this is a
    limit and not a safety net.
    """
    observed = observe_lanes(cache)
    engine = _by_key(observed)
    rebuilt_obs = reconstruct_lanes(activities, as_of=as_of, marks={}, strategy_of=strategy_of,
                                    instrument_of=instrument_of)
    rebuilt = _by_key(rebuilt_obs)

    disagreements: list[Disagreement] = []
    divergences: list[BasisDivergence] = []

    # A PARTIALLY READ BOOK IS NOT AN AGREEING BOOK. `observe_lanes` reports positions it could not
    # describe at all, and `reconstruct_lanes` reports symbols it could not resolve. Certifying
    # agreement across the survivors would read GREEN on exactly the case where the engine itself
    # recorded a gap — and if the fill ledger lacks those positions too (an adopted or transferred
    # position with no fills), nothing else would ever notice. Assert on the RECORD of the attempt.
    for what in tuple(getattr(observed, "skipped", ()) or ()):
        disagreements.append(Disagreement("?", str(what), "the engine could not be described", None, None))
    for what in tuple(rebuilt_obs.skipped or ()):
        disagreements.append(Disagreement("?", str(what), "could not be resolved to an instrument id", None, None))

    # THE UNION OF BOTH SIDES, minus UNCLAIMED only on the RECONSTRUCTION side. Iterating the
    # reconstruction's keys and skipping UNCLAIMED would hide the failure this gate exists for: a
    # lane whose fill-to-order join failed appears as UNCLAIMED, its engine key is never examined,
    # and the gate passes. Keeping every ENGINE key in scope means such a lane surfaces as
    # "held by only one derivation" instead of vanishing.
    # The `k not in engine` clause is DEAD and deliberately kept: Nautilus tags every position it
    # holds, so the engine cannot produce an UNCLAIMED lane and the check is unreachable. Mutation
    # confirms it (removing it changes nothing). It stays as the statement of what would have to be
    # true for the filter to be safe if that ever changed.
    keys = {k for k in set(engine) | set(rebuilt) if not (k[0] == UNCLAIMED and k not in engine)}

    # EVERY ENGINE KEY MUST BE EXAMINED. This is an INVARIANT of the union above, not a condition —
    # `AgreementReport.agrees` used to branch on it, and mutation proved the branch unreachable,
    # which made it a detector that could never fire. An assertion says where to find out if that
    # ever changes.
    #
    # DELETING THIS LINE LEAVES THE SUITE GREEN, and that is correct rather than a gap: nothing can
    # currently violate it, so nothing can test it. Same standing as the dead `k not in engine`
    # clause above. It is a tripwire for a future edit to `keys`, not a check on today's inputs — and
    # saying so is the point, because an unkillable mutant that nobody has labelled is
    # indistinguishable from an untested one.
    assert set(engine) <= keys, f"engine keys dropped before comparison: {sorted(set(engine) - keys)}"

    for key in sorted(keys):
        lane, instrument = key
        e, r = engine.get(key), rebuilt.get(key)
        if e is None or r is None:
            disagreements.append(Disagreement(
                lane, instrument, "held by only one derivation",
                None if e is None else e.qty, None if r is None else r.qty,
            ))
            continue
        # NOT `e.qty or 0.0`. A None quantity is never reachable here — `observe_lanes` routes an
        # unreadable one into `skipped` above — and zero-filling it would read "we could not tell"
        # as "flat", in the gate, which is the shape this whole file exists to refuse.
        if abs(e.qty - r.qty) > QTY_TOLERANCE:
            disagreements.append(Disagreement(lane, instrument, "quantity", e.qty, r.qty))
        # OBSERVED, NOT GATED. See BasisDivergence: the two conventions legitimately differ.
        eb, rb = e.avg_px_engine, r.avg_px_engine
        if eb is not None and rb is not None and abs(eb - rb) > BASIS_TOLERANCE:
            divergences.append(BasisDivergence(lane, instrument, eb, rb))

    # ANY UNCLAIMED AT T=NOW IS AN ATTRIBUTION FAILURE, not an exemption. The whole live book
    # postdates 2026-08-17, where the venue-order-cache join is 100% — so a lot the reconstruction
    # could not attribute today means the join broke, and letting it pass silently is precisely the
    # hole this gate is for. Exceptions must be named in the caller, dated, never assumed.
    for (lane, instrument), pos in rebuilt.items():
        if lane == UNCLAIMED:
            disagreements.append(Disagreement(
                lane, instrument, "unattributed at T=now — the fill-to-order join did not resolve",
                None, pos.qty,
            ))

    return AgreementReport(
        compared=len(keys),
        engine_pairs=len(engine),
        activities_fingerprint=fingerprint(activities),
        disagreements=tuple(disagreements),
        basis_divergences=tuple(divergences),
    )
