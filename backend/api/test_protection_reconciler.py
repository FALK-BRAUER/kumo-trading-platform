"""Gate tests for the #239 backstop reconciler — the three things that must be true before it places.

The reconciler is a method on `UiFeedStrategy`, which cannot be constructed offline (it needs a live
Nautilus node). So the unbound method is called against a double: `UiFeedStrategy._reconcile_protection(fake)`.
That exercises the REAL method body — a hand-rolled copy in the test would be a drifted double of exactly
the kind that has already shipped four defects here.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from api.engine_node import _PROTECTION_PENDING_NS, UiFeedStrategy
from api.failed_requests import FailedRequests

_ET = ZoneInfo("America/New_York")


def _ns(hour: int, minute: int, day_offset: int = 0) -> int:
    base = datetime(2026, 8, 5, hour, minute, tzinfo=_ET) + timedelta(days=day_offset)
    return int(base.astimezone(UTC).timestamp() * 1e9)


class _Clock:
    def __init__(self, ts_ns: int) -> None:
        self._ts = ts_ns

    def timestamp_ns(self) -> int:
        return self._ts


#: What Alpaca's `status=open` filter ACTUALLY returns. MEASURED, not assumed: `scripts/probe_held_orders.py`
#: read the paper account on 2026-08-20 and found 269 orders, of which the `open` filter returned 9 —
#: and TWO resting protective stops it did not (CRAK 180 @ 57.06, APA 231 @ 43.99), both `held`.
#: `held` being absent from this set is the entire subject of #387.
_ALPACA_OPEN_FILTER = {
    "new", "accepted", "accepted_for_bidding", "pending_new", "partially_filled",
    "pending_cancel", "pending_replace", "done_for_day",
}


class _Http:
    """Records whether the broker was read at all — a gate that lets execution reach the fetch has failed
    even if it then places nothing.

    HONOURS `status`, because the previous version did not and that is why #387's first suite could only
    scan source text (review finding 7). A double that returns the same rows for `open` and `all` cannot
    represent the one venue behaviour at issue — that `all` returns rows `open` does not — so no test
    written against it could distinguish the bug from the fix. CLAUDE.md: a double that cannot represent
    production IS the bug; fix the double, never loosen production to accommodate it.
    """

    def __init__(self, positions=None, orders=None, *, confirms_cancels: bool = True,
                 cancel_outcome: str = "canceled") -> None:
        self._positions = positions or []
        self._orders = orders or []
        #: What the venue reports the order AS once the cancel is confirmed (#898 review): `canceled`,
        #: or `filled` — a trail fills exactly when it is being cancelled, and a confirm predicate that
        #: reads "gone from the open set" cannot tell the two apart.
        self._cancel_outcome = cancel_outcome
        #: Whether a cancel FREES the shares (#898). Alpaca frees them when it CONFIRMS a cancel, and
        #: `_venue_shares_available` reads that as the row leaving the open set. `False` models a venue
        #: that accepts the request and never confirms — the case the confirm-wait exists for.
        self._confirms_cancels = confirms_cancels
        self.read = False
        #: Venue ids this double was asked to cancel over REST — the route taken when the cache cannot
        #: act on an order (a #807 corpse), which is otherwise indistinguishable from no cancel at all.
        self.cancelled_at_venue: list[str] = []
        #: Every (status, paginate) pair this double was asked for — lets a test assert HOW the broker
        #: was read, not merely that it was.
        self.order_queries: list[tuple[str, bool]] = []

    async def list_positions(self):
        self.read = True
        return self._positions

    async def cancel_order(self, venue_order_id):
        self.cancelled_at_venue.append(str(venue_order_id))
        self.confirm_cancel(venue_id=str(venue_order_id))

    def confirm_cancel(self, *, venue_id: str = "", coid: str = "") -> None:
        """The venue confirming: the row leaves the open set, so its reservation is released."""
        if not self._confirms_cancels:
            return
        for row in self._orders:
            if (venue_id and str(row.get("id")) == venue_id) or (coid and str(row.get("client_order_id")) == coid):
                row["status"] = self._cancel_outcome
                if self._cancel_outcome == "filled":
                    row["filled_qty"] = row.get("qty")

    async def get_order(self, venue_order_id):
        """`GET /v2/orders/{id}` — one order by Alpaca's id, as the REST confirm read uses it."""
        self.read = True
        for row in self._orders:
            if str(row.get("id")) == str(venue_order_id):
                return dict(row)
        raise RuntimeError(f"no such order {venue_order_id}")

    async def list_orders(self, status="all", limit=500, paginate=False):
        self.read = True
        self.order_queries.append((status, paginate))
        if status == "open":
            return [o for o in self._orders if str(o.get("status") or "") in _ALPACA_OPEN_FILTER]
        if status == "closed":
            return [o for o in self._orders if str(o.get("status") or "") not in _ALPACA_OPEN_FILTER]
        return list(self._orders)


class _Log:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, msg): self.warnings.append(str(msg))
    def info(self, msg): pass
    def error(self, msg): self.errors.append(str(msg))
    def exception(self, msg, ex):
        """Nautilus's CYTHON signature is `exception(str message, ex)` with `Condition.not_none(ex)`.

        A double that accepted one argument hid a live defect: FIVE `self.log.exception(...)` calls in
        the backstop's own error paths passed only a message, so an unreadable settings file or an
        unreachable broker would raise `TypeError` from INSIDE the error handler instead of logging and
        returning. Every test passed. Fix the double, never loosen production.
        """
        if ex is None:
            raise TypeError("Logger.exception requires the exception — Condition.not_none(ex, 'ex')")
        self.warnings.append(f"EXC {msg}")


#: Which lane holds which symbol in these doubles. Distinct per symbol on purpose — see the seeding.
_LANE_FOR = {"AEM": "BCTROT-004", "SSRM": "MOMENTUM-002", "NOW": "QC345-003", "AYA": "TECHIVOL-005"}

#: THE LANE'S OWN ENTRY, per symbol and DISTINCT FROM THE MARK (#872). `_last_price_for` answers 100.0
#: and `_protection_atr` 5.0 everywhere, so an entry of 100 would make `entry - 1.5 x ATR` and any
#: mark-relative mutant produce the same trigger — agreement is exactly when a severed wire is
#: invisible. Each is below 107.5, so `entry - 7.5` stays under the last price and a floor is placeable.
_ENTRY_FOR = {"AEM": 104.0, "SSRM": 103.0, "NOW": 102.0, "AYA": 101.0}


class _CachePosition:
    """A Nautilus `Position` as `stamp_for` reads it: instrument, owning lane, signed quantity.

    `is_open` is a real attribute rather than implied — `stamp_for` REFUSES on a position that cannot
    say whether it is open, deliberately, because a shape that cannot answer is not a confirmation.

    `avg_px_open` is here for the same reason (#872): production's every open Position carries one, and
    it is what an entry floor is measured from. A double without it would make `lane_entries` return an
    empty map and every floor test pass as a `no_lane_entry` refusal.
    """

    def __init__(self, instrument_id: str, strategy_id: str, signed_qty: float, is_open: bool = True,
                 avg_px_open: float = 100.0):
        self.instrument_id = instrument_id
        self.strategy_id = strategy_id
        self.signed_qty = signed_qty
        self.quantity = abs(signed_qty)
        self.is_open = is_open
        self.avg_px_open = avg_px_open


class _Cache:
    """Nautilus's `Cache`, reduced to the two reads the divergence detector makes.

    `orders_open()` and `orders()` are DIFFERENT sets in production and the detector needs both: the open
    set answers "what does the engine think is resting", and the full set is where a stop stuck in a
    terminal state can still be found so the log can name it. A double that returned the same list for
    both could not represent the defect — an order is in the divergence precisely because it is in one
    and not the other.
    """

    def __init__(self) -> None:
        self._open: list = []
        self._all: list = []
        #: PRODUCTION CARRIES THIS and the dispatch now reads it: a protective stop is stamped with
        #: the lane that holds the instrument, resolved from the open position book (#748). Empty
        #: means "no unambiguous holder", which is today's behaviour — so these tests keep asserting
        #: what they always did, and would fail loudly rather than silently if the read were dropped.
        self._positions: list = []

    def orders_open(self) -> list:
        return list(self._open)

    def orders(self) -> list:
        return list(self._all)

    def positions_open(self) -> list:
        return list(self._positions)


class _Instrument:
    """An equity instrument as the order-building path reads it: a tick, and a quantiser (#872).

    REAL Nautilus `Price` objects, not floats. `_tick_away_from_market` divides by `price_increment`
    and hands the result to `make_price`, and `_canon_price` then compares the two — a double using
    floats could not represent the precision boundary that comparison exists to police. A penny tick,
    which is what every US equity this book holds actually has.
    """

    def __init__(self, tick: str = "0.01", precision: int = 2) -> None:
        from nautilus_trader.model.objects import Price

        self.price_increment = Price.from_str(tick)
        self.price_precision = precision

    def make_price(self, value: float):
        from nautilus_trader.model.objects import Price

        return Price(float(value), self.price_precision)

    def make_qty(self, value: float):
        from nautilus_trader.model.objects import Quantity

        return Quantity.from_str(str(float(value)))


