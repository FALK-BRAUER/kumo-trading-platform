"""The 1D equity curve must be a real series, at intraday resolution, including extended hours (#536).

WHAT WAS ON SCREEN. A perfectly straight diagonal from 103.6K to 103.9K, with the headline reading
$103,963 above a chart topping out at ~103.9K.

It was not a rendering artifact. `_EQUITY_PERIODS` fetched `1W/1H`, `1M/1D`, `3M/1D`, `all/1D` and
NO 1D series at all, so `sessionCurve.ts` derived 1D by slicing the 1W/HOURLY series to today's
session (#345 item 4). Ten minutes into a session that is one or two points — drawn as a line.

MEASURED against the live paper account, `period=1D`, 2026-08-25 09:45 ET:

    timeframe   market_hours   extended_hours
    1Min              94             424
    5Min              19              85
    15Min              7              29
    1H                 2               8      <- what we drew

`1H / market_hours = 2 points` is the screenshot exactly.

THE STALE-BUCKET HALF, from the same measurement. The hourly series' final bucket is not closed:

    1H          last equity  103,890.31
    5Min        last equity  104,008.74

so the header (which reads the account) and the chart (which read a stale hourly bucket) disagreed by
about a hundred dollars. A fine-grained series fixes that as a side effect, and this file pins it.

EXTENDED HOURS IS THE POINT, NOT THE RESOLUTION. It multiplies the points ~4.5x, but what matters is
that the curve STARTS AT THE PRE-MARKET OPEN rather than 09:30. The lanes decide at open+5m and
open+150m, and #302 puts ~6 points of MOMENTUM's backtest/live gap on fill timing — the pre-market
segment is not decoration.

`get_portfolio_history` has ACCEPTED `extended_hours` since it was written and no caller has ever
passed `True`. A parameter accepted and discarded, which is a shape this repo keeps finding.
"""

from __future__ import annotations

from api.providers.alpaca.exec_client import _EQUITY_PERIODS


def test_the_fixture_can_see_the_period_table():
    """THE FIXTURE'S OWN PROPERTY FIRST. Every assertion below reads one tuple; if it moved or were
    empty they would all pass over nothing."""
    assert _EQUITY_PERIODS, "_EQUITY_PERIODS is empty — this test is blind"
    assert all(len(p) >= 2 for p in _EQUITY_PERIODS), "entries are not (period, timeframe, ...)"


def test_a_1D_SERIES_EXISTS():
    """THE DEFECT. There was no 1D period, so the default tab had nothing of its own to draw."""
    periods = {p[0] for p in _EQUITY_PERIODS}
    assert "1D" in periods, (
        f"no 1D series in {sorted(periods)}. The default tab then derives one by slicing the 1W/HOURLY "
        f"series, which is 2 points ten minutes into a session — the straight-line chart of 2026-08-25"
    )


def test_the_1D_series_is_INTRADAY_resolution():
    """Hourly is what produced the diagonal. Anything hourly or coarser is the same defect back."""
    tf = next(p[1] for p in _EQUITY_PERIODS if p[0] == "1D")
    assert tf in ("1Min", "5Min", "15Min"), (
        f"the 1D series is fetched at {tf!r}. Measured on the live account, `1H` returns TWO points for "
        f"period=1D and `1D` returns one — neither is a curve"
    )


def test_the_1D_series_asks_for_EXTENDED_HOURS():
    """Otherwise the curve starts at 09:30 and the pre-market segment — where the open+5m lane decides
    — is simply not drawn. 94 points market-hours vs 424 extended, measured."""
    entry = next(p for p in _EQUITY_PERIODS if p[0] == "1D")
    assert len(entry) >= 3 and entry[2] is True, (
        f"the 1D entry is {entry!r} and does not request extended hours. `get_portfolio_history` has "
        f"accepted `extended_hours` since it was written and no caller has ever passed True"
    )


def test_the_LONGER_windows_are_unchanged():
    """The regression this could easily introduce. 1W/1H is what the derived curve used and what the
    week tab still draws; 1M/3M/all stay daily because a month at 1Min is 30k points nobody reads."""
    by_period = {p[0]: p[1] for p in _EQUITY_PERIODS}
    assert by_period.get("1W") == "1H", f"1W changed to {by_period.get('1W')!r}"
    for w in ("1M", "3M", "all"):
        assert by_period.get(w) == "1D", f"{w} changed to {by_period.get(w)!r}"


def test_extended_hours_is_NOT_requested_for_the_longer_windows():
    """Extended hours on a daily series changes the session boundaries for no benefit, and on 1W it
    would silently change what the derived curve has always sliced."""
    for entry in _EQUITY_PERIODS:
        if entry[0] == "1D":
            continue
        ext = entry[2] if len(entry) >= 3 else False
        assert ext is False, f"{entry[0]} now requests extended hours: {entry!r}"


def test_the_fetch_PASSES_extended_hours_through_to_the_broker_call():
    """THE SEAM. A table entry nothing reads is the dead-parameter shape all over again — pin that the
    flag reaches `get_portfolio_history`, not merely that it is written down."""
    import inspect

    from api.providers.alpaca import exec_client

    src = inspect.getsource(exec_client._ReportingMixin._report_equity_curve) \
        if hasattr(exec_client, "_ReportingMixin") else inspect.getsource(exec_client)
    assert "extended_hours=" in src, (
        "`_report_equity_curve` never passes `extended_hours` to `get_portfolio_history`. The table "
        "would declare it and the call would ignore it — accepted and discarded, again"
    )
