"""Adapter: engine bars -> the market-compass payload -> the `rotation` WebSocket channel.

THE GRADING IS KUMO CODE: `strategies/rotation_grade.py`. This module converts bars into the tuple
shape it consumes and shapes the result for the stream. It computes nothing itself.

NO MOUNT, NO ENV VAR, NO `sys.path` INSERT. The maths used to be imported at runtime from
`/ledger-tool/tools`, behind `KUMO_LEDGER_TOOL_TOOLS` and a `LedgerToolUnavailable` exception. So an env var
could switch the market view off, and `KUMO_LEDGER_TOOL_TOOLS=/nonexistent` read as deliberate
configuration rather than a broken feature — which is exactly how ibkr-paper-retired showed
"No rotations to show" for days while the tile, the stream and the data path were all working.

There is nothing left to be unavailable, so the exception is gone rather than kept and never raised.
An instance that cannot grade a rotation now has a REASON per axis in `errors`, not a missing feature.
"""

from __future__ import annotations

import datetime as dt
import logging

_log = logging.getLogger(__name__)


def bars_to_tuples(bars) -> list[tuple]:
    """Nautilus bars -> the `(ts, o, h, l, c, v)` tuples the grading consumes.

    SECONDS, not nanoseconds: `weekly()` calls `dt.date.fromtimestamp(ts)` and `ratio_bars` joins the two
    legs BY TIMESTAMP, so a unit mismatch would silently produce an empty ratio series rather than an
    error — the join would simply never match.

    Ascending, because `ichi` and `window_stat` index from the end (`series[-1]`) and `weekly` folds in
    order. Nautilus returns newest-first from `cache.bars()`.
    """
    # KEYED BY EXCHANGE SESSION DATE, not by the raw event timestamp.
    #
    # `ratio_bars` joins the two legs on an EXACT key match, so the key has to be the thing that makes
    # two daily bars "the same day". `ts_event` is not: daily bars reach this cache by more than one
    # path — a strategy's own subscription and the compass's historical request — and those need not
    # agree about the time-of-day component. Measured 2026-08-26: MDY/SPY, SPHQ/SPY and GDX/GLD all
    # returned "thin history" with BOTH legs at exactly 700 bars. Equal length, non-overlapping keys.
    #
    # THE SESSION DATE IS ET, NOT UTC. Alpaca stamps a daily bar at 00:00 ET, which is 04:00 or 05:00
    # UTC depending on DST — so flooring to the UTC day is right for most of the year and off by one
    # for bars near the boundary. Flooring to UTC was tried and made things WORSE (paper 21/25,
    # staging 0/25) before being reverted.
    #
    # The key is emitted as epoch seconds at 00:00 UTC of that ET date: a canonical, timezone-free
    # integer that `weekly()`'s `dt.date.fromtimestamp` reads back as the same calendar day whatever
    # the host TZ is — which it currently depends on, and should not.
    from zoneinfo import ZoneInfo

    et = ZoneInfo("US/Eastern")
    by_session: dict[int, tuple] = {}
    for b in bars:
        try:
            ts_ns = int(b.ts_event)
            o, h, l, c = float(b.open), float(b.high), float(b.low), float(b.close)
            v = float(getattr(b, "volume", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 in (o, h, l, c):
            continue  # a zero leg makes the ratio undefined; ratio_bars drops these too
        session = dt.datetime.fromtimestamp(ts_ns / 1e9, dt.UTC).astimezone(et).date()
        key = int(dt.datetime(session.year, session.month, session.day,
                              tzinfo=dt.UTC).timestamp())
        # LAST WINS. A historical request and a live subscription can both deliver the same session,
        # and a duplicate key silently doubles that day's weight in `weekly` and shifts every window.
        by_session[key] = (key, o, h, l, c, v)
    return [by_session[k] for k in sorted(by_session)]


# `alpaca_bars_to_tuples` LIVED HERE AND IS GONE (2026-08-26). It converted raw rows from Alpaca's
# `/v2/stocks/{sym}/bars` because the compass fetched its deep history over that vendor's REST API —
# which made the market view Alpaca-only, and silently absent on any other provider. The bars now come
# from the Nautilus cache via `bars_to_tuples`, which every adapter fills. The orphan check found it
# the moment nothing called it, which is exactly what that check is for.


def build_payload(bars_for_ticker, source: str = "engine:nautilus-cache") -> dict:
    """The rotation payload, computed off our own bars.

    `bars_for_ticker(ticker) -> list[tuple]` is supplied by the caller, so this module needs no
    Nautilus import and can be tested with a plain dict.
    """
    from . import rotation_grade as grade

    missing: list[str] = []

    def bars(ticker):
        rows = bars_for_ticker(ticker)
        if not rows:
            missing.append(ticker)
            # Raise rather than return []: `read_pair` catches per axis and records the reason, so one
            # unavailable ETF costs its own axes and not the whole payload.
            raise LookupError(f"no cached bars for {ticker}")
        return rows

    # PASSED, NOT MONKEYPATCHED. The old path swapped a module-level `fetch` and restored it in a
    # `finally`, because the original resolved it from its own globals at call time.
    # HOW DEEP EACH LEG WAS, recorded per ticker. Two stacks graded the same market differently and
    # the verdicts agreed while ADX did not — Wilder's ADX is recursive from the start of the series,
    # so it is the one output that reports series LENGTH. Without this the only way to tell whether
    # the caches differ is to notice a number that looks wrong and reason backwards from it.
    depth: dict[str, int] = {}

    def bars_measured(ticker):
        rows = bars(ticker)
        depth[ticker] = len(rows)
        return rows

    results = [grade.read_pair(row, bars_measured) for row in grade.AXES]

    if missing:
        _log.warning("rotation: no cached bars for %s", ", ".join(sorted(set(missing))))

    return {
        "generated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        # Names the SOURCE, not just the tool — an operator comparing two payloads needs to know which
        # feed graded them, because two vendors can disagree about the same session. Parameterised
        # because the bars arrive over two transports (cache vs REST) and the payload must say which.
        "source": source,
        "depth": depth,
        "axes": [r for r in results if not r.get("err")],
        "errors": [{"pair": r["pair"], "err": r["err"]} for r in results if r.get("err")],
    }


def rotation_tickers() -> list[str]:
    """Every ticker the configured axes reference — the exact set that needs deep history."""
    from . import rotation_grade as grade

    return grade.tickers()