class _Fake:
    """Only the attributes `_reconcile_protection` actually touches."""

    def __init__(self, *, ts_ns: int, http: _Http | None) -> None:
        self.clock = _Clock(ts_ns)
        self._http = http
        self.log = _Log()
        self._seen_orders: set[str] = set()
        # Production carries this; a double missing it would fail for the wrong reason.
        self._protection_pending: dict[tuple[str, str], int] = {}
        self._protection_attempts: dict[tuple[str, str], int] = {}
        self._protection_running: bool = False
        #: Production carries this (#757): a refusal to rest a stop is a REQUEST THAT FAILED, and it
        #: is recorded as standing state rather than only logged. A double without it fails on the
        #: attribute instead of on the behaviour under test.
        self._failed_requests = FailedRequests()
        #: Production carries the observation registry; the cancel pass runs through it so a failure
        #: is standing state rather than a silently skipped tick.
        from api.observation import Observations

        self._observations = Observations()
        self._orders_by_coid: dict[str, object] = {}
        #: The divergence detector's output (#269). Production initialises it in `__init__`; a double
        #: without it would fail on the attribute rather than on the behaviour under test.
        self._protection_divergence: list[dict] = []
        #: Production carries this too (#358). An exit that is mid-flight has DELIBERATELY released its
        #: shares, so the reconciler must not re-arm and reserve them again — a double without it would
        #: fail on the attribute rather than on the behaviour under test.
        self._exit_suppressed: dict[str, int] = {}
        #: Production carries this (#907): how many consecutive passes each wrong-mode leg has wanted
        #: to flip and not. A double without it fails on the attribute rather than on the behaviour.
        self._flip_pending: dict[tuple[str, str], dict] = {}
        self._flip_evaluated: bool = False
        #: Production is a Nautilus `Strategy`, which ALWAYS has a cache — the detector compares what the
        #: broker reports against what the cache holds, and a double with no cache cannot express the
        #: disagreement that is the whole point of the check.
        self.cache = _Cache()
        # SEEDED FROM THE BROKER ROWS, because production's cache knows who holds what.
        #
        # These doubles used to leave the position book EMPTY and still expect a stop to be armed —
        # which passed only because the dispatch fell back to the submitting strategy when it could
        # not resolve an owner. That fallback is the mint (#748): the stop goes out stamped
        # MANUAL-001, its fill resolves to a position that never existed, the ExecEngine rejects it,
        # and the sale never reaches the cache.
        #
        # So an empty book here was a double that could not represent production, asserting the
        # defect was correct behaviour. A test that wants the EMPTY case now says so explicitly —
        # see the boot-race tests, where empty is the subject rather than the setup.
        for _row in (getattr(http, "_positions", None) or []):
            # A DISTINCT OWNER PER SYMBOL. Seeding every row with one constant means a mutant that
            # hardcodes that constant passes — "test a knob with a value the default could not
            # produce". The lane is derived from the symbol so each instrument's stamp is its own.
            self.cache._positions.append(_CachePosition(
                instrument_id=f"{_row['symbol']}.XNYS",
                strategy_id=_LANE_FOR.get(_row["symbol"], "BCTROT-004"),
                signed_qty=float(_row.get("qty") or 0),
                avg_px_open=_ENTRY_FOR.get(_row["symbol"], 105.0),
            ))
        self.modified: list[tuple] = []
        #: Every (order, price, trigger_price) the reconciler sent — so "quantity only, never a new
        #: trigger" is an assertion about what was SENT rather than about what the code looks like.
        self.repriced: list[tuple] = []
        self.cancelled: list = []
        self.submitted: list = []
        self.built: list[dict] = []

    # The REAL inner body, bound to this double. `_reconcile_protection` is now only the re-entrancy
    # guard, so the work happens in `_reconcile_protection_inner`; a hand-written stand-in for it would be
    # exactly the drifted double this file exists to avoid.
    _reconcile_protection_inner = UiFeedStrategy._reconcile_protection_inner
    # The reconciler sweeps closed release windows before anything else (#546 part 2); a
    # double without it cannot represent the method under test.
    _sweep_exit_windows = UiFeedStrategy._sweep_exit_windows
    _broker_state_unknown = UiFeedStrategy._broker_state_unknown
    # The REAL typed venue read, bound like the body above. The reconciler now prefers
    # `generate_order_status_reports` and falls back to the REST read when there is no execution client.
    # These doubles have none — `_exec_client` is None — so they exercise the FALLBACK, which is what
    # keeps this file's existing fixtures meaningful. A hand-written stub here could not represent the
    # None-means-unreadable contract the real accessor implements, and stubbing it to return [] would
    # silently switch every test in this file onto the typed path with an empty venue.
    _venue_order_reports = UiFeedStrategy._venue_order_reports
    # The typed POSITION accessor too (#641): the reconciler now reads positions typed-first and only
    # falls back to `list_positions` when there is no execution client. With `_exec_client = None`
    # these doubles exercise the REST fallback, which is the path their `_Http` fixtures were written
    # against — stubbing this to return [] would silently switch every test here onto the typed path
    # with an empty (flat) venue.
    _venue_position_reports = UiFeedStrategy._venue_position_reports
    # Production is a `UiFeedStrategy` and ALWAYS carries this (#758); a double that binds only
    # some methods raises AttributeError from the protection tick instead of failing on the
    # behaviour under test. It early-returns here because the double has no ledgers to record into.
    _record_book_truth = UiFeedStrategy._record_book_truth
    # Production always carries this (#748): resting stops that can never protect are cancelled on
    # every tick. A double binding only some methods raises from the tick instead of failing on the
    # behaviour under test.
    _cancel_unprotectable_stops = UiFeedStrategy._cancel_unprotectable_stops
    # Production is a `UiFeedStrategy` and always carries this (#762 — the protective coid now
    # includes the trader id so two instances cannot mint the same one). The real method
    # returns "" when the double has no trader_id, which still differs from a real one.
    _trader_id_str = UiFeedStrategy._trader_id_str
    _exec_client = None
    # The REAL pruner as well, for the same reason. Its expiry rule is what makes the standoff
    # temporary rather than a permanently naked position, and a hand-written copy here would be free to
    # not expire — which is precisely the mutation that escaped the first version of #358's suite.
    _active_exit_suppressions = UiFeedStrategy._active_exit_suppressions
    _EXIT_SUPPRESSION_S = UiFeedStrategy._EXIT_SUPPRESSION_S
    # The REAL confirm-wait and the venue reads under it (#898): a wrong-mode flip places its floor in
    # the same pass only once the venue has CONFIRMED the cancel, and a hand-written "shares are free"
    # stand-in would be free to answer True before the venue does — the exact race #245 was.
    _await_shares_available = UiFeedStrategy._await_shares_available
    _await_cancel_confirmed = UiFeedStrategy._await_cancel_confirmed
    _venue_order_status = UiFeedStrategy._venue_order_status
    _venue_shares_available = UiFeedStrategy._venue_shares_available
    _venue_net_position = UiFeedStrategy._venue_net_position
    _venue_reducing_orders = UiFeedStrategy._venue_reducing_orders
    _exec_reserves_shares = True
    #: Short, so a venue that never confirms fails the test in well under a second rather than 30 s.
    _SHARES_FREE_S = 0.4
    _FLIP_CONFIRM_S = 0.4
    # The REAL venue-cancel route and the REAL tick quantiser (#872). Both are bound rather than
    # stubbed for the reason every other binding here gives: a hand-written stand-in is free to round
    # to the nearest tick, or to swallow a missing client, and neither failure would show.
    _cancel_at_venue = UiFeedStrategy._cancel_at_venue
    _tick_away_from_market = UiFeedStrategy._tick_away_from_market
    _cache_status_of = UiFeedStrategy._cache_status_of

    def _build_order(self, payload: dict):
        self.built.append(payload)
        return object()

    def _submit(self, order):
        self.submitted.append(order)

    def _lookup_order(self, coid):
        return self._orders_by_coid.get(coid)

    def modify_order(self, order, quantity=None, price=None, trigger_price=None):
        """Mirrors Nautilus's CYTHON-TYPED signature: `modify_order(Order order, Quantity quantity=None,
        Price price=None, Price trigger_price=None)`.

        A double that accepted a plain float could not represent production — passing one raises TypeError
        at the real boundary, so the whole shrink path would have been dead on arrival while every test
        passed. Fix the double, never loosen production.

        `price`/`trigger_price` are ACCEPTED AND RECORDED (#872) rather than omitted, so a test can
        assert that a resting entry floor is resized and never RE-PRICED. A double that could not take a
        trigger would fail on a TypeError instead of on the behaviour, which reads as the fix.
        """
        from nautilus_trader.model.objects import Quantity

        if not isinstance(quantity, Quantity):
            raise TypeError(f"modify_order requires a Quantity, got {type(quantity).__name__}")
        self.modified.append((order, float(quantity)))
        self.repriced.append((order, price, trigger_price))

    def _cancel(self, order):
        self.cancelled.append(order)
        # Through Nautilus in production, and the venue then confirms — modelled here as the broker row
        # leaving the open set, which is what `_venue_shares_available` reads (#898).
        if self._http is not None:
            self._http.confirm_cancel(coid=str(getattr(order, "client_order_id", "")))

    def _make_qty(self, instrument, qty):
        from nautilus_trader.model.objects import Quantity

        return Quantity.from_str(str(float(qty)))

    def _instrument_or_raise(self, iid):
        return _Instrument()

    def _resolve_instrument_id(self, symbol: str):
        return f"{symbol}.XNYS"

    def _protection_atr(self, instrument_id: str, lookback: int):
        return 5.0

    def _last_price_for(self, instrument_id: str):
        return 100.0


def _run(fake) -> None:
    asyncio.run(UiFeedStrategy._reconcile_protection(fake))


def _settings(monkeypatch, **over):
    """The resolved domain AND the raw file, from one dict: what these doubles hand-build IS what the
    operator wrote (#1029). A lane key passed here is therefore an operator override (`source ==
    "settings"`); a lane key NOT passed is the tenant's silence, and the reconciler resolves it to the
    lane's own registry stance — the shape paper carries on Monday."""
    values = {"enabled": True, "atrMultiple": 1.5, "atrLookback": 14,
              "minTrailPct": 1.0, "maxTrailPct": 15.0, **over}
    import api.settings
    monkeypatch.setattr(api.settings, "resolve", lambda domain: values)
    monkeypatch.setattr(api.settings, "declared", lambda domain: values)
    return values


def _position(symbol="AEM", qty="54", mv="9825.0", side="long"):
    return {"symbol": symbol, "qty": qty, "market_value": mv, "side": side}


