"""#846 — a NETTING reopen drops the earlier leg from `positions_closed()`, and the day's realized with it.

ibkr-paper, 2026-09-09, `MPC.XNYS-MANUAL-001`: leg 1 closed 15:14:29 for +575.51, leg 2 (a 28-second
short) closed 15:14:58 for -3.62. `realized_session` read MANUAL-001 = 1841.46; the snapshots Nautilus
had written said 2416.97. Home REALIZED 1D was short $575.51 — a WRONG number, not a missing one.

Mechanism (installed nautilus_trader 1.229, `cache.pyx::add_position`): the NETTING position id is
`{instrument}-{strategy}`, and a reopen does `self._positions[position.id] = position` then
`_index_positions_closed.discard(position.id)  # Cleanup for NETTING reopen`. The new object replaces
the old; the first round trip's `realized_pnl` leaves `positions_closed()` for good.

The durable record exists: `snapshot_positions=True` makes the exec engine persist every closed
state (`ExecutionEngine._create_position_state_snapshot(position, open_only=False)` on close) to the
Redis list `trader-{id}:snapshots:positions:{pos_id}` as msgpack of `Position.to_dict()`.

EVERY OBJECT IN THE FIXTURE IS NAUTILUS'S OWN. A real `Cache`, real `Position`s driven by real
`OrderFilled`s under `OmsType.NETTING`, real `PositionClosed` events, and the persisted states are
`Position.to_dict()` exactly as the engine serializes them. A hand-built cache double could not have
represented this defect — the defect IS the cache replacing the object — so the first test pins that
the fixture expresses it before anything is asserted about the fix.

The seams under test, in the order production reaches them:
  restart   `read_restored_legs` -> `_apply_seed` -> `_closed_legs`
  live      `_on_any_position_event(PositionClosed)` -> `_closed_legs`  (every lane's, via the events.position.* wildcard)
  figures   `_session_realized` (today) and `_realized_windows` (1D/1W/1M/3M/all), both over
            positions_closed() UNION _closed_legs, deduplicated by (position id, open time)
  frame     `_publish_trades` puts `realized_periods` on the trades frame from legs when no broker
            sweep exists — the IBKR case — and says which source it came from
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import msgspec.msgpack as _msgpack
from nautilus_trader.cache.cache import Cache
from nautilus_trader.model.enums import OmsType, OrderSide
from nautilus_trader.model.identifiers import PositionId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from api.engine_node import UiFeedStrategy, _et_day_bounds_ns
from api.realized_broker import PERIOD_DAYS
from api.snapshot_reader import read_restored_legs

TRADER = "PLATFORM-TEST"
POS_ID = "MPC.XNYS-MANUAL-001"
#: 2026-08-14 18:30 UTC, inside a US session — the same instant `test_realized.py` uses.
NOW_NS = 1_786_818_600 * 1_000_000_000
_H = 3600 * 1_000_000_000
_DAY = 24 * _H
_ET = ZoneInfo("America/New_York")

_INST = TestInstrumentProvider.equity(symbol="MPC", venue="XNYS")
_MANUAL = StrategyId("MANUAL-001")
_MOMENTUM = StrategyId("MOMENTUM-002")


def _fill(side: OrderSide, qty: int, px: str, ts_ns: int, *, strategy=_MANUAL, pos_id=POS_ID):
    order = TestExecStubs.market_order(
        instrument=_INST, order_side=side, quantity=Quantity.from_int(qty), strategy_id=strategy,
    )
    return TestEventStubs.order_filled(
        order, instrument=_INST, position_id=PositionId(pos_id), strategy_id=strategy,
        last_px=Price.from_str(px), ts_event=ts_ns,
    )


def _round_trip(cache: Cache, states: list[dict], *, open_side: OrderSide, open_px: str, close_px: str,
                t_open: int, t_close: int, strategy=_MANUAL, pos_id=POS_ID) -> Position:
    """One leg the way the engine produces it: open fill -> snapshot(open) -> close fill -> snapshot(closed)."""
    close_side = OrderSide.SELL if open_side == OrderSide.BUY else OrderSide.BUY
    pos = Position(instrument=_INST, fill=_fill(open_side, 4, open_px, t_open, strategy=strategy, pos_id=pos_id))
    cache.add_position(pos, OmsType.NETTING)
    states.append(pos.to_dict())                                      # open_only=True snapshot on open
    pos.apply(_fill(close_side, 4, close_px, t_close, strategy=strategy, pos_id=pos_id))
    cache.update_position(pos)
    states.append(pos.to_dict())                                      # open_only=False snapshot on close
    return pos


def _reopened() -> tuple[Cache, list[dict], Position, Position]:
    """The live shape: buy 4 / sell 4 (leg 1), then short 4 / cover 4 under the SAME position id (leg 2),
    both closing inside today's ET day. Returns the cache after the reopen, the persisted states in the
    order the engine wrote them (open, closed, open, closed), and both leg objects."""
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    cache = Cache()
    states: list[dict] = []
    leg1 = _round_trip(cache, states, open_side=OrderSide.BUY, open_px="100.00", close_px="243.88",
                       t_open=day_start + 1 * _H, t_close=day_start + 2 * _H)
    leg2 = _round_trip(cache, states, open_side=OrderSide.SELL, open_px="100.00", close_px="100.90",
                       t_open=day_start + 3 * _H, t_close=day_start + 4 * _H)  # <- the reopen
    return cache, states, leg1, leg2


def _pnl(pos: Position) -> float:
    return round(pos.realized_pnl.as_double(), 2)


class _Redis:
    """The two calls `read_restored_legs` makes, over exactly the bytes Nautilus writes: one Redis LIST
    per position id, msgpack of `Position.to_dict()`, open and closed states interleaved."""

    def __init__(self, states_by_pos: dict[str, list[dict]]) -> None:
        self._rows = {f"trader-{TRADER}:snapshots:positions:{pid}".encode(): [_msgpack.encode(s) for s in states]
                      for pid, states in states_by_pos.items()}

    def scan_iter(self, match: str):
        yield from self._rows

    def lrange(self, key, start, end):
        return list(self._rows[key if isinstance(key, bytes) else key.encode()])


def _seed(eng, states_by_pos: dict[str, list[dict]], *, stopped_at: str | None = None) -> None:
    # The key is ALWAYS present, as the real loader sends it: None = the scan finished. An absent key is
    # a loader that never said, and `_apply_seed` refuses it (see the ABSENT test below).
    payload = {"legs": read_restored_legs(_Redis(states_by_pos), TRADER), "cycles": {},
               "legs_stopped_at": stopped_at}
    UiFeedStrategy._apply_seed(eng, payload)


class _Projection:
    """A seeded projection that projects nothing — `_publish_trades` needs one to leave the seeding branch."""

    def project(self, cache, now_ns):
        return []

    def seed_restored_legs(self, pos_id, legs):
        pass


def _engine(cache: Cache, *, swept: dict | None = None) -> SimpleNamespace:
    """A `UiFeedStrategy` double carrying what the methods under test read — the trades-frame publisher
    included, so the seam test drives the REAL `_publish_trades` (the shape `test_publish_trades_status`
    uses). `swept=None` is the IBKR instance: no Alpaca client, no broker sweep, `_realized_periods` None."""
    eng = SimpleNamespace(
        cache=cache,
        clock=SimpleNamespace(timestamp_ns=lambda: NOW_NS),
        log=SimpleNamespace(error=lambda m: None, warning=lambda m: None, info=lambda m: None),
        trader_id=TRADER,
        _projection_lock=threading.RLock(),
        _trade_cycles={"MANUAL-001": _Projection()},
        _cycles_seeded=True,
        _cycle_store=None,
        _loop=None,
        _last_good_trades=[],
        _broker_stop_prices={},
        _http=None,
        _realized_periods=swept,
        _closed_legs={},
        _legs_restored=0,
        _legs_seed_stopped_at=None,
        _legs_seeded=False,
        published=[],
    )
    eng._publish = lambda key, payload: eng.published.append((key, payload))
    eng._mark_cycle_financials = lambda dtos: None
    eng._mark_broker_stop_prices = lambda dtos: None
    # The REAL methods, bound to the double — a helper that nothing calls proves nothing. A method the
    # fix has not added yet stays absent, so the test fails on the missing seam rather than on a stub.
    for name in ("_publish_trades", "_session_realized", "_realized_windows", "_realized_legs_status",
                 "_register_closed_leg", "_on_any_position_event", "_load_seed", "_lane_flows"):
        real = getattr(UiFeedStrategy, name, None)
        if real is not None:
            setattr(eng, name, real.__get__(eng))
    return eng


def _frame(eng) -> dict:
    trades = [p for k, p in eng.published if k == "trades"]
    assert trades, "nothing was published on the trades key"
    return trades[-1]


# -- the fixture must be able to express the bug ------------------------------------------------------

def test_the_fixture_expresses_the_bug__reopen_leaves_ONE_closed_position_but_TWO_persisted_closed_states():
    """If this fails, nothing below means anything. `positions_closed()` must have already lost leg 1."""
    cache, states, leg1, leg2 = _reopened()

    closed = cache.positions_closed()
    assert len(closed) == 1, "the reopen must REPLACE the closed position, not add a second"
    assert _pnl(closed[0]) == _pnl(leg2)
    assert _pnl(leg1) > 500 > 0 > _pnl(leg2), "leg 1 must be the big one and leg 2 the small loss, as on ibkr-paper"

    persisted_closed = [s for s in states if s.get("ts_closed") is not None]
    assert len(persisted_closed) == 2, "the engine persists BOTH closed states under one position id"
    assert len({s["ts_opened"] for s in persisted_closed}) == 2, "the two legs differ by open time"
    assert persisted_closed[0]["strategy_id"] == "MANUAL-001"
    assert persisted_closed[0]["instrument_id"] == "MPC.XNYS"
    assert isinstance(persisted_closed[0]["ts_closed"], int), "Nautilus persists ns timestamps as ints"


# -- restart: read -> seed -> registry ----------------------------------------------------------------

def test_restored_legs_carry_the_strategy_and_instrument_the_state_dict_holds():
    """A leg with no strategy cannot be attributed. The persisted state carries `strategy_id` and
    `instrument_id`; `CycleLeg` dropped them because cycle P&L never needed them."""
    _, states, _, _ = _reopened()
    legs = read_restored_legs(_Redis({POS_ID: states}), TRADER)
    assert list(legs) == [POS_ID]
    for leg in legs[POS_ID]:
        assert leg.strategy_id == "MANUAL-001", "a restored leg must say whose it is"
        assert leg.instrument_id == "MPC.XNYS"
        assert leg.position_id == POS_ID
    assert len(legs[POS_ID]) == 2, "both closed legs are restored, deduplicated by open time"


def test_restored_legs_survive_reversed_order_and_a_repeated_closed_state():
    """Production's list order is not guaranteed and a closed state can be written more than once
    (a reconciliation re-apply). Dedup is by open time; the LATEST close wins; order is irrelevant."""
    _, states, leg1, leg2 = _reopened()
    twice = list(reversed(states)) + [states[1]]                      # reversed, and leg 1 closed again
    legs = read_restored_legs(_Redis({POS_ID: twice}), TRADER)[POS_ID]
    assert sorted(round(l.realized_pnl.as_double(), 2) for l in legs) == sorted([_pnl(leg1), _pnl(leg2)])


def test_the_restart_seed_keeps_the_legs_for_realized_not_only_for_cycles():
    """`_apply_seed` hands legs to the cycle projections and forgets them. The realized path needs
    the same legs, keyed the way the snapshot store keys them: (position id, open time)."""
    _, states, leg1, leg2 = _reopened()
    eng = _engine(Cache())

    _seed(eng, {POS_ID: states})

    assert set(eng._closed_legs) == {(POS_ID, leg1.ts_opened), (POS_ID, leg2.ts_opened)}


# -- live: a leg closing AFTER the seed, in this process ------------------------------------------------

def test_a_leg_closed_in_this_process_reaches_the_registry_without_a_restart():
    """The restart seed alone would ship green and still under-report every same-session reopen
    (codex, coverage review). `_on_any_position_event` is the wildcard handler for every lane's position
    transitions; a PositionClosed must land the leg in the registry from the EVENT, because the cache
    may already hold the reopened id."""
    cache, _, leg1, leg2 = _reopened()
    eng = _engine(cache)

    eng._on_any_position_event(TestEventStubs.position_closed(leg1))
    eng._on_any_position_event(TestEventStubs.position_closed(leg2))

    assert set(eng._closed_legs) == {(POS_ID, leg1.ts_opened), (POS_ID, leg2.ts_opened)}
    assert round(eng._closed_legs[(POS_ID, leg1.ts_opened)].realized_pnl.as_double(), 2) == _pnl(leg1)


def test_an_opened_or_changed_event_registers_no_leg():
    """Only a close is a leg. Registering opens would count P&L that has not happened."""
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    cache = Cache()
    still_open = Position(instrument=_INST, fill=_fill(OrderSide.BUY, 4, "100.00", day_start + 1 * _H))
    cache.add_position(still_open, OmsType.NETTING)
    assert still_open.is_open, "fixture: Nautilus refuses a PositionOpened for a closed position"
    eng = _engine(cache)

    eng._on_any_position_event(TestEventStubs.position_opened(still_open))

    assert eng._closed_legs == {}


# -- the day's figure -----------------------------------------------------------------------------------

def test_the_days_realized_counts_BOTH_legs_of_a_reopened_position_once_each():
    """The defect. Driven through `_session_realized` — the method the trades frame publishes — with
    the real cache after the reopen and the legs the seed restored. Leg 2 is in BOTH sources and must
    be counted once; leg 1 is only in the snapshots and must not be lost.

    ibkr-paper read 1841.46 where 2416.97 was true. Here: leg 2 alone (today) vs leg 1 + leg 2 (right)."""
    cache, states, leg1, leg2 = _reopened()
    eng = _engine(cache)
    _seed(eng, {POS_ID: states})

    out = eng._session_realized()

    assert out["closed_count"] == {"MANUAL-001": 2}, out
    assert round(out["by_strategy"]["MANUAL-001"], 2) == round(_pnl(leg1) + _pnl(leg2), 2), out
    assert round(out["total"], 2) == round(_pnl(leg1) + _pnl(leg2), 2)


def test_a_leg_closed_before_today_is_restored_but_not_todays_realized():
    """The seed restores every leg the cache ever held. The DAY window still applies to them — a
    restored leg from last week must not land in today's figure."""
    cache, states, leg1, leg2 = _reopened()
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    old = dict(states[1])                                             # leg 1's closed state, a week ago
    old["ts_opened"] = day_start - 8 * _DAY
    old["ts_closed"] = day_start - 7 * _DAY
    eng = _engine(cache)
    _seed(eng, {POS_ID: [states[0], old, states[2], states[3]]})
    # Without this line the test passes today for the wrong reason (nothing restored, nothing to
    # exclude) — a test about nothing.
    assert len(eng._closed_legs) == 2, "the week-old leg must be restored before it can be excluded"

    out = eng._session_realized()

    assert out["closed_count"] == {"MANUAL-001": 1}
    assert round(out["by_strategy"]["MANUAL-001"], 2) == _pnl(leg2)


