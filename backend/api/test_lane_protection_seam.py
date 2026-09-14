"""The engine seam for per-lane protection modes (#872) — the reconciler pass, not the planner.

TEST THE SEAM, NOT THE UNIT. `test_lane_protection_modes.py` proves `plan_protection` decides the right
thing; none of that says the reconciler READS the settings, builds the right order, or executes the
wrong-mode cancels. Five production breaks on 2026-08-14 had a correct unit and wrong wiring, every one
with a green suite.

So these drive `UiFeedStrategy._reconcile_protection` against the doubles in
`test_protection_reconciler.py` — the same ones, deliberately imported rather than re-declared. A second
copy of that harness would be free to accept a float where Nautilus demands a `Quantity`, or to answer
`avg_px_open` where production would not, which is four of the six defects `test_double_conformance.py`
exists for.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from api.protection import PROTECTION_COID_PREFIX
from api.test_protection_reconciler import (
    _ENTRY_FOR,
    _LANE_FOR,
    _Fake,
    _Http,
    _ns,
    _position,
    _run,
    _settings,
)

#: `_Fake` seeds AEM's cache position with this lane and this entry; `_protection_atr` answers 5.0 and
#: `_last_price_for` 100.0 everywhere. So the expected floor is 104.00 - 1.5 x 5.0 = 96.50.
_AEM_LANE = _LANE_FOR["AEM"]
_AEM_ENTRY = _ENTRY_FOR["AEM"]
_EXPECTED_FLOOR = "96.50"


def _floor_settings(monkeypatch, lane: str = _AEM_LANE, **lane_cfg):
    return _settings(monkeypatch, **{f"{lane}_protection": {"mode": "entry_floor", **lane_cfg}})


class _CachedOrder:
    """A Nautilus `Order` as the reconciler reads it: an id, an owning lane, and whether it is OPEN.

    `is_open` is REAL here because the cancel route turns on it. `Strategy.cancel_order` refuses a
    closed order, so a double that only answered "present in the cache" could not represent the #807
    corpse — a stop the cache holds terminal while the venue still rests it — and the venue route would
    have looked covered while being unreachable.
    """

    def __init__(self, coid: str, strategy_id: str, *, is_open: bool = True) -> None:
        self.client_order_id = coid
        self.strategy_id = strategy_id
        self.is_open = is_open
        self.status = type("S", (), {"name": "ACCEPTED" if is_open else "REJECTED"})()


def _resting_at_broker(symbol: str, coid: str, order_type: str, qty: float,
                       venue_id: str = "V-1") -> dict:
    return {"id": venue_id, "client_order_id": coid, "symbol": symbol, "side": "sell",
            "order_type": order_type, "type": order_type, "status": "new", "qty": qty,
            "filled_qty": 0}


def _with_cached(fake: _Fake, order: _CachedOrder) -> _Fake:
    fake._orders_by_coid[order.client_order_id] = order
    return fake


# ---------------------------------------------------------------------------------------------
# (k) THE SETTINGS VALUE TRAVELS — file shape to submitted order.
# ---------------------------------------------------------------------------------------------

def test_the_default_settings_still_produce_a_TRAILING_stop(monkeypatch):
    """FIXTURE PROPERTY. Without this, every assertion below could pass on a reconciler that builds a
    floor for every lane — and the class guard would be measuring nothing."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))

    _run(fake)

    assert fake.built[0]["order_type"] == "trailing_stop"
    assert "trigger_price" not in fake.built[0]


def test_a_lane_on_entry_floor_gets_a_STOP_MARKET_at_its_own_entry_minus_the_multiple(monkeypatch):
    """THE WHOLE WIRE: `<LANE>_protection` in the settings file -> `lane_modes` -> `plan_protection`
    -> `_build_order`. The entry is 104.00, distinct from the 100.00 mark, so a mark-relative mutant
    cannot produce this number."""
    _floor_settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))

    _run(fake)

    assert len(fake.submitted) == 1
    payload = fake.built[0]
    assert payload["order_type"] == "stop_market"
    assert payload["trigger_price"] == _EXPECTED_FLOOR
    assert payload["time_in_force"] == "gtc"      # must outlive the session
    assert payload["reduce_only"] is True         # a stop must never OPEN a position
    assert payload["extra_tags"] == ["mode:entry_floor"]
    assert payload["strategy_id"] == _AEM_LANE
    assert payload["client_order_id"].startswith(PROTECTION_COID_PREFIX)
    assert payload["quantity"] == 54.0


def test_the_lane_multiple_from_the_settings_file_reaches_the_trigger(monkeypatch):
    """2.25 is a value the domain default (1.5) could not produce: 104.00 - 2.25 x 5.0 = 92.75."""
    _floor_settings(monkeypatch, atrMultiple=2.25)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))

    _run(fake)

    assert fake.built[0]["trigger_price"] == "92.75"


def test_a_lane_on_mode_none_rests_nothing_and_says_OPTED_OUT(monkeypatch):
    """Neither covered nor naked. The refusal is standing state on `/health.failed_requests`, not a log
    line that scrolls past — that silence is where this whole class of defect lives."""
    _settings(monkeypatch, **{f"{_AEM_LANE}_protection": {"mode": "none"}})
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))

    _run(fake)

    assert fake.submitted == []
    rendered = " ".join(str(r) for r in fake._failed_requests.as_rows())
    assert "opted_out" in rendered
    assert any("opted_out" in w for w in fake.log.warnings)


def test_a_floor_at_or_above_the_last_price_is_refused_at_the_seam_and_never_submitted(monkeypatch):
    """The pre-submit rejection, driven through the real pass. AEM's entry is 104.00; drop the mark to
    95.00 and the 96.50 floor sits ABOVE it. Nothing is built, so no client order id is burned."""
    _floor_settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))
    fake._last_price_for = lambda iid: 95.0

    _run(fake)

    # FIXTURE PROPERTY: the floor really is above this mark, so the branch is reachable.
    assert _AEM_ENTRY - 1.5 * 5.0 > 95.0
    assert fake.built == [] and fake.submitted == []
    assert "floor_below_market" in " ".join(str(r) for r in fake._failed_requests.as_rows())


# ---------------------------------------------------------------------------------------------
# (j) COVERAGE. The floor the engine placed must be recognised as protection on the NEXT pass.
# ---------------------------------------------------------------------------------------------

def test_a_resting_floor_is_counted_as_coverage_and_the_leg_reports_NOTHING_WRONG(monkeypatch):
    """The mutation: treat a STOP_MARKET as non-coverage. The lane's own floor then stops counting as
    protection and the leg is uncovered on every single pass, forever.

    AND THE OBVIOUS ASSERTION IS VACUOUS, which is why it is not the one made here. "no second stop was
    submitted" survives that mutation: the resting floor still RESERVES all 54 shares, so `free` is 0,
    nothing is placeable and the pass refuses `reserved_by_other_order` instead of submitting. Green,
    with the mechanism cut. The observable difference is the REFUSAL — a lane permanently reading
    unprotected on `/health.failed_requests` while a correct stop rests at the venue, which is the alarm
    an operator learns to scroll past.
    """
    _floor_settings(monkeypatch)
    floor = _resting_at_broker("AEM", "PROT-SELL-AEM-XNYS-abc", "STOP_MARKET", 54)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[floor])),
                        _CachedOrder("PROT-SELL-AEM-XNYS-abc", _AEM_LANE))

    _run(fake)

    assert fake.submitted == []
    assert fake.cancelled == []
    # COVERED means covered: no refusal, no standing failure, nothing for an operator to chase.
    assert fake._failed_requests.as_rows() == []