def test_disabled_by_default_places_nothing_and_does_not_even_read_the_broker(monkeypatch):
    """The opt-in rule. Every new automation gate defaults False — the three-overnight-bug rule."""
    _settings(monkeypatch, enabled=False)
    http = _Http(positions=[_position()])
    fake = _Fake(ts_ns=_ns(10, 0), http=http)

    _run(fake)

    assert fake.submitted == []
    assert http.read is False


def test_outside_RTH_places_nothing(monkeypatch):
    """An Alpaca trailing stop does NOT trigger outside regular hours. Resting one after the close creates
    an order that protects nothing while making the book look covered — worse than a visible gap."""
    _settings(monkeypatch)
    http = _Http(positions=[_position()])
    fake = _Fake(ts_ns=_ns(18, 30), http=http)   # 18:30 ET, after the close

    _run(fake)

    assert fake.submitted == []
    # Reading state outside hours is intended (#289) — SECURED needs the venue's own stop price, which
    # exists nowhere else. What must not happen is PLACING.
    assert http.read is True


def test_with_no_broker_connection_it_refuses_rather_than_falling_back_to_the_cache(monkeypatch):
    """#285: the engine has held orders as REJECTED that Alpaca reported open. Cache truth is not a
    substitute for broker truth — a cache check answers 'nothing resting' and the next tick duplicates."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=None)

    _run(fake)

    assert fake.submitted == []


def test_a_naked_position_in_hours_with_the_flag_on_gets_a_trailing_stop(monkeypatch):
    """The positive case — without it every gate test above passes on a reconciler that never places."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))

    _run(fake)

    assert len(fake.submitted) == 1
    payload = fake.built[0]
    assert payload["instrument_id"] == "AEM.XNYS"
    assert payload["side"] == "SELL"
    assert payload["quantity"] == 54.0
    assert payload["order_type"] == "trailing_stop"
    assert payload["trail_bps"] == 750          # 5/100 = 5% ATR x 1.5
    assert payload["time_in_force"] == "gtc"    # must outlive the session — a DAY stop expires at close


def test_a_position_already_covered_at_the_BROKER_is_left_alone(monkeypatch):
    """The duplicate-stop guard, read from broker state rather than from our own order cache."""
    _settings(monkeypatch)
    resting = {"symbol": "AEM", "side": "sell", "qty": 54, "order_type": "trailing_stop",
               "status": "new", "filled_qty": 0}
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[resting]))

    _run(fake)

    assert fake.submitted == []


def test_a_take_profit_limit_at_the_broker_does_NOT_suppress_the_stop(monkeypatch):
    """The live shape on 2026-08-14: every resting order on the book was a limit sell above the market.

    A limit is not coverage, so the position stays in the audit as unprotected — but the shares it RESERVES
    cannot carry a stop either (#287). With a take-profit for 54 of 54 held, nothing is placeable and the
    honest outcome is a reported refusal, not the `403 insufficient qty available` the first version earned.

    Held 54 with a take-profit for only 20 leaves 34 free, and those 34 must still get a stop — a partly
    reserved position must not be abandoned entirely.
    """
    _settings(monkeypatch)
    tp = {"symbol": "AEM", "side": "sell", "qty": 20, "order_type": "limit",
          "status": "new", "filled_qty": 0}
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[tp]))

    _run(fake)

    assert len(fake.submitted) == 1
    assert fake.built[0]["quantity"] == 34.0


def test_an_unmappable_symbol_is_LOGGED_not_silently_skipped(monkeypatch):
    """Real exposure we cannot map. A position missing from the audit is one the audit calls protected."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position(symbol="WEIRD")]))
    fake._resolve_instrument_id = lambda s: None

    _run(fake)

    assert fake.submitted == []
    assert any("unmappable" in w and "WEIRD" in w for w in fake.log.warnings)


def test_a_refusal_is_logged_with_its_exposed_notional(monkeypatch):
    """A ceiling clamp refuses to place. It must still SAY so, with the number — that is the whole point of
    reporting refusals rather than skipping them."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake._protection_atr = lambda iid, lookback: 11.18   # 16.8% at 1.5x, past the 15% ceiling

    _run(fake)

    assert fake.submitted == []
    assert any("ceiling" in w for w in fake.log.warnings)


def test_it_does_not_place_the_same_stop_twice_within_one_process(monkeypatch):
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))

    _run(fake)
    _run(fake)

    assert len(fake.submitted) == 1


def test_settings_MISSING_the_enabled_key_entirely_still_places_nothing(monkeypatch):
    """The default is the safety property, and every other test here set `enabled` explicitly — so flipping
    `cfg.get("enabled", False)` to `True` passed the whole file. Found by mutation, not by review.

    A settings file written before this domain existed, a partial migration, or a hand-edited values file
    all produce a dict with no `enabled` key. The standing rule is that new automation is opt-in, and a
    default that no test exercises is a default that can be flipped by accident.
    """
    import api.settings
    monkeypatch.setattr(api.settings, "resolve", lambda domain: {"atrMultiple": 1.5, "atrLookback": 14})
    http = _Http(positions=[_position()])
    fake = _Fake(ts_ns=_ns(10, 0), http=http)

    _run(fake)

    assert fake.submitted == []
    assert http.read is False


def test_unreadable_settings_place_nothing_rather_than_falling_back_to_defaults(monkeypatch):
    """Fail-closed. A settings store that raises must not be read as 'use the defaults and carry on' —
    defaults include a width, and placing orders on an unreadable config is how a misconfiguration becomes
    a live order."""
    import api.settings

    def _boom(domain):
        raise RuntimeError("values file corrupt")

    monkeypatch.setattr(api.settings, "resolve", _boom)
    http = _Http(positions=[_position()])
    fake = _Fake(ts_ns=_ns(10, 0), http=http)

    _run(fake)

    assert fake.submitted == []
    assert http.read is False


def test_a_failed_submit_does_NOT_burn_the_position_for_the_rest_of_the_process(monkeypatch):
    """The coid was added to `_seen_orders` BEFORE `_submit`. A throw there — a venue reject, a transient
    connection fault — marked the position as handled forever, so the backstop never retried it and the
    position stayed naked with no further warning.

    Failure to place now is not a decision never to place. Same distinction as #255's terminal FAILED.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    boom = True

    def _submit(order):
        if boom:
            raise RuntimeError("venue rejected")
        fake.submitted.append(order)

    fake._submit = _submit
    _run(fake)                       # first pass throws
    assert fake.submitted == []

    boom = False
    _run(fake)                       # second pass must try again
    assert len(fake.submitted) == 1


def test_the_two_opposite_legs_of_one_instrument_cannot_share_a_client_order_id():
    """Nautilus caps client order ids at 36 chars. With the side appended LAST, an instrument id of 30+
    characters truncates the side away entirely and a long leg's stop collides with a short leg's — one
    silently replacing or rejecting the other.

    Not reachable with today's `TICKER.VENUE` ids, which is exactly why it would sit unnoticed until an
    instrument id got longer. The side goes first so truncation can never reach it.
    """
    from api.engine_node import _protection_coid

    long_iid = "SOMEVERYLONGTICKERNAMEINDEED.XNAS"
    sell = _protection_coid(long_iid, "SELL")
    buy = _protection_coid(long_iid, "BUY")

    assert sell != buy
    assert len(sell) <= 36 and len(buy) <= 36
    assert _protection_coid("AEM.XNYS", "SELL").startswith("PROT-SELL-AEM-XNYS")  # legible in the blotter


def test_a_coid_marked_pending_EXPIRES_so_an_async_venue_rejection_is_retried(monkeypatch):
    """codex, Critical. `_submit` returning does not mean Alpaca accepted.

    The venue can reject asynchronously, after the coid was already recorded. A permanent marker then
    suppresses every future attempt while the broker shows no open stop — the position is naked forever and
    silent about it. `_seen_orders` cannot be a permanent blocker for a reconciler whose whole job is to
    keep retrying until coverage exists.

    The marker exists only to stop a double-submit in the window before the order appears in broker REST.
    Past that window the BROKER is the guard: if the order was accepted, coverage suppresses the intent
    anyway, so expiring the marker is safe and self-healing.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))

    _run(fake)
    assert len(fake.submitted) == 1

    # Same tick window — must NOT double-submit while the order may simply not have appeared yet.
    _run(fake)
    assert len(fake.submitted) == 1

    # Past the grace window, with the broker STILL showing no coverage: the venue rejected it. Retry.
    fake.clock = _Clock(_ns(10, 0) + _PROTECTION_PENDING_NS + 1)
    _run(fake)
    assert len(fake.submitted) == 2


def test_two_instruments_sharing_a_long_prefix_get_DIFFERENT_client_order_ids():
    """codex, High. Putting the side first fixed the long-vs-short collision but not this one: two SELL
    instruments sharing the first 26 normalized characters still truncated to the same id at 36.

    A collision means one position's stop is rejected as a duplicate, or silently replaces the other's —
    and the loser is naked while the audit sees an order resting on the instrument.
    """
    from api.engine_node import _protection_coid

    a = _protection_coid("AVERYLONGINSTRUMENTNAMEHERE1.XNAS", "SELL")
    b = _protection_coid("AVERYLONGINSTRUMENTNAMEHERE2.XNAS", "SELL")

    assert a != b
    assert len(a) <= 36 and len(b) <= 36
    # Still legible in the blotter for the ordinary case.
    assert _protection_coid("AEM.XNYS", "SELL").startswith("PROT-SELL-AEM")