def test_the_same_instrument_reopened_by_ANOTHER_strategy_is_that_strategys_leg():
    """Two ids, two strategies, one instrument. A fix that parses `{instrument}-{strategy}` or dedups by
    instrument would fold MOMENTUM's leg into MANUAL's."""
    cache, states, leg1, leg2 = _reopened()
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    mom_states: list[dict] = []
    mom = _round_trip(cache, mom_states, open_side=OrderSide.BUY, open_px="50.00", close_px="60.00",
                      t_open=day_start + 5 * _H, t_close=day_start + 6 * _H,
                      strategy=_MOMENTUM, pos_id="MPC.XNYS-MOMENTUM-002")
    eng = _engine(cache)
    _seed(eng, {POS_ID: states, "MPC.XNYS-MOMENTUM-002": mom_states})

    out = eng._session_realized()

    assert out["closed_count"] == {"MANUAL-001": 2, "MOMENTUM-002": 1}
    assert round(out["by_strategy"]["MOMENTUM-002"], 2) == _pnl(mom)
    assert round(out["by_strategy"]["MANUAL-001"], 2) == round(_pnl(leg1) + _pnl(leg2), 2)


def test_an_EXTERNAL_leg_is_unclaimed_money_not_a_strategy_row():
    """Broker positions no strategy owns close too. Their P&L is real and must reconcile the total,
    but attributing it to a strategy charges one for a decision it never made (books.ts header)."""
    cache, states, leg1, leg2 = _reopened()
    ext = [dict(s, strategy_id="EXTERNAL", position_id="MPC.XNYS-EXTERNAL") for s in states[:2]]
    eng = _engine(cache)
    _seed(eng, {POS_ID: states, "MPC.XNYS-EXTERNAL": ext})

    out = eng._session_realized()

    assert "EXTERNAL" not in out["by_strategy"]
    assert round(out["unclaimed"], 2) == _pnl(leg1)
    assert round(out["total"], 2) == round(sum(out["by_strategy"].values()) + out["unclaimed"], 2)