def test_a_resting_floor_is_resized_by_QUANTITY_and_never_re_priced(monkeypatch):
    """A partial exit leaves 20 held under a 54-share floor. The correction is a PATCH of the quantity;
    the trigger was computed once at placement and no pass may send a new one — an entry floor that
    ratchets is a trail wearing the wrong name."""
    _floor_settings(monkeypatch)
    floor = _resting_at_broker("AEM", "PROT-SELL-AEM-XNYS-abc", "STOP_MARKET", 54)
    fake = _with_cached(
        _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position(qty="20")], orders=[floor])),
        _CachedOrder("PROT-SELL-AEM-XNYS-abc", _AEM_LANE))

    _run(fake)

    assert [q for _o, q in fake.modified] == [20.0]
    assert [(p, t) for _o, p, t in fake.repriced] == [(None, None)]
    assert fake.submitted == []


# ---------------------------------------------------------------------------------------------
# (h) THE TRANSITION, executed. Cancel on this pass, floor on the next.
# ---------------------------------------------------------------------------------------------

def test_a_resting_trail_on_an_entry_floor_lane_is_CANCELLED_and_the_flip_is_named(monkeypatch):
    """Pass one of the transition. The trail counts as coverage AND reserves the shares, so nothing can
    be placed until it is gone — and since #898 the floor follows in the same pass once the venue
    confirms (section (n) below pins that half). This pins the cancel and the standing-state row."""
    _floor_settings(monkeypatch)
    trail = _resting_at_broker("AEM", "PROT-SELL-AEM-XNYS-abc", "TRAILING_STOP_MARKET", 54)
    cached = _CachedOrder("PROT-SELL-AEM-XNYS-abc", _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])),
                        cached)

    _run(fake)

    assert fake.cancelled == [cached]
    assert "the floor is placed this pass once the venue confirms" in " ".join(
        str(r) for r in fake._failed_requests.as_rows())


def test_the_pass_AFTER_the_cancel_places_the_floor(monkeypatch):
    """Pass two, with the trail gone. Without this the cancel above is a strip, not a transition."""
    _floor_settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))

    _run(fake)

    assert [b["trigger_price"] for b in fake.built] == [_EXPECTED_FLOOR]


def test_a_trail_the_cache_holds_TERMINAL_is_cancelled_at_the_VENUE(monkeypatch):
    """The #807 corpse. `Strategy.cancel_order` refuses a closed order, so a stop the cache gave up on
    while the venue still rests it would be "cancelled" by a warning and keep reserving its shares
    forever — and the floor could never be placed. Routing on `is_open` is what makes the venue path
    reachable at all."""
    _floor_settings(monkeypatch)
    trail = _resting_at_broker("AEM", "PROT-SELL-AEM-XNYS-abc", "TRAILING_STOP_MARKET", 54,
                               venue_id="V-CORPSE")
    http = _Http(positions=[_position()], orders=[trail])
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=http),
                        _CachedOrder("PROT-SELL-AEM-XNYS-abc", _AEM_LANE, is_open=False))

    _run(fake)

    assert fake.cancelled == []                       # Nautilus could not
    assert http.cancelled_at_venue == ["V-CORPSE"]    # so the venue route did


def test_a_trail_on_a_TRAIL_lane_is_left_alone_at_the_seam(monkeypatch):
    """The sibling. AEM's lane is on entry_floor; SSRM's is not, and its trail must survive the pass."""
    _floor_settings(monkeypatch)
    trail = _resting_at_broker("SSRM", "PROT-SELL-SSRM-XNYS-def", "TRAILING_STOP_MARKET", 10)
    fake = _with_cached(
        _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position(symbol="SSRM", qty="10", mv="1000")],
                                           orders=[trail])),
        _CachedOrder("PROT-SELL-SSRM-XNYS-def", _LANE_FOR["SSRM"]))

    _run(fake)

    assert fake.cancelled == []
    assert fake.submitted == []


# ---------------------------------------------------------------------------------------------
# THE COID FAMILY IS ONE DERIVATION. `wrong_mode` filters on the prefix the engine mints.
# ---------------------------------------------------------------------------------------------

def test_the_coid_the_engine_mints_carries_the_prefix_the_planner_filters_on(monkeypatch):
    """Two derivations of one fact drift. If they did here, `wrong_mode` would either match nothing —
    a lane stuck on its trail forever, silently — or match orders this mechanism never placed."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))

    _run(fake)

    assert fake.built[0]["client_order_id"].startswith(PROTECTION_COID_PREFIX)


# ---------------------------------------------------------------------------------------------
# THE TICK. A computed price lands between ticks routinely, and `_canon_price` is fail-closed.
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("atr, expected", [(5.0, "96.50"), (5.01, "96.48"), (5.007, "96.48")])
def test_a_floor_between_ticks_is_quantised_DOWN_rather_than_refused(monkeypatch, atr, expected):
    """104.00 - 1.5 x 5.007 = 96.4895. `_canon_price` REJECTS a value that is not a clean tick multiple
    — correctly, for a price an operator typed — so a computed floor must be put on the tick before it
    gets there, or the order refuses to build on every single pass.

    DOWN for a SELL, never nearest: rounding toward the market by half a tick makes the stop marginally
    likelier to fire early, and can turn a placeable floor into one the venue rejects.
    """
    _floor_settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))
    fake._protection_atr = lambda iid, lookback: atr

    _run(fake)

    assert fake.built[0]["trigger_price"] == expected


# ---------------------------------------------------------------------------------------------
# (n) THE FLIP WINDOW (#898). Measured on paper 2026-09-10: `type` is not patchable and `stop_price`
# is refused on a trailing stop (422 42210000), so trail -> floor is a venue-forced cancel-then-place.
# What is NOT forced is a 60 s tick between the two, an RTH gate that lets a cancel go out at
# 15:59:30, three legs bared at once, and a standoff that silently blocks the floor. The floor lands in
# the SAME pass, after the venue confirms the cancel; one leg per pass; refused late in the session;
# deferred behind a live exit; and a nested pass in the window acts on nothing.
# ---------------------------------------------------------------------------------------------

def _rows_of(fake: _Fake) -> str:
    return " ".join(str(r) for r in fake._failed_requests.as_rows())


def _trail_on_floor_lane(symbol: str = "AEM", coid: str = "PROT-SELL-AEM-XNYS-abc", qty: float = 54,
                         venue_id: str = "V-1"):
    return _resting_at_broker(symbol, coid, "TRAILING_STOP_MARKET", qty, venue_id=venue_id)


def test_FIXTURE_the_confirm_wait_can_tell_a_confirmed_cancel_from_an_unconfirmed_one():
    """FIXTURE PROPERTY FIRST, through the PRIMITIVE the fix depends on, not one level below it. The
    venue confirming a cancel is the row LEAVING its open set; `_await_cancel_confirmed` polls the real
    `_venue_reducing_orders` for exactly that, three-stated: confirmed, timeout, unreadable. If the
    double released on the request rather than the confirm, every same-pass test below would pass for
    the wrong reason. Venue-neutral on purpose: IBKR does not reserve shares, so a share-availability
    wait would return at once there and place a floor beside a trail that may still rest (review)."""
    import asyncio

    trail = _trail_on_floor_lane()
    http = _Http(positions=[_position()], orders=[trail])
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=http), _CachedOrder(trail["client_order_id"], _AEM_LANE))
    assert asyncio.run(fake._await_cancel_confirmed("AEM.XNYS", trail["client_order_id"], "V-1")) == "timeout"
    fake._cancel(fake._orders_by_coid[trail["client_order_id"]])
    assert asyncio.run(fake._await_cancel_confirmed("AEM.XNYS", trail["client_order_id"], "V-1")) == "confirmed"

    stubborn = _Http(positions=[_position()], orders=[_trail_on_floor_lane()], confirms_cancels=False)
    fake2 = _with_cached(_Fake(ts_ns=_ns(10, 0), http=stubborn), _CachedOrder("PROT-SELL-AEM-XNYS-abc", _AEM_LANE))
    fake2._cancel(fake2._orders_by_coid["PROT-SELL-AEM-XNYS-abc"])
    assert asyncio.run(fake2._await_cancel_confirmed("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", "V-1")) == "timeout"

    class _Unreadable(_Http):
        async def get_order(self, venue_order_id):
            raise RuntimeError("venue down")

    fake3 = _Fake(ts_ns=_ns(10, 0), http=_Unreadable(positions=[_position()], orders=[_trail_on_floor_lane()]))
    assert asyncio.run(fake3._await_cancel_confirmed("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", "V-1")) == "unreadable"

    # THE FILL. Gone from the open set, but not cancelled: the shares were SOLD. A predicate reading
    # absence called this "confirmed" and the floor went out over shares just sold (review, HIGH).
    filled = _Http(positions=[_position()], orders=[_trail_on_floor_lane()], cancel_outcome="filled")
    fake4 = _with_cached(_Fake(ts_ns=_ns(10, 0), http=filled), _CachedOrder("PROT-SELL-AEM-XNYS-abc", _AEM_LANE))
    fake4._cancel(fake4._orders_by_coid["PROT-SELL-AEM-XNYS-abc"])
    assert asyncio.run(fake4._await_cancel_confirmed("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", "V-1")) == "filled"


def test_the_floor_lands_in_the_SAME_pass_once_the_venue_confirms_the_cancel(monkeypatch):
    """The window is the cancel-confirm latency, not a protection tick. One pass: the trail is
    cancelled, the venue confirms, the floor is planned through the one planner and placed."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)

    _run(fake)

    assert fake.cancelled == [cached]
    assert [b["trigger_price"] for b in fake.built] == [_EXPECTED_FLOOR]
    assert len(fake.submitted) == 1
    assert "next pass" not in _rows_of(fake), "the row still promises a floor on the NEXT pass"