def test_the_RTH_gate_holds_at_every_boundary_not_just_one_evening(monkeypatch):
    """codex, Medium: the gate was only tested at 18:30 ET, so a UTC/ET confusion or a weekend bug passed.

    A trailing stop does not trigger outside regular hours, so placing one then is theatre that makes the
    book look covered.
    """
    _settings(monkeypatch)
    closed = [
        (_ns(4, 0), "pre-market"),
        (_ns(9, 29), "one minute before the open"),
        (_ns(16, 1), "one minute after the close"),
        (_ns(20, 0), "post-market"),
        (_ns(11, 0, day_offset=-3), "Sunday"),      # 2026-08-05 is a Wednesday
        (_ns(11, 0, day_offset=-4), "Saturday"),
    ]
    for ts, label in closed:
        http = _Http(positions=[_position()])
        fake = _Fake(ts_ns=ts, http=http)
        _run(fake)
        assert fake.submitted == [], f"placed during {label}"
        # The broker IS read outside hours now (#289): a trailing stop's real trigger price lives only at
        # the venue, so SECURED went dark overnight when the read was gated too. Reading state is not
        # acting on it — what must not happen outside RTH is PLACING, and that is asserted above.
        assert http.read is True, f"stopped reading broker state during {label}"

    for ts, label in [(_ns(9, 30), "the open"), (_ns(15, 59), "one minute before the close")]:
        fake = _Fake(ts_ns=ts, http=_Http(positions=[_position()]))
        _run(fake)
        assert len(fake.submitted) == 1, f"did not place at {label}"


def test_a_covered_LONG_and_a_naked_SHORT_on_one_instrument_are_both_accounted(monkeypatch):
    """codex, Medium: mixed coverage across opposite legs of one instrument had no test.

    The short gets its BUY stop; the long is already covered. Neither may vanish from the accounting.
    """
    from api.protection import plan_protection

    positions = [
        {"instrument_id": "AEM.XNYS", "quantity": 50, "side": "LONG",
         "market_value": 5000.0, "strategy_id": "A"},
        {"instrument_id": "AEM.XNYS", "quantity": 30, "side": "SHORT",
         "market_value": -3000.0, "strategy_id": "B"},
    ]
    covering_the_long = {"symbol": "AEM", "side": "sell", "qty": 50,
                         "order_type": "trailing_stop", "status": "new", "filled_qty": 0}
    plan = plan_protection(
        positions=positions, orders=[covering_the_long],
        atr_by_symbol={"AEM.XNYS": 5.0}, price_by_symbol={"AEM.XNYS": 100.0},
    )
    assert [(i.side, i.quantity) for i in plan.intents] == [("BUY", 30.0)]
    assert plan.refusals == []
    assert "AEM.XNYS" in plan.covered_instrument_ids


def _stop_order(symbol="AEM", qty=100.0, coid="PROT-SELL-AEM-XNYS-abc12345"):
    return {"symbol": symbol, "side": "sell", "qty": qty, "order_type": "trailing_stop",
            "status": "new", "filled_qty": 0.0, "client_order_id": coid}


def test_an_oversize_stop_is_SHRUNK_in_place_not_cancelled_and_replaced(monkeypatch):
    """codex, Critical 5 / #169.

    A stop sized for more shares than are held over-liquidates when it triggers and FLIPS the position into
    opposite exposure. It must be corrected — and by PATCH, not cancel-and-replace.

    Measured 2026-08-12: a trailing stop's high-water mark SURVIVES a replace, and `qty` IS replaceable.
    Cancel-and-replace would surrender the high-water mark and open a naked window; submit-then-cancel
    would briefly rest EVEN MORE oversize coverage. Neither is acceptable when the defect is over-coverage.

    `Strategy.modify_order` is the native Nautilus path and bottoms out in the adapter's Alpaca PATCH —
    checked before hand-rolling anything, per CLAUDE.md.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(
        positions=[_position(qty="60")],
        orders=[_stop_order(qty=100.0)],
    ))
    resting = object()
    fake._orders_by_coid = {"PROT-SELL-AEM-XNYS-abc12345": resting}

    _run(fake)

    assert fake.submitted == []                       # nothing new — it is already over-covered
    assert fake.cancelled == []                       # never cancel a stop over shares still held
    assert fake.modified == [(resting, 60.0)]


def test_an_orphan_stop_on_a_CLOSED_position_is_cancelled(monkeypatch):
    """Flat with a stop still resting is the worst case: triggering it opens a naked SHORT out of nothing.
    Target quantity zero means cancel, because there is no position left to protect."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[], orders=[_stop_order(qty=100.0)]))
    resting = object()
    fake._orders_by_coid = {"PROT-SELL-AEM-XNYS-abc12345": resting}

    _run(fake)

    assert fake.modified == []
    assert fake.cancelled == [resting]


def test_an_oversize_stop_the_cache_cannot_find_is_REPORTED_not_silently_ignored(monkeypatch):
    """#285: the engine's order book disagrees with the broker, so a resting order may have no Nautilus
    Order object to modify. Failing silently would leave an over-liquidating stop in place while the
    reconciler reported success."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(
        positions=[_position(qty="60")], orders=[_stop_order(qty=100.0)],
    ))
    fake._orders_by_coid = {}                          # cache does not have it

    _run(fake)

    assert fake.modified == [] and fake.cancelled == []
    assert any("oversize" in w.lower() for w in fake.log.warnings)


def test_a_RETRY_uses_a_fresh_client_order_id_not_the_rejected_one(monkeypatch):
    """#295 — this bricked the engine on 2026-08-14, and a protective feature caused the outage.

    `_protection_coid` was deterministic, so when the in-flight marker expired the retry resubmitted under
    the SAME id as an order already REJECTED by the venue. The RiskEngine denied it as a duplicate and
    DENIED landed on a terminal order. On the next start `load_orders` replays both, the FSM refuses
    REJECTED -> DENIED, and TradingNode construction dies. The durable cache made it permanent: every
    subsequent start failed identically until the records were deleted by hand.

    The expiring marker is NOT the bug — a permanent one was an earlier Critical, because an async venue
    rejection would otherwise suppress every retry forever. The bug is retrying under an identifier that
    already carries a terminal event. A retry is a NEW order and needs a new identity.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))

    _run(fake)
    first = fake.built[0]["client_order_id"]

    # Past the in-flight window, broker still showing no coverage: the venue rejected it. Retry.
    fake.clock = _Clock(_ns(10, 0) + _PROTECTION_PENDING_NS + 1)
    _run(fake)
    second = fake.built[1]["client_order_id"]

    assert first != second, "a retry must not reuse the coid of an order that may be terminal"
    assert len(second) <= 36
    assert second.startswith("PROT-SELL-AEM")     # still legible in the blotter

    # A third attempt is distinct again — the sequence cannot collide with itself.
    fake.clock = _Clock(_ns(10, 0) + 2 * _PROTECTION_PENDING_NS + 2)
    _run(fake)
    assert len({first, second, fake.built[2]["client_order_id"]}) == 3


def test_a_coid_already_in_the_cache_is_SKIPPED_so_a_restart_cannot_reuse_a_terminal_id(monkeypatch):
    """#295, second half — the in-memory attempt counter does not survive a restart.

    `_protection_attempts` resets to 0 on boot, so the first post-restart retry regenerates attempt-0's
    coid. If that order is already REJECTED in the DURABLE cache, resubmitting under it reproduces exactly
    the REJECTED -> DENIED pair that made the engine unbootable. The counter alone fixed the within-process
    case and left the across-restart case fully live.

    So the attempt is advanced past any id the cache already knows, rather than trusted from memory. The
    cache is the durable record; process state is not.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))

    # Simulate a restart: memory is empty, but attempts 0 and 1 already exist in the durable cache.
    from api.engine_node import _protection_coid

    # LANE-STAMPED IDS, because that is what production mints now that #748's per-lane split is
    # wired. Built from unstamped ids this fixture cannot express the collision at all: the engine
    # would mint a stamped id, find it absent from the cache, and stay on attempt 0 — the test would
    # pass while proving nothing, which is the vacuity this repo keeps being bitten by.
    taken = {
        _protection_coid("AEM.XNYS", "SELL", 0, "BCTROT-004"): object(),
        _protection_coid("AEM.XNYS", "SELL", 1, "BCTROT-004"): object(),
    }
    fake._orders_by_coid = taken
    fake._protection_attempts = {}

    _run(fake)

    assert len(fake.built) == 1
    used = fake.built[0]["client_order_id"]
    assert used not in taken, "resubmitted under an id the cache already holds — the #295 Critical"
    # LANE-STAMPED, because the position book names a sole holder and #748's per-lane split is now
    # wired. The double must ask for the id production actually mints: before the split was wired
    # `intent.strategy_id` was blank and this read `_protection_coid(..., 2)`, which now names an id
    # nothing builds.
    assert used == _protection_coid("AEM.XNYS", "SELL", 2, "BCTROT-004")


def test_two_overlapping_ticks_submit_only_once_for_one_leg(monkeypatch):
    """codex, High. The reconciler does network I/O on a 60s timer, so a slow broker read can let a second
    tick start before the first has finished.

    Two ticks both submitting would rest two stops on shares that can carry one — the oversell of #245.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    calls = []

    async def slow_list_positions():
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0)      # yield, letting a second tick in
        return [_position()]

    fake._http.list_positions = slow_list_positions

    async def both():
        await asyncio.gather(
            UiFeedStrategy._reconcile_protection(fake),
            UiFeedStrategy._reconcile_protection(fake),
        )

    asyncio.run(both())
    assert len(fake.submitted) == 1


def test_nothing_awaits_between_reading_the_in_flight_marker_and_writing_it():
    """WHY the test above holds, pinned so it keeps holding.

    Concurrency here is not prevented by the `_protection_running` flag — that is defence in depth, and
    removing it changes no behaviour today. What actually makes a double submit impossible is that the
    critical section is synchronous: the marker is READ, the order is built and submitted, and the marker
    is WRITTEN with no `await` in between, so the event loop cannot interleave another tick inside it.

    That is a property of the code, not of the test, and it silently stops being true the moment someone
    adds an await there — a fill confirmation, a broker re-read, a settings lookup. Then two ticks really
    can both submit and no existing test would notice. This one would.
    """
    import inspect
    import re

    src = inspect.getsource(UiFeedStrategy._reconcile_protection_inner)
    start = src.index("pending_at = self._protection_pending.get(leg)")
    end = src.index("self._protection_pending[leg] = now_ns")
    critical = src[start:end]
    awaits = re.findall(r"\bawait\b", critical)
    assert not awaits, (
        "an await appeared between reading the in-flight marker and writing it — two ticks can now "
        "interleave inside the critical section and both submit for one leg"
    )