def test_a_partial_exit_on_a_still_open_position_is_counted_as_partial_not_as_a_leg():
    """Native cannot timestamp a partial realization on an open position (realized.py). It is reported
    as incompleteness, never folded into closed legs and never dropped."""
    cache, states, leg1, leg2 = _reopened()
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    still_open = Position(instrument=_INST, fill=_fill(OrderSide.BUY, 4, "10.00", day_start + 5 * _H,
                                                       strategy=_MOMENTUM, pos_id="MPC.XNYS-MOMENTUM-002"))
    cache.add_position(still_open, OmsType.NETTING)
    still_open.apply(_fill(OrderSide.SELL, 2, "12.00", day_start + 6 * _H, strategy=_MOMENTUM,
                           pos_id="MPC.XNYS-MOMENTUM-002"))
    cache.update_position(still_open)
    assert still_open.is_open and abs(still_open.realized_pnl.as_double()) > 0, "fixture: a partial exit realized something"
    eng = _engine(cache)
    _seed(eng, {POS_ID: states})

    # The PERIOD path, not only today's — a window implementation could count the partial as a closed
    # leg or drop the signal, and `_session_realized` alone (already correct here) would not notice.
    out = eng._realized_windows()["1D"]

    assert out["partial_open"] == 1 and out["is_partial"] is True
    assert "MOMENTUM-002" not in out["by_strategy"], "an open position's partial is not a closed leg"
    assert round(out["total"], 2) == round(_pnl(leg1) + _pnl(leg2), 2), "closed legs only"


