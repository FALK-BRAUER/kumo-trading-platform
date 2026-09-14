"""The port must AGREE WITH THE ORIGINAL, bar for bar, before the original is deleted.

`strategies/rotation_grade.py` moves ~350 lines of ratio-and-Ichimoku maths out of
`fintrack/tools/{rotation_read,grade_full}.py` and into kumo. A port is exactly the case where reading
the diff and pronouncing it correct is worthless — the two implementations either produce the same
numbers over the same bars or they do not, and that is measurable.

VERIFICATION BY DISAGREEMENT. Run both over identical input, compare every field of all 25 axes. Any
divergence is a defect in the port, and the comparison is only possible while the original still
exists — which is why this lands BEFORE the mount is removed.

It skips, loudly, where the original is absent. A skip here is not a pass: `test_the_ORIGINAL_is_
present_or_this_file_proves_nothing` states plainly that once the mount is gone this file no longer
compares anything, and the golden values recorded by `test_the_port_still_produces_the_recorded_
verdicts` are what remain.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

from . import rotation_grade as PORT

ORIGINAL_DIR = os.environ.get("ROTATION_ORIGINAL_DIR", "~/projects/fintrack/tools")
_HAVE_ORIGINAL = os.path.isdir(ORIGINAL_DIR)


def _original():
    if ORIGINAL_DIR not in sys.path:
        sys.path.insert(0, ORIGINAL_DIR)
    import rotation_read

    return rotation_read


def _bars(seed: int, n: int = 900):
    """Deterministic pseudo-random OHLC with drift, enough history for a >400-bar ratio.

    Not random: a fixture that changed between the two runs would make any comparison meaningless.
    Daily timestamps at midnight UTC, oldest first — the shape `alpaca_bars_to_tuples` emits.
    """
    out = []
    px = 50.0 + seed
    for i in range(n):
        # deterministic wobble, different per ticker, with a slow trend so verdicts are not all TURN
        wobble = math.sin((i + seed * 7) / 11.0) * 0.9 + math.cos((i + seed * 3) / 29.0) * 1.4
        px = max(1.0, px * (1 + (wobble + (seed % 5 - 2) * 0.35) / 400.0))
        hi = px * 1.006
        lo = px * 0.994
        op = (hi + lo) / 2
        out.append((86400 * (19000 + i), op, hi, lo, px, 1000 + i))
    return out


@pytest.fixture(scope="module")
def book():
    return {t: _bars(i + 1) for i, t in enumerate(PORT.tickers())}


def test_the_ORIGINAL_is_present_or_this_file_proves_nothing():
    """Stated rather than skipped silently. Once the mount is deleted this file compares NOTHING, and
    the only thing standing behind the port is the recorded verdicts below."""
    if not _HAVE_ORIGINAL:
        pytest.skip(f"original absent at {ORIGINAL_DIR} — the comparison tests below are inert, and "
                    f"only the recorded-verdict test still constrains the port")


def test_the_FIXTURE_produces_a_ratio_long_enough_to_grade(book):
    """A fixture whose ratio is under 400 bars makes every axis 'thin history', and then the two
    implementations agree on nothing but an error string."""
    r = PORT.ratio_bars(book["IWM"], book["SPY"])
    assert len(r) >= PORT.MIN_RATIO_BARS, f"ratio is {len(r)} bars — every axis would be thin history"


def test_the_FIXTURE_produces_MORE_THAN_ONE_VERDICT(book):
    """If every axis graded identically the comparison would pass over a constant. Asserted so a
    future change to the fixture cannot quietly make this file vacuous."""
    verdicts = {PORT.read_pair(row, book.__getitem__)["verdict"] for row in PORT.AXES}
    assert len(verdicts) > 1, f"every axis produced the same verdict {verdicts} — nothing discriminates"


@pytest.mark.skipif(not _HAVE_ORIGINAL, reason="original not mounted")
def test_EVERY_AXIS_matches_the_original_field_for_field(book):
    """THE POINT OF THIS FILE."""
    rr = _original()

    def fetch(ticker, _rng=None, _iv=None):
        return book[ticker]

    saved, rr.fetch = rr.fetch, fetch
    try:
        theirs = [rr.read_pair(row) for row in rr.AXES]
    finally:
        rr.fetch = saved

    mine = [PORT.read_pair(row, book.__getitem__) for row in PORT.AXES]

    assert len(mine) == len(theirs) == 25
    for m, t in zip(mine, theirs, strict=True):
        assert m.keys() == t.keys(), f"{m['pair']}: key sets differ {m.keys() ^ t.keys()}"
        for k in t:
            assert _same(m[k], t[k]), f"{t['pair']} field {k!r}: port={m[k]!r} original={t[k]!r}"


def _same(a, b, tol=1e-9):
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return abs(a - b) <= tol * max(1.0, abs(a), abs(b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y, tol) for x, y in zip(a, b, strict=True))
    return a == b


@pytest.mark.skipif(not _HAVE_ORIGINAL, reason="original not mounted")
def test_the_AXIS_TABLE_and_WINDOWS_were_copied_exactly(book):
    """25 axes, 27 tickers, five windows. A dropped row would silently shrink the market view, and
    every remaining axis would still match."""
    rr = _original()
    assert list(PORT.AXES) == list(rr.AXES)
    assert list(PORT.WINDOWS) == list(rr.WINDOWS)
    assert PORT.VOL_N == rr.VOL_N


#: WHAT THE ORIGINAL PRODUCED, captured 2026-08-26 while the mount still existed.
#:
#: THIS IS THE ONLY EVIDENCE THE TWO IMPLEMENTATIONS EVER AGREED. Every comparison test above goes
#: inert once the mount is gone, and it is gone — nobody can re-run them. kumo-strategies made the
#: point and it is right: the record has to be durable, not left in a review.
#:
#: pair -> (verdict, weekly pos, daily pos, ADX, 1M drift %, daily TENKAN, daily KIJUN)
#:
#: TENKAN AND KIJUN ARE IN THE RECORD DELIBERATELY. Without them the tenkan 9->10 mutation
#: was caught ONLY by the function-level comparison — which goes inert the moment the mount
#: is gone. The record has to carry the raw number the boolean collapse hid, or the gap
#: reopens the day this file stops being able to compare anything.
#:
#: Four of the five verdict kinds appear (ON, ON-wk, OFF, OFF-wk). No axis sat IN the weekly cloud on
#: this fixture, so TURN is unexercised — a mutation that only changed that branch would not be caught
#: here, and that gap is stated rather than left for someone to assume away.
AGREED = {
    'EEM/SPY': ('🟠 OFF-wk', 'BLW', 'BLW', 56.2, 1.096314, 0.330268416, 0.331695423),
    'EFA/SPY': ('🟠 OFF-wk', 'BLW', 'BLW', 62.9, 6.032034, 0.735208788, 0.728853206),
    'GDX/GLD': ('🔴 OFF', 'BLW', 'BLW', 99.6, -2.595215, 0.448106611, 0.455710001),
    'GLD/SPY': ('🟢 ON', 'ABV', 'ABV', 84.2, 13.373997, 3.656806595, 3.540212998),
    'HYG/IEI': ('🟠 OFF-wk', 'BLW', 'BLW', 47.8, 0.233896, 0.445759888, 0.445989976),
    'IGV/SMH': ('🟢 ON', 'ABV', 'ABV', 98.1, 1.322416, 4.109405254, 4.093653425),
    'IWF/IWD': ('🟢 ON', 'ABV', 'ABV', 67.7, 1.335794, 2.230760689, 2.22691911),
    'IWM/SPY': ('🟠 OFF-wk', 'BLW', 'BLW', 62.9, -1.442905, 0.179983861, 0.183055003),
    'KRE/XLU': ('🟢 ON', 'ABV', 'ABV', 80.3, 7.530417, 1.665597422, 1.64692448),
    'MDY/SPY': ('🟠 OFF-wk', 'BLW', 'ABV', 69.3, 6.619534, 0.895150443, 0.885358298),
    'MTUM/USMV': ('🟠 OFF-wk', 'BLW', 'ABV', 82.1, 9.568933, 0.400675514, 0.389457567),
    'RSP/SPY': ('🟢 ON', 'ABV', 'ABV', 90.9, 11.969622, 4.467550829, 4.31789114),
    'SMH/XLK': ('🔴 OFF', 'BLW', 'IN', 58.7, 1.538794, 0.181691651, 0.179212424),
    'SPHQ/SPY': ('🟠 OFF-wk', 'BLW', 'IN', 66.6, 1.243172, 0.445978851, 0.443866116),
    'TLT/IEI': ('🟡 ON-wk', 'ABV', 'IN', 71.4, -4.542641, 6.14544624, 6.24690063),
    'XLE/IWF': ('🔴 OFF', 'BLW', 'BLW', 99.4, -10.577622, 0.054173605, 0.05688115),
    'XLE/SPY': ('🔴 OFF', 'BLW', 'BLW', 98.4, -6.081957, 0.220392799, 0.229445877),
    'XLI/XLU': ('🟢 ON', 'ABV', 'ABV', 56.9, 2.063335, 2.03684545, 2.032923107),
    'XLI/XLV': ('🟠 OFF-wk', 'BLW', 'ABV', 62.2, 3.351846, 0.910129796, 0.906488537),
    'XLI/XLY': ('🟠 OFF-wk', 'BLW', 'ABV', 67.1, 4.670234, 0.407515783, 0.404647292),
    'XLK/SPY': ('🟠 OFF-wk', 'BLW', 'BLW', 51.8, 0.731906, 1.09326243, 1.095940394),
    'XLP/SPY': ('🟢 ON', 'ABV', 'BLW', 61.5, 3.471081, 2.436396217, 2.420693139),
    'XLRE/KRE': ('🟡 ON-wk', 'ABV', 'ABV', 72.5, 2.244153, 13.545628895, 13.303932045),
    'XLV/SPY': ('🔴 OFF', 'BLW', 'BLW', 99.5, -5.929825, 0.539697147, 0.552929913),
    'XLY/XLP': ('🔴 OFF', 'BLW', 'BLW', 87.3, -10.230665, 0.495069275, 0.511601756),
}


def test_the_port_still_produces_WHAT_THE_ORIGINAL_PRODUCED(book):
    """The port pinned against RECORDED OUTPUT, not against its own behaviour.

    Numbers, not just verdicts. A verdict is a boolean collapse: the tenkan 9->10 mutation moved every
    midpoint and flipped no verdict across all 25 axes, so a verdict-only record would accept exactly
    the wrong maths this file exists to catch.
    """
    for row in PORT.AXES:
        r = PORT.read_pair(row, book.__getitem__)
        pair = r["pair"]
        assert pair in AGREED, f"{pair} is a new axis with no recorded agreement"
        v, wk, dpos, adx, est, tk, kj = AGREED[pair]
        assert r["verdict"] == v, f"{pair} verdict: {r['verdict']!r} != recorded {v!r}"
        assert r["wk"] == wk and r["d_pos"] == dpos, f"{pair} position drifted from the record"
        assert _same(r["adx"], adx), f"{pair} adx: {r['adx']} != recorded {adx}"
        assert _same(round(r["win"]["1M"]["est"], 6), est), \
            f"{pair} 1M drift: {round(r['win']['1M']['est'], 6)} != recorded {est}"
        d = PORT.ichi(PORT.ratio_bars(book[r["num"]], book[r["den"]]))
        assert _same(round(d["tk"], 9), tk), f"{pair} tenkan: {d['tk']} != recorded {tk}"
        assert _same(round(d["kj"], 9), kj), f"{pair} kijun: {d['kj']} != recorded {kj}"
    assert len(AGREED) == 25


# --------------------------------------------------------------------------------------------------
# FUNCTION BY FUNCTION, NOT ONLY END TO END.
#
# The axis-level comparison passed with the tenkan period changed from 9 to 10. `tk` feeds `TgtK` and
# `abv`, which are BOOLEANS — a different midpoint moved every number and flipped not one of them
# across 25 axes. Identical output, wrong maths.
#
# Nothing was faked to produce that: real code, real fixture, a real comparison across every axis. The
# comparison was simply made through a LOSSY PROJECTION, and a boolean cannot report a small numeric
# error. Adding axes would not have helped — they all collapse the same way. Compare where the
# information still exists, upstream of the collapse.
# --------------------------------------------------------------------------------------------------


@pytest.mark.skipif(not _HAVE_ORIGINAL, reason="original not mounted")
def test_ICHI_matches_number_for_number_not_just_verdict_for_verdict(book):
    import grade_full

    for t, bars in book.items():
        assert _same(PORT.ichi(bars), grade_full.ichi(bars)), f"ichi diverges on {t}"
        assert _same(PORT.ichi(PORT.weekly(bars)), grade_full.ichi(grade_full.weekly(bars))), \
            f"weekly ichi diverges on {t}"


@pytest.mark.skipif(not _HAVE_ORIGINAL, reason="original not mounted")
def test_WEEKLY_and_ADX_match_number_for_number(book):
    import grade_full

    for t, bars in book.items():
        assert _same(PORT.weekly(bars), grade_full.weekly(bars)), f"weekly diverges on {t}"
        assert _same(PORT.adx_di(bars), grade_full.adx_di(bars)), f"adx_di diverges on {t}"


@pytest.mark.skipif(not _HAVE_ORIGINAL, reason="original not mounted")
def test_RATIO_WINDOW_and_PCT_match_number_for_number(book):
    rr = _original()

    for _group, _label, num, den, _m in PORT.AXES:
        r_mine = PORT.ratio_bars(book[num], book[den])
        r_theirs = rr.ratio_bars(book[num], book[den])
        assert _same(r_mine, r_theirs), f"ratio_bars diverges on {num}/{den}"
        for _name, n in PORT.WINDOWS:
            assert _same(PORT.window_stat(r_mine, n), rr.window_stat(r_theirs, n)), \
                f"window_stat({n}) diverges on {num}/{den}"
            assert _same(PORT.window_stat(r_mine, n, lag=n), rr.window_stat(r_theirs, n, lag=n)), \
                f"lagged window_stat({n}) diverges on {num}/{den}"
        for n in (5, 21, 63):
            assert _same(PORT.pct(r_mine, n), rr.pct(r_theirs, n)), f"pct({n}) diverges on {num}/{den}"