def test_the_attempt_counter_is_dropped_once_the_leg_is_covered(monkeypatch):
    """codex, Medium: the dict grew for every leg ever attempted and was never pruned.

    Bounded by the instrument universe rather than unbounded, so not a leak that matters operationally —
    but a leg that is now covered should start from a clean count if it ever needs protecting again,
    rather than inheriting a stale attempt number that walks the hash further each time.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    _run(fake)
    # Matched on the LEG, not on the whole key. The key gained a lane element in #748 and this test is
    # about pruning, not about the key's shape — an assertion that pins both fails for the wrong reason.
    assert any(k[:2] == ("AEM.XNYS", "SELL") for k in fake._protection_attempts)

    covered = {"symbol": "AEM", "side": "sell", "qty": 54, "order_type": "trailing_stop",
               "status": "new", "filled_qty": 0}
    fake._http = _Http(positions=[_position()], orders=[covered])
    fake.clock = _Clock(_ns(10, 5))
    _run(fake)

    assert not any(k[:2] == ("AEM.XNYS", "SELL") for k in fake._protection_attempts)


def test_THE_LIVE_PROTECTION_PATH_CARRIES_NO_LANE_so_the_748_split_is_INERT_here():
    """The gap that stops #748 Road 1, pinned so it cannot be forgotten or assumed closed.

    `plan_protection` now keys its legs by `(instrument, reducing_side, strategy_id)` and stamps each
    intent with its owner, so a protective stop can be submitted under the lane whose shares it covers
    — which is what makes a reduce-only fill resolve to a position that EXISTS instead of minting a
    phantom.

    IT CHANGES NOTHING IN PRODUCTION YET, because neither row source carries a lane. Both
    `broker_rows` and `position_rows_from_reports` set `strategy_id: ""` — deliberately, and the
    comment says why: "the broker does not know about our sleeves." So every intent on the live path
    gets the SAME empty owner, the legs re-aggregate exactly as before, and the split is invisible.

    This is the repo's own "a change that anchors on something absent does nothing, quietly" class,
    and it would have shipped GREEN: the planner tests pass because their fixtures supply a lane that
    production never supplies. A double that can represent something production cannot is the same
    defect as one that cannot represent something production can.

    WHAT ROAD 1 ACTUALLY NEEDS: a per-lane source for the split. The engine Cache has it
    (`positions_open()` carries `strategy_id`); the broker has only the net. That matches this repo's
    stated architecture — broker net is the only hard reconciliation anchor, and the per-strategy
    split is unverified by the broker — so the join must anchor the TOTAL on the broker and take the
    SPLIT from the cache, refusing where the two disagree rather than guessing.

    Until that exists, do not report #748 as fixed on the strength of the planner change.
    """
    from api.protection import broker_rows, position_rows_from_reports

    rows, _ = broker_rows(
        [{"symbol": "AEM", "qty": "54", "market_value": "9825.0", "side": "long"}],
        lambda sym: f"{sym}.XNYS",
    )
    assert rows, "fixture produced no rows — the assertion below would be vacuous"
    assert {r["strategy_id"] for r in rows} == {""}, (
        "broker_rows now carries a lane — if this fails, Road 1's blocker may be gone; re-check "
        "position_rows_from_reports too before celebrating"
    )
    src = inspect.getsource(position_rows_from_reports)
    assert '"strategy_id": ""' in src, (
        "the typed row source no longer blanks the sleeve — recheck whether the #748 split is live"
    )


def test_the_venues_stop_price_is_cached_so_SECURED_can_be_computed(monkeypatch):
    r"""#289 — SECURED read \$0.00 with eleven stops resting.

    A trailing stop carries an OFFSET, not a trigger. Alpaca derives the stop price and ratchets it up
    with its own high-water mark, and none of that reaches the order object we hold — so every protective
    order in our cache had `trigger_price: None`, `securedValue` read `trigger_price ?? price`, got null
    on all of them, and reported zero protected value. The number was unreachable from the book's real
    state, not merely wrong.

    Measured on the live book: FIG stop 25.5505 hwm 26.2057, SMH stop 553.865 hwm 589.72 — the venue has
    it, we did not.
    """
    _settings(monkeypatch)
    resting = {"symbol": "AEM", "side": "sell", "qty": 2, "order_type": "trailing_stop",
               "status": "new", "filled_qty": 0, "client_order_id": "PROT-SELL-AEM-XNYS-abc",
               "stop_price": "170.340338"}
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[resting]))

    _run(fake)

    assert fake._broker_stop_prices == {"PROT-SELL-AEM-XNYS-abc": 170.340338}


def test_an_order_WITHOUT_a_stop_price_is_not_cached_as_zero(monkeypatch):
    """A missing stop must stay missing. Caching 0.0 would make `securedValue` compute a gain against a
    stop of zero — a confidently wrong number in place of an honest blank."""
    _settings(monkeypatch)
    tp = {"symbol": "AEM", "side": "sell", "qty": 2, "order_type": "limit", "status": "new",
          "filled_qty": 0, "client_order_id": "TP-1", "stop_price": None}
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[tp]))

    _run(fake)

    assert "TP-1" not in fake._broker_stop_prices


def test_an_explicit_trigger_price_is_NOT_overwritten_by_the_venues_echo():
    """Only what is MISSING gets filled (#289).

    A bracket leg is submitted with an explicit trigger, so the order already carries the right number.
    Overwriting it with the venue's echo would replace our own value with a round-tripped copy — usually
    identical, occasionally not, and never an improvement. A trailing stop is the only case with nothing
    to preserve.
    """
    from types import SimpleNamespace

    from api.engine_node import UiFeedStrategy

    bracket_leg = SimpleNamespace(client_order_id="BR-1", trigger_price=101.5)
    trailing = SimpleNamespace(client_order_id="PROT-SELL-AEM-XNYS-abc", trigger_price=None)
    dto = SimpleNamespace(working_orders=[bracket_leg, trailing])

    fake = _Fake(ts_ns=_ns(10, 0), http=None)
    fake._broker_stop_prices = {"BR-1": 99.99, "PROT-SELL-AEM-XNYS-abc": 170.34}

    UiFeedStrategy._mark_broker_stop_prices(fake, [dto])

    assert bracket_leg.trigger_price == 101.5, "our own explicit trigger was replaced by the venue's echo"
    assert trailing.trigger_price == 170.34, "the trailing stop's venue trigger was not filled"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE DIVERGENCE DETECTOR (#269)
#
# Eight protective stops rested at Alpaca on 2026-08-17 while the engine held all eight as REJECTED —
# terminal, so reconciliation could never repair them. NOTHING NOTICED FOR A FULL DAY. The only symptom
# was an operator unable to exit a position, and by then the cause was five layers away from the button
# he pressed.
#
# Assert the residue, not the process: neither derivation is trusted, and the disagreement is the finding.
# ══════════════════════════════════════════════════════════════════════════════════════════════════


class _CacheOrder:
    """A cache order as the detector reads it. `is_closed` is what makes it invisible to `orders_open`."""

    def __init__(self, instrument_id, coid, status="ACCEPTED", side=None, otype=None, tags=None,
                 strategy_id="MANUAL-001"):
        from nautilus_trader.model.enums import OrderSide, OrderType
        from nautilus_trader.model.identifiers import ClientOrderId

        self.instrument_id = instrument_id
        # A REAL `ClientOrderId`. A plain string silently breaks the protective-stop predicate, which
        # reads `client_order_id.value` — `getattr("PROT-…", "value", "")` is `""`, so a legitimate stop
        # is classified as unidentifiable and the detector raises a false alarm on a healthy position.
        self.client_order_id = ClientOrderId(coid)
        self.side = side if side is not None else OrderSide.SELL
        self.order_type = otype if otype is not None else OrderType.TRAILING_STOP_MARKET
        self.tags = tags
        # PRODUCTION ORDERS CARRY THIS, and under NETTING it decides which position a fill belongs
        # to. A double without it cannot express a stop stamped with a lane that holds nothing —
        # which is the state 17 of 23 live protective orders were in on 2026-08-31.
        self.strategy_id = strategy_id
        self.status = type("S", (), {"name": status})()
        self.is_closed = status in {"REJECTED", "CANCELED", "EXPIRED", "FILLED", "DENIED"}


def _venue_stop(coid, symbol="AEM", venue_id="v-1", qty=54):
    """As ALPACA reports it: key is `type` (not `order_type`), side is lowercase, status is `new`."""
    return {"id": venue_id, "client_order_id": coid, "symbol": symbol, "side": "sell",
            "type": "trailing_stop", "status": "new", "qty": qty, "filled_qty": 0}


def _detector_fake(monkeypatch, *, cache_orders, venue_orders, hour=10):
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(hour, 0), http=_Http(positions=[_position()], orders=venue_orders))
    fake.cache._open = [o for o in cache_orders if not o.is_closed]
    fake.cache._all = list(cache_orders)
    return fake


def test_the_fixture_can_actually_EXPRESS_a_divergence(monkeypatch):
    """The premise. If `orders_open()` returned terminal orders — as a careless double would — the cache
    and the broker could never disagree, and every assertion below would pass with the detector deleted.
    """
    stuck = _CacheOrder("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", status="REJECTED")
    fake = _detector_fake(monkeypatch, cache_orders=[stuck],
                          venue_orders=[_venue_stop("PROT-SELL-AEM-XNYS-abc")])

    assert fake.cache.orders_open() == [], "a terminal order must not appear in the OPEN set"
    assert len(fake.cache.orders()) == 1, "and must still exist in the full set, or nothing can name it"


def test_a_stop_the_broker_holds_and_the_cache_CANNOT_SEE_is_reported(monkeypatch):
    """The live case. The engine is blind to a resting stop that is holding every share."""
    stuck = _CacheOrder("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", status="REJECTED")
    fake = _detector_fake(monkeypatch, cache_orders=[stuck],
                          venue_orders=[_venue_stop("PROT-SELL-AEM-XNYS-abc")])

    _run(fake)

    assert [d["instrument_id"] for d in fake._protection_divergence] == ["AEM.XNYS"]
    assert fake._protection_divergence[0]["coids"] == ["PROT-SELL-AEM-XNYS-abc"]
    assert any("PROTECTION DIVERGENCE" in e for e in fake.log.errors), (
        f"the divergence was not reported at ERROR: {fake.log.errors}"
    )
    assert any("REJECTED" in e for e in fake.log.errors), (
        "the log must say what the cache believed — otherwise it reads as a routine warning"
    )


def test_a_stop_BOTH_sides_can_see_is_NOT_reported(monkeypatch):
    """The complement. A detector that fires on the healthy case is one an operator learns to scroll
    past, and this runs every 60 seconds on eight positions."""
    live = _CacheOrder("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", status="ACCEPTED")
    fake = _detector_fake(monkeypatch, cache_orders=[live],
                          venue_orders=[_venue_stop("PROT-SELL-AEM-XNYS-abc")])

    _run(fake)

    assert fake._protection_divergence == [], "the healthy case raised a false alarm"


def test_a_SECOND_stuck_stop_beside_a_visible_one_is_still_caught(monkeypatch):
    """Why the comparison is per ORDER rather than per instrument.

    An instrument-level check asks "does the broker protect it, and does the cache see any stop" — and
    answers yes/yes here. But two stops rest on shares that can carry one, so every exit is still
    rejected and the position looks fine from every angle.
    """
    live = _CacheOrder("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", status="ACCEPTED")
    stuck = _CacheOrder("AEM.XNYS", "PKW-second-one", status="REJECTED")
    fake = _detector_fake(
        monkeypatch,
        cache_orders=[live, stuck],
        venue_orders=[_venue_stop("PROT-SELL-AEM-XNYS-abc"),
                      _venue_stop("PKW-second-one", venue_id="v-2")],
    )

    _run(fake)

    assert [d["coids"] for d in fake._protection_divergence] == [["PKW-second-one"]], (
        "the second, invisible stop was not reported"
    )


def test_the_detector_runs_OUTSIDE_regular_hours_too(monkeypatch):
    """Divergence does not keep market hours. A detector that slept overnight would have said nothing
    about the state we woke up to — and this state persisted across a restart and a full night."""
    stuck = _CacheOrder("AEM.XNYS", "PROT-SELL-AEM-XNYS-abc", status="REJECTED")
    fake = _detector_fake(monkeypatch, cache_orders=[stuck],
                          venue_orders=[_venue_stop("PROT-SELL-AEM-XNYS-abc")], hour=20)

    _run(fake)

    assert fake._protection_divergence, "the detector was gated behind the RTH check"


def test_a_TAKE_PROFIT_limit_is_not_reported_as_missing_protection(monkeypatch):
    """It is not protection, so its absence from the cache's protective set is not a divergence. Counting
    it would fire on every bracketed position forever."""
    fake = _detector_fake(
        monkeypatch, cache_orders=[],
        venue_orders=[{**_venue_stop("BR-tp"), "type": "limit"}],
    )

    _run(fake)

    assert fake._protection_divergence == []


def test_a_position_with_an_exit_in_flight_is_not_re_armed(monkeypatch):
    """#358. The reconciler must stand off while an exit is releasing the shares.

    Without this the fix defeats itself: `release_for_exit` cancels the protective stop so the venue
    frees the shares, and if the reconciler re-arms in that window the reservation is back and the exit
    is refused on `available: 0` a second time — now with the protection gone too. That is how FSM 933
    and VCTR 88 were refused on 2026-08-19 and the rotation could not rotate.

    Driven through the REAL reconciler body against the same naked position the positive case uses, so
    the difference between them is exactly the standoff and nothing else.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))
    fake._exit_suppressed = {"AEM.XNYS": _ns(10, 0) + 30_000_000_000}   # 30s left to run

    _run(fake)

    assert fake.submitted == [], (
        "the reconciler armed a stop while an exit was in flight — that re-reserves the shares the "
        "release just freed, and the exit is refused again with the position now unprotected"
    )


