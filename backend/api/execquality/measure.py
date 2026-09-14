"""Execution quality: what we paid versus the official opening auction (research/#210).

Pure functions plus a CLI. **Nothing here runs in the trading path** — it reads two Alpaca endpoints
after the fact and appends rows to a CSV, so a bad day for this module can never be a bad day for the
book.

WHY IT EXISTS. The backtest fills at the daily open, which is the opening auction print at 09:30:00.
The live runner submits at 09:35 (`KUMO_MOMENTUM_OPEN_OFFSET_MIN`, default 5) and fills 09:35–09:36.
They price the same decision at two different moments, by construction. The one-off study over three
sessions measured +35.7 bps of drift with t=1.32 — which establishes nothing, because 22 legs
clustered in three mornings is effectively n=3. Closing that out needs **30–60 trading days**, and
that only happens if the number is recorded as it goes.

WHAT THIS DOES NOT SETTLE. The drift is not necessarily a cost. These are momentum names; a winner
that keeps running between 09:30 and 09:35 makes us pay more, and that is the signal working, not the
broker failing. Separating the two needs a market/sector control over the same five minutes, which is
NOT implemented here. Treat the output as a measurement of divergence, never as a slippage bill.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

#: Auction condition for the primary listing exchange's official opening print.
OFFICIAL = "O"

FIELDS = ("session", "symbol", "side", "qty", "fill_vwap", "auction_px", "auction_exchange",
          "auction_size", "first_fill_utc", "lag_minutes", "drift_bps", "cost_usd")

#: Legs filling more than this long after the opening print are not measured against it. A midday
#: discretionary trade compared to the morning auction measures nothing, and on 3 Aug exactly that
#: pair (CVS, PENG at ~13:09 ET) produced the largest apparent "drift" in the original study. The
#: lag is computed from the auction record's OWN timestamp, so it needs no timezone or DST handling.
MAX_LAG_MINUTES = 60.0

#: Legs filling BEFORE the official opening print are not drift from the open either. Seen live: FIG
#: on 6 Aug filled at 13:30:36 with its NYSE opening print published a minute later — a delayed open,
#: where the stock traded on other venues before its primary auction cleared. Comparing a fill to a
#: print that did not exist yet measures the delay, not the execution. A small negative tolerance
#: absorbs clock skew between the two feeds.
MIN_LAG_MINUTES = -0.5


@dataclass(frozen=True)
class Leg:
    """One symbol-side on one session, compared against that morning's official open."""

    session: str
    symbol: str
    side: str
    qty: float
    fill_vwap: float
    auction_px: float
    auction_exchange: str
    auction_size: float
    first_fill_utc: str
    lag_minutes: float
    drift_bps: float
    cost_usd: float


def official_open(auction_day: dict) -> tuple[float, str, float, str] | None:
    """The official opening print from one symbol-day record: (price, exchange, size, timestamp).

    The endpoint returns ONE RECORD PER price/exchange/condition triplet, so a symbol-day carries the
    primary exchange's print (condition "O", tens of thousands of shares) beside other venues' opens
    of 1–100 shares. Taking the first record produced SU at −316 bps in the original study, which was
    a methodology error, not a finding.

    Prefers condition "O"; falls back to the largest size, because a venue that traded 30,000 shares
    at the open is a better estimate of the open than one that traded 100.
    """
    opens = (auction_day or {}).get("o") or []
    if not opens:
        return None
    official = [o for o in opens if str(o.get("c", "")).upper() == OFFICIAL]
    pick = max(official or opens, key=lambda o: float(o.get("s") or 0))
    return (float(pick["p"]), str(pick.get("x", "")), float(pick.get("s") or 0),
            str(pick.get("t", "")))


def fill_vwap(activities: list[dict]) -> dict[tuple[str, str, str], tuple[float, float, str]]:
    """Aggregate FILL activities to (session, symbol, side) -> (qty, vwap, earliest timestamp).

    Partial fills MUST be folded together: AFL alone filled in five pieces on 6 Aug for one intended
    sell, so comparing individual fills to a single auction print would count one decision five times
    and weight it by nothing meaningful.
    """
    acc: dict[tuple[str, str, str], list] = defaultdict(lambda: [0.0, 0.0, None])
    for a in activities:
        ts = str(a.get("transaction_time") or "")
        key = (ts[:10], str(a.get("symbol", "")).upper(), str(a.get("side", "")).lower())
        try:
            q, p = float(a["qty"]), float(a["price"])
        except (KeyError, TypeError, ValueError):
            continue
        row = acc[key]
        row[0] += q
        row[1] += q * p
        if row[2] is None or ts < row[2]:
            row[2] = ts
    return {k: (q, notional / q, ts or "") for k, (q, notional, ts) in acc.items() if q > 0}