def test_the_flip_order_key_exists_on_the_TYPED_venue_path():
    """FIXTURE PROPERTY for the ordering below. "Oldest first" was the first version and it was inert
    in production: `orders_from_reports` — the path every node with an exec client takes — carries no
    submission time (the parser stamps `ts_accepted` with NOW), so every row sorted equal. The key the
    engine sorts on must exist, and differ, on rows built from real typed reports."""
    from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId

    from api.protection import orders_from_reports
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    class _Clk:
        def timestamp_ns(self):
            return _ns(10, 0)

    class _Lg:
        def warning(self, *a, **k):
            pass

    class _Host:
        account_id = AccountId("ALPACA-TEST")
        _clock = _Clk()
        _log = _Lg()
        _symbol_to_id: ClassVar[dict] = {"AEM": InstrumentId.from_str("AEM.XNYS")}

        def _client_order_id_for(self, raw):
            return ClientOrderId(raw["client_order_id"])

    # ALPACA'S WIRE SHAPE: numbers are strings. The parser refuses an int (`Quantity.from_str`).
    raws = [dict(_trail_on_floor_lane("AEM", c, 10, v), qty="10", filled_qty="0", trail_percent="5",
                 stop_price="95.0", created_at="2026-08-05T13:31:00Z", updated_at="2026-08-05T13:31:00Z")
            for c, v in (("PROT-SELL-AEM-XNYS-b", "V-B"), ("PROT-SELL-AEM-XNYS-a", "V-A"))]
    reports = [AlpacaExecutionClient._parse_order_report(_Host(), r) for r in raws]
    assert all(reports), "the fixture rows did not survive the typed parse — this test would prove nothing"
    rows = orders_from_reports(reports)
    keys = [str(r.get("client_order_id") or "") for r in rows]
    assert keys == ["PROT-SELL-AEM-XNYS-b", "PROT-SELL-AEM-XNYS-a"] and sorted(keys) != keys
    assert all("submitted_at" not in r for r in rows), "the typed path grew a submission time — re-check the sort key"


def test_three_wrong_mode_legs_flip_ONE_per_pass_in_a_STABLE_order(monkeypatch):
    """`protection.py` said "one leg at a time" while the loop cancelled every wrong-mode entry on one
    pass. Three lanes on entry_floor, three resting trails: pass one flips exactly one — the first by
    client order id, a key both venue paths carry — and names the other two as queued; pass two flips
    the next. Stable across passes is what keeps a leg from starving under a global cap."""
    _settings(monkeypatch, **{f"{lane}_protection": {"mode": "entry_floor"} for lane in
                              (_LANE_FOR["AEM"], _LANE_FOR["SSRM"], _LANE_FOR["NOW"])})
    # LISTED OUT OF KEY ORDER on purpose: plan order, symbol order and venue-id order all disagree
    # with client-order-id order here, so a sort on any of them fails the assertion below.
    trails = [
        _trail_on_floor_lane("SSRM", "PROT-SELL-SSRM-XNYS-b", 10, "V-A"),
        _trail_on_floor_lane("NOW", "PROT-SELL-NOW-XNYS-c", 7, "V-B"),
        _trail_on_floor_lane("AEM", "PROT-SELL-AEM-XNYS-a", 54, "V-C"),
    ]
    http = _Http(positions=[_position("AEM", "54"), _position("SSRM", "10"), _position("NOW", "7")], orders=trails)
    fake = _Fake(ts_ns=_ns(10, 0), http=http)
    for t, sym in zip(trails, ("SSRM", "NOW", "AEM")):
        _with_cached(fake, _CachedOrder(t["client_order_id"], _LANE_FOR[sym]))

    _run(fake)

    assert [o.client_order_id for o in fake.cancelled] == ["PROT-SELL-AEM-XNYS-a"]
    assert [b["instrument_id"] for b in fake.built] == ["AEM.XNYS"]
    rows = _rows_of(fake)
    assert "queued_behind" in rows and "PROT-SELL-SSRM-XNYS-b" in rows and "PROT-SELL-NOW-XNYS-c" in rows

    fake.clock._ts = _ns(10, 1)
    _run(fake)
    assert [o.client_order_id for o in fake.cancelled] == ["PROT-SELL-AEM-XNYS-a", "PROT-SELL-NOW-XNYS-c"]
    assert [b["instrument_id"] for b in fake.built] == ["AEM.XNYS", "NOW.XNYS"]