def test_the_standoff_ends_on_its_own_and_protection_comes_back(monkeypatch):
    """The other direction, and the one that keeps the standoff from becoming a naked position.

    A failed exit leaves a mark behind and no operator is watching at 13:35Z, so recovery has to be
    automatic. Same fixture, same position, only the deadline moved into the past — if this does not
    place a stop, the standoff is permanent and #358's fix is worse than the bug it replaces.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[]))
    fake._exit_suppressed = {"AEM.XNYS": _ns(10, 0) - 1}                # already expired

    _run(fake)

    assert len(fake.submitted) == 1, (
        "an EXPIRED standoff still suppressed protection — the position stays naked for as long as the "
        "engine runs, which is strictly worse than the exit failure it was meant to survive"
    )
    assert "AEM.XNYS" not in fake._exit_suppressed, "the expired mark was never pruned"


def test_a_standoff_never_causes_the_reconciler_to_CANCEL_a_healthy_stop(monkeypatch):
    """#358, the critical both reviewers reproduced independently — and the first fix caused it.

    The standoff was applied by deleting the position's row from `rows`. But `plan_protection` derives
    oversize and orphan corrections from the ORDERS, not from `positions` — deliberately, so that a stop
    left behind on a position that closed entirely is caught (protection.py:546). Removing the row makes
    `held` 0, which turns a correctly-sized resting stop into an orphan with `target_quantity` 0, and the
    reconciler CANCELS it while logging "cancelled orphan stop (position closed)" over a fully-held
    position.

    It fires on exactly the branches where `release_for_exit` fails and correctly sends no exit: the
    position keeps its protection, as the docstring promises, and then loses it on the next tick. The fix
    for #358 would have stripped protection off live positions.

    Both earlier tests here used `orders=[]`, so their fixture had no stop to cancel and could not
    express this either way — the "a test that cannot fail carries no information" shape.
    """
    _settings(monkeypatch)

    # THE FIXTURE'S OWN PROPERTY FIRST. Without the standoff this stop is healthy: it matches the
    # position exactly, so nothing is oversize and nothing is orphaned. If the baseline cancelled
    # anything, the assertion below would pass for the wrong reason.
    def _fixture():
        f = _Fake(ts_ns=_ns(10, 0), http=_Http(
            positions=[_position(qty="54")], orders=[_stop_order(qty=54.0)]))
        # THE CANCEL MUST BE REACHABLE OR THIS TEST PROVES NOTHING. `_cancel` only runs on an order the
        # cache can hand back (`resting = self._lookup_order(coid)`); without this mapping the oversize
        # branch logs "not in the cache — cannot correct it" and returns, so the fixture stays green with
        # the defect fully reintroduced. That is exactly what happened on the first attempt at this test:
        # the mutation escaped, and the assertion had been passing for the wrong reason.
        f._orders_by_coid = {"PROT-SELL-AEM-XNYS-abc12345": object()}
        return f

    # THE FIXTURE'S OWN PROPERTY FIRST. Without the standoff this stop is healthy — it matches the
    # position exactly, so nothing is oversize and nothing is orphaned.
    base = _fixture()
    _run(base)
    assert base.cancelled == [] and base.modified == [], (
        f"the baseline already touched the stop (cancelled={base.cancelled}, modified={base.modified}) — "
        f"this fixture cannot demonstrate anything about the standoff"
    )

    fake = _fixture()
    fake._exit_suppressed = {"AEM.XNYS": _ns(10, 0) + 30_000_000_000}

    _run(fake)

    assert fake.cancelled == [], (
        "the standoff made the reconciler CANCEL the protective stop it was told to leave alone — the "
        "position is now genuinely naked, caused by the mechanism meant to protect the exit"
    )
    assert fake.modified == [], "the standoff caused the stop to be resized"
    assert fake.submitted == [], "a new stop was armed during the standoff, re-reserving the shares"


def test_the_typed_venue_read_produces_the_SAME_broker_stop_prices(monkeypatch):
    """The typed source must carry the trigger price, or SECURED goes blank in the UI.

    `_broker_stop_prices` is built from the venue read and flows to the trades payload via
    `_mark_broker_stop_prices`, which is what the SECURED column displays. Dropping `stop_price` from
    the typed normaliser passed every other test in this repo — the existing coverage
    (`test_broker_stop_prices_carries_only_LIVE_triggers`) drives the RAW path only, because these
    doubles have no execution client.

    So this runs the reconciler twice over the same orders — once raw, once typed — and compares. Same
    bytes, two parses, one answer, on the field a user actually looks at.
    """
    from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    #  does a bare `raw["id"]` — the venue order id is required, and
    # `_stop_order` does not carry one because the raw path never needed it. Supplying it here rather
    # than making the parser tolerant: production always has it, and a double that omits a field
    # production requires is how four defects shipped green this week.
    # `id` and STRING quantities, because that is what Alpaca actually sends. `_stop_order` emits floats
    # and omits the venue id — the raw path survives both (it calls `float()` and never needs the id),
    # so the drift was invisible until the real parser met it: `Quantity.from_str` raises on a float and
    # `raw["id"]` on a missing key. A double that cannot represent production is the bug.
    # An explicit row in ALPACA'S SHAPE rather than `_stop_order`, which cannot serve here: it emits
    # float quantities (the real parser needs `Quantity.from_str`), carries no venue `id` (a bare
    # `raw["id"]`), spells the type as `order_type` where the parser reads `type`, and has no
    # `stop_price` at all — so the raw path would produce an empty dict and the comparison would pass
    # against nothing. The raw path tolerated every one of those, which is exactly why the drift was
    # invisible until a second reader met the same fixture.
    rows = [{
        "id": "v-stop-1",
        "client_order_id": "PROT-SELL-AEM-XNYS-a1",
        "symbol": "AEM",
        "side": "sell",
        "status": "new",
        "type": "stop",
        "order_type": "stop",
        "qty": "54",
        "filled_qty": "0",
        "stop_price": "43.99",
    }]

    class _Clk:
        def timestamp_ns(self):
            return _ns(10, 0)

    class _Lg:
        def warning(self, *a, **k):
            pass

    class _ParseHost:
        account_id = AccountId("ALPACA-TEST")
        _clock = _Clk()
        _log = _Lg()
        _symbol_to_id = {"AEM": InstrumentId.from_str("AEM.XNYS")}

        def _client_order_id_for(self, raw):
            return ClientOrderId(raw["client_order_id"])

    reports = [r for r in (AlpacaExecutionClient._parse_order_report(_ParseHost(), r) for r in rows)
               if r is not None]
    assert reports, "the fixture order did not survive the typed parse — this test would prove nothing"

    _settings(monkeypatch)
    raw_fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position(qty="54")], orders=rows))
    _run(raw_fake)

    _settings(monkeypatch)
    typed_fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position(qty="54")], orders=[]))

    class _Client:
        async def generate_order_status_reports(self, command):
            return reports

    typed_fake._exec_client = _Client()
    _run(typed_fake)

    assert raw_fake._broker_stop_prices, "the raw path produced no stop prices — fixture cannot compare"
    assert typed_fake._broker_stop_prices == raw_fake._broker_stop_prices, (
        f"the typed venue read lost the trigger price: raw={raw_fake._broker_stop_prices} "
        f"typed={typed_fake._broker_stop_prices} — SECURED would render blank in the UI"
    )


# ==================================================================================================
# THE MINT: a protective stop must not be stamped with a strategy that does not hold the shares
#
# Under NETTING the execution engine derives a fill's position as `{instrument}-{fill.strategy_id}`,
# from the ORDER's strategy_id. A stop built by the display strategy carries MANUAL-001, and on an
# instrument MANUAL-001 does not hold, its reduce-only fill resolves to a position that has never
# existed: applied to NO position, then fabricated by the ~10s poll as a synthetic sell at a price
# that never traded. That is 8 mirror pairs and 262 fabricated shares on the live paper book.
#
# These drive the REAL dispatch and read what the ORDER actually carries. The first version of this
# work had nine tests on the helper and three AST assertions on the wiring, and a mutation replacing
# the helper call with a stub still passed everything — because nothing built an order and looked at
# it. Operator: "this needs proper tests".
# ==================================================================================================
def _pos_in_cache(instrument_id: str, strategy_id: str, qty: float, is_open: bool = True):
    """A cache position as `stamp_for` reads it — the fields production's `Position` exposes."""
    return SimpleNamespace(
        instrument_id=instrument_id, strategy_id=strategy_id, signed_qty=qty, is_open=is_open,
    )