# -- the windows ----------------------------------------------------------------------------------------

def _seed_spread(eng) -> None:
    """Legs closing 3, 20 and 60 ET-days ago, plus today's reopen pair — one per window band."""
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    _, states, _, _ = _reopened()
    older: list[dict] = []
    for days_ago, close_px in ((3, "110.00"), (20, "120.00"), (60, "130.00")):
        s: list[dict] = []
        _round_trip(Cache(), s, open_side=OrderSide.BUY, open_px="100.00", close_px=close_px,
                    t_open=day_start - days_ago * _DAY + 1 * _H, t_close=day_start - days_ago * _DAY + 2 * _H)
        older.append(s[1])
    _seed(eng, {POS_ID: states + older})


def test_windows_nest_and_are_keyed_by_the_sweeps_own_vocabulary():
    """1D/1W/1M/3M/all, the keys `PERIOD_DAYS` and the UI selector already use. 1D <= 1W <= 1M <= 3M <= all
    by construction — a shorter window reading higher than a longer one is the defect the sweep once had."""
    eng = _engine(Cache())
    _seed_spread(eng)

    w = eng._realized_windows()

    assert list(w) == list(PERIOD_DAYS), "one vocabulary for the sweep, the legs and the UI"
    counts = [w[k]["closed_count"].get("MANUAL-001", 0) for k in ("1D", "1W", "1M", "3M", "all")]
    assert counts == [2, 3, 4, 5, 5], counts
    totals = [w[k]["total"] for k in ("1D", "1W", "1M", "3M", "all")]
    assert totals == sorted(totals), "windows must nest"