@pytest.mark.parametrize(("hour", "minute", "second", "flips"), [
    (15, 58, 0, False),
    # STRADDLE THE 180 s LINE (review): 15:57:30 is 150 s to the close and refuses; 15:56:30 is 210 s
    # and flips. Without these two the threshold is pinned only to (120 s, 240 s].
    (15, 57, 30, False),
    (15, 56, 30, True),
    (15, 56, 0, True),
    (10, 0, 0, True),
])
def test_a_flip_is_REFUSED_in_the_last_three_minutes_of_the_session(monkeypatch, hour, minute, second, flips):
    """A cancel at 15:59:30 left the leg bare until the next session's first in-RTH tick — ~17.5 h on
    a weekday. The flip needs the cancel-confirm AND the floor submit inside the session, so it is
    refused, by name, when fewer than three minutes remain. The gate is per leg, not above the pass."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(hour, minute) + second * 10**9,
                              http=_Http(positions=[_position()], orders=[trail])), cached)

    _run(fake)

    if flips:
        assert fake.cancelled == [cached] and len(fake.built) == 1
        assert "too_late_in_session" not in _rows_of(fake)
    else:
        assert fake.cancelled == [] and fake.built == []
        assert "too_late_in_session" in _rows_of(fake)


def test_a_live_exit_standoff_DEFERS_the_cancel_rather_than_baring_the_leg(monkeypatch):
    """`protection.py` said "the exit standoff does not block the re-arm" — it did, silently, on the
    placement loop. The choice pinned here: the CANCEL waits while a standoff is live, so the trail keeps
    covering the leg through the exit, and the deferral is named."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    fake._exit_suppressed["AEM.XNYS"] = _ns(10, 0) + 30 * 10**9

    _run(fake)

    assert fake.cancelled == [] and fake.built == []
    assert "exit_in_flight" in _rows_of(fake)


def test_a_cancel_the_venue_never_confirms_places_NOTHING_and_says_so(monkeypatch):
    """The confirm-wait returning False means SEND NOTHING (the exit path's rule, reused). A floor fired
    on an unconfirmed release is rejected `available: 0` and burns a coid — #245's shape."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    http = _Http(positions=[_position()], orders=[trail], confirms_cancels=False)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=http), cached)

    _run(fake)

    assert fake.cancelled == [cached]
    assert fake.built == [] and fake.submitted == []
    assert "cancel_unconfirmed" in _rows_of(fake)


def test_THE_RACE_a_pass_entering_between_the_confirm_and_the_submit_acts_on_nothing(monkeypatch):
    """The coordinator's livelock: the reconciler re-covering the leg the moment the cancel frees it,
    ahead of the floor. The flip runs INSIDE one pass and the re-entrancy guard makes a second pass a
    no-op — pinned by firing one from inside the confirm-wait. Exactly one floor, no second stop."""
    import asyncio

    from api.engine_node import UiFeedStrategy

    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    real_wait = fake._await_cancel_confirmed
    nested: list[tuple[bool, int, int]] = []
    asked: list[tuple[str, str, str]] = []

    async def racing_wait(instrument_id, coid, venue_id, timeout_s=None):
        asked.append((instrument_id, coid, venue_id))
        before = len(fake.submitted)
        await UiFeedStrategy._reconcile_protection(fake)   # the second pass, mid-window
        nested.append((fake._protection_running, before, len(fake.submitted)))
        return await real_wait(instrument_id, coid, venue_id, timeout_s)

    fake._await_cancel_confirmed = racing_wait

    asyncio.run(UiFeedStrategy._reconcile_protection(fake))

    assert nested == [(True, 0, 0)], "the nested pass ran while the guard was down, or it submitted"
    # THE WAIT ASKS ABOUT THIS LEG'S OWN ORDER (review: a wait for any order, or for none, was green).
    assert asked == [("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", "V-1")]
    assert [b["trigger_price"] for b in fake.built] == [_EXPECTED_FLOOR]
    assert len(fake.submitted) == 1


def test_the_docstrings_no_longer_describe_the_window_that_was_removed():
    """A comment that reads as safety and is wrong is the most dangerous kind (CLAUDE.md). Three claims
    in `protection.py` were false at d6133bf: "next pass", "one leg at a time", and "the exit standoff
    does not block the re-arm". The value and the sentence change in one commit."""
    import inspect

    from api.protection import WrongMode

    doc = inspect.getdoc(WrongMode) or ""
    assert "the floor is planned on the NEXT pass" not in doc
    assert "standoff does not block" not in doc
    assert "same pass" in doc.lower() and "confirm" in doc.lower()


def test_a_floor_the_planner_would_REFUSE_is_pre_flighted_and_the_trail_is_KEPT(monkeypatch):
    """Cancel first and plan second cancels a working trail for a floor that then cannot be placed —
    and the second plan's refusal was never even recorded (`clear_kind` and the refusal loop run before
    the cancel loop). The floor is pre-flighted with the trail hypothetically gone; a leg that would not
    become an intent keeps its trail and the refusal is named with the planner's own reason."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    fake._last_price_for = lambda iid: 90.0   # entry 104 - 7.5 = 96.50 is ABOVE the market

    _run(fake)

    assert fake.cancelled == [] and fake.built == []
    rows = _rows_of(fake)
    assert "floor_unplaceable" in rows and "floor_below_market" in rows


def test_a_trail_the_cache_holds_TERMINAL_is_cancelled_at_the_VENUE_and_the_floor_still_lands_this_pass(monkeypatch):
    """The #807 corpse through the whole flip: Nautilus cannot cancel a closed order, so the venue
    route does, the venue confirms, and the same-pass floor follows. The route the cache cannot see is
    the one most likely to be left half-done."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane(venue_id="V-CORPSE")
    http = _Http(positions=[_position()], orders=[trail])
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=http),
                        _CachedOrder(trail["client_order_id"], _AEM_LANE, is_open=False))

    _run(fake)

    assert fake.cancelled == []
    assert http.cancelled_at_venue == ["V-CORPSE"]
    assert [b["trigger_price"] for b in fake.built] == [_EXPECTED_FLOOR]


def test_two_lanes_on_ONE_instrument_flip_one_at_a_time_and_the_other_trail_keeps_counting(monkeypatch):
    """The cap is GLOBAL — one naked leg across the book per pass — and the re-plan after the cancel
    drops the CANCELLED row only. A body that dropped every order on the instrument would plan both
    lanes' floors at once while the second trail still rests (review), and the second floor would be
    refused at the venue on reserved shares."""
    lane_a, lane_b = _LANE_FOR["AEM"], _LANE_FOR["SSRM"]
    _settings(monkeypatch, **{f"{lane_a}_protection": {"mode": "entry_floor"},
                              f"{lane_b}_protection": {"mode": "entry_floor"}})
    older = _trail_on_floor_lane("AEM", "PROT-SELL-AEM-XNYS-a", 30, "V-A")
    newer = _trail_on_floor_lane("AEM", "PROT-SELL-AEM-XNYS-b", 24, "V-B")
    http = _Http(positions=[_position("AEM", "54")], orders=[older, newer])
    fake = _Fake(ts_ns=_ns(10, 0), http=http)
    # Two holders of AEM: the seeded BCTROT slice plus a MOMENTUM slice, summing to the broker's 54.
    from api.test_protection_reconciler import _CachePosition
    fake.cache._positions[:] = [
        _CachePosition(instrument_id="AEM.XNYS", strategy_id=lane_a, signed_qty=30.0, avg_px_open=104.0),
        _CachePosition(instrument_id="AEM.XNYS", strategy_id=lane_b, signed_qty=24.0, avg_px_open=103.0),
    ]
    _with_cached(fake, _CachedOrder("PROT-SELL-AEM-XNYS-a", lane_a))
    _with_cached(fake, _CachedOrder("PROT-SELL-AEM-XNYS-b", lane_b))

    _run(fake)

    assert [o.client_order_id for o in fake.cancelled] == ["PROT-SELL-AEM-XNYS-a"]
    assert [(b["strategy_id"], b["quantity"]) for b in fake.built] == [(lane_a, 30.0)]