def test_the_stop_carries_THE_HOLDING_LANE_not_the_submitting_strategy(monkeypatch):
    """The defect, at the seam. AEM is held by MOMENTUM-002 alone; the stop must say so.

    If this ships blank or MANUAL-001, the fill resolves to `AEM.XNYS-MANUAL-001` — a position that
    does not exist — and the poll fabricates a short. Nothing else in the pipeline can recover it.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake.cache._positions = [_pos_in_cache("AEM.XNYS", "MOMENTUM-002", 54.0)]
    _run(fake)

    assert fake.built, "no protective order was built — the fixture does not reach the dispatch"
    payload = fake.built[0]
    assert payload["strategy_id"] == "MOMENTUM-002", (
        f"the stop is stamped {payload['strategy_id']!r}. Under NETTING its fill resolves to "
        f"{payload['instrument_id']}-{payload['strategy_id'] or 'MANUAL-001'}, which no lane holds — "
        f"the phantom mint"
    )


def test_an_instrument_TWO_LANES_hold_gets_ONE_STOP_PER_LANE(monkeypatch):
    """#748 wired (#801). Two lanes on one instrument each get their OWN stop, sized to their OWN
    holding — 54 for MOMENTUM-002 and 2 for MANUAL-001 against a broker net of 56.

    THIS TEST USED TO ASSERT THE OPPOSITE, and the reason it did is the reason the fix is safe. It
    pinned a refusal, because one AGGREGATE stop of 56 stamped MOMENTUM-002 would over-close that
    lane by 2 and FLIP IT SHORT. Per-lane stops remove the premise: nothing is stamped with a lane
    it does not fully cover, so there is no over-close to trade a bookkeeping error against.

    What the refusal cost, measured on paper 2026-09-08: CRAK, LAND, PAGP and GMAB each had one
    lane's slice covered and the other's naked — 433 shares carrying no stop for as long as both
    lanes held the name, because the refusal is permanent, not a one-tick wait.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position(qty="56")]))
    fake.cache._positions = [
        _pos_in_cache("AEM.XNYS", "MOMENTUM-002", 54.0),
        _pos_in_cache("AEM.XNYS", "MANUAL-001", 2.0),
    ]
    _run(fake)

    by_lane = {b["strategy_id"]: b for b in fake.built}
    assert set(by_lane) == {"MOMENTUM-002", "MANUAL-001"}, (
        f"expected one stop per lane, got {[(b['strategy_id'], b['quantity']) for b in fake.built]}"
    )
    assert by_lane["MOMENTUM-002"]["quantity"] == 54.0
    assert by_lane["MANUAL-001"]["quantity"] == 2.0
    assert sum(b["quantity"] for b in fake.built) == 56.0, (
        "the lanes' stops must sum to the broker net — more is an oversell that flips a lane short, "
        "less leaves shares naked"
    )
    # DISTINCT IDS. `_protection_coid` mixes the lane in, and until this was wired every lane on a
    # leg minted the SAME id — the second submit would be refused as a duplicate and that lane would
    # go naked while the audit saw an order resting on the instrument.
    coids = [b["client_order_id"] for b in fake.built]
    assert len(set(coids)) == 2, f"two lanes minted the same client_order_id: {coids}"


def test_a_split_that_DOES_NOT_RECONCILE_to_the_broker_is_still_refused(monkeypatch):
    """The other half of the three states, and the one that keeps the fix honest.

    THE BROKER NET IS THE ONLY HARD ANCHOR (ADR 0001). A cache split is an unverified claim, so it
    may only be acted on when it sums to what the broker actually holds. Here the cache says 54 + 2
    while the broker holds 60 — the 4-share difference belongs to nobody the cache can name.

    Splitting anyway would rest stops for 56 against 60 held AND attribute them to lanes whose
    quantities are already known to be wrong. The aggregate row survives, `stamp_for` sees two
    holders, and the instrument is refused exactly as before.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position(qty="60")]))
    fake.cache._positions = [
        _pos_in_cache("AEM.XNYS", "MOMENTUM-002", 54.0),
        _pos_in_cache("AEM.XNYS", "MANUAL-001", 2.0),
    ]
    _run(fake)

    assert not fake.built, (
        f"an unreconciled split was acted on: "
        f"{[(b['strategy_id'], b['quantity']) for b in fake.built]}"
    )
    assert not fake.submitted, "an unreconciled split reached the venue"
    rows = fake._failed_requests.as_rows()
    assert any(r["kind"] == "protection" and "AEM" in r["subject"] for r in rows), (
        "the refusal left no standing state — a stop that is silently not placed is the naked "
        "position nobody can see"
    )
    note = " ".join(str(r.get("note", "")) for r in rows)
    assert "MOMENTUM-002" in note and "MANUAL-001" in note, (
        f"the refusal does not name the lanes that made it ambiguous, so the cause still needs a "
        f"position query to establish: {note!r}"
    )


def test_a_FLAT_SIBLING_does_not_stop_the_stamp(monkeypatch):
    """The durable cache retains CLOSED and flat positions, so nearly every instrument shows a
    sibling lane at zero. If those counted as holders the stamp would refuse almost everywhere and
    the mint would survive — the change doing nothing while appearing to work."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake.cache._positions = [
        _pos_in_cache("AEM.XNYS", "MOMENTUM-002", 54.0),
        _pos_in_cache("AEM.XNYS", "BCTROT-004", 0.0),
        _pos_in_cache("AEM.XNYS", "QC345-003", 40.0, is_open=False),
    ]
    _run(fake)
    assert fake.built[0]["strategy_id"] == "MOMENTUM-002"


def test_an_EXTERNAL_phantom_leg_does_not_stop_the_stamp(monkeypatch):
    """EXTERNAL is the record that attribution FAILED, not a lane. Counting it as a holder would make
    every phantom-carrying instrument ambiguous — and those are exactly the ones already damaged, so
    the fix would skip the book it is most needed on."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake.cache._positions = [
        _pos_in_cache("AEM.XNYS", "MOMENTUM-002", 54.0),
        _pos_in_cache("AEM.XNYS", "EXTERNAL", -14.0),
    ]
    _run(fake)
    assert fake.built[0]["strategy_id"] == "MOMENTUM-002"


def test_an_EMPTY_position_cache_leaves_the_stop_UNSTAMPED_not_crashed(monkeypatch):
    """A seeding or stale frame is an empty book, and it must degrade to today's behaviour rather
    than raising inside the 60s protection tick — an attribution question must never become a
    protection outage."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake.cache._positions = []
    _run(fake)

    # THE BOOT RACE, and this is the scenario that produced the whole family.
    #
    # Measured 2026-08-31: the engine booted at 21:35:37; at 21:35:58 protection armed AEM and SSRM
    # and both went out stamped MANUAL-001; sixty-one seconds later, at 21:36:59, the SAME code
    # stamped VCTR, AYA and NOW correctly. Nothing about those instruments differed — the position
    # cache had simply not been reconciled yet at the first tick.
    #
    # An empty book is not "no unambiguous holder". It is "we cannot see yet", and arming on it
    # submits a stop whose fill lands on a position that never existed. So it is refused, and it
    # names the reason so the next occurrence does not need an evening of forensics.
    assert not fake.built, "a stop was built against a position book that had not loaded"
    assert not fake.submitted
    rows = fake._failed_requests.as_rows()
    assert any(r["kind"] == "protection" for r in rows), "the refusal left no standing state"
    assert any("cache at all" in str(r.get("note", "")) for r in rows), (
        f"the refusal does not distinguish an UNLOADED book from an ambiguous one — the two need "
        f"different responses: {[r.get('note') for r in rows]}"
    )