def test_a_window_is_half_open_at_both_ends_in_ET_calendar_days():
    """A close exactly at a window's start belongs to it; one 1ns before does not. The boundaries are
    ET midnights, the same rule `_et_day_bounds_ns` already pins for 1D."""
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    week_day = (datetime.fromtimestamp(day_start / 1e9, _ET) - timedelta(days=PERIOD_DAYS["1W"])).date()
    week_start = int(datetime.combine(week_day, datetime.min.time(), tzinfo=_ET).timestamp() * 1e9)
    _, states, _, _ = _reopened()
    at_start = dict(states[1], ts_opened=week_start - _H, ts_closed=week_start)
    before = dict(states[1], ts_opened=week_start - 2 * _H, ts_closed=week_start - 1)
    eng = _engine(Cache())
    _seed(eng, {"A.XNYS-MANUAL-001": [at_start], "B.XNYS-MANUAL-001": [before]})

    w = eng._realized_windows()

    assert w["1W"]["closed_count"] == {"MANUAL-001": 1}, "the close AT the start is in; the one 1ns before is out"
    assert w["1M"]["closed_count"] == {"MANUAL-001": 2}


def test_the_all_window_says_how_far_back_it_can_see():
    """`all` is not account inception — it is the oldest leg this cache holds (ibkr-paper: 2026-09-03). A
    window that cannot say its horizon reads as a lifetime figure, which it is not."""
    eng = _engine(Cache())
    _seed_spread(eng)
    day_start, _ = _et_day_bounds_ns(NOW_NS)

    w = eng._realized_windows()

    assert w["all"]["horizon_ts"] == day_start - 60 * _DAY + 2 * _H
    assert w["1D"]["horizon_ts"] == w["all"]["horizon_ts"], "one horizon, stated on every window"


def test_the_1D_window_and_the_session_figure_are_the_same_derivation():
    """Two derivations of one fact will disagree. `realized_session` (Home) and `realized_periods[1D]`
    (the panel) must come from the same legs over the same window, or the two surfaces drift apart —
    which is exactly what paper showed on 2026-09-10 (-332.45 vs -194.53 for one lane, one day)."""
    cache, states, _, _ = _reopened()
    eng = _engine(cache)
    _seed(eng, {POS_ID: states})

    assert eng._realized_windows()["1D"]["by_strategy"] == eng._session_realized()["by_strategy"]


def test_a_window_start_across_the_DST_change_is_still_an_ET_midnight():
    """A 1W window that started 7 x 24h ago would start at 01:00 or 23:00 ET across the DST change and
    silently move a close between windows. Anchor on ET calendar days, not on 86,400-second days."""
    from api.engine_node import _et_window_start_ns
    after_dst_end = int(datetime(2026, 11, 3, 15, 30, tzinfo=_ET).timestamp() * 1e9)   # DST ended 2026-11-01
    start = _et_window_start_ns(after_dst_end, days=PERIOD_DAYS["1W"])
    local = datetime.fromtimestamp(start / 1e9, _ET)
    assert local.strftime("%H:%M") == "00:00"
    assert local.date() == datetime(2026, 10, 27, tzinfo=_ET).date()


# -- the frame ------------------------------------------------------------------------------------------

def test_on_IBKR_the_trades_frame_publishes_realized_periods_from_legs_and_says_so():
    """The panel reads `realized_periods.by_strategy`. On the IBKR instance there is no Alpaca sweep,
    `_realized_periods` is None for the life of the process, and the panel is blank. The frame must
    carry the legs-derived windows — and name the source, so a reader can tell which derivation
    they are looking at."""
    cache, states, leg1, leg2 = _reopened()
    eng = _engine(cache, swept=None)
    _seed(eng, {POS_ID: states})

    eng._publish_trades()

    frame = _frame(eng)
    assert frame["status"] == "ok"
    periods = frame["realized_periods"]
    assert periods is not None, "the IBKR frame published null periods — the panel stays blank"
    assert round(periods["1D"]["by_strategy"]["MANUAL-001"], 2) == round(_pnl(leg1) + _pnl(leg2), 2)
    assert periods["source"] == "legs"
    assert frame["realized_session"]["by_strategy"] == periods["1D"]["by_strategy"]