def test_an_UNREADABLE_venue_during_the_wait_is_its_own_state_not_a_timeout(monkeypatch):
    """Three states. "We could not read the order book" is not "the venue did not confirm" — the first
    says nothing about the cancel, the second says it is still resting. Both place nothing; they are
    named differently so the reader knows which surface to look at."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)

    class _DiesAfterTheCancel(_Http):
        async def get_order(self, venue_order_id):
            raise RuntimeError("venue down")

    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_DiesAfterTheCancel(positions=[_position()], orders=[trail])), cached)

    _run(fake)

    assert fake.cancelled == [cached] and fake.built == []
    rows = _rows_of(fake)
    assert "venue_unreadable" in rows and "cancel_unconfirmed" not in rows


def test_a_floor_refused_AFTER_the_confirm_is_named_not_lost(monkeypatch):
    """The pre-flight ran on the pre-cancel book; the confirm-wait is seconds long; the re-plan can
    still refuse. That refusal used to have no surface at all — `clear_kind` and the refusal loop run
    ahead of the flip — so the one bare-leg state this change can create was the one it never named.
    The market moves through the floor during the wait: the pre-flight passes at 100, the re-plan
    refuses at 90."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    fake._last_price_for = lambda iid: 90.0 if fake.cancelled else 100.0

    _run(fake)

    assert fake.cancelled == [cached] and fake.built == []
    rows = _rows_of(fake)
    assert "after the flip" in rows and "floor_below_market" in rows


def test_an_exit_starting_DURING_the_wait_is_seen_by_the_placement(monkeypatch):
    """`suppressed` is sampled at the top of the pass; the wait is seconds long. An exit that starts in
    that window must still stop the floor from re-reserving the shares it is releasing — #358 restored
    through the flip. The standoff is re-sampled after the confirm."""
    from api.engine_node import UiFeedStrategy

    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    real_wait = fake._await_cancel_confirmed

    async def wait_then_exit_starts(instrument_id, coid, venue_id, timeout_s=None):
        state = await real_wait(instrument_id, coid, venue_id, timeout_s)
        # AS THE EXIT PATH DOES IT: it consults the pruner first (`engine_node.py` ~6491), which
        # REASSIGNS `_exit_suppressed` to a fresh dict, and then `_extend_standoff` writes into that
        # one. The pass's earlier `suppressed` still points at the OLD dict, so without a re-sample the
        # new standoff is invisible to the placement loop. A write into the old dict would have been
        # seen by aliasing alone — and that mutant survived the first round.
        fake._active_exit_suppressions(fake.clock.timestamp_ns())
        fake._exit_suppressed["AEM.XNYS"] = _ns(10, 0) + 30 * 10**9
        return state

    fake._await_cancel_confirmed = wait_then_exit_starts

    asyncio_run = __import__("asyncio").run
    asyncio_run(UiFeedStrategy._reconcile_protection(fake))

    assert fake.cancelled == [cached]
    assert fake.built == [], "the floor re-reserved the shares an exit had just released"
    assert "exit_in_flight" in _rows_of(fake), "a bare leg with its reason only in an info line"


def test_a_trail_that_FILLS_while_being_cancelled_gets_no_floor_this_pass_and_is_named(monkeypatch):
    """A trail is most likely to fill exactly when it is being cancelled. Gone from the open set, but the
    shares were SOLD: a floor sized off the pass's position read would rest over shares that are no
    longer held, and `_submit` names no position so `reduce_only` is never examined — the orphan
    would sit until the next oversize sweep. Named, nothing placed; the next pass sizes off the venue."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    http = _Http(positions=[_position()], orders=[trail], cancel_outcome="filled")
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=http), cached)

    _run(fake)

    assert fake.cancelled == [cached]
    assert fake.built == [] and fake.submitted == []
    rows = _rows_of(fake)
    assert "trail_filled_during_flip" in rows and "cancel_unconfirmed" not in rows


def test_a_transient_blip_during_the_wait_is_RETRIED_not_reported_as_unreadable():
    """By the time the wait runs the trail is already cancelled. A single failed poll returning
    `unreadable` would leave the leg bare for a whole tick over an HTTP hiccup (delta review). One
    failure, then a readable CANCELED: confirmed. `unreadable` is only ever "no poll could be read"."""
    import asyncio

    trail = _trail_on_floor_lane()

    class _BlipsOnce(_Http):
        calls = 0

        async def get_order(self, venue_order_id):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("blip")
            return await super().get_order(venue_order_id)

    http = _BlipsOnce(positions=[_position()], orders=[trail])
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=http), _CachedOrder(trail["client_order_id"], _AEM_LANE))
    fake._cancel(fake._orders_by_coid[trail["client_order_id"]])
    assert asyncio.run(fake._await_cancel_confirmed("AEM.XNYS", trail["client_order_id"], "V-1")) == "confirmed"
    assert _BlipsOnce.calls == 2


def test_an_ORDINARY_standoff_on_a_covered_leg_writes_no_protection_row(monkeypatch):
    """The `exit_in_flight` row is for a leg whose trail this pass cancelled. A trail-only book with an
    exit in flight must keep its EMPTY protection rows — that emptiness is the readback criterion for
    #872 and for this PR — so the plain standoff stays an info line (delta review)."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))
    fake._exit_suppressed["AEM.XNYS"] = _ns(10, 0) + 30 * 10**9

    _run(fake)

    assert fake.built == []
    assert [r for r in fake._failed_requests.as_rows() if r.get("kind") == "protection"] == []


# ---------------------------------------------------------------------------------------------
# (o) A FLIP DEFERRED FOREVER IS A NUMBER (#907). `exit_in_flight`, `floor_unplaceable` and
# `queued_behind` are each correct and each named — and `failed_requests` is cleared every pass, so a
# lane deferred a hundred times reads exactly like one deferred once: `entry_floor` in settings,
# trails forever, every surface a legitimate deferral. A livelock made of correct decisions is the
# same silence as QC345 reading TRADING for a month. The engine keeps, per (instrument, lane), how many
# CONSECUTIVE passes the leg has wanted to flip and not, since when, and why last — and publishes it.
# ---------------------------------------------------------------------------------------------

def _three_floor_lanes(monkeypatch):
    _settings(monkeypatch, **{f"{lane}_protection": {"mode": "entry_floor"} for lane in
                              (_LANE_FOR["AEM"], _LANE_FOR["SSRM"], _LANE_FOR["NOW"])})
    trails = [
        _trail_on_floor_lane("SSRM", "PROT-SELL-SSRM-XNYS-b", 10, "V-A"),
        _trail_on_floor_lane("NOW", "PROT-SELL-NOW-XNYS-c", 7, "V-B"),
        _trail_on_floor_lane("AEM", "PROT-SELL-AEM-XNYS-a", 54, "V-C"),
    ]
    http = _Http(positions=[_position("AEM", "54"), _position("SSRM", "10"), _position("NOW", "7")], orders=trails)
    fake = _Fake(ts_ns=_ns(10, 0), http=http)
    for t, sym in zip(trails, ("SSRM", "NOW", "AEM")):
        _with_cached(fake, _CachedOrder(t["client_order_id"], _LANE_FOR[sym]))
    return fake


