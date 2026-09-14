"""The market compass: every rotation axis, graded, as kumo code.

A rotation — large-vs-small, growth-vs-value, cyclical-vs-defensive — is a RATIO of two ETFs. A ratio
series has OHLC like any instrument, so the same weekly-gate / daily-timing Ichimoku stack that grades
a stock grades the rotation itself: the ratio above the weekly cloud and above the daily tenkan means
the numerator leg is winning.

WHY THIS FILE EXISTS. The maths used to live in `fintrack/tools/{rotation_read,grade_full}.py`,
reached at runtime by `sys.path.insert(0, "/fintrack/tools")` against a read-only Docker mount, behind
`KUMO_FINTRACK_TOOLS` and a `FintrackUnavailable` exception. The feature was always kumo's — the data
path is kumo's, the Alpaca REST fetch is kumo's, the WebSocket stream is kumo's — but ~350 lines of
grading were outside the repo. That is why an env var could switch the market view off, why
staging-ibkr showed "No rotations to show", and why `KUMO_FINTRACK_TOOLS=/nonexistent` read as a
deliberate configuration rather than a broken feature.

Ported verbatim in behaviour, not rewritten. `test_rotation_grade_matches_the_original.py` ran both
implementations over the same bars and asserted identical output while the mount still existed; that
comparison is the reason to trust this file, and its recorded values are what remain now the original
is gone. Verification by disagreement, not by reading the port and pronouncing it correct.

Bars are `(ts, open, high, low, close, volume)` tuples, oldest first — what `alpaca_bars_to_tuples`
and `bars_to_tuples` produce.
"""

from __future__ import annotations

import datetime as dt
import math

#: (group, label, numerator, denominator, what a RISING ratio means)
AXES: list[tuple[str, str, str, str, str]] = [
    ("size",    "Small vs Large",           "IWM",  "SPY", "small caps leading = easing + domestic growth"),
    ("size",    "Mid vs Large",             "MDY",  "SPY", "mid caps leading"),
    ("style",   "Growth vs Value",          "IWF",  "IWD", "duration bid, long rates falling"),
    ("risk",    "Discretionary vs Staples", "XLY",  "XLP", "THE risk gauge — tape believes growth"),
    ("risk",    "Industrials vs Utilities", "XLI",  "XLU", "cyclical growth over bond proxy"),
    ("risk",    "Cyclical vs Defensive",    "XLI",  "XLV", "offense over healthcare defense"),
    ("breadth", "Equal vs Cap weight",      "RSP",  "SPY", "broadening — not just mega-cap"),
    ("tech",    "Semis vs Tech",            "SMH",  "XLK", "semis leading = front-end risk appetite"),
    ("tech",    "Software vs Semis",        "IGV",  "SMH", "software over hardware = capex fear off"),
    ("tech",    "Tech vs Market",           "XLK",  "SPY", "tech leadership"),
    ("quality", "Quality vs Market",        "SPHQ", "SPY", "de-risking into balance sheets"),
    ("quality", "Momentum vs LowVol",       "MTUM", "USMV", "trend persistence, speculation alive"),
    ("credit",  "HY credit vs Treasury",    "HYG",  "IEI", "credit appetite — usually turns first"),
    ("credit",  "Long duration vs Short",   "TLT",  "IEI", "duration bid = growth/recession pricing"),
    ("geo",     "Intl vs US",               "EFA",  "SPY", "weak dollar / relative policy easing"),
    ("geo",     "EM vs US",                 "EEM",  "SPY", "dollar down, EM risk on"),
    ("real",    "Energy vs Market",         "XLE",  "SPY", "supply shock / inflation trade"),
    ("real",    "Gold vs Market",           "GLD",  "SPY", "real assets over financial assets"),
    ("real",    "Real assets vs Growth",    "XLE",  "IWF", "inflation trade over duration"),
    ("real",    "Miners vs Gold",           "GDX",  "GLD", "risk appetite inside the metal trade"),
    ("rates",   "Banks vs Utilities",       "KRE",  "XLU", "curve steepening, NIM over bond proxy"),
    ("rates",   "REITs vs Banks",           "XLRE", "KRE", "rates falling"),
    ("cycle",   "Capex vs Consumer",        "XLI",  "XLY", "spending is corporate, not household"),
    ("cycle",   "Healthcare vs Market",     "XLV",  "SPY", "late-cycle / defensive rotation"),
    ("cycle",   "Staples vs Market",        "XLP",  "SPY", "risk-off, cash-flow certainty bid"),
]