def test_with_a_broker_sweep_the_frame_still_publishes_the_legs_split_beside_it():
    """Alpaca has both. The sweep's account total (fees, withholding) is the broker's number; the
    per-strategy split is the legs'. Publishing only one hides the disagreement that found #846."""
    cache, states, leg1, leg2 = _reopened()
    swept = {k: {"by_strategy": {"MANUAL-001": -1.0}, "total": -1.0} for k in PERIOD_DAYS}
    eng = _engine(cache, swept=swept)
    _seed(eng, {POS_ID: states})

    eng._publish_trades()

    frame = _frame(eng)
    assert frame["realized_periods"]["source"] == "legs"
    assert round(frame["realized_periods"]["1D"]["by_strategy"]["MANUAL-001"], 2) == round(_pnl(leg1) + _pnl(leg2), 2)
    assert frame["realized_periods_swept"] is swept, "the broker's own figure stays visible beside the legs'"


def test_a_seed_that_stopped_early_is_reported_on_the_frame_not_absorbed():
    """`read_restored_legs` stops at its deadline and logs. A realized figure computed over a partial
    seed is a wrong number wearing a complete label — the frame must say the legs are incomplete."""
    cache, states, _, _ = _reopened()
    eng = _engine(cache)
    _seed(eng, {POS_ID: states}, stopped_at=f"trader-{TRADER}:snapshots:positions:ZZZ")

    eng._publish_trades()

    legs = _frame(eng)["realized_legs"]
    assert legs["restored"] == 2
    assert legs["seed_stopped_at"].endswith(":ZZZ")
    assert legs["complete"] is False


# -- scope self-review (codex died at the usage wall; these are the two findings that changed the scope) --

def test_the_legs_windows_carry_every_key_the_UI_reads_off_the_sweeps_windows():
    """`books.ts` reads `by_strategy`, `total`, `unclaimed` and `unmatched` off `realized_periods[p]`
    (schema.ts types it as free-form, so nothing rejects a missing key — it reads undefined). A legs
    window that lacks `unmatched` silently kills the partial asterisk. `unmatched` here means
    `partial_open`: both say "the total understates", and the UI treats them the same way."""
    cache, states, _, _ = _reopened()
    eng = _engine(cache)
    _seed(eng, {POS_ID: states})

    w = eng._realized_windows()

    for period, window in w.items():
        for key in ("by_strategy", "total", "unclaimed", "unmatched", "closed_count", "partial_open",
                    "is_partial", "horizon_ts"):
            assert key in window, f"{period} lacks {key!r}"
        assert window["unmatched"] == window["partial_open"]


def test_the_sweep_and_the_legs_use_ONE_window_floor_predicate():
    """Two derivations of one fact will disagree — and must not disagree by CONSTRUCTION. The sweep's
    floors were `day_start - days * 86400s` (UTC arithmetic: 23:00 or 01:00 ET across a DST change);
    the legs use ET calendar midnights. A leg closing at 00:30 ET on the 1W boundary would be in one
    window and out of the other. One helper, both callers."""
    from api.engine_node import _et_window_start_ns
    from api.realized_broker import realized_by_period
    import inspect

    sig = inspect.signature(realized_by_period)
    assert "floor_of" in sig.parameters, "realized_by_period must accept the shared floor predicate"
    src = inspect.getsource(UiFeedStrategy._refresh_realized_periods)
    assert "floor_of=_et_window_start_ns" in src, "the sweep must be handed the same predicate the legs use"


# -- scope review (subagent, step 5): the two blockers and the shoulds ----------------------------------

def test_the_display_strategy_asks_the_bus_for_EVERY_strategys_position_events():
    """Nautilus delivers `on_position_event` only for the subscribing strategy's OWN positions
    (`trading/strategy.pyx:322-323` subscribes `events.position.{self.id}`), and this strategy is
    MANUAL. So a registry fed from `on_position_event` covers MANUAL-001 and no other lane — the
    paper defect (MOMENTUM-002's GMAB reopen) would survive the fix until the next restart. Same hole
    the order receipt had (#207); same fix: the wildcard, asked for in `on_start`."""
    import inspect

    from api.engine_node import _POSITION_EVENTS_TOPIC

    assert _POSITION_EVENTS_TOPIC == "events.position.*"
    # Structural half only: `on_start` must CALL the subscription helper. Delivery — that the helper
    # subscribes the right topic to the right handler — is proven with a real bus below, because a
    # comment naming the topic would satisfy a source-string check (impl review, #846).
    src = inspect.getsource(UiFeedStrategy.on_start)
    assert "self._subscribe_position_events()" in src, "on_start does not ask for every strategy's position events"


def test_the_leg_read_does_not_die_with_the_cycle_store():
    """`_load_seed` returned None when the cycle store was absent — Postgres unreachable at boot meant
    zero legs, the windows silently reverted to `positions_closed()`, and `restored: 0` read like an
    account with no closed legs. The legs live in Redis; they must be read whether or not the envelope
    store is."""
    import asyncio

    cache, states, leg1, leg2 = _reopened()
    eng = _engine(cache)
    eng._cycle_store = None
    eng._SEED_TIMEOUT_SECS = 5.0
    eng._snapshot_redis = lambda: _Redis({POS_ID: states})

    payload = asyncio.run(eng._load_seed())

    assert payload is not None, "no cycle store must not mean no legs"
    assert payload["cycles"] == {}
    assert sorted(l.ts_opened for l in payload["legs"][POS_ID]) == sorted([leg1.ts_opened, leg2.ts_opened])
    assert "legs_stopped_at" in payload and payload["legs_stopped_at"] is None