def test_queued_legs_COUNT_their_passes_and_a_flipped_leg_leaves_the_record(monkeypatch):
    """Three legs, one flip per pass. After pass one the flipped leg has no entry and the two queued
    legs read one pass each, since pass one, reason `queued_behind`; after pass two the next flipped
    leg is gone and the last reads TWO passes with the SAME first-seen; after pass three, nothing."""
    fake = _three_floor_lanes(monkeypatch)
    t0 = _ns(10, 0)

    _run(fake)
    assert ("AEM.XNYS", _LANE_FOR["AEM"]) not in fake._flip_pending
    assert fake._flip_pending[("SSRM.XNYS", _LANE_FOR["SSRM"])] == {
        "passes": 1, "streak_started_ns": t0, "last_reason": "queued_behind"}
    assert fake._flip_pending[("NOW.XNYS", _LANE_FOR["NOW"])]["passes"] == 1

    fake.clock._ts = _ns(10, 1)
    _run(fake)
    assert ("NOW.XNYS", _LANE_FOR["NOW"]) not in fake._flip_pending
    assert fake._flip_pending[("SSRM.XNYS", _LANE_FOR["SSRM"])] == {
        "passes": 2, "streak_started_ns": t0, "last_reason": "queued_behind"}

    fake.clock._ts = _ns(10, 2)
    _run(fake)
    assert fake._flip_pending == {}


def test_a_leg_refused_the_same_way_pass_after_pass_reads_its_count_and_reason(monkeypatch):
    """The entrenched case: `floor_unplaceable` on three consecutive passes is `passes: 3` with the
    planner's own reason — a number, where before there was a fresh-looking row each minute."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    fake._last_price_for = lambda iid: 90.0

    for minute in (0, 1, 2):
        fake.clock._ts = _ns(10, minute)
        _run(fake)

    assert fake.cancelled == []
    assert fake._flip_pending[("AEM.XNYS", _AEM_LANE)] == {
        "passes": 3, "streak_started_ns": _ns(10, 0), "last_reason": "floor_unplaceable:floor_below_market"}


def test_a_leg_that_stops_wanting_to_flip_leaves_the_record(monkeypatch):
    """The trail is gone (cancelled by another route, or the lane flipped back to trail): the leg is no
    longer in `wrong_mode`, so its entry is dropped rather than frozen at its last count."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    http = _Http(positions=[_position()], orders=[trail])
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=http), cached)
    fake._last_price_for = lambda iid: 90.0
    _run(fake)
    assert fake._flip_pending[("AEM.XNYS", _AEM_LANE)]["passes"] == 1

    http._orders.clear()   # the trail is gone by another route
    fake.clock._ts = _ns(10, 1)
    _run(fake)
    assert fake._flip_pending == {}


def test_a_cancel_ISSUED_but_not_confirmed_keeps_the_leg_in_the_record(monkeypatch):
    """The bare leg. `cancel_unconfirmed` / `trail_filled_during_flip` / `venue_unreadable` all leave the
    first leg cancelled and NOT flipped; a record that dropped the key when the cancel was SENT rather
    than CONFIRMED would read "flip done" on exactly the leg that is naked (review, HIGH)."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    http = _Http(positions=[_position()], orders=[trail], confirms_cancels=False)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=http), cached)

    _run(fake)

    assert fake.cancelled == [cached]
    assert fake._flip_pending[("AEM.XNYS", _AEM_LANE)] == {
        "passes": 1, "streak_started_ns": _ns(10, 0), "last_reason": "cancel_unconfirmed"}


def test_two_lanes_on_one_instrument_keep_SEPARATE_counts_and_the_prune_spares_the_sibling(monkeypatch):
    """The lane is part of the key for the reason #748 split the rows: an instrument-only prune that
    dropped every AEM entry when lane A flipped would erase lane B's count while B still waits."""
    lane_a, lane_b = _LANE_FOR["AEM"], _LANE_FOR["SSRM"]
    _settings(monkeypatch, **{f"{lane_a}_protection": {"mode": "entry_floor"},
                              f"{lane_b}_protection": {"mode": "entry_floor"}})
    a = _trail_on_floor_lane("AEM", "PROT-SELL-AEM-XNYS-a", 30, "V-A")
    b = _trail_on_floor_lane("AEM", "PROT-SELL-AEM-XNYS-b", 24, "V-B")
    http = _Http(positions=[_position("AEM", "54")], orders=[a, b])
    fake = _Fake(ts_ns=_ns(10, 0), http=http)
    from api.test_protection_reconciler import _CachePosition
    fake.cache._positions[:] = [
        _CachePosition(instrument_id="AEM.XNYS", strategy_id=lane_a, signed_qty=30.0, avg_px_open=104.0),
        _CachePosition(instrument_id="AEM.XNYS", strategy_id=lane_b, signed_qty=24.0, avg_px_open=103.0),
    ]
    _with_cached(fake, _CachedOrder("PROT-SELL-AEM-XNYS-a", lane_a))
    _with_cached(fake, _CachedOrder("PROT-SELL-AEM-XNYS-b", lane_b))

    _run(fake)
    assert ("AEM.XNYS", lane_a) not in fake._flip_pending
    assert fake._flip_pending[("AEM.XNYS", lane_b)]["passes"] == 1

    fake.clock._ts = _ns(10, 1)
    _run(fake)
    assert fake._flip_pending == {}


def test_last_reason_FOLLOWS_a_changed_refusal(monkeypatch):
    """Pass one refused by the planner, pass two by a live exit: the count carries on, the reason is
    the current one, the first-seen is the first."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    fake._last_price_for = lambda iid: 90.0
    _run(fake)
    fake._last_price_for = lambda iid: 100.0
    fake._exit_suppressed["AEM.XNYS"] = _ns(10, 1) + 30 * 10**9
    fake.clock._ts = _ns(10, 1)
    _run(fake)

    assert fake.cancelled == []
    assert fake._flip_pending[("AEM.XNYS", _AEM_LANE)] == {
        "passes": 2, "streak_started_ns": _ns(10, 0), "last_reason": "exit_in_flight"}


def test_a_pass_that_does_not_EVALUATE_the_book_clears_the_record(monkeypatch):
    """Outside RTH the flip block does not run — and neither does it on an unreadable settings file, a
    disabled flag or a missing broker. A count frozen at `passes: 3` for the 17.5 h between sessions
    describes a book nobody evaluated (review, HIGH). Every pass that returns early leaves the record
    empty; a session boundary breaks consecutiveness anyway."""
    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], _AEM_LANE)
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    fake._last_price_for = lambda iid: 90.0
    _run(fake)
    assert fake._flip_pending[("AEM.XNYS", _AEM_LANE)]["passes"] == 1

    fake.clock._ts = _ns(16, 30)   # after the close: the pass returns above the flip block
    _run(fake)
    assert fake._flip_pending == {}
    # THREE STATES ON THE FRAME (impl review): a pass that never evaluated the book publishes None,
    # so `[]` can only mean "evaluated, nothing deferred". Same shape as the DTO's None, one level in.
    from api.engine_node import UiFeedStrategy
    assert UiFeedStrategy._flip_pending_rows(fake) is None

    fake.clock._ts = _ns(10, 5)
    fake._last_price_for = lambda iid: 100.0   # the floor is placeable now: the leg flips and leaves
    _run(fake)
    assert UiFeedStrategy._flip_pending_rows(fake) == []


def test_PRODUCTION_initialises_the_record_not_only_the_double():
    """The double already declares `_flip_pending`, so every test here is green if production's
    `__init__` never gets the line — and production then raises AttributeError on the first wrong-mode
    pass (review, HIGH). The precedent for this family of guard is the fake's own comment block."""
    import ast
    import inspect

    from api.engine_node import UiFeedStrategy

    tree = ast.parse(inspect.getsource(UiFeedStrategy.__init__).lstrip() if False else
                     "\n".join(line.removeprefix("    ")
                               for line in inspect.getsource(UiFeedStrategy.__init__).splitlines()))
    assigned = {
        t.attr for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))
        for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(t, ast.Attribute)
    }
    assert "_flip_pending" in assigned, "UiFeedStrategy.__init__ does not create _flip_pending"


