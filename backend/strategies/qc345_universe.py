"""Deriving QC345's live ranking universe from Alpaca (#324, issue 43).

QC345 ranks ~50 names selected from thousands. The selection is the strategy's, not cockpit's —
`QC345ComputedSource` owns it. What cockpit owns is SUBSTRATE: enough daily bars, over a wide enough
set of symbols, that the selection has the same material to work with that the research had.

THE MEASURED BOUND, AND WHY THE FLOORS ARE NOT A SHORTCUT
--------------------------------------------------------
issue 43 measured, on local evidence:

    source substrate after the strategy's own asset filter    ~5,523 symbols
    rankable per rebalance                        min 4,569 / median 4,885 / max 5,460
    the algorithm then narrows: top 100 by 21-session median dollar volume,
                                then top 50 by price x dollar volume
    observed preservation floors    price >= 67.08
                                    21-session median dollar volume >= 518,190,000.09
    those floors left a median of 136 names historically

The floors are labelled CONFORMANCE HEURISTICS, NOT A LAW, and this module treats that label as
binding rather than decorative. They are used to CHECK the fetch, never to decide it:

  * The fetch is sized by the substrate (~5.5k symbols), which is what actually reproduces the
    research. Fetching only names above the floors would be an order of magnitude cheaper and would
    silently become wrong the first month a selected name prints below one — and nothing downstream
    could tell, because the missing name simply would not be in the panel to be ranked.

  * After the fetch, `preselection_bounds()` reports what the selection ACTUALLY needed this month.
    If a selected name sits below a floor, the floor is stale — that is a finding, logged loudly,
    not a reason to have dropped it.

An 80% cheaper fetch that is right 11 months in 12 is not a saving. It is a strategy that
occasionally ranks the wrong universe and reports a number nobody can distinguish from the right one.

MEASURED AGAINST THE LIVE ACCOUNT, 2026-08-17 (paper, SIP, 420 calendar days)
--------------------------------------------------------------------------
    assets                    33,431
    substrate                  5,805      upstream measured 5,523 — 5% apart, derived independently
    bars               1,507,704 rows over 5,499 symbols     (306 substrate names printed nothing
                                                              in the window: recent listings)
    rankable                   5,458      upstream range was min 4,569 / median 4,885 / MAX 5,460
    selected                      50
    min selected price            90.20   floor 67.08        HOLDS
    min selected liquidity  1,118,309,668 floor 518,190,000  HOLDS, ~2.2x
    wall time                    373.8s

Both floors hold with room, and rankable lands one name under upstream's observed maximum. Two
independent derivations agreeing that closely is the strongest evidence available that cockpit is
handing the selection the same material the research had.

THE WALL TIME DECIDES WHERE THIS RUNS, and it is why `build_qc345_strategy` does not call it.
Six minutes of REST inside synchronous node startup would delay every other strategy and the UI
data feed behind a universe refresh — for a universe that changes MONTHLY. It belongs in a
scheduled job that writes `strategies.QC345_UNIVERSE`, which is also why the operator-supplied
setting is not a temporary scaffold: it is the interface the job will write to.

WHAT THIS IS NOT
----------------
Not a scanner. It fetches and hands over. Every selection stage — the fund filter, the liquidity
filter, the market-cap proxy, the carry-forward between rebalances — belongs to
`QC345ComputedSource`, and reimplementing any of it here would be a second derivation of a rule that
already has one.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger(__name__)

_DATA = "https://data.alpaca.markets"
_TRADING = "https://paper-api.alpaca.markets"

#: Alpaca's multi-symbol bars endpoint takes a comma-separated list. Kept well under any URL length
#: limit rather than at it — a request that 414s on an unlucky batch of long tickers would look like
#: a data outage.
_BATCH = 200

#: The floors measured in issue 43. Used to VERIFY a fetch, never to size one. See above.
PRESERVATION_PRICE_FLOOR = 67.08
PRESERVATION_DOLLAR_VOLUME_FLOOR = 518_190_000.09

#: Substrate size the same measurement observed, for a sanity check on our own asset filter. A fetch
#: that produces a tenth of this has gone wrong upstream of the bars and would otherwise show up as
#: a strategy that simply picked different names.
EXPECTED_SUBSTRATE = 5_523
_SUBSTRATE_TOLERANCE = 0.40


def _headers() -> dict[str, str]:
    key = os.environ.get("APCA_API_KEY_ID", "")
    secret = os.environ.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _get(url: str, headers: dict[str, str], *, attempts: int = 4) -> dict:
    """One GET with backoff on 429/5xx.

    Retried rather than failed because this runs at node startup over thousands of symbols: a single
    transient 429 partway through would otherwise drop a batch, and a MISSING batch is not visible
    downstream — those symbols merely never rank, which looks exactly like them not being selected.
    """
    delay = 1.0
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts - 1:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def fetch_assets() -> object:
    """Every Alpaca US equity as `symbol / name / exchange / status`. One call.

    NOT filtered to active or tradable, and both omissions are deliberate. `terminal_buckets`
    classifies a holding that has stopped printing by reading `status`, so filtering inactive names
    out of the reference data is precisely how a delisted position stops being visible.
    """
    import pandas as pd

    rows = _get(f"{_TRADING}/v2/assets?asset_class=us_equity", _headers())
    return pd.DataFrame([{"symbol": a.get("symbol"), "name": a.get("name"),
                          "exchange": a.get("exchange"), "status": a.get("status"),
                          "tradable": bool(a.get("tradable"))} for a in rows])


def substrate(assets, cfg) -> list[str]:
    """The symbols worth fetching bars for — the STRATEGY's asset filter, applied by the strategy.

    `is_fundamental_like_asset` is imported rather than reimplemented. Cockpit deciding separately
    what counts as a fund would be a second derivation of the rule that defines the universe, and the
    two would diverge on the first marker anyone added upstream.
    """
    from kumo_strategies.strategies.qc345_rotation.engine import is_fundamental_like_asset

    keep = [
        r["symbol"] for _, r in assets.iterrows()
        if r.get("tradable") and is_fundamental_like_asset(r.get("name"), r.get("exchange"))
    ]
    out = sorted(set(keep))
    low = EXPECTED_SUBSTRATE * (1 - _SUBSTRATE_TOLERANCE)
    high = EXPECTED_SUBSTRATE * (1 + _SUBSTRATE_TOLERANCE)
    if not (low <= len(out) <= high):
        # Loud, and not fatal. The measurement is local evidence from one point in time and the
        # listed universe genuinely moves; but an order-of-magnitude miss means the asset filter is
        # not doing what it did when the research ran, and that surfaces downstream only as the
        # strategy picking different names for no visible reason.
        _log.error(
            "qc345 substrate is %d symbols, outside %.0f–%.0f around the measured %d "
            "(issue 43) — the asset filter may not match the researched one",
            len(out), low, high, EXPECTED_SUBSTRATE)
    return out


def fetch_daily_bars(symbols: list[str], *, start: str, end: str | None = None,
                     feed: str = "sip") -> object:
    """Daily bars for every symbol, as the `ticker/date/open/high/low/close/volume` panel the engine
    expects. Batched and paginated to completion.

    `feed="sip"` because the selection is a LIQUIDITY ranking: IEX carries a fraction of consolidated
    volume, so a median-dollar-volume filter computed on it ranks venue share rather than liquidity.
    The account has SIP (2026-08-01).

    A batch that fails after its retries RAISES rather than being skipped. A silently missing batch
    does not look like an error downstream — those symbols simply never rank, which is
    indistinguishable from them not having been selected.
    """
    import pandas as pd

    headers = _headers()
    rows: list[dict] = []
    for i in range(0, len(symbols), _BATCH):
        batch = symbols[i:i + _BATCH]
        token = None
        while True:
            q = {"symbols": ",".join(batch), "timeframe": "1Day", "start": start,
                 "limit": "10000", "feed": feed, "adjustment": "raw"}
            if end:
                q["end"] = end
            if token:
                q["page_token"] = token
            payload = _get(f"{_DATA}/v2/stocks/bars?{urllib.parse.urlencode(q)}", headers)
            for symbol, bars in (payload.get("bars") or {}).items():
                for b in bars:
                    rows.append({"ticker": symbol, "date": pd.Timestamp(b["t"]).tz_convert(None).normalize(),
                                 "open": b["o"], "high": b["h"], "low": b["l"],
                                 "close": b["c"], "volume": b["v"]})
            token = payload.get("next_page_token")
            if not token:
                break
    if not rows:
        raise RuntimeError("qc345: the bar fetch returned nothing — refusing to derive a universe "
                           "from an empty panel")
    return pd.DataFrame(rows).sort_values(["ticker", "date"]).reset_index(drop=True)


def derive_universe(bars, assets, cfg, *, on: object | None = None,
                    check_floors: bool = True) -> tuple[list[str], dict]:
    """Bars + assets -> the ranking universe, decided ENTIRELY by `QC345ComputedSource`.

    Returns (symbols, diagnostics). The diagnostics carry `preselection_bounds()` for the session,
    which is what turns the measured floors into a check rather than an assumption: it reports the
    minimum price and liquidity the selection ACTUALLY needed, so a floor that has gone stale is
    visible in the record instead of silently truncating next month's universe.
    """
    import pandas as pd
    from kumo_strategies.strategies.qc345_rotation import QC345ComputedSource

    source = QC345ComputedSource(bars, assets, cfg)
    session = pd.Timestamp(on) if on is not None else pd.Timestamp(bars["date"].max())
    selected = sorted(source.eligible(session))

    bounds = source.preselection_bounds()
    diag: dict = {"session": str(session.date()), "selected": len(selected),
                  "substrate": int(bars["ticker"].nunique())}
    if not bounds.empty:
        last = bounds.iloc[-1]
        diag["rankable_names"] = int(last.get("rankable_names") or 0)
        diag["min_selected_price"] = last.get("min_selected_price")
        diag["min_selected_liquidity_proxy"] = last.get("min_selected_liquidity_proxy")
        if check_floors:
            # Skippable ONLY for a deliberately truncated substrate. A partial pool makes the weakest
            # selected name arbitrarily weak, so the floors would report stale every time — a false
            # finding manufactured by the caller, in the one check meant to detect real drift.
            _check_floors(diag)
        else:
            diag["floors_checked"] = False
    return selected, diag


def _check_floors(diag: dict) -> None:
    """Compare what the selection needed against the floors measured in issue 43.

    A breach is LOGGED, never acted on. The floors are observed history, and history is exactly the
    thing that stops being true — the point of measuring them was to know when they go stale, and a
    module that quietly enforced them would make that impossible to find out.
    """
    price = diag.get("min_selected_price")
    liquidity = diag.get("min_selected_liquidity_proxy")
    if price is not None and price < PRESERVATION_PRICE_FLOOR:
        _log.warning(
            "qc345: a selected name printed at %.2f, below the measured preservation floor %.2f — "
            "the floor is stale; a fetch prefiltered on it would have dropped a researched name",
            price, PRESERVATION_PRICE_FLOOR)
        diag["price_floor_breached"] = True
    if liquidity is not None and liquidity < PRESERVATION_DOLLAR_VOLUME_FLOOR:
        _log.warning(
            "qc345: a selected name's 21-session median dollar volume was %.2f, below the measured "
            "floor %.2f — the floor is stale", liquidity, PRESERVATION_DOLLAR_VOLUME_FLOOR)
        diag["liquidity_floor_breached"] = True