def test_the_refusal_RECOVERS_once_the_position_book_loads(monkeypatch):
    """THE QUESTION THAT DECIDES WHETHER REFUSING IS SAFE.

    If a refused stop is never retried, refusing on an unloaded book leaves every position naked for
    the life of the process — strictly worse than the mint it prevents. The refusal is only correct
    if the NEXT tick arms it once the cache is populated.

    Two ticks on one `_Fake`, so process-level state (`_protection_attempts`, `_protection_pending`,
    `_seen_orders`) carries between them exactly as it does in production. A guard that suppressed
    the retry would be invisible to a single-tick test.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))

    fake.cache._positions = []                       # boot: reconciliation has not run
    _run(fake)
    assert not fake.submitted, "armed against an unloaded book"

    fake.cache._positions = [_pos_in_cache("AEM.XNYS", "BCTROT-004", 54.0)]   # reconciliation lands
    _run(fake)

    assert fake.submitted, (
        "the stop was refused at boot and NEVER retried — the position stays naked for the life of "
        "the process, which is worse than the phantom the refusal prevents"
    )
    assert fake.built[-1]["strategy_id"] == "BCTROT-004", (
        f"recovered but stamped {fake.built[-1]['strategy_id']!r}"
    )


def test_a_SHORT_position_is_left_UNSTAMPED_at_the_seam(monkeypatch):
    """The live case tonight, and the only one this cut deliberately declines.

    `RDN.XNYS` is SHORT 72 under MANUAL-001 on staging, where protection was armed hours before the
    open. A short's protection reduces by BUYING, and this cut does not reason about side — so the
    stamp must decline rather than aim a BUY at a position by guess.

    The helper covers this; nothing drove it through the dispatch, and a mutation removing the
    long-only check survived the whole behavioural file. That is the class this file exists for: a
    guard proven on the unit and unproven where it runs.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position(qty="-72", side="short")]))
    fake.cache._positions = [_pos_in_cache("AEM.XNYS", "MANUAL-001", -72.0)]
    _run(fake)

    if not fake.built:
        return  # a short may plan no SELL leg at all; the point is that nothing is mis-stamped
    assert not fake.built[0]["strategy_id"], (
        f"a SHORT position's protective order was stamped {fake.built[0]['strategy_id']!r}. Its "
        f"reducing side is BUY, and this cut does not reason about side — aiming it at a lane is a "
        f"guess about which position a BUY closes"
    )


# ==================================================================================================
# Cancelling stops that can never protect anything (#748) — the 17 resting on paper
# ==================================================================================================
def test_a_stop_stamped_with_a_lane_that_HOLDS_NOTHING_is_CANCELLED(monkeypatch):
    """The state left on paper by the fallback that was just removed.

    Measured 2026-08-31: 17 of 23 live protective orders carried MANUAL-001 on instruments MANUAL-001
    holds nothing of. Refusing to place NEW ones does not touch those, and they cannot be replaced
    either — coverage is judged per INSTRUMENT, so a resting wrong stop permanently blocks a correct
    one. Each one that triggers repeats the damage in full: shares leave the broker, the fill is
    rejected, the sale never reaches the cache, the lane's budget never returns.

    Cancelling costs nothing real, because that order could never have protected anything: every fill
    it produces is rejected. The correct stop follows on the next 60s tick.
    """
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake.cache._positions = [_pos_in_cache("AEM.XNYS", "BCTROT-004", 54.0)]
    stale = _CacheOrder("AEM.XNYS", "PROT-SELL-AEM-XNYS-old", strategy_id="MANUAL-001")
    fake.cache._open = [stale]
    fake.cache._all = [stale]

    _run(fake)

    assert stale in fake.cancelled, (
        "a protective stop stamped with a lane holding nothing was left resting — it cannot protect, "
        "it blocks the correct stop, and its fill corrupts the book"
    )


def test_a_CORRECTLY_stamped_stop_is_never_cancelled(monkeypatch):
    """The direction that would be catastrophic to get wrong: churning real protection off a live
    book. A reconciler that cancels a working stop is worse than the defect it fixes."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake.cache._positions = [_pos_in_cache("AEM.XNYS", "BCTROT-004", 54.0)]
    good = _CacheOrder("AEM.XNYS", "PROT-SELL-AEM-XNYS-ok", strategy_id="BCTROT-004")
    fake.cache._open = [good]
    fake.cache._all = [good]

    _run(fake)

    assert good not in fake.cancelled, "a correctly stamped, resting protective stop was cancelled"


def test_an_UNREADABLE_position_book_cancels_NOTHING(monkeypatch):
    """THE GUARD THAT MATTERS MOST. With no positions visible every resting stop looks orphaned, so a
    reconciler that trusted an empty book would cancel ALL protection on a book it merely could not
    see. Empty IS the failure being guarded against, so empty can never authorise a cancel."""
    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake.cache._positions = []
    stale = _CacheOrder("AEM.XNYS", "PROT-SELL-AEM-XNYS-old", strategy_id="MANUAL-001")
    fake.cache._open = [stale]
    fake.cache._all = [stale]

    _run(fake)

    assert not fake.cancelled, "an unreadable position book authorised cancels"


def test_a_lane_ALREADY_COVERED_gets_NO_SECOND_STOP_while_its_naked_neighbour_gets_a_full_one(monkeypatch):
    """The `lane_of` half of #748, which shipped with NOTHING exercising it.

    Measured 2026-09-10: cutting `lane_of=_lane_of` out of the `plan_protection` call left the suite
    at 3784 passed — byte-identical to the wired tree. Two derivations of one fact that agree when
    one of them is severed is a dead mechanism, so this test exists to make them disagree.

    THE FIXTURE IS THE HAZARD, not a generic two-lane book. AEM: broker holds 64, the cache splits it
    32 MOMENTUM-002 / 32 MANUAL-001 (so the split RECONCILES and attribution is allowed to act), and
    ONE stop for 32 already rests, owned by MOMENTUM-002. That lane is whole; MANUAL-001 is naked.

    Pro-rata cannot see that. It divides 32 of coverage across 64 held, calls each lane half-covered,
    and rests 16 MORE for the lane that was already complete: MOMENTUM-002 ends at 48 of coverage
    against 32 held, which on trigger sells shares that do not exist and FLIPS the lane short — while
    MANUAL-001 is still 16 naked. That is strictly worse than doing nothing, and it is what this
    assertion forbids.

    Per-lane coverage answers both: nothing for MOMENTUM-002, a full 32 for MANUAL-001.
    """
    _settings(monkeypatch)
    resting = _stop_order(qty=32.0, coid="PROT-SELL-AEM-XNYS-momentum1")
    fake = _Fake(ts_ns=_ns(10, 0),
                 http=_Http(positions=[_position(qty="64")], orders=[resting]))
    fake.cache._positions = [
        _pos_in_cache("AEM.XNYS", "MOMENTUM-002", 32.0),
        _pos_in_cache("AEM.XNYS", "MANUAL-001", 32.0),
    ]
    # The resting stop's lane lives ONLY here — a venue report cannot carry it, which is why
    # `_lane_of` goes through the cache. Without this the leg is unattributable and the code falls
    # back to pro-rata BY DESIGN, so the fixture would not be able to express the bug at all.
    fake._orders_by_coid = {
        "PROT-SELL-AEM-XNYS-momentum1": SimpleNamespace(strategy_id="MOMENTUM-002"),
    }

    # --- THE FIXTURE'S OWN PROPERTIES FIRST -------------------------------------------------------
    # A test whose fixture cannot violate the invariant passes for free. Assert the three conditions
    # that make the defect REACHABLE before asserting it does not happen.
    from api.protection import lane_quantities
    split = lane_quantities(fake.cache._positions)[("AEM.XNYS", "LONG")]
    assert sum(split.values()) == 64.0, (
        "fixture is inert: the split must reconcile to the broker net or attribution refuses to act "
        "and this test would be pinning the refusal, not the coverage"
    )
    assert set(split) == {"MOMENTUM-002", "MANUAL-001"} and len(split) == 2, (
        "fixture is inert: one holder cannot expose a per-lane vs pro-rata disagreement"
    )
    assert fake._lookup_order("PROT-SELL-AEM-XNYS-momentum1").strategy_id == "MOMENTUM-002", (
        "fixture is inert: an unattributable resting stop makes the leg fall back to pro-rata by "
        "design, so the wired and unwired paths would agree and the test could not fail"
    )

    _run(fake)

    by_lane = {b["strategy_id"]: b for b in fake.built}
    assert "MOMENTUM-002" not in by_lane, (
        f"a lane already resting 32 on 32 held was given ANOTHER stop of "
        f"{by_lane.get('MOMENTUM-002', {}).get('quantity')} — 48 of coverage against 32 held "
        f"over-liquidates on trigger and flips the lane short"
    )
    assert set(by_lane) == {"MANUAL-001"}, (
        f"expected exactly one stop, for the naked lane, got "
        f"{[(b['strategy_id'], b['quantity']) for b in fake.built]}"
    )
    assert by_lane["MANUAL-001"]["quantity"] == 32.0, (
        f"the naked lane must be covered in FULL, not pro-rata's 16: "
        f"{by_lane['MANUAL-001']['quantity']}"
    )