def test_the_engine_PUBLISHES_flip_pending_rows_on_the_health_frame():
    """HOP 1 of the #438 chain, VALUE and key. The key alone is satisfied by a hardcoded `[]`; the raw
    dict has tuple keys and would raise inside `json.dumps`, killing the whole frame (review, HIGH).
    So: the frame's value is a call to `_flip_pending_rows`, and that method's output is checked."""
    import ast
    import json
    import pathlib

    from api.engine_node import UiFeedStrategy

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
            if "engine_ok" in keys and "armed_lanes" in keys:
                assert "flip_pending" in keys, "the health frame does not carry flip_pending"
                value = node.values[keys.index("flip_pending")]
                assert (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                        and value.func.attr == "_flip_pending_rows"), "flip_pending is not the rows method"
                break
    else:
        raise AssertionError("health frame not found — this test is blind")

    fake = _Fake(ts_ns=_ns(10, 0), http=_Http())
    fake._flip_evaluated = True
    fake._flip_pending = {
        ("NOW.XNYS", "QC345-003"): {"passes": 2, "streak_started_ns": 5, "last_reason": "queued_behind"},
        ("AEM.XNYS", "BCTROT-004"): {"passes": 1, "streak_started_ns": 7, "last_reason": "exit_in_flight"},
    }
    rows = UiFeedStrategy._flip_pending_rows(fake)
    json.dumps(rows)
    assert rows == [
        {"instrument_id": "AEM.XNYS", "strategy_id": "BCTROT-004", "passes": 1, "streak_started_ns": 7,
         "last_reason": "exit_in_flight"},
        {"instrument_id": "NOW.XNYS", "strategy_id": "QC345-003", "passes": 2, "streak_started_ns": 5,
         "last_reason": "queued_behind"},
    ]
    fake._flip_pending = {}
    fake._flip_evaluated = True
    assert UiFeedStrategy._flip_pending_rows(fake) == []
    fake._flip_evaluated = False
    assert UiFeedStrategy._flip_pending_rows(fake) is None, "an unevaluated book must not read as 'nothing pending'"


def test_HOP_2_flip_pending_crosses_the_process_split_and_is_None_without_a_bridge():
    """The consumer copies the frame KEY BY KEY, and its own comment records seven fields that died
    exactly here (review, HIGH). Live frame → the rows; no frame → None, never []."""
    import time

    from api.consumer import RedisConsumer
    from api.feed_config import load_feed_config

    rows = [{"instrument_id": "NOW.XNYS", "strategy_id": "QC345-003", "passes": 2, "streak_started_ns": 5,
             "last_reason": "queued_behind"}]
    c = RedisConsumer(load_feed_config())
    c._health = {"engine_ok": True, "armed_lanes": {}, "flip_pending": rows}
    c._health_at = time.monotonic()
    assert c.health()["flip_pending"] == rows

    stale = RedisConsumer(load_feed_config())
    assert stale.health()["flip_pending"] is None


