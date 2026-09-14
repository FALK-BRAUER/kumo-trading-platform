"""Feed freshness must be MONOTONIC and must read ARRIVAL: historical data can never age it (#917).

Measured on paper 2026-09-11 00:01Z–08:10 SGT: `/health.status degraded`, all four subsystems ok,
`feed_stale True`, `feed_last_tick_ts 1789012800000000000` = 2026-09-10T04:00:00Z = 00:00 ET — a
round midnight. That is not a trade: it is a DAILY BAR's stamp. Alpaca stamps daily bars at 00:00 ET
and our own `_parse_bar` writes that stamp into BOTH `ts_event` and `ts_init` (data_client.py:196-207)
— as do `_parse_quote` and `_parse_trade`, so on this provider `ts_init` is NEVER arrival. The bar
reaches `on_bar` through the live `dailyBars` channel (a `request_bars` response routes to
`on_historical_data`, which does not write the clock). Three writers, none monotonic
(`engine_node.py`):

    on_trade_tick   self._last_tick_ts = tick.ts_event
    on_quote_tick   self._last_tick_ts = tick.ts_init
    on_bar          self._last_tick_ts = bar.ts_init

so the freshness clock reads whatever arrived LAST, and a bar that is 16 h old by its own stamp drags
it back 16 h. `feed_is_stale` then counts real trading minutes against a wrong last-tick, `status`
reads `degraded` continuously with nothing wrong, and a status that reads degraded unconditionally
carries NO information — the exact state in which a real degradation goes unnoticed.

The properties this file pins:
  1. `_last_tick_ts` only ever moves FORWARD, through every handler.
  2. What moves it is `ts_init` — arrival on a venue whose adapter stamps arrival (IBKR); the data's
     own time on one that does not (Alpaca). Never `ts_event`, which is the data's own time everywhere.
  3. `on_historical_data` does not touch it: on IB a `request_bars` DAY response carries `ts_init` =
     END of that day (bar start + period − 1 ns), in the FUTURE at request time — a monotonic write there
     would PIN freshness at end-of-day and a warmup after a recreate would mark a dead feed live for the
     whole session. Monotonic is not enough for that; "never written" is.
  4. The measured symptom: with the clock guarded, `feed_is_stale` over the measured instants is False.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pandas as pd
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from api.engine_node import UiFeedStrategy
from api.feed_staleness import feed_is_stale
from api.providers.alpaca.data_client import AlpacaDataClient

_IID = InstrumentId.from_str("AEM.XNYS")
_DAY = BarType.from_str("AEM.XNYS-1-DAY-LAST-EXTERNAL")
#: The value read off paper's /health — 2026-09-10 00:00 ET, the daily bar's stamp.
_MEASURED_LAST_TICK = 1_789_012_800_000_000_000
#: The last real print of that session: 15:59 ET on 2026-09-10, 16 h AFTER the bar's stamp (the 20 h
#: in the ticket is the bar's age at the 00:01Z read — freshness was measured from the bar, not this).
_LIVE = pd.Timestamp("2026-09-10T19:59:00Z").value
#: When /health was read: 2026-09-11 00:01Z, one trading minute after the live print, 390 after the bar.
_READ = pd.Timestamp("2026-09-11T00:01:00Z").value
_MINUTE = 60_000_000_000
_DATA_HANDLERS = ("on_trade_tick", "on_quote_tick", "on_bar")


class _Instrument:
    """What the three parsers read off the instrument, and nothing else."""
    id = _IID
    price_precision = 2
    size_precision = 0


class _Subscriptions:
    def __init__(self) -> None:
        self.bound_calls: list[tuple] = []

    def bound(self, *args) -> None:
        self.bound_calls.append(args)


class _Log:
    def debug(self, *a, **k) -> None: ...
    def info(self, *a, **k) -> None: ...
    def warning(self, *a, **k) -> None: ...


class _Fresh:
    """Only what the three data handlers touch. The handlers themselves are the REAL ones, bound below —
    a double that re-implemented the assignment would pin its own copy, not production's."""

    def __init__(self) -> None:
        self._last_tick_ts: int = 0
        self._subscriptions = _Subscriptions()
        self._last_close: dict = {}
        self.log = _Log()
        self.published: list[tuple] = []
        self.vwap_calls: list[Bar] = []

    def _publish(self, *args, **kwargs) -> None:
        self.published.append((args, kwargs))

    def _update_vwap_from_bar(self, bar: Bar, historical: bool) -> None:
        self.vwap_calls.append(bar)          # not under test here; `on_bar` must still reach it

    on_trade_tick = UiFeedStrategy.on_trade_tick
    on_quote_tick = UiFeedStrategy.on_quote_tick
    on_bar = UiFeedStrategy.on_bar
    on_historical_data = UiFeedStrategy.on_historical_data
    _publish_bar = UiFeedStrategy._publish_bar


