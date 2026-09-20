"""Walking past sessions and writing what each lane held — the backfill half of #734.

WHAT THIS DOES. `reconstruct_lanes` answers one day. This walks a list of sessions, prices each one
from that day's bars, and writes `reconstructed-eod` rows so #699's 1W/1M/3M headline has a base to
subtract from on the day it ships instead of an em dash for a month.

THE GATE IS A PRECONDITION, NOT A STEP. `backfill_sessions` takes an `AgreementReport` and REFUSES
to write anything unless it agrees. It does not run the gate itself, because the gate needs a live
Nautilus cache and this runs offline — but taking the report as a required argument means a caller
cannot reach the write path without having produced one. A gate you can forget to call is decoration.

WHY THE GATE CATCHES THE WORST FAILURE. If the fill ledger does not reach account inception, every
reconstructed day is wrong — the missing opening lots make positions look smaller, or invent phantom
shorts where a close has no lot to match against. That error is invisible day by day and unbounded in
size. It is visible at T=now, because the engine holds the real book: a truncated ledger cannot agree
with it. So the same check that validates today's derivation also validates the ledger's REACH, which
is the one thing no per-day inspection could establish.

WHAT IT REFUSES.
- A session before `ledger_start`. Reconstructing there returns an EMPTY book, which reads as "every
  lane was flat" when the truth is "we cannot see". No row at all is the correct record of a day
  nobody could observe — a manifest saying `observed, 0` there would be the absence-as-flatness
  defect this table was built to end.
- A session whose marks source RAISED. Returning no marks and raising are different states: the
  first says "no bar for that symbol", which is recorded per-position as an unknown valuation; the
  second says we do not know whether bars exist. Writing the day's quantities anyway would be
  correct data with a silently degraded valuation, and the day cannot be rewritten afterwards
  (base rows are append-only). Refusing costs nothing, because nothing was written and a re-run
  after the bar source is fixed writes normally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from api.eod_backfill import AgreementReport, fingerprint
from api.eod_observation_store import METHOD_VERSION, observation_rows, manifest_rows
from api.eod_reconstruct import reconstruct_lanes

#: What a reconstructed row is. Kept here rather than passed in: a caller free to choose the kind
#: could write reconstructions as `close`, and the whole point of the column is that a close-to-close
#: series can be told apart from a rebuilt one.
CAPTURE_KIND = "reconstructed-eod"
PROVENANCE = "reconstruction"
#: A reconstructed day is priced from that day's historical bar, never from a live quote.
MARK_SOURCE = "reconstructed-bar"


@dataclass(frozen=True)
class SessionResult:
    """What happened to ONE session, in states that cannot be confused with each other.

    `wrote 0 rows` is three different facts — every lane was flat, the rows were already there from
    an earlier run, or the day was refused — and a caller reading a single integer cannot tell them
    apart. That is the same shape as `WriteResult.skipped`, one level up.
    """

    session_date: str
    #: "written" | "already-present" | "refused"
    action: str
    reason: str
    rows_written: int = 0
    rows_skipped: int = 0
    lanes: int = 0
    #: Positions carried at that date whose valuation is UNKNOWN because no bar priced them. Not an
    #: error — a fact about the row, surfaced so a period return computed off these rows knows how
    #: much of the book it could actually value.
    unpriced: int = 0
    #: The store COMPUTES these and the runner used to drop them — computed-and-discarded, in the
    #: file whose own comments cite the rule. A run that wrote no observations but did write
    #: manifests is a real and different outcome from one that wrote neither.
    manifests_written: int = 0
    lanes_attempted: int = 0


@dataclass(frozen=True)
class BackfillReport:
    gate: AgreementReport
    sessions: tuple[SessionResult, ...] = field(default_factory=tuple)
    #: Why the whole run was refused before any session was attempted, or None. Distinct from a
    #: per-session refusal: this one says the RUN was never eligible.
    refusal: str | None = None
    #: Dates INSIDE the walked range that carry fills and are not themselves sessions. Their fills
    #: move the book and appear in no session's delta, so `session_fill_qty` reads 0 across a
    #: quantity jump — which is the corporate-action signature. Reported because the arithmetic
    #: cannot be fixed without inventing a session that did not happen, and a named gap is a
    #: different thing from a wrong number that looks right.
    uncovered_fill_dates: tuple[str, ...] = field(default_factory=tuple)

    @property
    def written(self) -> int:
        return sum(s.rows_written for s in self.sessions)

    @property
    def refused(self) -> tuple[SessionResult, ...]:
        return tuple(s for s in self.sessions if s.action == "refused")

    @property
    def summary(self) -> str:
        if self.refusal:
            return f"NOTHING WRITTEN — {self.refusal}"
        if not self.gate.agrees:
            return f"NOTHING WRITTEN — the acceptance gate did not pass: {self.gate.verdict}"
        gap = ""
        if self.uncovered_fill_dates:
            gap = (f"; WARNING {len(self.uncovered_fill_dates)} fill date(s) not covered by any "
                   f"session, so their quantity moves appear in no delta: "
                   f"{', '.join(self.uncovered_fill_dates)}")
        return (
            f"{self.written} row(s) across {len(self.sessions)} session(s); "
            f"{len(self.refused)} refused; gate {self.gate.verdict}{gap}"
        )


def session_deltas(
    activities: list[dict],
    session_date: str,
    *,
    instrument_of,
    strategy_of=None,
) -> dict[tuple[str, str], float]:
    """How much each lane's position MOVED during that session — the matcher's own answer, not a
    second reading of the same fills.

    `session_fill_qty` exists so a reader can tell a position whose VALUE moved from one whose SIZE
    moved, and so the invariant `qty_t == qty_{t-1} + session_fill_qty_t + transfers_t` can detect a
    corporate action. Leaving it at zero for every historical row would be a specific false statement
    — "nothing traded that day" — rather than a missing one.

    THE FIRST VERSION WALKED THE FILLS ITSELF AND WAS WRONG TWICE, in ways that cancelled out of every
    test I wrote because all of them were same-lane and pre-namespace:

    - IT FOLLOWED THE FILL'S TAG, NOT THE LOT'S. Position deltas move by the lot's OPENING strategy —
      #292's whole design, and what `_match` walks. MOMENTUM opens 10 AEM and MANUAL sells 4:
      MOMENTUM's position drops to 6, but the -4 was keyed under `(MANUAL, AEM)`, a pair with no
      observation row, where `observation_rows`' `.get(..., 0.0)` discarded it. MOMENTUM's row then
      read 0 on the day it sold 4, and the corporate-action invariant was violated by an ordinary
      cross-lane sell — TECHIVOL's incident shape.
    - IT KEYED ON THE BARE SYMBOL while the rows key on the resolved `AEM.XNYS`, so after the
      namespace fix the lookup missed on every row and every delta silently became 0.0.

    Both are gone rather than corrected, because the fix for "two derivations of one fact disagree"
    is one derivation. The difference between the book at D and the book at D-1 is computed by the
    same matcher, in the same namespace, attributed the same way — it cannot drift from the rows it
    annotates, and the sign convention is no longer written down twice.

    THE PRIOR BOOK IS THE DAY BEFORE, NEVER THE PREVIOUS SESSION IN THE LIST. Sessions are not
    contiguous — weekends, holidays, or a caller backfilling only Fridays — and anchoring on the list
    would make a Monday's delta swallow the whole preceding week.

    KNOWN LIMIT: a position closed to FLAT has no row that day (flat symbols are absent by design),
    so its closing delta lands nowhere and the invariant cannot be checked on the day a position
    vanishes. That is deliberate — a zero-qty row would force every consumer to decide whether zero
    means flat or unknown — and #699's headline does not need it, because a closed position's
    contribution is realized P&L and comes from the realized sweep. Stated here rather than left for
    someone to discover from a column that does not add up.
    """
    prior_date = (date.fromisoformat(session_date) - timedelta(days=1)).isoformat()
    now = _qty_by_key(reconstruct_lanes(activities, as_of=session_date, marks={},
                                        strategy_of=strategy_of, instrument_of=instrument_of))
    prior = _qty_by_key(reconstruct_lanes(activities, as_of=prior_date, marks={},
                                          strategy_of=strategy_of, instrument_of=instrument_of))
    return {k: now.get(k, 0.0) - prior.get(k, 0.0) for k in set(now) | set(prior)}


def _qty_by_key(observation) -> dict[tuple[str, str], float]:
    return {(p.strategy_id, p.instrument_id): p.qty for lane in observation for p in lane.positions}


async def backfill_sessions(
    store,
    activities: list[dict],
    sessions,
    *,
    gate: AgreementReport,
    marks_for,
    strategy_of=None,
    #: Bare broker symbol -> the engine's `AEM.XNYS` id. REQUIRED, with no default, for the same
    #: reason `currency` has none three lines down: a default of None means IDENTITY, and identity is
    #: the one value under which the wiring cannot be seen broken. Review demonstrated it live — the
    #: gate called WITH a resolver passes honestly while the runner called WITHOUT one writes every
    #: row bare, and every test stays green because the tests omitted it too. A caller that genuinely
    #: wants identity must now write `lambda s: s`, which is a decision instead of an accident.
    instrument_of,
    currency: str,
    ledger_start: str,
    known_lanes=(),
    snapshot_ts_for,
    method_version: str = METHOD_VERSION,
) -> BackfillReport:
    """Write `reconstructed-eod` rows for each session, oldest first.

    `gate` MUST agree or nothing is written — see the module docstring.

    `ledger_start` is the first date `activities` is known to COVER, and the caller must state it.
    It is deliberately not inferred from the earliest fill in the list: that is a property of the
    fetch, not of the account, and a ledger truncated by a lookback window would report its own
    truncation point as the account's inception — the inference would be most confident exactly when
    it is most wrong.

    `marks_for(session_date) -> dict[symbol, close]` and `snapshot_ts_for(session_date) -> int` are
    the caller's; both are asked once per session.

    `known_lanes` gets each lane a manifest row even on days it held nothing, which is what makes a
    flat day distinguishable from an unobserved one.
    """
    # THE REPORT MUST CERTIFY *THESE* ACTIVITIES. Holding an agreeing report is not the same as
    # having gated this ledger: verify against a full fetch, backfill against one truncated by a
    # lookback window, and `agrees` is honestly true while every row is wrong. Nothing in the call
    # would look wrong. The docstring above used to claim a caller "cannot reach the write path
    # without having produced one" — true, and beside the point, because producing one takes no gate
    # run at all and certifying the wrong ledger takes no mistake.
    if gate.activities_fingerprint is None:
        return BackfillReport(gate=gate, sessions=(), refusal=(
            "the gate report was not produced by a gate run — it carries no fingerprint, and "
            "'never certified' is not a weaker form of 'certified'"))
    if gate.activities_fingerprint != fingerprint(activities):
        return BackfillReport(gate=gate, sessions=(), refusal=(
            "the gate was certified against a different set of activities than the ones being "
            "written from — its verdict says nothing about this ledger's reach"))
    if not gate.agrees:
        # REFUSE THE WHOLE RUN, not each session. A gate failure means the derivation itself is
        # wrong, so every day it would write is wrong in the same way — and history has nothing to
        # be checked against afterwards, which is why this is the one place it can still be caught.
        return BackfillReport(gate=gate, sessions=())

    walked = sorted(sessions)
    results: list[SessionResult] = []
    for session_date in walked:
        if session_date < ledger_start:
            results.append(SessionResult(
                session_date, "refused",
                f"before the fill ledger's coverage ({ledger_start}) — reconstructing here would "
                f"report an empty book as 'every lane flat'",
            ))
            continue
        try:
            marks = marks_for(session_date)
        except Exception as exc:                      # noqa: BLE001 — the reason is the point
            results.append(SessionResult(
                session_date, "refused",
                f"the marks source raised, so it is unknown whether bars exist for this day: "
                f"{type(exc).__name__}: {exc}"[:200],
            ))
            continue

        observation = reconstruct_lanes(
            activities, as_of=session_date, marks=marks or {}, strategy_of=strategy_of,
            instrument_of=instrument_of,
        )
        lanes = observation.lanes
        rows = observation_rows(
            session_date, CAPTURE_KIND, snapshot_ts_for(session_date), lanes,
            currency=currency,
            mark_source=MARK_SOURCE,
            provenance=PROVENANCE,
            method_version=method_version,
            session_fills=session_deltas(activities, session_date,
                                         strategy_of=strategy_of, instrument_of=instrument_of),
        )
        manifests = manifest_rows(
            session_date, CAPTURE_KIND, snapshot_ts_for(session_date), lanes,
            known_lanes=known_lanes,
            skipped=observation.skipped,
            provenance=PROVENANCE,
            method_version=method_version,
        )
        result = await store.write(rows, manifests)
        results.append(SessionResult(
            session_date,
            # THREE STATES, because the comment here used to promise three and the expression gave
            # two: ALL-present mapped to `already-present` and SOME-present fell through to
            # `written` — the exact ambiguity the comment said must not exist, with only the reason
            # string carrying it. Partial presence is a real surprise (two captures racing for one
            # slot, or a re-run after a method_version change that only some rows carry) and it
            # deserves its own name rather than the name of the healthy case.
            _action(rows, result),
            f"{result.written} written, {result.skipped} already present",
            rows_written=result.written,
            rows_skipped=result.skipped,
            lanes=len(lanes),
            unpriced=sum(lane.unpriced for lane in lanes),
            manifests_written=result.manifests_written,
            lanes_attempted=result.lanes_attempted,
        ))
    return BackfillReport(gate=gate, sessions=tuple(results),
                          uncovered_fill_dates=_uncovered_fill_dates(activities, walked))


def _uncovered_fill_dates(activities, walked) -> tuple[str, ...]:
    """Fill dates inside the walked range that are not themselves sessions.

    ONLY INSIDE THE RANGE. A backfill of one week must not complain about every fill in the other
    five — outside the walked span there is no claim of coverage to violate, and a detector that
    fires on every run is muted within a week. That is the notifier-death pattern this repo has
    already paid for once.
    """
    if not walked:
        return ()
    first, last = walked[0], walked[-1]
    sessions = set(walked)
    dates = {str(a.get("transaction_time") or "")[:10] for a in activities}
    return tuple(sorted(d for d in dates if d and first <= d <= last and d not in sessions))


def _action(rows, result) -> str:
    if not rows or result.written == len(rows):
        return "written"
    return "already-present" if result.written == 0 else "partially-present"
