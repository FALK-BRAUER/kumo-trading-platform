"""The equity curve, built from NAUTILUS's own account history (#559).

WHY THIS EXISTS. The curve came from `AlpacaHttpClient` hitting `/v2/account/portfolio/history`, and
`broker.equity_curve` had exactly one publisher — the Alpaca exec client. On an IBKR stack nothing
ever wrote the topic, so the tile rendered "No account history yet" over an account holding
$1,000,000 with months of history. Operator: "we can't keep the alpaca hardcoding — nothing in nautilus?"

THERE IS, AND IT WAS ALREADY FULL. `Account.events` returns `list[AccountState]`, every one carrying
balances and `ts_event`, and Nautilus persists them itself (`persist_account_events`, `cache_accounts`).
Measured 2026-08-26:

    paper    trader-PLATFORM-001:accounts:ALPACA-…                280,000 events
    staging  trader-PLATFORM-STG:accounts:INTERACTIVE_BROKERS-…    22,000 events

The IBKR curve was in the cache the whole time. Nobody read it.

CURRENCY IS NOT INCIDENTAL. Staging's account is SGD and paper's is USD. `balances_total` is per
currency, and silently summing across them would produce a number that is not money. Each curve names
its currency, and a state carrying more than one balance currency is skipped rather than guessed at.

NOT A PARALLEL LEDGER. Every value here is the broker's own `AccountState.balances_total`, read back
from Nautilus. Nothing is derived, accumulated or reconciled — the same rule the trade-cycle
projection follows.
"""

from __future__ import annotations

#: (period key the UI asks for, bucket seconds, how far back)
#: The bucket is what makes 284k events a chart. It also has to be IDENTICAL on both stacks, or the
#: same account renders differently depending on how long a node has been up — the failure the market
#: compass produced when its depth was incidental.
def _session_day_start(ts: int) -> int:
    """Epoch seconds at the start of the ET day containing `ts`.

    THE BOUNDARY IS THE VENUE'S DAY. Flooring in UTC puts it at 20:00 ET and splits a session's own
    evening from its afternoon, so a Friday-evening state is labelled Saturday. The same trap
    `venue_hours` documents for session times, one plane over.

    THE REAL ZONE, NOT A FIXED OFFSET. A hardcoded −5 or −4 is right for half the year and an hour
    out for the other half — which turns this into a once-a-year one-day shift nobody can reproduce
    in August.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(ny)
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


PERIODS: tuple[tuple[str, int, int], ...] = (
    ("1D", 60, 86_400),
    ("1W", 3_600, 7 * 86_400),
    ("1M", 86_400, 31 * 86_400),
    ("3M", 86_400, 92 * 86_400),
    ("all", 86_400, 3_650 * 86_400),
)


def _total(state) -> tuple[float, str] | None:
    """The account's total balance and its currency, or None if it cannot be read UNAMBIGUOUSLY."""
    try:
        balances = list(state.balances_total.values()) if hasattr(state, "balances_total") else []
        if not balances:
            balances = [b.total for b in getattr(state, "balances", [])]
    except Exception:                                                   # noqa: BLE001
        return None
    if len(balances) != 1:
        # ZERO is nothing to plot; MORE THAN ONE is a multi-currency account, and adding SGD to USD
        # produces a number that is not money. Skipped, not summed and not silently taking the first.
        return None
    b = balances[0]
    try:
        return float(getattr(b, "as_double", lambda: b)()), str(getattr(b, "currency", "") or "")
    except Exception:                                                   # noqa: BLE001
        try:
            return float(b), ""
        except (TypeError, ValueError):
            return None


#: The value `AccountBalance.total` carries. Written by our Alpaca connector from #588 onward; absent
#: on every row written before it, and absent on IBKR rows (Nautilus's adapter writes no such key,
#: and does not need to — its `total` has ALWAYS been NetLiquidation).
_NET_LIQ = "NET_LIQUIDATION"
_SEMANTICS_KEY = "BalanceTotalSemantics"


def _semantics(state) -> str | None:
    try:
        return (getattr(state, "info", None) or {}).get(_SEMANTICS_KEY)
    except Exception:                                                   # noqa: BLE001
        return None


