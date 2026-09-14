"""Rotation graded off our own bars, not a second feed (#384 step 2).

The file-backed payload had no scheduler and went 18 hours stale. Even refreshed on a timer it is a
SECOND data source: the cockpit would grade rotations off Yahoo while trading off Alpaca bars, and the
two can disagree about the same session. This is the seam that removes it.
"""

from __future__ import annotations

import sys

import pytest

from strategies.rotation_from_cache import (
    bars_to_tuples,
    build_payload,
)
from strategies.rotation_grade import MIN_RATIO_BARS

FINTRACK = "/fintrack/tools"

#: SCOPED TO THE TESTS THAT ACTUALLY NEED THE MOUNT. This was a module-level `pytestmark`, so all six
#: tests skipped on any host without `/fintrack` — which is every developer machine and every CI run,
#: leaving the converters covered by nothing. Only `build_payload` imports fintrack's module; the
#: converters are pure and must run everywhere.
needs_fintrack = pytest.mark.skipif(
    not __import__("os").path.exists(f"{FINTRACK}/rotation_read.py"),
    reason="fintrack tools are not mounted in this container",
)


class _Bar:
    """Shaped like Nautilus's Bar where this code touches it — ns timestamps, Decimal-ish prices."""

    def __init__(self, ts_ns: int, o: float, h: float, l: float, c: float, v: float = 1.0):
        self.ts_event = ts_ns
        self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v


def _et_ns(y: int, m: int, d: int) -> int:
    """A daily bar stamped 00:00 ET, the way Alpaca emits one."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    return int(_dt.datetime(y, m, d, 0, 0, tzinfo=ZoneInfo("US/Eastern")).timestamp() * 1_000_000_000)


def _session(y: int, m: int, d: int) -> int:
    """The canonical key the converter emits: epoch seconds at 00:00 UTC of that ET session date."""
    import datetime as _dt

    return int(_dt.datetime(y, m, d, tzinfo=_dt.UTC).timestamp())


def test_timestamps_become_SECONDS_not_nanoseconds():
    """THE SILENT FAILURE THIS GUARDS.

    `weekly()` calls `dt.date.fromtimestamp(ts)` and `ratio_bars` joins the two legs BY TIMESTAMP. Feed it
    nanoseconds and the join simply never matches — an EMPTY ratio series, reported as "thin history",
    with no error anywhere. Wrong units here do not raise; they produce a plausible-looking refusal.
    """
    # A bar stamped 00:00 ET, which is what Alpaca emits for a daily bar. The converter now keys by
    # the ET SESSION DATE and returns epoch seconds at 00:00 UTC of that date — a canonical integer
    # that `weekly()`'s `dt.date.fromtimestamp` reads back as the same calendar day whatever the host
    # timezone is. It currently depends on that, and should not.
    rows = bars_to_tuples([_Bar(_et_ns(2026, 8, 6), 1, 2, 0.5, 1.5)])
    assert rows[0][0] == _session(2026, 8, 6)
    assert 1_000_000_000 < rows[0][0] < 4_000_000_000, "not a plausible epoch-seconds value"


def test_bars_come_back_ASCENDING():
    """`ichi` and `window_stat` index from the end and `weekly` folds in order; Nautilus's cache returns
    newest-first. Reversed input yields a real number computed from the wrong end of history."""
    a = _Bar(_et_ns(2026, 8, 7), 1, 1, 1, 1)
    b = _Bar(_et_ns(2026, 8, 6), 2, 2, 2, 2)
    assert [r[0] for r in bars_to_tuples([a, b])] == [_session(2026, 8, 6), _session(2026, 8, 7)]


def test_the_SAME_SESSION_arriving_TWICE_is_deduped():
    """Daily bars reach the cache by more than one path — a strategy's subscription and the compass's
    historical request. A duplicate session silently doubles that day's weight in `weekly` and shifts
    every window, and a row COUNT cannot see it: two rows, one day."""
    early = _Bar(_et_ns(2026, 8, 6), 1, 1, 1, 1)
    late = _Bar(_et_ns(2026, 8, 6) + 14 * 3600 * 1_000_000_000, 9, 9, 9, 9)
    rows = bars_to_tuples([early, late])
    assert len(rows) == 1, f"the same session survived twice: {rows}"
    assert rows[0][4] == 9, "last write should win — the later delivery is the more complete bar"


def test_a_zero_leg_is_dropped_rather_than_dividing():
    """`ratio_bars` divides by the denominator's OHLC. A zero would raise mid-payload."""
    assert bars_to_tuples([_Bar(1_000_000_000_000_000_000, 0, 1, 1, 1)]) == []


@needs_fintrack
def test_one_missing_ticker_costs_its_own_axes_and_not_the_payload():
    """`read_pair` catches per axis. Returning [] instead of raising would hand it an empty series, which
    it would report as "thin history" — the same wrong reason for every axis, and unactionable."""
    payload = build_payload(lambda _t: [])
    assert payload["axes"] == []
    assert payload["errors"], "every axis failed and nothing recorded why"


@needs_fintrack
def test_the_payload_names_WHICH_FEED_graded_it():
    """Yahoo and Alpaca can disagree about the same session. An operator comparing two payloads has to be
    able to tell which one produced which."""
    payload = build_payload(lambda _t: [])
    assert payload["source"] == "engine:nautilus-cache"
    assert payload["generated"]


@needs_fintrack
def test_the_history_floor_matches_what_read_pair_actually_refuses():
    """Two derivations of one fact: the bar request and the refusal threshold live in different files and
    must agree, or the engine requests history that is one bar short of usable and nothing says why."""
    sys.path.insert(0, FINTRACK)
    import rotation_read

    src = __import__("inspect").getsource(rotation_read.read_pair)
    assert f"< {MIN_RATIO_BARS}" in src, (
        f"read_pair's history floor changed; MIN_RATIO_BARS={MIN_RATIO_BARS} no longer matches it"
    )


# ==================================================================================================
# The REST converter (#384). Same feed as the cache, different transport — the cache holds only the
# QC345 window (288 daily bars) and `read_pair` refuses a ratio series shorter than 400, so the deep
# history has to come over `/v2/stocks/{sym}/bars`.
# ==================================================================================================

_ALPACA_ROW = {"t": "2026-08-20T04:00:00Z", "o": 44.1, "h": 45.0, "l": 43.9, "c": 44.62, "v": 1234}


def test_the_fixture_is_shaped_like_ALPACA_not_like_nautilus():
    # The fixture's own property first: an RFC3339 string `t`, not an ns integer. A converter tested
    # against the wrong shape proves nothing about the payload it will actually meet.
    assert isinstance(_ALPACA_ROW["t"], str) and _ALPACA_ROW["t"].endswith("Z")


# `alpaca_bars_to_tuples` AND ITS TESTS ARE GONE (2026-08-26). It converted raw rows from Alpaca's
# `/v2/stocks/{sym}/bars`, because the market compass fetched its deep history over that vendor's REST
# API — which made the whole feature Alpaca-only and silently absent on any other provider. The bars
# now come from the Nautilus cache through `bars_to_tuples`, which every adapter fills.
#
# 5 tests removed with it. They were correct about the converter and the converter should not exist.