def compare(activities: list[dict], auctions: dict, exclude: set[tuple[str, str, str]] | None = None,
            max_lag_minutes: float = MAX_LAG_MINUTES) -> list[Leg]:
    """Join fills to opening auctions. Legs with no auction record are dropped, not guessed at.

    Legs are also dropped when the first fill lands more than `max_lag_minutes` after the opening
    print. That is not a nicety: a midday discretionary trade measured against the morning auction
    produces a large meaningless number, and in the original study exactly that (CVS and PENG at
    ~13:09 ET) sat at the top of the table. Lag comes from the auction record's own timestamp, so no
    timezone or DST reasoning is involved.

    `exclude` additionally removes named legs — e.g. a manual trade that did happen at the open.
    """
    skip = exclude or set()
    out: list[Leg] = []
    by_day: dict[tuple[str, str], dict] = {}
    for sym, rows in (auctions.get("auctions") or {}).items():
        for r in rows or []:
            if r.get("d"):
                by_day[(str(r["d"]), str(sym).upper())] = r

    for (day, sym, side), (qty, vwap, first) in sorted(fill_vwap(activities).items()):
        if (day, sym, side) in skip:
            continue
        found = official_open(by_day.get((day, sym)))
        if not found:
            continue
        px, exch, size, auction_ts = found
        if px <= 0:
            continue
        lag = _lag_minutes(auction_ts, first)
        if lag is not None and not (MIN_LAG_MINUTES <= lag <= max_lag_minutes):
            continue
        # Signed so that positive always means "worse than the auction", whichever way we traded.
        signed = (vwap - px) if side == "buy" else (px - vwap)
        out.append(Leg(session=day, symbol=sym, side=side, qty=round(qty, 4),
                       fill_vwap=round(vwap, 6), auction_px=px, auction_exchange=exch,
                       auction_size=size, first_fill_utc=first,
                       lag_minutes=round(lag, 2) if lag is not None else -1.0,
                       drift_bps=round(10000 * signed / px, 2), cost_usd=round(signed * qty, 2)))
    return out


def append_csv(path: Path, legs: list[Leg]) -> int:
    """Append legs not already recorded. Idempotent on (session, symbol, side) so re-running a day
    corrects nothing and duplicates nothing — the point is accumulation over months."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[tuple[str, str, str]] = set()
    if path.exists():
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                seen.add((row["session"], row["symbol"], row["side"]))
    fresh = [x for x in legs if (x.session, x.symbol, x.side) not in seen]
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        for leg in fresh:
            w.writerow(asdict(leg))
    return len(fresh)


def summarise(legs: list[Leg]) -> dict:
    """Descriptive only. Deliberately reports the SESSION count beside the leg count.

    Legs cluster by morning — every leg in one session shares that morning's market shock — so the
    effective sample is nearer the number of sessions than the number of legs. Reporting only n=legs
    is how the first pass overstated its confidence.
    """
    if not legs:
        return {"legs": 0, "sessions": 0}
    bps = sorted(x.drift_bps for x in legs)
    n = len(bps)
    mean = sum(bps) / n
    var = sum((b - mean) ** 2 for b in bps) / (n - 1) if n > 1 else 0.0
    return {
        "legs": n,
        "sessions": len({x.session for x in legs}),
        "mean_bps": round(mean, 1),
        "median_bps": round(bps[n // 2] if n % 2 else (bps[n // 2 - 1] + bps[n // 2]) / 2, 1),
        "stdev_bps": round(var ** 0.5, 1),
        "total_usd": round(sum(x.cost_usd for x in legs), 2),
        "worse_than_100bps": sum(1 for b in bps if b > 100),
        "better_than_100bps": sum(1 for b in bps if b < -100),
    }


def _lag_minutes(auction_ts: str, fill_ts: str) -> float | None:
    """Minutes between the opening print and our first fill — the quantity this study is really about.

    Returns None when either timestamp is unusable, and the caller then keeps the leg: a missing
    timestamp is a reason to look, not a reason to silently drop a real trade.
    """
    a, f = _parse(auction_ts), _parse(fill_ts)
    if a is None or f is None:
        return None
    return (f - a).total_seconds() / 60.0


def _parse(ts: str):
    from datetime import datetime
    raw = (ts or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    # Alpaca timestamps carry nanoseconds; fromisoformat wants at most microseconds.
    if "." in raw:
        # Alpaca sends nanoseconds; fromisoformat accepts at most microseconds. Split the fraction
        # from any trailing timezone offset, truncate to 6 digits, reattach.
        head, _, tail = raw.partition(".")
        tz = ""
        for marker in ("+", "-"):
            if marker in tail:
                tz = tail[tail.index(marker):]
                tail = tail[:tail.index(marker)]
                break
        digits = "".join(c for c in tail if c.isdigit())[:6].ljust(6, "0")
        raw = f"{head}.{digits}{tz}"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