class _RthCalendar:
    """Trading minutes between two instants for the ONE session in play, 2026-09-10 09:30–16:00 ET —
    computed, not a constant, so the two derivations (from the bar, from the live print) differ."""
    _OPEN = pd.Timestamp("2026-09-10T13:30:00Z").value
    _CLOSE = pd.Timestamp("2026-09-10T20:00:00Z").value

    def trading_minutes_between(self, a: int, b: int) -> float:
        lo, hi = max(a, self._OPEN), min(b, self._CLOSE)
        return max(0.0, (hi - lo) / _MINUTE)


def _iso(ns: int) -> str:
    return pd.Timestamp(ns, unit="ns", tz="UTC").isoformat()


# --- fixtures built by the PRODUCTION parsers (Alpaca: one stamp into both fields) --------------------

def _daily_bar(stamp_ns: int) -> Bar:
    """As `_parse_bar` builds it — the same code for the live `dailyBars` frame and a request response."""
    bar = AlpacaDataClient._parse_bar(_DAY, _Instrument(), {"t": _iso(stamp_ns), "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1})
    assert bar is not None
    return bar


def _quote(stamp_ns: int) -> QuoteTick:
    return AlpacaDataClient._parse_quote(None, _Instrument(), {"bp": 200.0, "ap": 200.02, "bs": 1, "as": 1, "t": _iso(stamp_ns)})


def _trade(stamp_ns: int) -> TradeTick:
    return AlpacaDataClient._parse_trade(None, _Instrument(), {"p": 200.0, "s": 1, "i": str(stamp_ns), "t": _iso(stamp_ns)})


# --- fixtures shaped like an ARRIVAL-stamping adapter (IBKR: ts_event = data time, ts_init = now) -------

def _ib_bar(event_ns: int, init_ns: int) -> Bar:
    p = Price.from_str("1.00")
    return Bar(_DAY, p, p, p, p, Quantity.from_int(1), event_ns, init_ns)


def _ib_quote(event_ns: int, init_ns: int) -> QuoteTick:
    p = Price.from_str("200.00")
    s = Quantity.from_int(100)
    return QuoteTick(instrument_id=_IID, bid_price=p, ask_price=p, bid_size=s, ask_size=s, ts_event=event_ns, ts_init=init_ns)


def _ib_trade(event_ns: int, init_ns: int) -> TradeTick:
    return TradeTick(instrument_id=_IID, price=Price.from_str("200.00"), size=Quantity.from_int(1),
                     aggressor_side=AggressorSide.NO_AGGRESSOR, trade_id=TradeId(str(event_ns)),
                     ts_event=event_ns, ts_init=init_ns)


# --- fixture properties: the bug must be REACHABLE before monotonicity means anything -----------------

def test_FIXTURE_every_alpaca_parser_writes_the_datas_own_time_into_BOTH_stamps():
    """So on this provider no handler can tell "when it happened" from "when it arrived", and a daily
    bar delivered at 16:01 ET says 00:00 ET in the field `on_bar` reads. The measured `/health` value
    IS that midnight, 16 h before the session's last real print."""
    for datum in (_daily_bar(_MEASURED_LAST_TICK), _quote(_MEASURED_LAST_TICK), _trade(_MEASURED_LAST_TICK)):
        assert datum.ts_init == datum.ts_event == _MEASURED_LAST_TICK, type(datum).__name__
    assert pd.Timestamp(_MEASURED_LAST_TICK, unit="ns", tz="UTC").tz_convert("America/New_York").strftime("%H:%M") == "00:00"
    assert _LIVE - _MEASURED_LAST_TICK == 16 * 60 * _MINUTE - _MINUTE, "the bar is 16 h OLDER than the last live print"


def test_FIXTURE_the_ib_shaped_fixtures_can_tell_the_two_stamps_apart():
    """The Alpaca fixtures cannot distinguish `max(clock, ts_event)` from `max(clock, ts_init)`. These
    can: old event time, fresh arrival."""
    for datum in (_ib_bar(_MEASURED_LAST_TICK, _LIVE), _ib_quote(_MEASURED_LAST_TICK, _LIVE), _ib_trade(_MEASURED_LAST_TICK, _LIVE)):
        assert datum.ts_event == _MEASURED_LAST_TICK and datum.ts_init == _LIVE, type(datum).__name__


def test_FIXTURE_the_double_runs_the_REAL_handlers_and_they_write_the_clock():
    """A double whose handlers are stubs pins nothing. Each real handler, given data, must move the
    clock from 0 — otherwise every 'stays put' assertion below is vacuous."""
    for handler, datum in (("on_bar", _daily_bar(_LIVE)), ("on_quote_tick", _quote(_LIVE)), ("on_trade_tick", _trade(_LIVE))):
        fresh = _Fresh()
        assert getattr(_Fresh, handler) is getattr(UiFeedStrategy, handler)
        getattr(fresh, handler)(datum)
        assert fresh._last_tick_ts == _LIVE, handler
    fresh = _Fresh()
    fresh.on_bar(_daily_bar(_LIVE))
    assert fresh.published and fresh.vwap_calls, "on_bar ran to its end — the write is not the only thing it does"


def test_FIXTURE_the_calendar_double_computes_minutes_rather_than_returning_one_number():
    """Two derivations that must differ: 390 open minutes from the bar's stamp to the read, 1 from the
    live print to the read. A constant calendar could not make the seam test below mean anything."""
    cal = _RthCalendar()
    assert cal.trading_minutes_between(_MEASURED_LAST_TICK, _READ) == 390.0
    assert cal.trading_minutes_between(_LIVE, _READ) == 1.0


# --- the defect: older data, whatever handler it arrives through, leaves the clock alone ---------------

def test_a_live_daily_bar_cannot_age_the_freshness_clock():
    """THE MEASURED CASE. Live quote at 15:59 ET, then the `dailyBars` frame stamped 00:00 ET arrives
    through `on_bar`. Freshness must still read 15:59 ET."""
    fresh = _Fresh()
    fresh.on_quote_tick(_quote(_LIVE))
    fresh.on_bar(_daily_bar(_MEASURED_LAST_TICK))
    assert fresh._last_tick_ts == _LIVE, f"a 16 h old bar aged the clock to {fresh._last_tick_ts}"


def test_an_old_trade_tick_cannot_age_it():
    fresh = _Fresh()
    fresh.on_bar(_daily_bar(_LIVE))
    fresh.on_trade_tick(_trade(_MEASURED_LAST_TICK))
    assert fresh._last_tick_ts == _LIVE


def test_an_old_quote_cannot_age_it():
    fresh = _Fresh()
    fresh.on_trade_tick(_trade(_LIVE))
    fresh.on_quote_tick(_quote(_MEASURED_LAST_TICK))
    assert fresh._last_tick_ts == _LIVE


def test_newer_data_still_advances_it_through_EVERY_handler():
    """Monotonic, not frozen: a guard that never wrote would pass the three tests above."""
    fresh = _Fresh()
    fresh.on_quote_tick(_quote(_LIVE))
    fresh.on_bar(_daily_bar(_LIVE + _MINUTE))
    assert fresh._last_tick_ts == _LIVE + _MINUTE
    fresh.on_trade_tick(_trade(_LIVE + 2 * _MINUTE))
    assert fresh._last_tick_ts == _LIVE + 2 * _MINUTE
    fresh.on_quote_tick(_quote(_LIVE + 3 * _MINUTE))
    assert fresh._last_tick_ts == _LIVE + 3 * _MINUTE


# --- which stamp: ARRIVAL, on a venue whose adapter stamps it ------------------------------------------

def test_EVERY_handler_reads_ARRIVAL_not_the_datas_own_time():
    """On IBKR a daily bar built now carries yesterday's close in `ts_event` and now in `ts_init`; the
    first version of #608's fix read `ts_event` and staging's freshness read 39 h old the moment it
    went live. `max(clock, ts_event)` would pass every Alpaca-shaped test in this file and fail here."""
    for handler, datum in (("on_bar", _ib_bar(_MEASURED_LAST_TICK, _LIVE)),
                           ("on_quote_tick", _ib_quote(_MEASURED_LAST_TICK, _LIVE)),
                           ("on_trade_tick", _ib_trade(_MEASURED_LAST_TICK, _LIVE))):
        fresh = _Fresh()
        getattr(fresh, handler)(datum)
        assert fresh._last_tick_ts == _LIVE, f"{handler} read the data's own time, not arrival"


def test_historical_data_does_NOT_touch_the_clock_even_when_its_stamp_is_in_the_FUTURE():
    """A `request_bars` response (warmup after a recreate, backfill) routes here. On IB a DAY response is
    stamped `ts_init` = END of that day (`_ib_bar_to_ts_init`: bar start + period − 1 ns) — ahead of now
    at request time — so a monotonic guard would PIN freshness at end-of-day and a warmup would mark a
    DEAD feed live for the rest of the session. The only safe answer is that this handler never writes
    freshness at all."""
    fresh = _Fresh()
    fresh.on_quote_tick(_quote(_LIVE))
    end_of_day = pd.Timestamp("2026-09-10T04:00:00Z").value + 24 * 60 * _MINUTE - 1   # IB's DAY stamp
    assert end_of_day > _LIVE, "the fixture's stamp IS in the future relative to the live print"
    fresh.on_historical_data(_ib_bar(_MEASURED_LAST_TICK, end_of_day))
    assert fresh._last_tick_ts == _LIVE
    assert fresh.published, "on_historical_data ran — it publishes the bar as historical"
    # By AST, not substring: the handler's own comment names the field, and a grep would fail on the
    # explanation of why it must not write — the trap `test_feed_freshness_is_venue_neutral.py` records.
    assert "on_historical_data" not in _writers_of_last_tick_ts()


# --- the measured symptom, one hop up --------------------------------------------------------------------

def test_the_measured_instants_read_FRESH_once_the_clock_is_guarded():
    """What #917 cost: `feed_stale True` → `status degraded` for 20 h with nothing wrong. Feed the
    clock a guarded handler produced into `feed_is_stale` with the read time and a computing calendar:
    1 trading minute since the live print, not 390 since the bar."""
    fresh = _Fresh()
    fresh.on_quote_tick(_quote(_LIVE))
    fresh.on_bar(_daily_bar(_MEASURED_LAST_TICK))
    assert feed_is_stale(fresh._last_tick_ts, _READ, _RthCalendar()) is False
    assert feed_is_stale(_MEASURED_LAST_TICK, _READ, _RthCalendar()) is True, "the fixture CAN read stale"


# --- the CLASS: every writer, including the one not written yet -----------------------------------------

def _class_tree() -> ast.ClassDef:
    return ast.parse(textwrap.dedent(inspect.getsource(UiFeedStrategy))).body[0]


def _is_clock(t: ast.AST) -> bool:
    return isinstance(t, ast.Attribute) and t.attr == "_last_tick_ts" and isinstance(t.value, ast.Name) and t.value.id == "self"


def _writers_of_last_tick_ts() -> dict[str, list[ast.Assign]]:
    """`{method: [assignments to self._last_tick_ts]}` outside `__init__`."""
    out: dict[str, list[ast.Assign]] = {}
    for fn in _class_tree().body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name == "__init__":
            continue
        hits = [n for n in ast.walk(fn) if isinstance(n, ast.Assign) and any(_is_clock(t) for t in n.targets)]
        if hits:
            out[fn.name] = hits
    return out


def _calls(fn_name: str) -> set[str]:
    fn = next(f for f in _class_tree().body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == fn_name)
    return {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"}


def test_FIXTURE_the_class_guard_sees_every_data_handler_write_or_delegate():
    """Every data handler must reach a writer — directly, or through ONE helper (the shape that actually
    protects a fourth handler). A walk that found nothing would make the guard below vacuous."""
    writers = _writers_of_last_tick_ts()
    assert writers, "no writer of self._last_tick_ts found — the walk is broken or the field was renamed"
    helpers = set(writers) - set(_DATA_HANDLERS)
    assert len(helpers) <= 1, f"more than one non-handler writes the clock: {sorted(helpers)}"
    for h in _DATA_HANDLERS:
        assert h in writers or (helpers and helpers & _calls(h)), f"{h} neither writes the clock nor calls the helper"
    assert set(writers) <= set(_DATA_HANDLERS) | helpers


def test_EVERY_writer_of_the_freshness_clock_is_monotonic_on_ARRIVAL():
    """Aimed at the class, not the instance. Each write must be `max(self._last_tick_ts, <x>.ts_init)`
    (or, in a helper, `max(self._last_tick_ts, <name>)`): both operands pinned, because
    `max(clock, ts_event)` is exactly the #608 regression and no Alpaca-shaped fixture can see it."""
    writers = _writers_of_last_tick_ts()
    for name, nodes in writers.items():
        for node in nodes:
            v = node.value
            assert isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "max" and len(v.args) == 2, \
                f"{name}: `self._last_tick_ts = {ast.unparse(v)}` can move the clock BACKWARDS"
            assert any(_is_clock(a) for a in v.args), f"{name}: max() does not include the clock itself"
            other = next(a for a in v.args if not _is_clock(a))
            if name in _DATA_HANDLERS:
                assert isinstance(other, ast.Attribute) and other.attr == "ts_init", \
                    f"{name}: freshness must read ARRIVAL (`ts_init`), got `{ast.unparse(other)}`"
            else:
                assert isinstance(other, ast.Name), f"{name}: helper must take the stamp as an argument"


def test_no_other_form_can_write_the_clock():
    """`ast.Assign` is what the guard reads; `+=`, `setattr` and a tuple-unpack would slip past it."""
    src = inspect.getsource(UiFeedStrategy)
    assert "setattr(self, \"_last_tick_ts\"" not in src and "setattr(self, '_last_tick_ts'" not in src
    for n in ast.walk(_class_tree()):
        assert not (isinstance(n, ast.AugAssign) and _is_clock(n.target)), "AugAssign on the clock"
        if isinstance(n, ast.Assign):
            for t in n.targets:
                assert not (isinstance(t, (ast.Tuple, ast.List)) and any(_is_clock(e) for e in t.elts)), "tuple-unpack into the clock"


# --- HOP 1: the health frame carries THIS field ------------------------------------------------------

def test_the_health_frame_forwards_the_clock_verbatim():
    """`_on_snapshot` publishes `"last_tick_ts": self._last_tick_ts` and `app` builds
    `HealthResponse(feed_last_tick_ts=int(observed.get("last_tick_ts", …)))` — AST, not a substring,
    so a comment cannot satisfy it (the trap `test_feed_freshness_is_venue_neutral.py` documents)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(UiFeedStrategy._on_snapshot)))
    hits = [v for node in ast.walk(tree) if isinstance(node, ast.Dict)
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and k.value == "last_tick_ts"]
    assert hits and all(_is_clock(v) for v in hits), [ast.unparse(v) for v in hits]
    from api import app
    kw = [k for n in ast.walk(ast.parse(inspect.getsource(app))) if isinstance(n, ast.Call)
          and getattr(n.func, "id", None) == "HealthResponse"
          for k in n.keywords if k.arg == "feed_last_tick_ts"]
    assert len(kw) == 1 and 'observed.get(\'last_tick_ts\'' in ast.unparse(kw[0].value).replace('"', "'"), \
        [ast.unparse(k.value) for k in kw]