def test_HOPS_3_and_4_the_DTO_declares_it_and_the_lane_row_summarises_it_three_stated():
    """The DTO must declare it (an undeclared key is silently dropped — #233/#322/#336) and the lane row
    must carry the MAX over the lane's legs, `0` when the frame says none, `None` when the frame could
    not be read — the collapse of unreadable into 0 is the one this ticket exists to prevent."""
    import ast
    import pathlib
    from datetime import date

    from api.models import HealthResponse
    from api.test_cadence_on_strategies import _drive_strategies

    assert "flip_pending" in HealthResponse.model_fields, "HealthResponse drops flip_pending"
    src = (pathlib.Path(__file__).parent / "app.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "HealthResponse"]
    assert calls and all(any(k.arg == "flip_pending" for k in c.keywords) for c in calls), (
        "the /health endpoint builds HealthResponse without flip_pending")

    rows = [
        {"instrument_id": "MU.XNAS", "strategy_id": "QC345-003", "passes": 2, "streak_started_ns": 5, "last_reason": "queued_behind"},
        {"instrument_id": "STX.XNAS", "strategy_id": "QC345-003", "passes": 7, "streak_started_ns": 5, "last_reason": "queued_behind"},
    ]
    with_rows = _drive_strategies(mp := __import__("pytest").MonkeyPatch(), today=date(2026, 9, 11), decided={},
                                  health={"armed_lanes": {}, "next_fire_ns": {}, "flip_pending": rows})
    mp.undo()
    assert with_rows["QC345-003"]["flip_pending_passes"] == 7
    assert with_rows["MOMENTUM-002"]["flip_pending_passes"] == 0

    empty = _drive_strategies(mp := __import__("pytest").MonkeyPatch(), today=date(2026, 9, 11), decided={},
                              health={"armed_lanes": {}, "next_fire_ns": {}, "flip_pending": []})
    mp.undo()
    assert empty["QC345-003"]["flip_pending_passes"] == 0

    unread = _drive_strategies(mp := __import__("pytest").MonkeyPatch(), today=date(2026, 9, 11), decided={},
                               health={"armed_lanes": {}, "next_fire_ns": {}})
    mp.undo()
    assert unread["QC345-003"]["flip_pending_passes"] is None


# ---------------------------------------------------------------------------------------------
# (l) THE LANE'S OWN DECLARATION REACHES THE PASS WITH NO SETTINGS KEY AT ALL (#1029).
# ---------------------------------------------------------------------------------------------

def _smhgld_book(fake: _Fake) -> _Fake:
    """SMH and GLD held by SMHGLD-007 alone, at the venue and in the cache — the book paper will hold
    at 15:36Z on 2026-09-14, one pass after the lane's first fill. TWO legs, deliberately: a per-row
    mutant that opts out only the first row is invisible with one."""
    from api.test_protection_reconciler import _CachePosition
    fake.cache._positions[:] = [
        _CachePosition(instrument_id="SMH.XNYS", strategy_id="SMHGLD-007", signed_qty=11.0, avg_px_open=104.0),
        _CachePosition(instrument_id="GLD.XNYS", strategy_id="SMHGLD-007", signed_qty=33.0, avg_px_open=103.0),
    ]
    return fake


def _smhgld_venue() -> _Http:
    return _Http(positions=[_position("SMH", "11"), _position("GLD", "33", mv="3300.0")], orders=[])


def _refusal_rows(fake: _Fake) -> list[tuple[str, str, str | None, str | None]]:
    """(subject, refusal, lane, source) per failed-request row — the ROW, not a substring across all
    rows. The note is what the operator reads:
        `cannot rest a stop: opted_out ($9,825 exposed) — SMHGLD-007, stance declared`
    and this parses exactly that shape, so a note that stops carrying the provenance fails here."""
    out = []
    for r in fake._failed_requests.as_rows():
        note = str(r.get("note") or "")
        head, _, prov = note.partition(" — ")
        reason = head.split("cannot rest a stop: ", 1)[1].split(" ", 1)[0] if "cannot rest a stop: " in head else head
        lane, _, stance = prov.partition(", stance ")
        out.append((str(r.get("subject")), reason, lane or None, stance or None))
    return sorted(out)


def test_FIXTURE_the_smhgld_book_reaches_the_dispatch_when_the_lane_is_on_trail(monkeypatch):
    """FIXTURE PROPERTY FIRST. The same book with the lane EXPLICITLY on trail must build a stop per
    leg; otherwise the test below would pass on a fixture that never reaches the planner, and
    "nothing submitted" would be measuring an unreachable dispatch rather than an opted-out lane."""
    _settings(monkeypatch, **{"SMHGLD-007_protection": {"mode": "trail"}})
    fake = _smhgld_book(_Fake(ts_ns=_ns(10, 0), http=_smhgld_venue()))

    _run(fake)

    assert sorted((b["strategy_id"], b["instrument_id"], b["quantity"]) for b in fake.built) == [
        ("SMHGLD-007", "GLD.XNYS", 33.0), ("SMHGLD-007", "SMH.XNYS", 11.0)]


def test_smhgld_with_NO_settings_key_rests_NOTHING_and_the_pass_says_OPTED_OUT_per_leg(monkeypatch):
    """THE DEFECT AT THE SEAM. Both tenants' files carry no lane key. On aed1373 this pass built a
    trailing stop on SMH for SMHGLD-007 — the lane whose module declared `PROTECTION_MODE = "none"`.
    The refusal must be standing state (`opted_out` on failed_requests), ONE ROW PER LEG, not
    silence: neither covered nor naked, and never an absence that reads as healthy."""
    _settings(monkeypatch)                      # exactly `{"enabled": true, <domain widths>}` — no lane key
    fake = _smhgld_book(_Fake(ts_ns=_ns(10, 0), http=_smhgld_venue()))

    _run(fake)

    assert fake.built == [] and fake.submitted == [], (
        f"a stop was built for the lane that declares none: {fake.built}")
    assert _refusal_rows(fake) == [("GLD.XNYS", "opted_out", "SMHGLD-007", "declared"),
                                   ("SMH.XNYS", "opted_out", "SMHGLD-007", "declared")]
    assert any("SMHGLD-007, stance declared" in w for w in fake.log.warnings)


def test_smhgld_through_the_REAL_resolve_and_declared_reads_of_a_silent_tenant_file(monkeypatch, tmp_path):
    """The production path, not the harness's monkeypatch: `api.settings.resolve` fills every schema
    default (so the resolved dict SAYS none for SMHGLD-007) and `api.settings.declared` reads the raw
    file (so the plane knows the operator wrote nothing). Both real, pointed at a tmp file that is
    byte-for-byte the tenant's. This is how paper runs the pass on Monday."""
    import api.settings
    from api.settings import store
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    (tmp_path / "protection.json").write_text('{"enabled": true}')
    assert api.settings.resolve("protection")["SMHGLD-007_protection"] == {"mode": "none"}   # filled default
    assert "SMHGLD-007_protection" not in api.settings.declared("protection")                # written: nothing
    fake = _smhgld_book(_Fake(ts_ns=_ns(10, 0), http=_smhgld_venue()))

    _run(fake)

    assert fake.built == [] and fake.submitted == []
    assert _refusal_rows(fake) == [("GLD.XNYS", "opted_out", "SMHGLD-007", "declared"),
                                   ("SMH.XNYS", "opted_out", "SMHGLD-007", "declared")]


def test_smhgld_with_the_key_WRITTEN_renders_stance_settings_on_the_same_row(monkeypatch):
    """#965 ON A SURFACE. Same value (`none`), different fact: the operator wrote it. The row says so
    — this is the only place the two are distinguishable once the pass has run."""
    _settings(monkeypatch, **{"SMHGLD-007_protection": {"mode": "none"}})
    fake = _smhgld_book(_Fake(ts_ns=_ns(10, 0), http=_smhgld_venue()))

    _run(fake)

    assert fake.built == []
    assert _refusal_rows(fake) == [("GLD.XNYS", "opted_out", "SMHGLD-007", "settings"),
                                   ("SMH.XNYS", "opted_out", "SMHGLD-007", "settings")]


def test_a_TRAIL_lane_on_the_same_book_is_unchanged_by_the_registry_read(monkeypatch):
    """The sibling: the registry default must move only the lanes that declare none. BCTROT-004
    holding the same SMH slice with no settings key still gets its trail, exactly as before #1029."""
    _settings(monkeypatch)
    from api.test_protection_reconciler import _CachePosition
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position("SMH", "11")], orders=[]))
    fake.cache._positions[:] = [
        _CachePosition(instrument_id="SMH.XNYS", strategy_id="BCTROT-004", signed_qty=11.0, avg_px_open=104.0),
    ]

    _run(fake)

    assert [(b["strategy_id"], b["quantity"]) for b in fake.built] == [("BCTROT-004", 11.0)]
    assert _refusal_rows(fake) == []


def test_a_CRSISHORT_short_row_is_lane_unattributable_whatever_the_stance_says_TODAY(monkeypatch):
    """PINNED SO MONDAY'S ROWS ARE EXPECTED, NOT A SURPRISE. CRSISHORT-006 declares `none` in the
    registry — and it makes no difference to a SHORT row, because `attribute_rows_to_lanes` is long-only
    (protection.py: "a short row stays aggregate and keeps today's refusal") and `mode_of` is never
    asked for a lane the row was not attributed to. Measured 2026-09-12 with the key DECLARED as none:
    `cannot rest a stop: lane_unattributable ($1,000 exposed)`, once per pass. Nothing is built either
    way, so the exposure is reported and no stop is rested; the standing refusal every 60 s per short
    row from Monday 13:20Z on staging2 is the plane's long-only defect, ticketed separately, and NOT
    something this ticket's registry change can silence. When that ticket lands this test flips to
    `opted_out` — and that flip is the proof the wire reached the short side."""
    _settings(monkeypatch)
    from api.test_protection_reconciler import _CachePosition
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(
        positions=[_position("RGTI", "10", mv="-1000.0", side="short")], orders=[]))
    fake.cache._positions[:] = [
        _CachePosition(instrument_id="RGTI.XNYS", strategy_id="CRSISHORT-006", signed_qty=-10.0, avg_px_open=104.0),
    ]

    _run(fake)

    assert fake.built == [] and fake.submitted == []
    # No lane, no source: the row was never attributed, and that absence is the honest one.
    assert _refusal_rows(fake) == [("RGTI.XNYS", "lane_unattributable", None, None)]
