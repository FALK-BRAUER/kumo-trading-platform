"""What the engine ASKED FOR and did not get — as standing state, not a log line.

WHY THIS EXISTS. On 2026-08-31 half of staging's book could not be priced — 11 of 22 held positions
with no `last_px` — and every automated surface reported healthy. `/health` said `status: ok`. The
venue had refused 275 subscription requests (`10089 requires additional subscription`) and 136 more
for permissions, the engine logged each one, and nothing added up. The defect was found four days
later from a phone screenshot, because the UI happened to render a stale-feed badge.

Operator: "failing requests in general can go to health with a note."

THAT GENERALITY IS THE POINT. The specific failures differ — a market-data subscription refused, a
bar request answered "instrument not found", a price lookup with nothing behind it — but the SHAPE is
one: we asked the outside world for something, it declined, and the consequence was invisible because
each site handled its own failure locally and correctly.

A REQUEST THAT FAILS IS NOT THE SAME AS ONE NEVER MADE, and neither is the same as one that
succeeded. Three states — the rule this codebase keeps rediscovering — and the middle one had no
home. `0 of 0` is not `0 of 4`.

COUNTED AND COALESCED, never a stream. 275 identical refusals are ONE condition with a count, not 275
events; a surface that grew per occurrence would be scrolled past, which is the same silence at the
other extreme.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FailedRequest:
    """One kind of request that is failing, with a note an operator can act on."""

    kind: str
    #: What was asked for — a symbol, an instrument id, a subscription name. Coalesced on this, so
    #: "no market data permission" for 132 symbols is 132 rows, while one symbol refused 275 times is
    #: one row with count 275.
    subject: str
    count: int = 0
    #: The venue's own words where there are any. An operator reading "requires additional
    #: subscription for API" can act; "request failed" cannot be acted on by anyone.
    note: str = ""
    first_ts: int = 0
    last_ts: int = 0


@dataclass
class Recurrence:
    """What SURVIVES the per-pass clear for one (kind, subject) (#908).

    The row table answers "what is true NOW" and is cleared every protection pass, which made its
    `count`/`first_ts`/`last_ts` dead for that kind: structurally `count: 1`, `first_ts == last_ts`,
    for a refusal that had recurred every 60 s for as long as two lanes held a name — the only
    evidence of 55 naked shares, understated by construction (2026-09-11 01:15 SGT).

    A PASS is delimited by `clear_kind(kind)`, so this counts EVALUATED passes, not minutes: a pass that
    returned before its clear (broker unreadable, outside RTH) neither increments nor resets — UNKNOWN
    must not count (#873). Two numbers, because a consecutive count cannot see an INTERMITTENT refusal:
    `consecutive_passes` resets on a pass that records nothing for the key; `total_passes` and
    `recurring_since_ns` never do while the recorder lives. Named apart from `first_ts`/`count` on the
    same row (one word apart, different facts) and from #907's RESETTING `streak_started_ns`.
    """

    consecutive_passes: int = 0
    total_passes: int = 0
    recurring_since_ns: int = 0
    seen_this_pass: bool = False


@dataclass
class FailedRequests:
    """A coalescing recorder. Cheap enough to call from a hot path, since it never allocates per event
    after the first for a given (kind, subject).

    Memory: the recurrence table is bounded by the distinct (kind, subject) pairs ever seen in the
    process — instrument ids and granularities, never client order ids or timestamps — and is never
    forgotten while the process lives. Larger than the row table for a per-pass-cleared kind (whose
    rows come and go), by exactly the history this exists to keep."""

    _rows: dict[tuple[str, str], FailedRequest] = field(default_factory=dict)
    _recurrence: dict[tuple[str, str], Recurrence] = field(default_factory=dict)
    #: Kinds that have had at least one `clear_kind` — the only kinds with a pass structure to report.
    _delimited: set[str] = field(default_factory=set)

    def record(self, kind: str, subject: str, note: str = "", *, ts: int = 0) -> None:
        key = (str(kind), str(subject))
        row = self._rows.get(key)
        if row is None:
            row = FailedRequest(kind=key[0], subject=key[1], note=str(note or ""), first_ts=ts)
            self._rows[key] = row
        row.count += 1
        row.last_ts = ts
        # THE LATEST NOTE WINS, so a refusal that changes reason (a permission that becomes a pacing
        # limit) reports what is true NOW rather than what was true first.
        if note:
            row.note = str(note)
        # ONCE PER PASS, however many records: `count` counts records, the passes count passes.
        rec = self._recurrence.get(key)
        if rec is None:
            rec = self._recurrence[key] = Recurrence(recurring_since_ns=ts)
        if not rec.seen_this_pass:
            rec.consecutive_passes += 1
            rec.total_passes += 1
            rec.seen_this_pass = True

    def clear(self, kind: str, subject: str) -> None:
        """A request that starts succeeding stops being a condition — otherwise the first failure of
        the day is reported until restart, and the surface becomes a history rather than a state.
        The streak ends; the history (`total_passes`, `recurring_since_ns`) does not. A record after
        it in the SAME pass starts a new streak (`consecutive 1, total +1`) — a failure after a
        success is a new failure. No production caller today — the per-pass `clear_kind` is how the
        protection reconciler clears."""
        key = (str(kind), str(subject))
        self._rows.pop(key, None)
        rec = self._recurrence.get(key)
        if rec is not None:
            rec.consecutive_passes = 0
            rec.seen_this_pass = False

    def clear_kind(self, kind: str) -> None:
        """The PASS BOUNDARY for `kind` (#908): rows of that kind are cleared as before; a key of that
        kind NOT recorded during the pass just ended loses its streak (never its total); every key of
        that kind is open to be counted once on the next pass. Other kinds are untouched — the
        protection reconciler's 60 s clear must not delimit `aggregation`'s passes."""
        k = str(kind)
        for key in [key for key in self._rows if key[0] == k]:
            self._rows.pop(key, None)
        for key, rec in self._recurrence.items():
            if key[0] != k:
                continue
            if not rec.seen_this_pass:
                rec.consecutive_passes = 0
            rec.seen_this_pass = False
        self._delimited.add(k)

    def as_rows(self) -> list[dict]:
        """Worst first: most recurrent, then most failures, then most recent. Recurrence FIRST because
        a per-pass-cleared row is `count 1` forever and would otherwise sort below any twice-recorded
        row — and off the readback's 300-character prefix (#892). The recurrence fields sit BEFORE
        `note` for the same reason: a flip note alone is ~180 characters.

        A kind that has never been delimited reads None on all three — three states: `1` beside
        `count: 275` would be indistinguishable from a first occurrence."""
        def _rec(r: FailedRequest) -> tuple[int | None, int | None, int | None]:
            rec = self._recurrence.get((r.kind, r.subject))
            if r.kind not in self._delimited or rec is None:
                return None, None, None
            return rec.consecutive_passes, rec.total_passes, rec.recurring_since_ns

        rows = sorted(self._rows.values(), key=lambda r: (-(_rec(r)[1] or 0), -r.count, -r.last_ts))
        out = []
        for r in rows:
            consecutive, total, since = _rec(r)
            out.append({
                "kind": r.kind, "subject": r.subject, "count": r.count,
                "consecutive_passes": consecutive, "total_passes": total, "recurring_since_ns": since,
                "note": r.note, "first_ts": r.first_ts, "last_ts": r.last_ts,
            })
        return out

    @property
    def total(self) -> int:
        return sum(r.count for r in self._rows.values())

    def __len__(self) -> int:
        return len(self._rows)