#: ChartTile's RANGE_DAYS vocabulary — the same windows the rest of the cockpit uses.
WINDOWS = [("1D", 1), ("1W", 5), ("1M", 21), ("3M", 63), ("1Y", 252)]

#: Dispersion is ALWAYS measured over 63 bars, whatever the horizon. See `window_stat`.
VOL_N = 63

#: A ratio needs this much history before Ichimoku means anything (52 + 26 forward shift, plus room).
MIN_RATIO_BARS = 400


def ratio_bars(a, b):
    """Ratio OHLC from two aligned daily series. High = ha/lb, Low = la/hb.

    The cross is deliberate: the ratio is highest when the numerator peaks against the denominator's
    trough, so the extremes cannot be taken from the same side of both bars.
    """
    index = {r[0]: r for r in b}
    out = []
    for ts, o, h, l, c, _v in a:
        m = index.get(ts)
        if not m or 0 in (m[1], m[2], m[3], m[4]):
            continue
        _, mo, mh, ml, mc, _ = m
        out.append((ts, o / mo, h / ml, l / mh, c / mc, 0))
    return out


def pct(series, n):
    if len(series) <= n:
        return None
    return (series[-1][4] / series[-1 - n][4] - 1) * 100


def weekly(daily):
    """Daily bars folded to ISO weeks: first open, running high/low, last close, summed volume."""
    wk: dict = {}
    for ts, o, h, l, c, v in daily:
        y, w, _ = dt.date.fromtimestamp(ts).isocalendar()
        k = (y, w)
        if k not in wk:
            wk[k] = [ts, o, h, l, c, v]
        else:
            e = wk[k]
            e[2] = max(e[2], h)
            e[3] = min(e[3], l)
            e[4] = c
            e[5] += v
    return [tuple(wk[k]) for k in sorted(wk)]


def _mid(b, n, i):
    w = b[i - n + 1: i + 1]
    return (max(x[2] for x in w) + min(x[3] for x in w)) / 2


def ichi(b):
    """Ichimoku position of the last bar. `pos` is ABV/IN/BLW the cloud; `abv` is above the tenkan."""
    i = len(b) - 1
    px = b[i][4]
    tk = _mid(b, 9, i)
    kj = _mid(b, 26, i)
    j = i - 26
    if j - 51 < 0:
        sa = sb = None
        pos, color = "n/a", "?"
    else:
        sa = (_mid(b, 9, j) + _mid(b, 26, j)) / 2
        sb = _mid(b, 52, j)
        top, bot = max(sa, sb), min(sa, sb)
        pos = "ABV" if px > top else ("BLW" if px < bot else "IN")
        color = "G" if sa >= sb else "R"
    chk = px > b[i - 26][4] if i >= 26 else None
    return dict(px=px, tk=tk, kj=kj, pos=pos, color=color, TgtK=tk > kj, abv=px > tk, chk=chk,
                cbot=(min(sa, sb) if sa else None), ctop=(max(sa, sb) if sa else None))


def adx_di(b, n=9):
    """Wilder ADX and directional indicators. Returns `(adx, +DI, -DI)`, each rounded to 1dp."""
    if len(b) < 2 * n + 2:
        return (None, None, None)
    tr, pd, nd = [], [], []
    for i in range(1, len(b)):
        h, l = b[i][2], b[i][3]
        ph, pl, pc = b[i - 1][2], b[i - 1][3], b[i - 1][4]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pd.append(up if (up > dn and up > 0) else 0.0)
        nd.append(dn if (dn > up and dn > 0) else 0.0)

    def wil(x):
        s = sum(x[:n])
        o = [s]
        for v in x[n:]:
            s = s - s / n + v
            o.append(s)
        return o

    atr, pdi, ndi = wil(tr), wil(pd), wil(nd)
    dx = []
    for k in range(len(atr)):
        if atr[k] == 0:
            dx.append(0)
            continue
        p = 100 * pdi[k] / atr[k]
        m = 100 * ndi[k] / atr[k]
        dx.append(100 * abs(p - m) / (p + m) if (p + m) else 0)
    a = sum(dx[:n]) / n
    for v in dx[n:]:
        a = (a * (n - 1) + v) / n
    p_last = 100 * pdi[-1] / atr[-1] if atr[-1] else 0
    m_last = 100 * ndi[-1] / atr[-1] if atr[-1] else 0
    return (round(a, 1), round(p_last, 1), round(m_last, 1))


