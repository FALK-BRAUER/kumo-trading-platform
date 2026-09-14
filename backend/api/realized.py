"""Realized P&L for the session, taken from NATIVE closed positions (#233).

Operator, seeing `REALIZED $0.00` on Home while four positions had been closed that day for +$1,451.77:
the number was not wrong, it was structurally unreachable.

`realized_pnl` lives on the trade-cycle DTO, and `trade_cycle.py:235` is explicit: "A cycle that
reaches CLOSED is emitted once (terminal) and then dropped from the map." The BOOK tile sums realized
across the CURRENT `/trades` rows, which by construction contain only live cycles. So the moment a
position closed, its P&L left the screen — realized read $0.00 except in the seconds around a close.

The durable store cannot answer it either: the cycle envelope deliberately holds no P&L ("NOT a P&L
ledger — P&L is recomputed from native"), which is the right call and leaves this question to native.

So it is answered from `cache.positions_closed()`. Nautilus keeps closed positions with their
`realized_pnl` and `ts_closed`, which is exactly the fact needed and is not a second bookkeeping of
anything — the same rule that keeps this codebase from growing a parallel ledger beside the Cache.

ATTRIBUTED BY CLOSE DATE, not open date. A position opened Tuesday and closed today books its P&L
today, which is what a broker statement says and what an operator means by "what did today make".

WHAT THIS DOES NOT COVER, and says so rather than hiding it: a partial exit realizes P&L on a position
that is still OPEN, and Nautilus timestamps the position's close, not each realization. There is no
native per-realization timestamp to filter on, so partials on still-open positions cannot be attributed
to a session without rebuilding P&L from fills — which would be the parallel ledger this avoids. They
are COUNTED instead, so the caller can say the figure is partial. A number known to be incomplete is
worth far more than one silently so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from api.realized_broker import PERIOD_DAYS
from api.trade_cycle import newer_close_wins

_ET = ZoneInfo("America/New_York")


def _money(value) -> float:
    """A Nautilus `Money` (or None) as a float. `Money.as_double()` is the documented accessor; the
    string fallback covers a double that carries a plain number."""
    if value is None:
        return 0.0
    for attr in ("as_double", "as_decimal"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return float(fn())
            except Exception:  # noqa: BLE001 — fall through to the next accessor
                pass
    try:
        return float(str(value).split(" ")[0])
    except (TypeError, ValueError):
        return 0.0


# -- windows over Nautilus's own closed legs (#846) --------------------------------------------------------
#
# `positions_closed()` is right for a position that closed ONCE. Under NETTING the position id is
# `{instrument}-{strategy}`, and a reopen REPLACES the object in the cache (`cache.pyx::add_position`
# discards the id from the closed index), so the earlier round trip's `realized_pnl` leaves
# `positions_closed()` for good. staging2, 2026-09-09: MPC.XNYS-MANUAL-001 closed for +575.51, reopened,
# closed again for -3.62 — and the day read -3.62. Home REALIZED 1D was short $575.51.
#
# The durable record is Nautilus's own: with `snapshot_positions=True` the exec engine persists every
# closed state to `snapshots:positions:{pos_id}`; the engine keeps those legs (restart seed + live
# `PositionClosed` events) and hands them here. This is a READ of native state, not a parallel ledger.


def et_day_bounds_ns(ts_ns: int) -> tuple[int, int]:
    """The ET CALENDAR DAY containing `ts_ns`, as [start, end) in epoch ns.

    A calendar day rather than the RTH session, because realized P&L is attributed to the day a
    position closed and a close can land outside 09:30-16:00 — an extended-hours exit, or a
    reconciliation that books a close after the bell. Bounding by RTH would silently drop those and
    make the day's figure quietly short.
    """
    local = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).astimezone(_ET)
    start = datetime.combine(local.date(), time(0, 0), tzinfo=_ET)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)


def et_window_start_ns(ts_ns: int, *, days: int) -> int:
    """00:00 ET on the ET calendar day `days` before the one containing `ts_ns`.

    DATE ARITHMETIC, THEN LOCALISE. `start - days * 86_400s` lands at 23:00 or 01:00 ET across a DST
    change and silently moves a close between windows. This is the ONE window-floor predicate — the
    broker sweep (`realized_broker.realized_by_period`) is handed it too, so the two derivations of
    "which legs are in 1W" cannot disagree by construction.
    """
    local = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).astimezone(_ET)
    day = local.date() - timedelta(days=days)
    return int(datetime.combine(day, time(0, 0), tzinfo=_ET).timestamp() * 1e9)


@dataclass(frozen=True)
class ClosedLeg:
    """One closed leg, in the one shape both sources reduce to."""

    position_id: str
    ts_opened: int
    ts_closed: int
    strategy_id: str
    realized: float
    currency: str


def _closed_leg(obj) -> ClosedLeg | None:
    """A Nautilus `Position` (`.id`) or a `CycleLeg` (`.position_id`) as a `ClosedLeg`; None if not closed."""
    ts_closed = getattr(obj, "ts_closed", None)
    if ts_closed is None:
        return None
    pid = getattr(obj, "position_id", None) or getattr(obj, "id", None)
    return ClosedLeg(
        position_id=str(pid or ""),
        ts_opened=int(getattr(obj, "ts_opened", 0) or 0),
        ts_closed=int(ts_closed),
        strategy_id=str(getattr(obj, "strategy_id", "") or "UNKNOWN"),
        realized=_money(getattr(obj, "realized_pnl", None)),
        currency=str(getattr(getattr(obj, "realized_pnl", None), "currency", "") or ""),
    )


def dedup_legs(positions_closed, legs) -> list[ClosedLeg]:
    """positions_closed() UNION the kept legs, one entry per (position id, open time).

    The open time is the identity the snapshot store already dedups by, and it survives a flip: the
    flipped leg opens at the CLOSING fill's timestamp, never the old leg's. When both sources hold the
    same leg the LATEST close wins — a reconciliation re-apply can rewrite a closed state.
    """
    best: dict[tuple[str, int], ClosedLeg] = {}
    for src in (positions_closed or [], legs or []):
        for obj in src:
            leg = _closed_leg(obj)
            if leg is None:
                continue
            key = (leg.position_id, leg.ts_opened)
            prior = best.get(key)
            if newer_close_wins(None if prior is None else prior.ts_closed, leg.ts_closed):
                best[key] = leg
    return list(best.values())


#: Not a strategy. A broker position no strategy owns closes too; its money is real and reconciles the
#: total, but attributing it charges a strategy for a decision it never made (books.ts).
UNCLAIMED_STRATEGY = "EXTERNAL"


def _window(legs: list[ClosedLeg], start_ns: int | None, end_ns: int, *, partial_open: int,
            horizon_ts: int | None) -> dict:
    by_strategy: dict[str, float] = {}
    closed_count: dict[str, int] = {}
    unclaimed = 0.0
    unclaimed_count = 0
    for leg in legs:
        if not ((start_ns is None or leg.ts_closed >= start_ns) and leg.ts_closed < end_ns):
            continue
        if leg.strategy_id == UNCLAIMED_STRATEGY:
            unclaimed += leg.realized
            unclaimed_count += 1
            continue
        by_strategy[leg.strategy_id] = by_strategy.get(leg.strategy_id, 0.0) + leg.realized
        closed_count[leg.strategy_id] = closed_count.get(leg.strategy_id, 0) + 1
    return {
        "by_strategy": by_strategy,
        "closed_count": closed_count,
        "total": sum(by_strategy.values()) + unclaimed,
        "unclaimed": unclaimed,
        "unclaimed_count": unclaimed_count,
        # A partial exit on a still-OPEN position realizes P&L native cannot timestamp (see the module
        # header). Counted, never folded in. `unmatched` is the name the UI reads for "the total
        # understates" — the sweep's word for it; same meaning, same asterisk.
        "partial_open": partial_open,
        "is_partial": partial_open > 0,
        "unmatched": partial_open,
        # How far back this cache can see. `all` is NOT account inception — it is the oldest leg held
        # (staging2: 2026-09-03). Stated on every window so no figure reads as a lifetime number.
        "horizon_ts": horizon_ts,
    }


def realized_windows(*, positions_closed, positions_open, legs, now_ns: int) -> dict[str, dict]:
    """Realized P&L per strategy over every window at once, keyed by the sweep's own vocabulary
    (`PERIOD_DAYS`: 1D/1W/1M/3M/all). Pure.

    Windows are half-open `[start, end)` on ET calendar days: `end` is the end of today's ET day, `start`
    is 00:00 ET `N` days earlier (`et_window_start_ns`), or None for `all`. 1D of this function IS the
    session figure — Home and the per-strategy panel read one derivation.
    """
    all_legs = dedup_legs(positions_closed, legs)
    # ONE SETTLEMENT CURRENCY, or no number. `_money` drops the currency; an SGD-based account can hold a
    # leg settling in SGD beside USD ones, and a sum across them is a wrong figure that looks right.
    currencies = sorted({leg.currency for leg in all_legs if leg.currency})
    if len(currencies) > 1:
        return empty_windows(f"mixed settlement currencies: {', '.join(currencies)} — refusing to sum")
    _, day_end = et_day_bounds_ns(now_ns)
    horizon = min((leg.ts_closed for leg in all_legs), default=None)
    partial_open = sum(
        1 for pos in (positions_open or []) if abs(_money(getattr(pos, "realized_pnl", None))) > 0.005
    )
    out: dict[str, dict] = {}
    for key, days in PERIOD_DAYS.items():
        start = None if days is None else et_window_start_ns(now_ns, days=days)
        out[key] = _window(all_legs, start, day_end, partial_open=partial_open, horizon_ts=horizon)
    return out


def empty_windows(error: str | None = None) -> dict[str, dict]:
    """Every window empty, carrying the reason. THREE STATES: a window that could not be computed must
    not render as zero P&L — the shape is the same so the frame never lacks the keys, and `error` says why."""
    out = {key: _window([], None, 0, partial_open=0, horizon_ts=None) for key in PERIOD_DAYS}
    if error is not None:
        for w in out.values():
            w["error"] = error
            # NOT ZERO. A window that could not be computed has no total; the tile rendered `0.0` as a
            # confident $0.00 with nothing reading `error`. None renders as "—".
            w["total"] = None
    return out


# -- per-lane net invested per session, for the window identity (#699 option a) ------------------------
#
# `net(W) = ΔMV − invested(W)`: the lane's change in market value over the window minus the cash it put
# in. No basis rule at all — the FIFO-vs-average-cost counter-example that made `realized(W) +
# Δunrealized(W)` wrong (`books.ts::cellHeadline`) nets to exactly zero under it — and the same identity
# the account's DELTA NET states (equity change net of flows), so the lane cells compose like the
# headline above them. `MV` at the window start comes from the EOD observation rows (`pnl_base`); `MV`
# now from the live frame; this supplies the flows, per lane, per ET session, from the cache's own
# filled orders — which survive restarts and carry both the `strategy_id` and the `OrderFilled` events.
#
# INTERNAL_TRANSFER, REPAIR (`RPR-` coids, #771/#779/#1038) and RECONCILIATION fills are COUNTED, not
# filtered: within the cache's own world a transfer moves MV between lanes AT A PRICE, a repair closes
# a phantom AT BASIS and a reconciliation leg is minted WITH A BASIS, so their flows are what keep each
# lane's identity closed (engine_node `_apply_leg` writes a real OrderFilled for every one of them).
# They are NAMED beside the numbers (`internal`), because a window whose net carries a transfer at
# carry-over price or a repair at basis is one the reader must be able to see (review on #699 a).
#
# COMMISSIONS ARE INVESTED. The account headline is equity, which paid them; a cost is money put in.
# Without this the lanes drift from DELTA NET by exactly Σcommissions. Dividends and other cash the
# venue books outside fills are in neither the lane MV nor the flows — the account sees them, the
# lanes do not, and that residual is stated on the tile rather than hidden.

#: How far back flows are bucketed. 3M is 90 days; the slack covers a base that resolved earlier.
FLOWS_HORIZON_DAYS = 100


def _et_session_date(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).astimezone(_ET).date().isoformat()


def _fill_class(order, ev) -> str | None:
    """`transfer` | `repair` | `inferred` | `reconciliation` | None — for NAMING and for REFUSING,
    never for filtering. BY ORIGIN, NEVER BY THE FILL'S FLAG: `OrderFilled.reconciliation` is True
    on EVERY polled Alpaca fill (paper readback 2026-09-14 named ordinary trading "reconciliation
    (19)" and caught nothing), so the coid decides. Cockpit prefixes (`cache_repair.COCKPIT_PREFIXES`
    — ONE list) are ours: a transfer leg (`TR-` / INTERNAL_TRANSFER), a repair (`RPR-`), or ordinary
    trading (`kumo-`, `PROT-`, `FL-`). Anything else under a STRATEGY lane is INFERRED — the kernel
    attributed a venue-side or synthesised fill to the lane (MOMENTUM LAND 3×BUY 429 on uuid coids,
    09-08); under EXTERNAL, a RECONCILIATION-tagged order is the kernel's minted leg and a VENUE-
    tagged one is an ordinary venue-side trade."""
    from api.cache_repair import COCKPIT_PREFIXES

    coid = str(getattr(order, "client_order_id", ""))
    tags = [str(t) for t in (getattr(order, "tags", None) or ())]
    if coid.startswith("TR-") or "INTERNAL_TRANSFER" in tags:
        return "transfer"
    if coid.startswith("RPR-"):
        return "repair"
    if coid.startswith(COCKPIT_PREFIXES):
        return None
    # A cockpit FLATTEN is `close_position(pos, tags=["flatten:FL-…"])`: Nautilus mints the `O-` coid
    # and the cockpit's identity rides in the tag (staging2, measured 2026-09-14). Ours.
    if any(t.startswith("flatten:") and t[len("flatten:"):].startswith(COCKPIT_PREFIXES) for t in tags):
        return None
    if str(getattr(order, "strategy_id", "")) == "EXTERNAL":
        # Under EXTERNAL both of the kernel's boot-mirror legs are kernel fills: the BUY it books at
        # the venue's avgCost (tag VENUE, coid == instrument id) and the RECONCILIATION SELL it
        # writes a second later at the lane's fill px (staging2 09-11 12:08:17, fr2467iv's table).
        # Classing the VENUE leg is what lets `split_boot_pairs` match the two. A hand trade at the
        # venue is VENUE-tagged too — it then refuses EXTERNAL's own row, never a strategy lane
        # (a lane's vouch is its OWN fills and the qty invariant, #1072 b).
        if "RECONCILIATION" in tags:
            return "reconciliation"
        return "venue" if "VENUE" in tags else None
    return "inferred"


#: The fill classes that make a window UNVOUCHABLE for EVERY lane. Both are the kernel writing the
#: cache without the cockpit: an inferred fill carries synthetic cash into a lane's flows, and a
#: reconciliation-minted leg under EXTERNAL is how a lane's departed shares get booked AWAY from it
#: (paper 2026-09-04..08: TECHIVOL's GWRE/ZETA sells landed under EXTERNAL, TECHIVOL read a 4.4k
#: loss). Cross-lane by construction, so the refusal is window-wide.
DIRTY_FILL_CLASSES = frozenset({"inferred", "reconciliation", "venue"})


def lane_flows_by_day(orders, *, now_ns: int, days: int = FLOWS_HORIZON_DAYS, positions=None) -> dict:
    """`{"by_day": {strategy_id: {ET date: net invested}}, "internal": {strategy_id: {ET date:
    {class: count}}}, "earliest": date|None, "horizon_days": n, "error": str|None}` over the cache's
    orders. `internal` names the fills that did not happen at a venue at market (see `_fill_class`);
    every one of them is ALSO in `by_day`.

    BUYS POSITIVE, SELLS NEGATIVE — "invested". `earliest` is the ET date the cache VOUCHES FROM:
    the oldest order it holds (filled or not — the cache's birth, not the first fill), floored at the
    horizon; None when it holds no order at all. The reader compares it with a window's base date,
    because a base older than the oldest order the cache holds is a window whose flows this cannot
    vouch for (UNKNOWN, not zero).

    ONE UNREADABLE ORDER COSTS THE WHOLE FIELD, loudly. A map missing one lane's flows is a wrong
    number wearing a complete label — the shape this repo keeps paying for.
    """
    floor_ns = now_ns - days * 86_400 * 1_000_000_000
    by_day: dict[str, dict[str, float]] = {}
    internal: dict[str, dict[str, dict[str, int]]] = {}
    kernel_fills: list[dict] = []
    touched: dict[str, dict[str, list[str]]] = {}
    net_qty: dict[str, dict[str, dict[str, float]]] = {}
    oldest_init: int | None = None
    try:
        for order in orders:
            lane = str(order.strategy_id)
            born = int(order.ts_init)
            oldest_init = born if oldest_init is None else min(oldest_init, born)
            for ev in order.events:
                if type(ev).__name__ != "OrderFilled":
                    continue
                ts = int(ev.ts_event)
                if ts < floor_ns:
                    continue
                notional = float(ev.last_qty) * float(ev.last_px)
                signed = (notional if ev.is_buy else -notional) + _money(getattr(ev, "commission", None))
                day = _et_session_date(ts)
                bucket = by_day.setdefault(lane, {})
                bucket[day] = bucket.get(day, 0.0) + signed
                kind = _fill_class(order, ev)
                if kind is not None:
                    counts = internal.setdefault(lane, {}).setdefault(day, {})
                    counts[kind] = counts.get(kind, 0) + 1
                iid = str(getattr(ev, "instrument_id", "") or getattr(order, "instrument_id", ""))
                seen = touched.setdefault(lane, {}).setdefault(day, [])
                if iid not in seen:
                    seen.append(iid)
                # SIGNED NET FILL QTY per lane per instrument per session — the qty invariant's own
                # term: a lane's position may only change by its own fills (#1072 b).
                q = net_qty.setdefault(lane, {}).setdefault(day, {})
                q[iid] = q.get(iid, 0.0) + (float(ev.last_qty) if ev.is_buy else -float(ev.last_qty))
                if kind in DIRTY_FILL_CLASSES:
                    # EVERY dirty fill, with what per-instrument scoping needs (#1072): where it landed,
                    # which way and how much — so a boot PAIR can be matched and an unpaired one placed.
                    kernel_fills.append({"lane": lane, "day": day, "instrument": iid,
                                         "side": "BUY" if ev.is_buy else "SELL",
                                         "qty": float(ev.last_qty), "px": float(ev.last_px), "kind": kind,
                                         "ts_ns": ts})
    except Exception as exc:  # noqa: BLE001 — a display field, refused whole rather than served partial
        return {"by_day": {}, "internal": {}, "kernel_fills": [], "touched": {}, "net_qty": {}, "qty_now": None,
                "earliest": None, "horizon_days": days, "error": repr(exc)[:160]}
    earliest = _et_session_date(max(oldest_init, floor_ns)) if oldest_init is not None else None
    # `qty_now` FROM THE SAME PASS (review on #1072 b): the open positions' signed qty per lane and
    # instrument, read beside the fills so the qty invariant compares two readings of one moment —
    # not the api's 2 s positions plane against an engine frame, where a fill between the reads
    # fails the invariant for one poll. None when not asked, never an empty book.
    qty_now: dict[str, dict[str, float]] | None = None
    if positions is not None:
        from api.ownership import signed_qty_of

        qty_now = {}
        try:
            for pos in positions:
                q = signed_qty_of(pos)
                if not q:
                    continue
                qty_now.setdefault(str(pos.strategy_id), {})[str(pos.instrument_id)] = float(q)
        except Exception as exc:  # noqa: BLE001 — an unreadable book is None, never flat
            return {"by_day": {}, "internal": {}, "kernel_fills": [], "touched": {}, "net_qty": {}, "qty_now": None,
                    "earliest": None, "horizon_days": days, "error": repr(exc)[:160]}
    kernel_fills.sort(key=lambda f: (f["day"], f["lane"], f["instrument"]))   # stable: insertion order within
    for lane_days in touched.values():
        for lst in lane_days.values():
            lst.sort()
    return {"by_day": by_day, "internal": internal, "kernel_fills": kernel_fills, "touched": touched,
            "net_qty": net_qty, "qty_now": qty_now, "earliest": earliest, "horizon_days": days, "error": None}


#: A boot pair is two legs reconciliation wrote together: seconds apart, a cent apart. Wider than
#: this and it is a trade.
BOOT_PAIR_MAX_GAP_S = 60
BOOT_PAIR_MAX_PX_FRAC = 0.005


def _looks_like_boot_pair(b: dict, s: dict) -> bool:
    tb, ts_ = b.get("ts_ns"), s.get("ts_ns")
    if tb is None or ts_ is None:
        return False                                          # an old frame without timestamps cannot pair
    if abs(int(tb) - int(ts_)) > BOOT_PAIR_MAX_GAP_S * 1_000_000_000:
        return False
    pb, ps = float(b["px"]), float(s["px"])
    ref = max(abs(pb), abs(ps))
    return ref > 0 and abs(pb - ps) / ref <= BOOT_PAIR_MAX_PX_FRAC


def split_boot_pairs(kernel_fills: list[dict], *, since: str) -> tuple[list[dict], dict[str, int], dict[str, float]]:
    """`(unpaired, pairs_by_lane, residual_by_lane)` over the dirty fills dated at or after `since`
    (#1072). `residual_by_lane` is the pairs' net cash as it sits in `by_day` (Σ buy − sell notional,
    strictly after `since`) — the kernel's arithmetic, not money that moved — for the caller to take
    back OUT of `invested`.

    A BOOT PAIR is what IB reconciliation writes per held name at boot (staging2 2026-09-11: a SELL
    on a uuid coid at the entry price and a BUY on a `SYM.XNYS` coid a cent away): same lane, same
    session, same instrument, same qty, opposite sides. Net cash is the cent residual, which stays
    inside the identity; the pair is vouch-NEUTRAL and is counted per lane so the cell can name it.
    Matching is a multiset per (lane, day, instrument, qty), and a BUY and a SELL pair only when
    they look like what reconciliation writes: within `BOOT_PAIR_MAX_GAP_S` of each other and within
    `BOOT_PAIR_MAX_PX_FRAC` in price. A REAL same-day round trip that arrived under kernel coids
    after a cache recreate — enter at open+5m, give back at close−20m, same qty — fails both gates
    and stays UNPAIRED (review on #1072): pairing it would take the trade's whole P&L out of the
    window under a benign note. Anything unpaired is a real kernel intervention and refuses.
    """
    buckets: dict[tuple[str, str, str, float], dict[str, list[dict]]] = {}
    for f in kernel_fills:
        if str(f.get("day", "")) < since:
            continue
        key = (str(f["lane"]), str(f["day"]), str(f["instrument"]), round(float(f["qty"]), 6))
        buckets.setdefault(key, {"BUY": [], "SELL": []})[str(f["side"])].append(f)
    unpaired: list[dict] = []
    pairs: dict[str, int] = {}
    residual: dict[str, float] = {}
    for (lane, day, _iid, qty), sides in buckets.items():
        buys = sorted(sides["BUY"], key=lambda f: int(f.get("ts_ns") or 0))
        sells = list(sorted(sides["SELL"], key=lambda f: int(f.get("ts_ns") or 0)))
        for b in buys:
            mate = next((s for s in sells if _looks_like_boot_pair(b, s)), None)
            if mate is None:
                unpaired.append(b)
                continue
            sells.remove(mate)
            pairs[lane] = pairs.get(lane, 0) + 1
            if day > since:                                   # what `net_of_flows` counted
                residual[lane] = residual.get(lane, 0.0) + qty * (float(b["px"]) - float(mate["px"]))
        unpaired.extend(sells)
    unpaired.sort(key=lambda f: (str(f["day"]), str(f["instrument"]), str(f["kind"]), str(f["lane"])))
    return unpaired, pairs, residual


def symbol_of(instrument_id: str) -> str:
    """The vouch's identity is the SYMBOL, not the venue-qualified id: a base row spelled LAND.XNYS
    and a kernel fill spelled LAND.XNAS (#1057's two-MIC names) are the same shares."""
    return str(instrument_id).split(".", 1)[0]


def dirty_by_instrument(unpaired: list[dict]) -> dict[str, str]:
    """`{symbol: "GWRE.XNYS reconciliation 2026-09-08 (1) on EXTERNAL, …"}` — one NAME per SYMBOL for
    the unpaired kernel fills that touched it (the note keeps the fill's full id), so a refused lane
    can say which of its instruments was written by the kernel and by whom."""
    counts: dict[tuple[str, str, str, str], int] = {}
    for f in unpaired:
        k = (str(f["instrument"]), str(f["day"]), str(f["kind"]), str(f["lane"]))
        counts[k] = counts.get(k, 0) + 1
    out: dict[str, list[str]] = {}
    for (iid, day, kind, lane), n in sorted(counts.items()):
        out.setdefault(symbol_of(iid), []).append(f"{iid} {kind} {day} ({n}) on {lane}")
    return {sym: ", ".join(parts) for sym, parts in out.items()}


def dirty_fills_after(internal: dict, *, after: str) -> str | None:
    """A NAME for every DIRTY fill (see `DIRTY_FILL_CLASSES`) in ANY lane strictly after `after`, or
    None when the window is clean: `inferred 2026-09-08 (4) on MOMENTUM-002, reconciliation
    2026-09-08 (8) on EXTERNAL` — dates ascending, then class, then lane."""
    seen: dict[tuple[str, str, str], int] = {}
    for lane, days in (internal or {}).items():
        for day, counts in days.items():
            # ON the base day counts as dirty too (review): an evening reconciliation can re-mint the
            # base-day position AFTER the close capture, so the captured MV is clean while the day is
            # not. Over-refusing one day is the cheap side.
            if day < after:
                continue
            for kind, n in counts.items():
                if kind in DIRTY_FILL_CLASSES:
                    seen[(day, kind, lane)] = seen.get((day, kind, lane), 0) + int(n)
    if not seen:
        return None
    return ", ".join(f"{kind} {day} ({n}) on {lane}" for (day, kind, lane), n in sorted(seen.items()))


def own_fill_qty_after(net_qty: dict, lane: str, *, after: str) -> dict[str, float]:
    """`{symbol: signed net qty}` the lane filled itself strictly after `after` (#1072 b)."""
    out: dict[str, float] = {}
    for day, by_iid in (net_qty.get(lane) or {}).items():
        if day <= after:
            continue
        for iid, q in by_iid.items():
            out[symbol_of(iid)] = out.get(symbol_of(iid), 0.0) + float(q)
    return out


def internal_fills_after(internal: dict, lane: str, *, after: str) -> str | None:
    """A NAME for the lane's non-market fills strictly after `after`, or None: e.g.
    `transfer 2026-09-11 (2), repair 2026-09-13 (1)` — dates ascending, classes alphabetical."""
    seen: dict[tuple[str, str], int] = {}
    for day, counts in (internal.get(lane) or {}).items():
        if day <= after:
            continue
        for kind, n in counts.items():
            if kind in DIRTY_FILL_CLASSES:
                continue                      # named window-wide by `dirty_fills_after`, and refused
            seen[(kind, day)] = seen.get((kind, day), 0) + int(n)
    if not seen:
        return None
    return ", ".join(f"{kind} {day} ({n})" for (kind, day), n in sorted(seen.items(), key=lambda kv: (kv[0][1], kv[0][0])))


def net_of_flows(by_day: dict, lane: str, *, after: str) -> float:
    """The lane's net invested STRICTLY AFTER `after` (an ET date). The base is a CLOSE, so every fill
    on the base day is already inside MV_base; counting it again would subtract it twice."""
    return float(sum(v for day, v in (by_day.get(lane) or {}).items() if day > after))
