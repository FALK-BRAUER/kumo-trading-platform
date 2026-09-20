"""A malformed tick costs its ROW, never the CONNECTION (#698).

Measured on an Alpaca paper instance 2026-08-28 23:00-23:30 UTC (market closed), 12 times:

    [ERROR] DataClient-ALPACA: Alpaca WS read error:
            ValueError("'size' not a positive integer, was 0"); reconnecting

Alpaca prints size-0 trades routinely overnight (corrections, odd-lot artefacts). Quantity refuses
0, the exception escapes the frame handler, and the READ LOOP treats it as a broken socket — full
reconnect plus a resubscribe burst, every couple of minutes, for as long as such prints arrive.
The quote branch already guards this shape (`bp/ap <= 0` → skip the row); the trade branch never
got the same fix. Third venue surface of the one-bad-row-costs-the-batch class (#643, #697).
"""

from __future__ import annotations

from types import SimpleNamespace

from api.providers.alpaca import data_client as mod


class _Instrument:
    """Rejects what production rejects: Nautilus's Quantity refuses a non-positive size, so a
    double that accepts 0 would make this test pass against the very defect it exists for."""

    id = "AAPL.XNAS"
    price_precision = 2
    size_precision = 0


def _host(frames_handled):
    host = SimpleNamespace(
        _live_trade_ids={"AAPL": "AAPL.XNAS"},
        _live_quote_ids={},
        _instrument_provider=SimpleNamespace(find=lambda _i: _Instrument()),
        _handle_data=lambda d: frames_handled.append(d),
        _parse_trade=_parse_trade_like_production,
        _log=SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None),
    )
    # The wrapper delegates to the inner router; bind the REAL one so the test drives production's
    # path rather than a stand-in for it.
    host._on_ws_frame_inner = lambda f: mod.AlpacaDataClient._on_ws_frame_inner(host, f)
    return host


def _parse_trade_like_production(_instrument, frame):
    """What production does with the size — the raise is the point."""
    size = int(frame.get("s") or 0)
    if size <= 0:
        raise ValueError(f"'size' not a positive integer, was {size}")
    return SimpleNamespace(size=size)


def test_the_fixture_raises_where_production_raises():
    """FIXTURE PROPERTY first: a size-0 frame really does blow up the parse — without this the
    test below could pass against a double that quietly tolerates zero."""
    try:
        _parse_trade_like_production(_Instrument(), {"S": "AAPL", "p": 100.0, "s": 0})
    except ValueError:
        return
    raise AssertionError("the double accepts size 0 — it cannot represent the defect")


def test_a_zero_size_trade_is_SKIPPED_not_raised():
    handled = []
    host = _host(handled)
    mod.AlpacaDataClient._on_ws_frame(host, {"T": "t", "S": "AAPL", "p": 100.0, "s": 0})
    assert handled == [], "a zero-size print was handled as a real tick"


def test_a_REAL_trade_still_flows():
    """The other direction: the guard must not eat live ticks — that would be a dead feed wearing
    a fix."""
    handled = []
    host = _host(handled)
    mod.AlpacaDataClient._on_ws_frame(host, {"T": "t", "S": "AAPL", "p": 100.0, "s": 25})
    assert len(handled) == 1 and handled[0].size == 25


def test_ANY_malformed_frame_costs_its_row_not_the_socket():
    """REVIEW FINDING (2026-08-29): the size guard is an INSTANCE fix. A bad price, an unparseable
    timestamp or a non-numeric size all still escaped into the read loop, which reads any exception
    as a broken socket. The class is closed by a counted drop at the frame boundary."""
    handled = []
    host = _host(handled)
    host._parse_trade = lambda _i, _f: (_ for _ in ()).throw(ValueError("bad price 'x'"))
    # Must not raise — the read loop stays up.
    mod.AlpacaDataClient._on_ws_frame(host, {"T": "t", "S": "AAPL", "p": "x", "s": 25})
    assert handled == []
    assert getattr(host, "_malformed_frames", 0) == 1, "the drop was not counted — a silent feed hole"
