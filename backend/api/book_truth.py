"""Per-instrument: what the CACHE thinks, what the VENUE says, and which side each number came from.

WHY THIS EXISTS (#758). On 2026-08-31 four confident, wrong claims were made about the live paper
stack between the deploy and the open. Every one was corrected by Operator, none by any check, and in
every case the information existed and could only be recovered by grepping ~100k log lines:

  - "66 working protective stops"          -> 25 live; the rest were CANCELED and FILLED history
  - "cache and broker agree in aggregate"  -> GMAB cache 59 / venue 0, CGAU cache 80 / venue 0
  - "staging unaffected"                   -> that was zero ERROR LINES, not zero problems

The second is the one this module is shaped by. It came from reading

    Portfolio: GMAB.XNAS account=ALPACA-… net_position=59

as a broker fact. It is derived from the CACHE. The check compared the cache against itself and
called the agreement confirmation — which is the repo's own agreement-by-construction trap, written
down in the operating notes that same afternoon and walked into anyway.

SO EVERY QUANTITY HERE CARRIES ITS SOURCE. Not as a comment — as a field. `cache_qty` and `venue_qty`
cannot be confused for one another, and a row where the venue was unreadable says so rather than
quietly showing the cache twice.

THREE STATES ON THE VENUE SIDE, NEVER TWO. `venue_qty=None` means the venue could not be read;
`venue_qty=0` means it was read and holds nothing. `_venue_position_reports` already draws exactly
this distinction and its docstring says why — confusing them is how a naked book reads as protected.

WHAT MAKES A ROW INTERESTING is disagreement, and there are two kinds. The NET disagreeing with the
venue is a hard fault: broker net is the only anchor the venue can verify (ADR 0001). The per-lane
SPLIT disagreeing is expected to be unverifiable — the broker has no opinion on it — so it is
reported, never "corrected" against a source that cannot adjudicate it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: A discrepancy Nautilus has stopped trying to fix. Its own words, on abandoning GMAB:
#:
#:    Position discrepancy for GMAB.XNAS unresolved after 3 attempts
#:      (cached_qty=59, venue_qty=0); no further reconciliation attempts will be made
#:
#: One ERROR line, once, never carried forward — while every number derived from that symbol stays
#: wrong on every surface. This is the condition that must persist instead.
ABANDONED = "reconciliation abandoned"


#: Consecutive venue reads an instrument may disagree before it is reported as stuck. Two, not one:
#: a single read can straddle a fill in flight, and a surface that flags those is noise.
STUCK_AFTER_READS = 2


@dataclass
class DisagreementStreaks:
    """How many CONSECUTIVE venue reads each instrument has disagreed on.

    WHY THIS IS MEASURED HERE rather than read from Nautilus. The ExecEngine announces its own
    give-up —

        Position discrepancy for GMAB.XNAS unresolved after 3 attempts
          (cached_qty=59, venue_qty=0); no further reconciliation attempts will be made

    — as a log line and nothing else: no event, no flag, no queryable state. Parsing our own logs to
    recover it would bind this to one library's message text, which drifts silently on upgrade.

    So the SYMPTOM is measured instead, which is the repo's standing preference and is also the fact
    an operator actually needs: this instrument has disagreed on N consecutive reads and nothing has
    fixed it. That is true whether Nautilus gave up, is still retrying, or never noticed.

    AN UNREADABLE VENUE NEITHER ADVANCES NOR CLEARS A STREAK. A read that did not happen is not
    evidence of agreement — clearing on it would let a venue outage silently reset every alarm — and
    it is not evidence of disagreement either.
    """

    streaks: dict[str, int] = field(default_factory=dict)

    def observe(self, rows) -> None:
        for row in rows:
            disagrees = row.net_disagrees
            if disagrees is None:
                continue                       # not asked — see the docstring
            if disagrees:
                self.streaks[row.instrument_id] = self.streaks.get(row.instrument_id, 0) + 1
            else:
                self.streaks.pop(row.instrument_id, None)

    def stuck(self) -> list[str]:
        return sorted(k for k, n in self.streaks.items() if n >= STUCK_AFTER_READS)


@dataclass
class InferredFills:
    """Fills the ENGINE FABRICATED, counted per instrument.

    `OrderFilled.reconciliation` is Nautilus's own flag for a fill it generated to force the cache to
    match what it believed the venue said — not a fill the venue reported. Per the operating notes this poll
    is the phantom's mint: a reduce-only fill lands on no position and the ≤10s poll "fabricates the
    difference as a synthetic sell at a price that never traded".

    It is the highest-signal event the engine produces and it logs at INFO, among thousands. GMAB's
    entire history had to be reconstructed by COUNTING GREP MATCHES: 5 fills, 3 of them inferred.

    Counted, not streamed: 128 discrepancy lines is one condition with a number, and a surface that
    grew per occurrence would be scrolled past — the same silence at the other extreme.
    """

    by_instrument: dict[str, int] = field(default_factory=dict)
    #: Instruments where ANY inferred fill could not land — its position id resolved to no position of
    #: the stamped lane, or the fill was not on it — the exact shape that mints. STICKY until restart:
    #: a later fill that lands does not erase it (pinned by `test_an_INFERRED_FILL_THAT_CANNOT_LAND`).
    #: Kept separate because a count alone cannot say which ones to look at.
    #:
    #: RESIDUAL SCOPE — read this before reading `unlanded: []` as "no mint" (#901 review): this
    #: catches REDUCE-ONLY mints and nothing else. A fill that is not reduce-only on a lane holding
    #: nothing OPENS a position at its id and lands as an ordinary fill (entries are not reduce-only;
    #: protective stops are, and they are the case #807 was filed for). And startup reconciliation
    #: completes before the trader starts, so a boot-time mint never reaches this surface — only
    #: continuous-reconciliation fills are recorded.
    unlanded: dict[str, str] = field(default_factory=dict)

    def record(self, instrument_id: str, *, landed: bool, strategy_id: str = "") -> None:
        key = str(instrument_id)
        self.by_instrument[key] = self.by_instrument.get(key, 0) + 1
        if not landed:
            self.unlanded[key] = str(strategy_id or "<unstamped>")

    @property
    def total(self) -> int:
        return sum(self.by_instrument.values())

    def as_rows(self) -> list[dict]:
        return [
            {
                "instrument_id": k,
                "count": v,
                # Present only when at least one of them could not land, so an operator reading the
                # row knows whether this is bookkeeping or the mint.
                "unlanded_strategy": self.unlanded.get(k),
            }
            for k, v in sorted(self.by_instrument.items(), key=lambda kv: -kv[1])
        ]


class TerminalFills:
    """Fills that arrived for an order the cache holds TERMINAL (#807 item 4).

    Nautilus 1.229 `execution/engine.pyx:1586` applies such a fill to the POSITION after the order
    refuses it (`InvalidStateTrigger`), so the position plane diverges from the order plane with no
    signal but a WARN line — 237,216 identical ones on 2026-09-08. One row per order, coalesced:
    PATH took five fills on one REJECTED stop and the count is the story, not five rows.
    Capped, and the drop COUNTED: a defence that hides what it discarded is a second bug.
    """

    def __init__(self, cap: int = 200) -> None:
        self._rows: dict[str, dict] = {}
        self._cap = cap
        self.dropped = 0

    def record(self, client_order_id: str, order_status: str, instrument_id: str, strategy_id: str,
               quantity: float, *, ts_ns: int) -> None:
        row = self._rows.get(client_order_id)
        if row is None:
            if len(self._rows) >= self._cap:
                self.dropped += 1
                return
            row = self._rows[client_order_id] = {
                "client_order_id": client_order_id, "order_status": order_status,
                "instrument_id": instrument_id, "strategy_id": strategy_id,
                "quantity": 0.0, "count": 0, "first_ts_ns": ts_ns, "ts_ns": ts_ns,
            }
        row["quantity"] += float(quantity)
        row["count"] += 1
        row["ts_ns"] = ts_ns

    def as_rows(self) -> list[dict]:
        return [dict(r) for r in self._rows.values()]


@dataclass(frozen=True)
class TruthRow:
    instrument_id: str
    #: Signed, from `cache.positions_open()`. Summed across lanes.
    cache_qty: float
    #: Signed, from the venue's own `PositionStatusReport`. None = THE VENUE COULD NOT BE READ, which
    #: is not zero and must never be rendered as agreement.
    venue_qty: float | None
    #: Per-lane split beneath the net. The broker has no opinion on this and cannot adjudicate it.
    by_lane: dict[str, float]
    #: Set when reconciliation has given up on this instrument.
    state: str | None = None

    @property
    def net_disagrees(self) -> bool | None:
        """True/False, or None when the venue is unreadable — three states, never two."""
        if self.venue_qty is None:
            return None
        return abs(self.cache_qty - self.venue_qty) > 1e-9

    @property
    def split_disagrees(self) -> bool:
        """Two lanes claiming shares that sum past the net. Reported, never auto-corrected: the
        broker cannot verify a split, so a 'fix' here would be a guess wearing a measurement's
        clothes (ADR 0001)."""
        longs = sum(q for q in self.by_lane.values() if q > 0)
        return longs > abs(self.cache_qty) + 1e-9


def position_truth(cache_positions, venue_reports, *, abandoned=()) -> list[TruthRow]:
    """One row per instrument either side knows about.

    `venue_reports` is `None` when the venue could not be read — NOT an empty list, which is a real
    answer meaning it was read and holds nothing. Passing the wrong one turns "I could not look" into
    "there is nothing there", which is the failure this whole module exists to end.
    """
    by_lane: dict[str, dict[str, float]] = {}
    for p in cache_positions or []:
        if not hasattr(p, "is_open") or not p.is_open:
            continue
        iid, lane = str(getattr(p, "instrument_id", "")), str(getattr(p, "strategy_id", ""))
        if not iid:
            continue
        qty = float(getattr(p, "signed_qty", 0) or 0)
        by_lane.setdefault(iid, {})[lane] = by_lane.setdefault(iid, {}).get(lane, 0.0) + qty

    venue: dict[str, float] | None = None
    if venue_reports is not None:
        venue = {}
        for r in venue_reports:
            iid = str(getattr(r, "instrument_id", ""))
            if not iid:
                continue
            venue[iid] = venue.get(iid, 0.0) + float(getattr(r, "signed_decimal_qty", 0) or 0)

    abandoned_set = {str(a) for a in (abandoned or ())}
    rows = []
    for iid in sorted(set(by_lane) | set(venue or {})):
        lanes = by_lane.get(iid, {})
        rows.append(TruthRow(
            instrument_id=iid,
            cache_qty=sum(lanes.values()),
            # `.get` ONLY when the venue was readable. On an unreadable venue every row is None,
            # rather than an absent instrument silently becoming a zero that agrees with nothing.
            venue_qty=None if venue is None else venue.get(iid, 0.0),
            by_lane=dict(lanes),
            state=ABANDONED if iid in abandoned_set else None,
        ))
    return rows


def truth_summary(rows) -> dict:
    """The shape `/health` carries: counts, plus the NAMES, because a count cannot be looked into."""
    unreadable = [r.instrument_id for r in rows if r.net_disagrees is None]
    net_bad = [r.instrument_id for r in rows if r.net_disagrees is True]
    split_bad = [r.instrument_id for r in rows if r.split_disagrees]
    abandoned = [r.instrument_id for r in rows if r.state == ABANDONED]
    return {
        "instruments": len(rows),
        "net_disagrees": len(net_bad),
        "split_disagrees": len(split_bad),
        "venue_unreadable": len(unreadable),
        "abandoned": len(abandoned),
        # Bounded lists; the counts above are never truncated.
        "net_disagrees_names": sorted(net_bad)[:20],
        "split_disagrees_names": sorted(split_bad)[:20],
        "abandoned_names": sorted(abandoned)[:20],
    }
