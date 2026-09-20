"""Tests for session realized P&L (#233).

The operator saw `REALIZED $0.00` on Home while four positions had been closed that day for +$1,451.77. The
figure was not wrong — it was structurally unreachable, because a CLOSED cycle is emitted once and
dropped, and the tile summed only live cycles.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.realized import et_day_bounds_ns, realized_windows


def session_realized(positions_closed, positions_open, *, session_start_ns: int, session_end_ns: int):
    """The session rules, pinned against the ONE fold (#846): `realized_windows(...)["1D"]`, which is
    what the engine publishes as `realized_session`. The window is the ET day containing `now`; the
    tests' `_START`/`_END` are that day's bounds, so the explicit arguments here only assert the fixture
    agrees with the fold's own window."""
    start, end = et_day_bounds_ns(session_start_ns)
    assert (start, end) == (session_start_ns, session_end_ns), "fixture window must be an ET day"
    w = realized_windows(positions_closed=positions_closed, positions_open=positions_open, legs=[],
                         now_ns=session_start_ns)["1D"]
    return SimpleNamespace(**w)

_DAY = 24 * 3600 * 1_000_000_000
#: An ET calendar day (#846) — the fold's own window, so boundary tests mean what they say.
_START, _END = et_day_bounds_ns(1_786_000_000 * 1_000_000_000)


def _pos(strategy_id: str, realized: float, ts_closed: int | None):
    """A double shaped like a Nautilus `Position`.

    `realized_pnl` is a `Money` in production and is read through `as_double()`, so the double carries
    that accessor rather than a bare float — a double that only worked for floats would pass here and
    return 0.0 against the real object.
    """
    money = SimpleNamespace(as_double=lambda: realized)
    # `id` and `ts_opened` are what the legs union dedups by (#846); a double without them would
    # collapse every position onto one key and pass a test that counts one where production counts many.
    _pos.n = getattr(_pos, "n", 0) + 1
    return SimpleNamespace(strategy_id=strategy_id, realized_pnl=money, ts_closed=ts_closed,
                           id=f"P-{_pos.n}", ts_opened=(ts_closed or 0) - 1)


def test_the_days_closes_are_summed_per_strategy():
    """The live case, reconstructed: PEAK closed four MANUAL positions and the tile read $0.00."""
    closed = [
        _pos("MANUAL-001", 492.64, _START + 3600 * 1_000_000_000),    # FIG
        _pos("MANUAL-001", 71.28, _START + 3700 * 1_000_000_000),     # OKTA
        _pos("MANUAL-001", -80.70, _START + 3800 * 1_000_000_000),    # PBF
        _pos("MOMENTUM-002", 968.37, _START + 3900 * 1_000_000_000),  # WDAY
    ]
    r = session_realized(closed, [], session_start_ns=_START, session_end_ns=_END)

    assert round(r.by_strategy["MANUAL-001"], 2) == 483.22
    assert round(r.by_strategy["MOMENTUM-002"], 2) == 968.37
    assert round(r.total, 2) == 1451.59
    assert r.closed_count == {"MANUAL-001": 3, "MOMENTUM-002": 1}


def test_a_position_closed_YESTERDAY_is_not_todays_realized():
    """Attribution is by CLOSE date. Without the window every close since the engine started would
    land in today, and the figure would only ever grow."""
    closed = [_pos("MANUAL-001", 500.0, _START - 3600 * 1_000_000_000)]
    r = session_realized(closed, [], session_start_ns=_START, session_end_ns=_END)
    assert r.by_strategy == {} and r.total == 0.0


def test_the_window_is_half_open_so_a_boundary_close_is_counted_once():
    """A close landing exactly on the boundary must belong to ONE session. Double-counting inflates a
    number an operator judges the day by, which is worse than missing one."""
    at_start = session_realized([_pos("M", 10.0, _START)], [], session_start_ns=_START,
                                session_end_ns=_END)
    at_end = session_realized([_pos("M", 10.0, _END)], [], session_start_ns=_START,
                              session_end_ns=_END)
    assert at_start.total == 10.0, "the session's own opening instant was excluded"
    assert at_end.total == 0.0, "the next session's opening instant was claimed by this one"


def test_a_still_open_position_with_partial_realized_is_counted_not_summed():
    """Honesty about what native cannot answer.

    A partial exit realizes P&L on a position that is still open, and Nautilus timestamps the
    position's CLOSE, not each realization. Attributing it to a session would need P&L rebuilt from
    fills — the parallel ledger this deliberately avoids. So it is reported as incompleteness.
    """
    open_positions = [_pos("MANUAL-001", 120.0, None), _pos("MANUAL-001", 0.0, None)]
    r = session_realized([], open_positions, session_start_ns=_START, session_end_ns=_END)

    assert r.total == 0.0, "guessed at when a partial exit was realized"
    assert r.partial_open == 1
    assert r.is_partial is True


def test_a_clean_book_reports_complete_not_partial():
    """The counter-case, so `is_partial` is not simply always true and therefore ignorable."""
    r = session_realized([_pos("M", 5.0, _START + 1)], [_pos("M", 0.0, None)],
                         session_start_ns=_START, session_end_ns=_END)
    assert r.is_partial is False and r.total == 5.0


def test_a_position_with_no_close_timestamp_is_skipped_not_counted_as_now():
    """A closed position missing `ts_closed` is unattributable. Treating a missing timestamp as the
    current session is how a restart's backfill lands entirely in today."""
    r = session_realized([_pos("M", 999.0, None)], [], session_start_ns=_START, session_end_ns=_END)
    assert r.total == 0.0


def test_money_is_read_through_the_accessor_production_uses():
    """`realized_pnl` is a Nautilus `Money`, not a float. A reader that assumed float would return 0.0
    against the real object while every float-based test passed."""
    r = session_realized([_pos("M", 12.5, _START + 1)], [], session_start_ns=_START,
                         session_end_ns=_END)
    assert r.total == 12.5


# -- the SEAM: does the published frame actually carry it -------------------------------------------
#
# Testing `session_realized` proves nothing about whether anything calls it. That is the defect class
# this repo has shipped repeatedly with a green suite, and it caught me again two commits ago — so
# these drive the real methods.


def test_the_engine_reads_realized_from_the_NATIVE_cache():
    """Drives `_session_realized` itself, against a cache double shaped like the real one."""
    from api.engine_node import UiFeedStrategy

    now = 1_786_000_000 * 1_000_000_000

    class _Cache:
        def positions_closed(self):
            return [_pos("MANUAL-001", 483.22, now)]

        def positions_open(self):
            return [_pos("MOMENTUM-002", 0.0, None)]

    class _Fake:
        cache = _Cache()
        clock = SimpleNamespace(timestamp_ns=lambda: now)
        _closed_legs: dict = {}                      # production's registry (#846); empty = no seed, no closes
        _realized_windows = UiFeedStrategy._realized_windows

    out = UiFeedStrategy._session_realized(_Fake())
    assert out["by_strategy"] == {"MANUAL-001": 483.22}
    assert out["total"] == 483.22
    assert out["is_partial"] is False


def test_the_published_TRADES_FRAME_carries_realized_session():
    """The call site. Removing the field from the payload must fail something.

    `_publish_trades` is what the UI consumes; a correct `_session_realized` that nothing puts on the
    frame leaves the tile reading $0.00 exactly as before.
    """
    import inspect

    from api.engine_node import UiFeedStrategy

    source = inspect.getsource(UiFeedStrategy._publish_trades)
    assert "realized_session" in source, "the trades frame does not carry realized_session"
    # Since #846 the session figure IS the 1D window — one derivation for Home and the panel — so the
    # frame computes `_realized_windows()` and publishes its "1D" under `realized_session`.
    assert "_realized_windows" in source, "the frame carries the key but never computes it"
    assert 'windows["1D"]' in source, "realized_session must be the 1D window, not a second derivation"


def test_the_day_window_is_an_ET_CALENDAR_day_not_the_rth_session():
    """A close can land outside 09:30-16:00 — an extended-hours exit, or a reconciliation booking a
    close after the bell. Bounding by RTH would drop those and make the day quietly short."""
    from api.engine_node import _et_day_bounds_ns

    # 2026-08-14 18:30 UTC = 14:30 ET, inside the session.
    ts = 1_786_818_600 * 1_000_000_000
    start, end = _et_day_bounds_ns(ts)
    assert start <= ts < end
    assert (end - start) in (23 * 3600 * 10**9, 24 * 3600 * 10**9, 25 * 3600 * 10**9), \
        "a calendar day must be 23, 24 or 25 hours — DST transitions are real days"

    # An after-hours close at 21:00 ET the same day must fall in the SAME window.
    after_hours = start + int(21 * 3600 * 1e9)
    assert start <= after_hours < end, "an after-hours close fell outside its own day"


def test_a_position_appearing_twice_is_counted_once():
    """Double-counting INFLATES the day, which is the direction of error that matters here.

    `positions_closed()` should not repeat a position, but "should not" is not a guarantee across a
    reconciliation pass, and an inflated realized figure is one an operator acts on (codex review).
    """
    a = _pos("MANUAL-001", 500.0, _START + 1)
    a.id = "P-1"
    dup = _pos("MANUAL-001", 500.0, _START + 1)
    dup.id = "P-1"

    r = session_realized([a, dup], [], session_start_ns=_START, session_end_ns=_END)
    assert r.total == 500.0, "the same position was counted twice"
    assert r.closed_count == {"MANUAL-001": 1}


def test_two_DIFFERENT_positions_are_both_counted():
    """The counter-case, so dedupe does not swallow genuine closes."""
    a = _pos("MANUAL-001", 500.0, _START + 1)
    a.id = "P-1"
    b = _pos("MANUAL-001", 300.0, _START + 2)
    b.id = "P-2"

    r = session_realized([a, b], [], session_start_ns=_START, session_end_ns=_END)
    assert r.total == 800.0 and r.closed_count == {"MANUAL-001": 2}


def test_the_field_survives_the_WHOLE_HOP_engine_to_consumer_to_rest():
    """The gap that shipped: every end was tested, the hop was not.

    The engine published `realized_session`, `_publish_trades` was pinned to include it, and the tile
    was pinned to read it — and the field STILL arrived as null on `/trades`. `TradesResponse` is a
    Pydantic model and drops keys it does not declare, so it was filtered out at the REST boundary
    while both ends passed their tests.

    Then, once declared, it was STILL null: the endpoint builds the response field-by-field, so a
    declared-but-unpassed field is silently None.

    So this drives the actual chain — a published payload through the consumer's dispatch and out of
    the response model — rather than either end alone.
    """
    import json

    from api.consumer import RedisConsumer
    from api.models import TradesResponse

    payload = {
        "trades": [],
        "status": "ok",
        "error": None,
        "realized_session": {"by_strategy": {"MANUAL-001": 483.22}, "closed_count": {"MANUAL-001": 3},
                             "total": 483.22, "partial_open": 0, "is_partial": False},
    }

    consumer = RedisConsumer.__new__(RedisConsumer)   # no redis wiring needed to exercise dispatch
    consumer._trades = []
    consumer._trades_status = None
    consumer._trades_error = None
    consumer._trades_realized = None

    # The REAL dispatch, on the REAL frame shape the engine writes to the stream.
    RedisConsumer._apply(consumer, {"type": "trades", "payload": json.dumps(payload)})
    assert consumer.trades_realized() is not None, "the consumer dropped the field on the way in"

    body = TradesResponse(
        trades=consumer.trades() if consumer._trades else [],
        status=consumer._trades_status or "ok",
        error=None,
        realized_session=consumer.trades_realized(),
    )
    dumped = body.model_dump()

    assert "realized_session" in dumped, "the response model drops the field"
    assert dumped["realized_session"]["total"] == 483.22, "the field survived the model but lost its value"


def test_the_trades_endpoint_passes_realized_session_explicitly():
    """The second half of the same defect: declaring the field is not passing it.

    A source assertion because the endpoint is an async FastAPI handler over a live node, and the bug
    is precisely that a field can be declared and never populated.
    """
    import inspect

    from api import app as app_module

    source = inspect.getsource(app_module.get_trades)
    assert "realized_session=" in source, "/trades declares the field but never passes it"


def test_none_and_zero_are_different_answers():
    """None means "no engine said" and the tile falls back to the live-cycle sum; zero means "the engine
    looked and nothing closed". Collapsing them would resurrect the stale sum on every flat day."""
    from api.models import TradesResponse

    assert TradesResponse(trades=[], realized_session=None).realized_session is None
    zero = TradesResponse(trades=[], realized_session={"total": 0.0})
    assert zero.realized_session == {"total": 0.0}


def test_broker_protection_reaches_the_trades_frame(monkeypatch):
    """#285: the cache cannot answer whether a position is protected.

    The engine holds orders as REJECTED that Alpaca reports OPEN — a submit whose HTTP call failed
    after the venue accepted it — and Nautilus refuses REJECTED -> ACCEPTED as an invalid transition,
    so reconciliation can never repair them. They drop out of `orders_open()` and the book reported
    8 of 8 positions unprotected while 8 GTC stops rested at the broker.
    """
    from types import SimpleNamespace

    from api.engine_node import UiFeedStrategy

    class _Fake:
        # KEYED BY (instrument, REDUCING SIDE) since the short-side fix: a SELL protects a LONG and
        # does not protect a SHORT, which the old instrument-level set could not say.
        _broker_protected = {("BETA.XNYS", "SELL")}
        _broker_stop_prices: dict = {}

        def _venue_working(self, order):
            return None

        cache = SimpleNamespace(order=lambda coid: None)

    # `side` because production's trade DTO always carries one, and protection is now judged against
    # the position's own reducing side. A double without it would answer UNKNOWN and this test would
    # fail on the missing field rather than on the behaviour it exists to pin.
    protected = SimpleNamespace(instrument_id="BETA.XNYS", side="LONG", working_orders=[],
                                broker_protected=None)
    naked = SimpleNamespace(instrument_id="NBIS.XNAS", side="LONG", working_orders=[],
                            broker_protected=None)
    UiFeedStrategy._mark_broker_stop_prices(_Fake(), [protected, naked])

    assert protected.broker_protected is True
    assert naked.broker_protected is False, "an unprotected position was not reported as such"


def test_not_having_asked_the_broker_is_NOT_the_same_as_unprotected():
    """Three-state, and this is the state that keeps it honest.

    A node with the protection poll off has no broker answer. Reporting False there would declare the
    whole book naked — a false alarm on every position, which is how a real one gets scrolled past.
    """
    from types import SimpleNamespace

    from api.engine_node import UiFeedStrategy

    class _Fake:
        _broker_protected = None
        _broker_stop_prices: dict = {}

        def _venue_working(self, order):
            return None

        cache = SimpleNamespace(order=lambda coid: None)

    dto = SimpleNamespace(instrument_id="BETA.XNYS", working_orders=[], broker_protected="unset")
    UiFeedStrategy._mark_broker_stop_prices(_Fake(), [dto])
    assert dto.broker_protected is None