def test_the_legs_status_has_THREE_states_and_never_ran_is_not_complete():
    """`0 of 0` is not `0 of 4`. A seed that never ran (store down, thread timed out) must be readable
    as its own condition — not as "complete, nothing to restore"."""
    cache, states, _, _ = _reopened()
    eng = _engine(cache)

    assert eng._realized_legs_status()["state"] == "never_ran"
    assert eng._realized_legs_status()["complete"] is False

    _seed(eng, {POS_ID: states}, stopped_at="trader-X:snapshots:positions:ZZZ")
    assert eng._realized_legs_status()["state"] == "stopped_early"
    assert eng._realized_legs_status()["complete"] is False

    _seed(eng, {POS_ID: states})
    assert eng._realized_legs_status()["state"] == "complete"
    assert eng._realized_legs_status()["complete"] is True


def test_the_sweep_floors_a_DST_crossing_window_at_ET_midnight_when_handed_the_predicate():
    """By VALUE, not by source text. DST ended 2026-11-01; from Tuesday 2026-11-03 the 1W floor is
    00:00 ET on 2026-10-27. UTC-second arithmetic lands at 01:00 EDT that day and drops a fill made in
    the first hour. `floor_of` is REQUIRED so no caller can silently get the wrong floor back."""
    import inspect

    from api.realized import et_window_start_ns
    from api.realized_broker import realized_by_period

    assert inspect.signature(realized_by_period).parameters["floor_of"].default is inspect.Parameter.empty, \
        "an optional floor_of is the invisible-missing-argument fallback"

    day_start = int(datetime(2026, 11, 3, 0, 0, tzinfo=_ET).timestamp() * 1e9)
    at_floor = datetime(2026, 10, 27, 0, 30, tzinfo=_ET)                 # inside 1W only at an ET-midnight floor
    fills = [
        {"symbol": "A", "side": "buy", "qty": "1", "price": "100", "transaction_time": at_floor.isoformat(), "order_id": "o1"},
        {"symbol": "A", "side": "sell", "qty": "1", "price": "110", "transaction_time": at_floor.isoformat(), "order_id": "o2"},
    ]
    ns_of = lambda iso: int(datetime.fromisoformat(iso).timestamp() * 1e9)  # noqa: E731

    out = realized_by_period(fills, day_start_ns=day_start, ns_of=ns_of, floor_of=et_window_start_ns)

    assert out["1W"]["closed_count"] == 1, "the 00:30 ET round trip on the floor day is inside 1W"
    assert out["1D"]["closed_count"] == 0


def test_legs_in_two_settlement_currencies_are_REFUSED_not_summed():
    """`_money` drops the currency. An SGD-based IBKR account could hold a leg settling in SGD beside
    the USD ones; summing them as one number is a wrong figure that looks right. Refuse, and say so."""
    from nautilus_trader.model.currencies import SGD
    from nautilus_trader.model.objects import Money

    from api.trade_cycle import CycleLeg

    cache, states, leg1, leg2 = _reopened()
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    sgd = CycleLeg(day_start + 5 * _H, day_start + 6 * _H, Money(100, SGD), "MANUAL-001", "D05.XSES", "D05.XSES-MANUAL-001")
    eng = _engine(cache)
    _seed(eng, {POS_ID: states})
    eng._register_closed_leg(sgd)

    w = eng._realized_windows()

    assert w["1D"]["by_strategy"] == {}, "a mixed-currency book must not publish a sum"
    assert "SGD" in w["1D"]["error"] and "USD" in w["1D"]["error"]


# -- implementation review (subagent, step 7): the unearned mechanisms, earned ---------------------------

def test_a_seed_that_STOPS_EARLY_reaches_the_status_through_the_real_loader():
    """`legs_stopped_at` was hand-built by the test helper, so `_load_seed` could hardcode None and stay
    green. Drive the real loader with a Redis too slow for its budget: the stop must travel
    reader -> payload -> `_apply_seed` -> `_realized_legs_status()`."""
    import asyncio
    import time as _t

    from api.test_snapshot_read_is_deadlined import _SlowRedis

    eng = _engine(Cache())
    eng._cycle_store = None
    eng._SEED_TIMEOUT_SECS = 0.05
    slow = _SlowRedis(n_keys=200, per_key_secs=0.01)
    eng._snapshot_redis = lambda: slow

    payload = asyncio.run(eng._load_seed())
    assert payload["legs_stopped_at"] is not None, "the loader must carry the reader's stop"
    UiFeedStrategy._apply_seed(eng, payload)

    st = eng._realized_legs_status()
    assert st["state"] == "stopped_early" and st["complete"] is False
    assert st["seed_stopped_at"] == payload["legs_stopped_at"]