def _has_net_liq_rows(events) -> bool:
    """Whether ANY row in this history states that `total` means net liquidation.

    THE SEAM #588 CREATES. Before that fix the Alpaca connector wrote CASH into `total`; after it,
    net liquidation. A curve spanning both plots a step of the entire book at deploy time and reads
    as a gain that never happened — on 2026-08-27 that step would have been +60,867.38, sitting in
    NET-1W/1M/3M for up to three months.

    False means NO row is marked, which is the IBKR case and the pre-deploy case alike: nothing is
    dropped and the behaviour is exactly what it was. Once a marked row exists, ONLY marked rows are
    a series — refusing a period rather than reporting a known-wrong number for it, the same rule as
    #343/#370/#382.

    NOT A TIMESTAMP PREFIX, and that was the first attempt. Dropping rows OLDER than the earliest
    marked one leaves any unmarked row that arrives LATER — which a rollback produces — sitting
    between two equity rows. Measured: [104,989.31, 44,121.93, 104,989.31], a 60,867.38 crash and
    instant recovery, the same defect in a different arrangement.
    """
    return any(_semantics(e) == _NET_LIQ for e in events or [])


def build_curves(events, now_ns: int, periods=PERIODS) -> dict[str, dict]:
    """`{period: {points, base_value, timeframe, currency}}` from a list of `AccountState`.

    LAST WRITE PER BUCKET, because an account emits many states per second and a chart wants the
    closing value of each interval — the same rule daily bars follow.
    """
    samples: list[tuple[int, float, str]] = []
    # MATERIALIZED ONCE. `_has_net_liq_rows` walks the events and so does the loop below; a one-shot
    # iterator would be exhausted by the first pass and the second would see nothing — an EMPTY curve
    # for an entirely unmarked history, which is staging (codex, merged review). The live caller
    # passes a list today, so this closes the class rather than a live defect.
    events = list(events or [])
    net_liq_history = _has_net_liq_rows(events)
    for e in events or []:
        ts = getattr(e, "ts_event", None)
        if ts is None:
            continue
        # MIXED SEMANTICS ARE NOT A SERIES. See `_has_net_liq_rows`.
        if net_liq_history and _semantics(e) != _NET_LIQ:
            continue
        got = _total(e)
        if got is None:
            continue
        value, ccy = got
        # ZERO IS NOT A DATA POINT. A state before the account was funded plots a cliff from the axis
        # up to the real balance, which reads as a catastrophic loss recovered. The Alpaca path
        # dropped these for the same reason.
        if value == 0.0:
            continue
        samples.append((int(ts) // 1_000_000_000, value, ccy))

    if not samples:
        return {}
    samples.sort(key=lambda s: s[0])

    out: dict[str, dict] = {}
    for key, bucket, lookback in periods:
        cutoff = now_ns // 1_000_000_000 - lookback
        window = [s for s in samples if s[0] >= cutoff]
        if len(window) < 2:
            continue  # a single point is not a curve; drawing one asserts a flat line
        folded: dict[int, tuple[float, str]] = {}
        for ts, value, ccy in window:
            # DAILY BUCKETS ARE ET SESSION DAYS, NOT UTC DAYS. `ts // 86400 * 86400` puts the
            # boundary at UTC midnight, which is 20:00 ET — so every account state after the close
            # landed on the NEXT calendar label, and the engine publishes states continuously. A
            # Friday-evening equity was stamped Saturday: a day that never traded, carrying a number,
            # labelled as its own. Sub-daily buckets are unaffected and keep the plain floor.
            # NOT `key` — the enclosing loop binds that to the PERIOD name ("1D", "1W"), and
            # shadowing it made `out[key]` index by a timestamp. Six existing tests caught it
            # immediately, which is the whole argument for them.
            slot = _session_day_start(ts) if bucket >= 86_400 else ts // bucket * bucket
            folded[slot] = (value, ccy)
        pts = [{"t": t, "equity": v, "pnl": 0.0} for t, (v, _c) in sorted(folded.items())]
        base = pts[0]["equity"]
        # PERIOD P&L = last - base, never a running sum of per-point deltas. Alpaca's own series taught
        # that lesson: its last 1M `profit_loss` was +1161.81 (that day's move) while the month was
        # DOWN 2244.29, and taking the last element printed "+$1,161 this 1M" over a falling chart.
        for p in pts:
            p["pnl"] = p["equity"] - base
        out[key] = {
            "points": pts,
            "base_value": base,
            "timeframe": f"{bucket}s",
            "currency": sorted({c for _t, _v, c in window if c})[:1] or [""],
        }
        out[key]["currency"] = out[key]["currency"][0]
    return out