def window_stat(series, n, lag=0, z=1.96, voln=VOL_N):
    """Cumulative drift over n bars with a 95% CI.

    The point estimate uses exactly n bars. The dispersion behind the interval is measured over `voln`
    bars and scaled by sqrt(n) — A ONE-DAY MOVE CANNOT ESTIMATE ITS OWN NOISE FROM ONE OBSERVATION,
    and a band built that way would be a lie. `lag` shifts the whole window back, giving the position
    the rotation moved FROM.
    """
    if lag:
        series = series[:len(series) - lag]
    if len(series) <= max(n, voln):
        return None

    def lr(i):
        return math.log(series[i][4] / series[i - 1][4])

    cum = sum(lr(i) for i in range(len(series) - n, len(series)))
    v = [lr(i) for i in range(len(series) - voln, len(series))]
    m = sum(v) / voln
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (voln - 1))
    half = z * sd * math.sqrt(n)
    return dict(
        est=(math.exp(cum) - 1) * 100,
        lo=(math.exp(cum - half) - 1) * 100,
        hi=(math.exp(cum + half) - 1) * 100,
        t=(cum / (sd * math.sqrt(n))) if sd else 0.0,
        sig=abs(cum) > half,
        vol=sd * math.sqrt(252) * 100,
    )


def verdict(w, d):
    """WEEKLY GOVERNS, DAILY TIMES — the same hierarchy the single-name grade uses.

    ON      weekly above cloud + daily above tenkan   numerator leg is winning
    ON-wk   weekly above cloud, daily broke tenkan    trend intact, pausing
    TURN    weekly IN the cloud                       no gate, transition
    OFF-wk  weekly below cloud, daily reclaimed       denominator winning, bouncing
    OFF     weekly below cloud + daily below tenkan   denominator leg is winning
    """
    if w["pos"] == "ABV":
        return ("🟢 ON", "trend intact") if d["abv"] else ("🟡 ON-wk", "daily broke tenkan")
    if w["pos"] == "BLW":
        return ("🟠 OFF-wk", "daily reclaimed tenkan") if d["abv"] else ("🔴 OFF", "denominator winning")
    return ("⬜ TURN", "weekly in cloud — no gate")


def read_pair(row, bars_for_ticker):
    """One axis. `bars_for_ticker(ticker) -> list[tuple]`, or raises if it has none.

    INJECTED, NOT MONKEYPATCHED. The original resolved `fetch` from module globals, so the adapter had
    to swap a module attribute and put it back in a `finally`. Passing the accessor removes that.

    Failure is PER AXIS: one unavailable ETF costs its own axes and not the whole payload.
    """
    group, label, num, den, meaning = row
    try:
        a = bars_for_ticker(num)
        b = bars_for_ticker(den)
        r = ratio_bars(a, b)
        if len(r) < MIN_RATIO_BARS:
            return dict(group=group, label=label, pair=f"{num}/{den}", err="thin history")
        d = ichi(r)
        w = ichi(weekly(r))
        adx, _pdi, _ndi = adx_di(r)
        v, note = verdict(w, d)
        win = {}
        for name, n in WINDOWS:
            cur, prv = window_stat(r, n), window_stat(r, n, lag=n)
            if cur:
                cur["prev"] = prv["est"] if prv else None
                cur["move"] = (cur["est"] - prv["est"]) if prv else None
                cur["n"] = n
            win[name] = cur
        s21, s63 = win["1M"], win["3M"]
        accel = ((s21["est"] / 21) - (s63["est"] / 63)) if (s21 and s63) else None
        return dict(group=group, label=label, pair=f"{num}/{den}", meaning=meaning,
                    verdict=v, note=note, wk=w["pos"], wkcolor=w["color"],
                    d_pos=d["pos"], d_abv=d["abv"], d_TgtK=d["TgtK"], adx=adx,
                    c5=pct(r, 5), c21=pct(r, 21), c63=pct(r, 63),
                    num=num, den=den, win=win, accel=accel,
                    px=d["px"], err=None)
    except Exception as e:                                              # noqa: BLE001
        return dict(group=group, label=label, pair=f"{num}/{den}", err=str(e)[:40])


def tickers() -> list[str]:
    """Every ticker any axis needs. What the engine must have bars for."""
    return sorted({t for _g, _l, num, den, _m in AXES for t in (num, den)})