def test_an_ABSENT_stopped_at_key_is_not_read_as_complete():
    """`payload.get("legs_stopped_at") or ""` read an absent key as a finished scan — absence as
    permission. A payload without the key is a loader that never said; refuse it."""
    import pytest

    eng = _engine(Cache())
    with pytest.raises(KeyError):
        UiFeedStrategy._apply_seed(eng, {"legs": {}, "cycles": {}})


def test_a_duplicate_leg_with_DIFFERENT_money_resolves_to_the_LATEST_close_everywhere():
    """"Latest close wins" lived in three places (reader, dedup, registry) and its only test re-appended
    an identical state, so first-wins passed. One predicate, and a duplicate that carries different
    money so first and latest are distinguishable — at the reader, the registry and the fold."""
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.objects import Money

    from api.trade_cycle import CycleLeg

    _, states, leg1, _ = _reopened()
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    first = dict(states[1])                                              # leg 1 closed at +2h, its money
    later = dict(states[1], ts_closed=day_start + 2 * _H + 1, realized_pnl="1.00 USD")  # re-applied: later, different

    # the reader, reversed order so list position cannot be what decides
    legs = read_restored_legs(_Redis({POS_ID: [later, states[0], first]}), TRADER)[POS_ID]
    assert [round(l.realized_pnl.as_double(), 2) for l in legs] == [1.0]

    # the registry
    eng = _engine(Cache())
    eng._register_closed_leg(CycleLeg.from_state_dict(later, position_id=POS_ID))
    eng._register_closed_leg(CycleLeg.from_state_dict(first, position_id=POS_ID))
    assert round(eng._closed_legs[(POS_ID, leg1.ts_opened)].realized_pnl.as_double(), 2) == 1.0

    # the fold, with the cache holding the EARLIER state of the same leg
    cache = Cache()
    cache.add_position(leg1, OmsType.NETTING); cache.update_position(leg1)
    eng.cache = cache
    assert round(eng._session_realized()["by_strategy"]["MANUAL-001"], 2) == 1.0


def test_the_wildcard_delivers_ANOTHER_lanes_close_to_the_registry_through_a_REAL_bus():
    """By delivery, not by source text (a comment naming the topic passed the old test). A real Nautilus
    MessageBus, the engine's own subscription helper, and a MOMENTUM-002 close published where the
    exec engine publishes it: `events.position.MOMENTUM-002`."""
    from nautilus_trader.common.component import MessageBus, TestClock
    from nautilus_trader.model.identifiers import TraderId

    from api.engine_node import _POSITION_EVENTS_TOPIC

    day_start, _ = _et_day_bounds_ns(NOW_NS)
    mom_states: list[dict] = []
    mom = _round_trip(Cache(), mom_states, open_side=OrderSide.BUY, open_px="50.00", close_px="60.00",
                      t_open=day_start + 5 * _H, t_close=day_start + 6 * _H,
                      strategy=_MOMENTUM, pos_id="MPC.XNYS-MOMENTUM-002")
    bus = MessageBus(trader_id=TraderId("PLATFORM-TEST"), clock=TestClock())
    eng = _engine(Cache())
    eng.msgbus = bus
    eng._subscribe_position_events = UiFeedStrategy._subscribe_position_events.__get__(eng)

    eng._subscribe_position_events()
    bus.publish(f"events.position.{mom.strategy_id}", TestEventStubs.position_closed(mom))
    bus.publish("events.order.MOMENTUM-002", object())                   # the other plane must not register

    assert ("MPC.XNYS-MOMENTUM-002", mom.ts_opened) in eng._closed_legs
    assert len(eng._closed_legs) == 1
    assert _POSITION_EVENTS_TOPIC == "events.position.*"


def test_a_window_that_could_not_be_computed_says_so_on_the_frame():
    """`empty_windows(error)` carries `total: 0.0`; the UI read `total` and nothing read `error`, so a
    refusal rendered as a confident $0.00. The frame's `realized_legs` carries the error, and the
    window's `total` is None — never zero — when it has no answer."""
    from nautilus_trader.model.currencies import SGD
    from nautilus_trader.model.objects import Money

    from api.trade_cycle import CycleLeg

    cache, states, _, _ = _reopened()
    day_start, _ = _et_day_bounds_ns(NOW_NS)
    eng = _engine(cache)
    _seed(eng, {POS_ID: states})
    eng._register_closed_leg(CycleLeg(day_start + 5 * _H, day_start + 6 * _H, Money(100, SGD),
                                      "MANUAL-001", "D05.XSES", "D05.XSES-MANUAL-001"))

    eng._publish_trades()

    frame = _frame(eng)
    assert frame["realized_periods"]["1D"]["total"] is None, "no answer must not read as zero"
    assert frame["realized_session"]["total"] is None
    assert "SGD" in frame["realized_legs"]["error"]
