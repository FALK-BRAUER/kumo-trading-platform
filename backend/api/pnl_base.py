"""The per-lane window base, assembled for the tile (#699 read path).

THE LAST SEAM BEFORE THE SCREEN. The tile already computes each lane's standing unrealized NOW; the
observation table holds what each lane held at a window's START. This serves the second half so the
tile can subtract, and the delta it renders is `now − base`.

NOTHING HERE DECIDES ANYTHING TWICE. The date each window starts at comes from `eod_window`, the
derivation to read from `eod_read.select_version`, the lane total from `eod_read.lane_unrealized_at`.
This is assembly.

NULL IS A STATEMENT, ABSENCE IS A BUG. Every period appears in the answer; a period with no base — or
whose base day was never captured, or whose read failed — is an explicit null. A missing KEY is
indistinguishable from a serialisation fault, while an explicit null is something an operator can
act on. The tile renders an em dash for all three, but only this way is the em dash trustworthy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from api.eod_read import lane_instruments_at, lane_market_value_at, lane_qty_at, lane_unrealized_at, select_version
from api.eod_window import window_base_date
from api.realized_broker import PERIOD_DAYS

_log = logging.getLogger("kumo.pnl_base")


@dataclass(frozen=True)
class WindowBases:
    """Per-period bases, and SEPARATELY the dates that could not be read.

    THREE STATES, AND THE THIRD WAS INVISIBLE. The first version returned only the map, with `None`
    for every unknown — and a mutant replacing a failed read's `None` with `[]` was behaviourally
    IDENTICAL, because both are falsy and both became `None`. That equivalence was the finding: the
    endpoint could not tell an OUTAGE from a day nobody captured, and reported them the same.

    They are not the same. An uncaptured day is a gap the backfill can fill. A database that would
    not answer is a condition someone must act on, and rendering it as "no data for that window"
    means nobody ever does. The tile draws an em dash for both; `unreadable` is what makes the
    difference recoverable.
    """

    by_period: dict[str, dict | None] = field(default_factory=dict)
    #: The lanes' MARKET VALUE at each window's base (#699 a) — the same rows, the same version, the
    #: same three states as `by_period`; the ΔMV end of `net(W) = ΔMV − invested(W)`.
    market_value: dict[str, dict | None] = field(default_factory=dict)
    #: The instruments each lane HELD at each window's base (#1072) — what a kernel fill must touch
    #: to make that lane's window unvouchable. Same rows, same day, same version.
    instruments: dict[str, dict[str, list[str]] | None] = field(default_factory=dict)
    #: `{period: {lane: {instrument_id: signed qty}}}` at the base (#1072 b) — the base end of the
    #: qty invariant `qty_now − qty_base == Σ own fills`.
    qty_at: dict[str, dict[str, dict[str, float]] | None] = field(default_factory=dict)
    #: WHICH DAY ACTUALLY ANSWERED each period. The base is a calendar date and may resolve to an
    #: earlier captured day — a weekend base inherits Friday's close. Without this the store's own
    #: "evidence of which day answered" dies here and never reaches an operator, who then cannot
    #: tell a 1M delta measured from 30 days back from one measured from 34.
    base_date: dict[str, str | None] = field(default_factory=dict)
    #: Base DATES whose read failed. Dates only — see `error` for the other kind.
    unreadable: tuple[str, ...] = ()


async def unrealized_base_by_period(store, *, today) -> WindowBases:
    """`{period: {strategy_id: unrealized_at_the_window_start} | None}` for every period.

    ONE READ PER DISTINCT DATE — and the de-duplication is UNREACHABLE with the current vocabulary,
    which is said here rather than left looking tested. 1D/1W/1M/3M are 1/7/30/90 days back, so two
    periods can never land on the same date. My first test for this asserted no-duplicates and could
    therefore not fail for any implementation. The `set()` stays as defence against a future period
    being added that does coincide; what is actually pinned is that `all` produces no read at all.

    A FAILED READ IS NULL FOR THAT PERIOD AND DOES NOT TAKE THE OTHERS DOWN. A database that will not
    answer must never render as "every lane started flat"; and a single broken read taking the whole
    payload with it is the contract `/health` already refuses, for the same reason.

    THE VERSION IS CHOSEN PER DAY, from that day's own rows. A corrected re-derivation lands beside
    the original under a new `method_version`, and days genuinely differ — a backfill re-run may have
    corrected last month and not last week. Choosing once globally would read a version that does not
    exist on some of the days being read.
    """
    wanted: dict[str, str | None] = {p: window_base_date(p, today=today) for p in PERIOD_DAYS}

    rows_by_date: dict[str, list | None] = {}
    resolved: dict[str, str | None] = {}
    unreadable: list[str] = []
    for day in {d for d in wanted.values() if d}:
        try:
            # AT-OR-BEFORE, BOUNDED. The base is a calendar date and lands on a weekend for 1M on
            # every Monday and Tuesday — measured. Marks do not move over a weekend, so Friday's
            # close IS the unrealized prevailing at a Saturday boundary.
            actual, rows = await store.base_rows_on_or_before(day)
            resolved[day] = actual
            rows_by_date[day] = rows
        except Exception as exc:                                    # noqa: BLE001 — see docstring
            _log.warning("window base for %s could not be read (%s); that period is UNKNOWN, "
                         "not flat", day, exc)
            rows_by_date[day] = None
            resolved[day] = None
            unreadable.append(day)

    out: dict[str, dict | None] = {}
    mv: dict[str, dict | None] = {}
    held: dict[str, dict[str, list[str]] | None] = {}
    qty_at: dict[str, dict[str, dict[str, float]] | None] = {}
    answered: dict[str, str | None] = {}
    for period, day in wanted.items():
        rows = rows_by_date.get(day) if day else None
        answered[period] = resolved.get(day) if day else None
        if rows is None:
            # THREE CAUSES, ONE HONEST ANSWER: the window has no base (`all`), the day was never
            # captured (a weekend, a holiday, or a gap), or the read failed. All three are UNKNOWN.
            # An empty map here would let the tile render a confident 0.00 for every lane.
            out[period] = mv[period] = held[period] = qty_at[period] = None
            continue
        actual = resolved.get(day) if day else None
        if actual is None:
            # NO DAY RESOLVED. The manifest holds no successful capture at or before the base within
            # the bound, so this window's start was never observed. UNKNOWN.
            out[period] = mv[period] = held[period] = qty_at[period] = None
            continue
        if not rows:
            # CAPTURED AND FLAT — and the two lines above are what earn the difference. A day the
            # MANIFEST says was captured, holding no base rows, means every lane genuinely held
            # nothing: base zero, not unknown. Resolving on ROWS instead conflated this with "never
            # captured" and served an older day's unrealized as the base — `now − 500` where the
            # truth is `now − 0`. Refusing it outright, as the version before that did, suppressed a
            # value we actually know.
            out[period] = {}
            mv[period] = {}
            held[period] = {}
            qty_at[period] = {}
            continue
        try:
            version = select_version(rows)
        except ValueError as exc:
            # PER-PERIOD ISOLATION. An unorderable method_version on ONE day used to raise out of
            # this function and the endpoint's outer except nulled EVERY period — one bad day taking
            # the whole payload, which contradicts the contract this function's own test asserts.
            _log.warning("window base %s has an unreadable version set (%s)", actual, exc)
            out[period] = mv[period] = held[period] = qty_at[period] = None
            unreadable.append(actual)
            continue
        if version is None:
            out[period] = mv[period] = held[period] = qty_at[period] = None
            continue
        lanes = {str(r.get("strategy_id")) for r in rows}
        out[period] = {lane: lane_unrealized_at(rows, actual, lane, version) for lane in sorted(lanes)}
        # THE SAME ROWS, THE SAME VERSION, ONE PASS: the two maps cannot disagree about which day or
        # which derivation answered, because they were never asked separately.
        mv[period] = {lane: lane_market_value_at(rows, actual, lane, version) for lane in sorted(lanes)}
        held[period] = {lane: lane_instruments_at(rows, actual, lane, version) for lane in sorted(lanes)}
        qty_at[period] = {lane: lane_qty_at(rows, actual, lane, version) for lane in sorted(lanes)}
    return WindowBases(by_period=out, market_value=mv, instruments=held, qty_at=qty_at, base_date=answered,
                       unreadable=tuple(sorted(set(unreadable))))


def _sessions_between(after: str, before) -> list[str]:
    """Weekdays STRICTLY between two ET dates. Holidays are not modelled here (`_sessions_since` in
    app.py says the same): a holiday reads as "not captured", which over-reports a gap — the safe
    direction for a label that says a window is unverified — and names the date, so a reader can see
    it was a holiday."""
    from datetime import date, timedelta

    start = date.fromisoformat(after) + timedelta(days=1)
    end = before if isinstance(before, date) else date.fromisoformat(str(before))
    out: list[str] = []
    day = start
    while day < end:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def lane_net_terms(bases: WindowBases, flows: dict | None, *, coverage: dict, today,
                   qty_now: dict[str, dict[str, float]] | None = None) -> dict[str, dict | None]:
    """`{period: {lane: {mv_base, invested, partial}} | None}` — the two terms the tile subtracts from
    the lane's live market value, and the named reason a window is PARTIAL (#699 a).

    THE IDENTITY: `net(W) = mv_now − mv_base − invested(W)`. No basis rule: the FIFO-vs-average-cost
    counter-example that made `realized(W) + Δunrealized(W)` wrong nets to exactly zero here. It is
    the account headline's own identity (equity change net of flows), so the lane cells compose.

    THREE STATES, PER TERM. `mv_base` None = the lane had an unpriced leg at the base. `invested`
    None = the engine published no flows, or its flows carry an error, or the cache cannot VOUCH back
    to the base day (`earliest` after it — fills between could have happened and be gone). A period
    is None whole when it has no base. Zero is a number the caller established, never a default.

    `invested` is STRICTLY AFTER the base day: the base is a close, so that day's fills are already
    inside `mv_base` — counting them again subtracts them twice (test pinned).

    PARTIAL IS A NAME, NOT A FLAG. "first observed <d>": the lane's first manifest row lies inside
    the window — it was flat at the base (captured-and-flat), its whole net is flows-vs-now, and the
    cell says so. "not captured: <d>, <d>": weekday sessions inside the window with no successful
    capture (#1040's gaps). "internal fills: transfer <d> (n), repair <d> (n), reconciliation <d>
    (n)": non-market fills inside the window, counted in `invested` and named. The arithmetic is
    exact between the two OBSERVED ends either way; what is partial is the interior's verifiability
    and its composition, and both are named rather than bridged.

    A lane ABSENT from the base day's rows but present in `by_day` after it held nothing at the base
    — captured-and-flat — so `mv_base` is 0.0 by absence from `mv_map`, the same fact
    `lane_unrealized_at` reads as "captured" (its `captured` parameter is never passed from here; it
    is absence from the row set that means zero on a captured day). A lane never captured on its
    birth day reads mv_base 0 with the day's gap note but no lane note: the gap covers the day.
    """
    from api.realized import (dirty_by_instrument, dirty_fills_after, internal_fills_after, net_of_flows,
                              own_fill_qty_after, split_boot_pairs, symbol_of)

    first_observed: dict = coverage.get("first_observed") or {}
    captured: set = set(coverage.get("captured") or ())
    flows_ok = bool(flows) and not flows.get("error") and flows.get("by_day") is not None
    by_day = (flows or {}).get("by_day") or {}
    internal = (flows or {}).get("internal") or {}
    earliest = (flows or {}).get("earliest")
    # PER-INSTRUMENT SCOPING (#1072) needs the engine's `kernel_fills` and `touched`. An engine that
    # predates them publishes neither, and their ABSENCE is not "clean": the #1071 window-wide rule
    # then applies. `kernel_fills` present-but-empty IS clean.
    can_scope = flows_ok and "kernel_fills" in (flows or {}) and "touched" in (flows or {})
    kernel_fills = (flows or {}).get("kernel_fills") or []
    touched = (flows or {}).get("touched") or {}
    # THE QTY INVARIANT (#1072 b): a lane's position may change ONLY by its own fills. Needs the
    # engine's `net_qty` and a readable positions plane (`qty_now`); without either nothing can be
    # vouched for — absence is never clean.
    net_qty = (flows or {}).get("net_qty")
    # THE FRAME'S OWN qty_now WINS (same engine pass as net_qty, no read skew); the api's positions
    # plane is the fallback for an engine that publishes none. Required whenever the frame can scope;
    # an OLD frame (no kernel_fills) keeps #1071's window-wide rule and never reaches the invariant.
    if (flows or {}).get("qty_now") is not None:
        qty_now = (flows or {}).get("qty_now")
    can_check_qty = net_qty is not None and qty_now is not None

    out: dict[str, dict | None] = {}
    for period, base_map in bases.by_period.items():
        base_day = bases.base_date.get(period)
        mv_map = bases.market_value.get(period)
        if base_map is None or mv_map is None or not base_day:
            out[period] = None
            continue
        can_vouch = flows_ok and earliest is not None and str(earliest) <= str(base_day)
        # A DIRTY WINDOW IS REFUSED FOR EVERY LANE (paper 2026-09-14). A kernel-inferred fill puts
        # synthetic cash in one lane's flows; a reconciliation-minted EXTERNAL leg is how another
        # lane's departed shares get booked away from it with no fill of its own. Both are
        # cross-lane, so a per-lane check cannot see them: while any lies inside the window, no
        # lane's flows can be vouched for. `mv_base` stays known; only `invested` is refused.
        # A DIRTY WINDOW IS REFUSED PER INSTRUMENT (#1072) — window-wide only when the engine cannot
        # say where the kernel wrote. Boot PAIRS (IB reconciliation's per-name SELL+BUY at boot) are
        # vouch-neutral and named; an UNPAIRED kernel fill refuses every lane that held or traded
        # its instrument in the window, naming the instrument and the lane that booked it.
        dirty_wide = None
        by_instrument: dict[str, str] = {}
        unpaired_by_lane: dict[str, list[dict]] = {}
        pairs: dict[str, int] = {}
        residual: dict[str, float] = {}
        qty_map = bases.qty_at.get(period) or {}
        if can_vouch:
            if can_scope:
                unpaired, pairs, residual = split_boot_pairs(kernel_fills, since=base_day)
                by_instrument = dirty_by_instrument(unpaired)
                for f in unpaired:
                    unpaired_by_lane.setdefault(str(f["lane"]), []).append(f)
            else:
                dirty_wide = dirty_fills_after(internal, after=base_day)
                if dirty_wide:
                    can_vouch = False
        gaps = [d for d in _sessions_between(base_day, today) if d not in captured]
        gap_note = f"not captured: {', '.join(gaps)}" if gaps else None
        lanes = set(mv_map) | {lane for lane, days in by_day.items() if any(d > base_day for d in days)}
        held_map = bases.instruments.get(period) or {}
        terms: dict[str, dict] = {}
        for lane in sorted(lanes):
            mv_base = mv_map[lane] if lane in mv_map else 0.0      # captured-and-flat: the lane held nothing
            # THIS LANE'S INSTRUMENTS over the window: held at the base, or touched by any of its own
            # fills at or after the base day.
            mine = {symbol_of(i) for i in (held_map.get(lane) or ())}
            for day, iids in (touched.get(lane) or {}).items():
                if day >= base_day:
                    mine.update(symbol_of(i) for i in iids)
            # A KERNEL FILL REFUSES THE LANE IT WAS WRITTEN UNDER (an unpaired inferred fill is synthetic
            # cash in this lane's flows). What the kernel wrote under OTHER lanes is not this lane's
            # problem — unless it moved this lane's shares, which the invariant below catches.
            own_kernel = {symbol_of(f["instrument"]) for f in unpaired_by_lane.get(lane, ())}
            hits = [by_instrument[sym] for sym in sorted(mine) if sym in by_instrument and sym in own_kernel]
            if can_vouch and can_scope and not can_check_qty:
                hits.append("positions plane unreadable — the qty invariant cannot be checked")
            elif can_vouch and can_scope:
                # qty_now − qty_base == Σ own fills, per symbol, over every symbol the lane held at
                # the base, holds now, or filled in the window. Paper's GWRE: held 14, 0 now, own 0.
                base_q = {symbol_of(i): q for i, q in (qty_map.get(lane) or {}).items()}
                now_q = {symbol_of(i): float(q) for i, q in (qty_now.get(lane) or {}).items()}
                own_q = own_fill_qty_after(net_qty, lane, after=base_day)
                for sym in sorted(set(base_q) | set(now_q) | set(own_q)):
                    b0, n0, o0 = base_q.get(sym, 0.0), now_q.get(sym, 0.0), own_q.get(sym, 0.0)
                    if abs((n0 - b0) - o0) > 1e-6:
                        hits.append(f"{sym} held {b0:g} at the base, {n0:g} now, own fills {o0:g} — moved without a fill")
            lane_vouch = can_vouch and not hits
            # A boot pair's cent residual is the kernel's arithmetic, not cash that moved: taken back out.
            invested = (net_of_flows(by_day, lane, after=base_day) - residual.get(lane, 0.0)) if lane_vouch else None
            notes = []
            if dirty_wide:
                notes.append(f"cache cannot vouch for this window: {dirty_wide}")
            elif hits:
                notes.append(f"cache cannot vouch for this window: {', '.join(hits)}")
            born = first_observed.get(lane)
            if born and str(born) > str(base_day):
                notes.append(f"first observed {born}")
            if gap_note:
                notes.append(gap_note)
            # NON-MARKET FILLS INSIDE THE WINDOW — a transfer at carry-over price, a repair at basis,
            # a reconciliation-minted leg. Counted in `invested` (the identity stays closed) and
            # NAMED here, because a reader cannot otherwise tell a lane's net from a lane's trading.
            named = internal_fills_after(internal, lane, after=base_day) if lane_vouch else None
            if named:
                notes.append(f"internal fills: {named}")
            if lane_vouch and pairs.get(lane):
                notes.append(f"boot pairs: {pairs[lane]}")
            terms[lane] = {"mv_base": mv_base, "invested": invested, "partial": "; ".join(notes) or None}
        out[period] = terms
    return out
