"""The engine's order cache and the broker disagree, and every exit path believed the cache (#269).

THE DEFECT, as it happened on 2026-08-17.

Eight protective trailing stops rested at Alpaca, reserving every share of all eight positions. The
engine held all eight as REJECTED. `REJECTED` is TERMINAL in Nautilus, so the reconciler's `OrderAccepted`
— read from the venue every few seconds — was discarded every single time:

    InvalidStateTrigger: REJECTED -> ACCEPTED, did not apply OrderAccepted(
        instrument_id=WPM.XNYS, client_order_id=PROT-SELL-WPM-XNYS-b7f41735, ...)

81,128 of those between 2026-08-16 20:02 and 2026-08-17 14:41. The orders were absent from
`cache.orders_open()` for the entire time while holding the shares at the broker.

Consequence, in the operator's words: "none of this works. cannot get out and it does not auto sell or
stop."

    13:33  PK-519d9490…  rejected  insufficient qty available (requested: 16, available: 0)
    13:38  PK-4aff907c…  rejected  insufficient qty available (requested: 16, available: 0)
    13:42  FL-79c244f8…  rejected  insufficient qty available (requested: 29, available: 0)   ← FLATTEN
    13:42  FL-af03e10d…  rejected  insufficient qty available (requested: 29, available: 0)   ← FLATTEN
    13:44  PK-2f5f95b0…  rejected  insufficient qty available (requested: 29, available: 0)

WHY NINE PREVIOUS FIXES DID NOT PREVENT IT. The cancel-then-confirm-then-close sequence was already
written, already reviewed, already tested — and it never executed. `flatten.decide()` was handed
`resting_reducing_qty=0` from the cache, so `cancel_resting_first` was never set. Every prior fix
improved the logic ON TOP OF the cache; the cache was the thing that was wrong.

#285 had already found this exact divergence and fixed only the DISPLAY (`_broker_protected`, the SECURED
badge). The order paths kept asking the cache. One fact, derived two ways, one of them wrong.

So these tests drive the SEAMS — what the exit paths call — and every fixture asserts its own premise
first: a fixture where the cache and the broker agree cannot fail either way, and would pass with the bug
fully reintroduced.

Run with: PYTHONPATH=~/projects/kumo-strategies/src:.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import pytest
from nautilus_trader.model.enums import OrderSide, OrderType
from nautilus_trader.model.identifiers import ClientOrderId

from api.engine_node import UiFeedStrategy, _side_name

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Doubles built from what production ACTUALLY emits
# ══════════════════════════════════════════════════════════════════════════════════════════════════


class _Order:
    """A Nautilus order as the exit paths read it, with a REAL `ClientOrderId`.

    `status` is carried so a test can build the thing that broke us: an order the cache holds as REJECTED.
    `orders_open()` must then EXCLUDE it, exactly as Nautilus does — a double that returned terminal
    orders from the open set could not represent the defect at all.
    """

    def __init__(self, instrument_id, coid, *, status="ACCEPTED", side=OrderSide.SELL,
                 order_type=OrderType.TRAILING_STOP_MARKET, qty=29.0, tags=None):
        self.instrument_id = instrument_id
        self.strategy_id = "MANUAL-001"
        self.side = side
        self.order_type = order_type
        self.client_order_id = ClientOrderId(coid)
        self.leaves_qty = qty
        self.tags = tags
        self.status = type("S", (), {"name": status})()
        self.is_closed = status in {"REJECTED", "CANCELED", "EXPIRED", "FILLED", "DENIED"}


class _Cache:
    """Nautilus's cache. `orders_open()` filters terminal orders out — that IS the defect's mechanism."""

    def __init__(self, orders):
        self._orders = list(orders)

    def orders(self):
        return list(self._orders)

    def orders_open(self):
        return [o for o in self._orders if not o.is_closed]

    def order(self, coid):
        return next((o for o in self._orders if str(o.client_order_id) == str(coid)), None)


class _Broker:
    """Alpaca. Holds the orders the venue actually has, and frees shares only on a CONFIRMED cancel."""

    def __init__(self, rows, held=29.0, *, symbol="NBIS", readable=True):
        self.rows, self.held, self.symbol, self.readable = list(rows), held, symbol, readable
        self.cancelled: list[str] = []

    async def list_orders(self, status: str = "all", limit: int = 500, paginate: bool = False) -> list[dict]:
        """MIRRORS PRODUCTION'S SIGNATURE, including `paginate` (#387).

        A double whose signature is narrower than production's does not fail where the drift is. The
        caller's `except Exception` swallows the `TypeError` and the test sees "could not read broker
        orders — assuming nothing is UNSAFE", which is a LEGITIMATE production state with its own
        branch. So the failure surfaces as an unrelated assertion about a missing log line, and 33
        tests across this file and `test_peak.py` failed pointing at the wrong thing.

        That is the mild version. The dangerous version is a test whose expectation happens to match
        the unreadable-broker branch: it would have stayed green and asserted nothing at all. Keep
        this signature identical to `AlpacaHttpClient.list_orders`. Fix the double, never loosen
        production.
        """
        if not self.readable:
            raise RuntimeError("alpaca unreachable")
        return [r for r in self.rows if r["id"] not in self.cancelled]

    async def list_positions(self):
        if not self.readable:
            raise RuntimeError("alpaca unreachable")
        reserved = sum(float(r["qty"]) for r in await self.list_orders())
        return [{"symbol": self.symbol, "qty": str(self.held),
                 "qty_available": str(max(0.0, self.held - reserved))}]

    async def cancel_order(self, venue_order_id):
        if not self.readable:
            raise RuntimeError("alpaca unreachable")
        self.cancelled.append(str(venue_order_id))


def _row(coid, venue_id, *, symbol="NBIS", qty=29.0, side="sell", otype="trailing_stop"):
    """One order as Alpaca reports it — lowercase side, string-ish numbers, `status: new`."""
    return {"id": venue_id, "client_order_id": coid, "symbol": symbol, "side": side,
            "type": otype, "status": "new", "qty": qty, "filled_qty": 0}


class _Log:
    """Nautilus's `Logger`, whose contract differs from the standard library's in the one way that bites.

        cpdef void exception(self, str message, ex)      # component.pyx:1564
            Condition.not_none(ex, "ex")

    A stdlib logger accepts `exception(msg, ex)` and then tries `msg % (ex,)`, which raises
    `TypeError: not all arguments converted`. So neither shape is a safe stand-in for the other, and
    picking the wrong one hides a real defect in whichever direction you get it wrong: FIVE production
    call sites passed only a message, which would have raised `TypeError` from inside the error handler
    the first time the broker was unreachable.

    Forwards to a real stdlib logger so `caplog` still sees the text.
    """

    def __init__(self):
        self._out = logging.getLogger("test.divergence")

    def warning(self, msg):
        self._out.warning("%s", msg)

    def error(self, msg):
        self._out.error("%s", msg)

    def info(self, msg):
        self._out.info("%s", msg)

    def exception(self, msg, ex):
        if ex is None:
            raise TypeError("Logger.exception requires the exception — Condition.not_none(ex, 'ex')")
        self._out.error("%s", msg)


class _Engine:
    """`self` for the real methods under test. Plain Python, so `cache` is an ordinary attribute — the
    real class is Cython and its slot is unwritable."""


    # A cache that answers NOTHING, which is the honest shape for these fixtures: the venue sweep runs
    # on orders the cache has written off, so blindness is the case under test. Attribution then falls
    # through to the `_OURS` prefix inside the REAL `_owner_of`, exactly as it does in production.
    class _BlindCache:
        def client_order_id(self, venue_order_id):
            return None

        def order(self, client_order_id):
            return None

    # THE REAL `_owner_of`, not a stub — delegated rather than assigned, because `UiFeedStrategy` is
    # imported inside the test functions here, not at module scope. Production's own method means the
    # prefix fallback AND the cache lookup are exercised, rather than replaced by a value the double
    # invents for itself.
    def _owner_of(self, coid, venue_id):
        from api.engine_node import UiFeedStrategy

        return UiFeedStrategy._owner_of(self, coid, venue_id)

    def __init__(self, cache_orders, broker, *, native_cancel_confirms=True):
        self.cache = self._BlindCache()
        self.cache = _Cache(cache_orders)
        self._http = broker
        self.log = _Log()
        self.cancelled_natively: list[str] = []
        #: Whether a native cancel has already been CONFIRMED by the venue by the time the venue loop
        #: reads the book. False is the realistic race — Alpaca keeps reporting the order as open until
        #: it confirms, which is the entire reason `_await_shares_available` exists. With this always
        #: True the row vanishes instantly and a double-cancel becomes UNREACHABLE, so a test asserting
        #: "not cancelled twice" would pass with the skip-set removed.
        self.native_cancel_confirms = native_cancel_confirms

    def _cancel(self, order):
        # A native cancel reaches the venue — that is what sending it means. A double that recorded the
        # call without freeing the shares would make every availability wait time out on a failure that
        # never happened.
        self.cancelled_natively.append(str(order.client_order_id))
        # Matched by venue id as well as coid — a bracket leg's venue-side client order id is ALPACA's,
        # so a coid-only match would leave the leg resting here while production frees it, and the
        # double would then disagree with production about what a native cancel does.
        if not self.native_cancel_confirms:
            return  # requested, not yet confirmed — the venue still reports it open
        keys = {str(order.client_order_id), str(getattr(order, "venue_order_id", "") or "")} - {""}
        for r in self._http.rows:
            if {str(r.get("id") or ""), str(r.get("client_order_id") or "")} & keys:
                self._http.cancelled.append(r["id"])

    # The REAL methods, bound to this double.
    _reducing_order_qty = UiFeedStrategy._reducing_order_qty
    _reducing_orders_open = UiFeedStrategy._reducing_orders_open
    _reducing_qty_for_exit = UiFeedStrategy._reducing_qty_for_exit
    _venue_reducing_orders = UiFeedStrategy._venue_reducing_orders
    # The REAL typed accessor, bound like the method above. `_venue_reducing_orders` now prefers
    # `generate_order_status_reports` and falls back to the REST read when there is no execution client.
    # With `_exec_client = None` these fixtures exercise the FALLBACK, which is the path their `_Http`
    # doubles were written against. Omitting it made the accessor raise inside the try, which the handler
    # turned into "the venue cannot be read" — and the exit path then saw zero shares reserved, which is
    # precisely the failure this file exists to catch.
    _venue_order_reports = UiFeedStrategy._venue_order_reports
    _exec_client = None
    _venue_only_reducing_rows = UiFeedStrategy._venue_only_reducing_rows
    # `_venue_shares_available` is now DERIVED (#641): |net| − Σ resting reduces, with the net read
    # typed-first, then REST (`list_positions.qty` — this file's `_Http` double already computes its
    # `qty_available` by exactly that arithmetic, so the two derivations move together), then cache.
    # With `_exec_client = None` these fixtures exercise the REST branch their doubles were written
    # against. The venue here is Alpaca-shaped, so the availability wait must keep waiting.
    _venue_position_reports = UiFeedStrategy._venue_position_reports
    _venue_net_position = UiFeedStrategy._venue_net_position
    _exec_reserves_shares = True
    _venue_shares_available = UiFeedStrategy._venue_shares_available
    _cancel_reducing_leg = UiFeedStrategy._cancel_reducing_leg
    # The venue route the leg cancel bottoms out in, bound rather than stubbed (#872 extracted it so
    # the wrong-mode cancels take the SAME path — two derivations of "cancel what the cache cannot see"
    # would drift, and the drift that hides is the one where the REST fallback survives in one copy).
    _cancel_at_venue = UiFeedStrategy._cancel_at_venue
    _cache_status_of = UiFeedStrategy._cache_status_of
    _await_shares_available = UiFeedStrategy._await_shares_available
    _lookup_order = UiFeedStrategy._lookup_order


def _stuck_engine():
    """THE PRODUCTION STATE OF 2026-08-17: the cache holds NBIS's stop as REJECTED, the broker has it
    resting on all 29 shares."""
    stuck = _Order("NBIS.XNAS", "PKW-d46739a7d60a48a3917f", status="REJECTED")
    broker = _Broker([_row("PKW-d46739a7d60a48a3917f", "venue-nbis")], held=29.0)
    return _Engine([stuck], broker)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# The fixture's own premise, asserted before anything is derived from it
# ══════════════════════════════════════════════════════════════════════════════════════════════════


def test_the_fixture_really_reproduces_the_divergence():
    """A test that cannot fail carries no information.

    If the cache and the broker agreed here, every assertion below would pass with the bug fully
    reintroduced — cache-only truth and broker truth would return the same number. So pin the
    disagreement itself first: the cache must see NOTHING open and the broker must be holding all 29.
    """
    e = _stuck_engine()

    assert e.cache.orders_open() == [], "the cache must not see the stop, or there is no bug to detect"
    assert len(e.cache.orders()) == 1, "and the order must still EXIST in the cache, held as terminal"
    assert e.cache.orders()[0].status.name == "REJECTED"

    rows = asyncio.run(e._http.list_orders())
    assert len(rows) == 1 and rows[0]["qty"] == 29.0, "the broker must be holding the shares"
    assert float(asyncio.run(e._http.list_positions())[0]["qty_available"]) == 0.0, (
        "with 29 held and 29 reserved, nothing is available — this is the 403 the operator hit"
    )


def test_the_side_name_helper_survives_the_enum_to_string_seam():
    """`str(OrderSide.SELL)` is not `"sell"`. Comparing them directly is silently always-false, and an
    always-false filter on an exit path reads as "nothing is resting" — the same wrong answer, one layer
    down. Pinned because the broker payload's own word is lowercase."""
    assert _side_name(OrderSide.SELL) == "sell"
    assert _side_name(OrderSide.BUY) == "buy"
    assert _side_name("SELL") == "sell"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# The size question every exit path asks
# ══════════════════════════════════════════════════════════════════════════════════════════════════


def test_the_cache_alone_reports_NOTHING_resting_which_is_the_whole_bug():
    """Not a test of desired behaviour — a RECORD of the wrong answer, so the fix has something to beat.

    This is precisely what `flatten.decide()` received on 2026-08-17. Zero means `cancel_resting_first`
    stays False, which means the cancel-and-confirm path never runs.
    """
    e = _stuck_engine()
    assert e._reducing_order_qty("NBIS.XNAS", "MANUAL-001", "LONG") == Decimal(0)


def test_the_exit_size_check_SEES_the_order_the_cache_cannot():
    """The fix. Broker truth reaches the number `flatten.decide()` is given."""
    e = _stuck_engine()
    qty = asyncio.run(e._reducing_qty_for_exit("NBIS.XNAS", "MANUAL-001", "LONG"))
    assert qty == Decimal("29.0"), (
        "the exit path still cannot see the stop holding the shares — it will submit and be rejected"
    )


def test_an_UNREADABLE_broker_falls_back_to_the_cache_rather_than_reporting_zero():
    """A broker that cannot be read must never be reported as an empty book. `None` means "no answer",
    and treating it as "nothing resting" is the exact inference that fired the rejected orders."""
    e = _stuck_engine()
    e._http.readable = False
    e.cache = _Cache([_Order("NBIS.XNAS", "PROT-SELL-NBIS-XNAS-abc", status="ACCEPTED", qty=29.0)])

    qty = asyncio.run(e._reducing_qty_for_exit("NBIS.XNAS", "MANUAL-001", "LONG"))
    assert qty == Decimal("29.0"), "an unreadable broker discarded what the cache DID know"


def test_the_LARGER_of_the_two_derivations_wins():
    """Neither source is trusted over the other, because each misses something the other sees: the cache
    misses terminal-state orders, and a venue read can lag a submit Nautilus has already accepted.

    Over-reporting costs one unnecessary cancel step. Under-reporting fires an exit into reserved shares.
    Those are not symmetric, so the maximum is taken rather than either source alone.
    """
    cached_only = _Order("NBIS.XNAS", "PK-cache-only", status="ACCEPTED", qty=29.0)
    e = _Engine([cached_only], _Broker([], held=29.0))
    assert asyncio.run(e._reducing_qty_for_exit("NBIS.XNAS", "MANUAL-001", "LONG")) == Decimal("29.0"), (
        "the venue's silence erased an order the cache knew was live"
    )


def test_a_divergence_is_LOGGED_with_both_numbers(caplog):
    """Silence is how this survived a full day. The log must name both derivations, not just the winner."""
    e = _stuck_engine()
    with caplog.at_level(logging.WARNING, logger="test.divergence"):
        asyncio.run(e._reducing_qty_for_exit("NBIS.XNAS", "MANUAL-001", "LONG"))
    assert any("cache says 0" in r.message and "broker says 29" in r.message for r in caplog.records), (
        f"the disagreement was not reported: {[r.message for r in caplog.records]}"
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Cancelling what the cache cannot cancel
# ══════════════════════════════════════════════════════════════════════════════════════════════════


def test_a_cache_TERMINAL_order_is_cancelled_AT_THE_VENUE():
    """The escape hatch, and the reason the cockpit's Orders-tab cancel did nothing for NBIS.

    Verified against the installed package rather than assumed
    (nautilus_trader/trading/strategy.pyx:1649):

        if order.is_closed_c() or order.is_pending_cancel_c():
            self.log.warning(f"Cannot cancel order: state is {order.status_string_c()}, {order}")
            return None  # Cannot send command

    REJECTED is closed, so `Strategy.cancel_order` logs and drops the command. Without a venue-level
    cancel these shares can never be released by this engine — not by flatten, not by PEAK, not by the
    operator clicking cancel.
    """
    e = _stuck_engine()
    asyncio.run(e._cancel_reducing_leg("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))

    assert e._http.cancelled == ["venue-nbis"], "the resting order was never cancelled at the venue"
    assert e.cancelled_natively == [], (
        "a terminal order was pushed through Nautilus, which silently drops it"
    )


def test_a_cache_OPEN_order_goes_through_NAUTILUS_not_around_it():
    """Native first, always. The venue path exists for what Nautilus cannot do, not instead of it —
    cancelling behind Nautilus's back for an order it is tracking would strand its state machine."""
    live = _Order("NBIS.XNAS", "PROT-SELL-NBIS-XNAS-live", status="ACCEPTED")
    e = _Engine([live], _Broker([_row("PROT-SELL-NBIS-XNAS-live", "venue-live")], held=29.0))

    asyncio.run(e._cancel_reducing_leg("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))

    assert e.cancelled_natively == ["PROT-SELL-NBIS-XNAS-live"], "the native cancel never happened"
    assert e._http.cancelled == ["venue-live"], "the native cancel must still reach the venue"


def test_BOTH_kinds_are_cleared_in_one_pass():
    """The live book had both shapes at once: seven `PROT-SELL-` backstop stops and one `PKW-` PEAK trail,
    some visible to the cache and some not. Clearing only one kind leaves the other holding the shares."""
    live = _Order("NBIS.XNAS", "PROT-SELL-NBIS-XNAS-live", status="ACCEPTED", qty=10.0)
    stuck = _Order("NBIS.XNAS", "PKW-stuck", status="REJECTED", qty=19.0)
    broker = _Broker(
        [_row("PROT-SELL-NBIS-XNAS-live", "venue-live", qty=10.0), _row("PKW-stuck", "venue-stuck", qty=19.0)],
        held=29.0,
    )
    e = _Engine([live, stuck], broker)

    asyncio.run(e._cancel_reducing_leg("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))

    assert sorted(broker.cancelled) == ["venue-live", "venue-stuck"], "a holder of shares was left resting"


def test_the_venue_cancel_names_what_the_CACHE_believed(caplog):
    """The disagreement belongs in the same line as the action. An operator reading "cancelled at the
    venue" with no explanation cannot tell a routine cancel from the engine healing a split brain."""
    e = _stuck_engine()
    with caplog.at_level(logging.WARNING, logger="test.divergence"):
        asyncio.run(e._cancel_reducing_leg("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))
    assert any("as REJECTED" in r.message for r in caplog.records), (
        f"the log does not say what the cache thought: {[r.message for r in caplog.records]}"
    )


def test_an_unreadable_broker_cancels_NOTHING_extra_and_does_not_raise():
    """Fail closed and quietly on the venue leg: the native cancels have already gone out, and a throw
    here would reach a handler that reports `str(exc)` with no hint that protection was removed."""
    live = _Order("NBIS.XNAS", "PROT-SELL-live", status="ACCEPTED")
    broker = _Broker([_row("PROT-SELL-live", "venue-live")], held=29.0, readable=False)
    e = _Engine([live], broker)

    asyncio.run(e._cancel_reducing_leg("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))
    assert e.cancelled_natively == ["PROT-SELL-live"], "the native cancel must still be attempted"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Waiting on the RESERVATION, which is not order status
# ══════════════════════════════════════════════════════════════════════════════════════════════════


def test_the_wait_is_on_qty_available_not_on_the_order_being_gone():
    """#245 stated as a number. `status: canceled` and the shares becoming available are different
    events, and the next submit needs the shares."""
    e = _stuck_engine()
    assert asyncio.run(e._venue_shares_available("NBIS.XNAS")) == Decimal(0), (
        "29 held against a 29-share resting stop must report ZERO available"
    )
    asyncio.run(e._cancel_reducing_leg("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))
    assert asyncio.run(e._venue_shares_available("NBIS.XNAS")) == Decimal("29.0")


def test_the_wait_TIMES_OUT_rather_than_sending_into_a_reserved_position():
    """False means SEND NOTHING. The position keeps whatever protects it, which is the safe end state;
    firing an exit on an unconfirmed release is how five protective stops were lost on 2026-08-12."""
    e = _stuck_engine()  # nothing is ever cancelled here, so the shares never free
    assert asyncio.run(e._await_shares_available("NBIS.XNAS", Decimal(29), timeout_s=0.4)) is False


def test_the_wait_returns_TRUE_once_the_venue_frees_the_shares():
    """The complement — without it a wait hard-wired to False would pass the test above and block every
    exit forever."""
    e = _stuck_engine()
    asyncio.run(e._cancel_reducing_leg("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))
    assert asyncio.run(e._await_shares_available("NBIS.XNAS", Decimal(29), timeout_s=0.4)) is True


def test_an_unreadable_broker_NEVER_reports_the_shares_as_available():
    """No answer is not a yes. This is the branch that must not fail open — everything downstream of it
    places an order."""
    e = _stuck_engine()
    e._http.readable = False
    assert asyncio.run(e._await_shares_available("NBIS.XNAS", Decimal(29), timeout_s=0.4)) is False


def test_a_position_the_broker_does_not_hold_reports_ZERO_not_None():
    """Flat at the broker is a real answer, and a different one from "cannot read". Conflating them would
    make a genuinely flat position look like an outage."""
    e = _Engine([], _Broker([], held=0.0, symbol="OTHER"))
    assert asyncio.run(e._venue_shares_available("NBIS.XNAS")) == Decimal(0)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# What the cache cannot see at all — the guard input
# ══════════════════════════════════════════════════════════════════════════════════════════════════


def test_the_hidden_rows_are_exactly_those_the_cache_is_missing():
    """PEAK's arm guard inspects the cache and concludes the position is bare. These are the rows that
    make that conclusion wrong."""
    live = _Order("NBIS.XNAS", "PROT-SELL-live", status="ACCEPTED", qty=10.0)
    broker = _Broker(
        [_row("PROT-SELL-live", "venue-live", qty=10.0), _row("PKW-stuck", "venue-stuck", qty=19.0)],
        held=29.0,
    )
    e = _Engine([live], broker)

    hidden = asyncio.run(e._venue_only_reducing_rows("NBIS.XNAS", "MANUAL-001", OrderSide.SELL))
    assert [r["client_order_id"] for r in hidden] == ["PKW-stuck"], (
        "the row the cache already knows about must not be reported as hidden, and the one it misses must"
    )


def test_hidden_rows_are_None_when_the_broker_cannot_be_read():
    """So the caller can REFUSE. An empty list would read as "nothing hidden" and permit an arm that
    cancels protection blind."""
    e = _stuck_engine()
    e._http.readable = False
    assert asyncio.run(e._venue_only_reducing_rows("NBIS.XNAS", "MANUAL-001", OrderSide.SELL)) is None


def test_the_OPPOSITE_side_is_not_counted_as_reserving():
    """A resting BUY does not hold shares against a long. Counting it would refuse exits that are fine —
    and a guard that cries wolf is one an operator learns to click through."""
    broker = _Broker([_row("BUY-order", "venue-buy", side="buy")], held=29.0)
    e = _Engine([], broker)
    assert asyncio.run(e._reducing_qty_for_exit("NBIS.XNAS", "MANUAL-001", "LONG")) == Decimal(0)


def test_ANOTHER_symbols_stop_does_not_count_against_this_position():
    """The broker read is account-wide. Filtering by symbol is what makes it usable per position."""
    broker = _Broker([_row("PROT-SELL-WPM", "venue-wpm", symbol="WPM")], held=29.0)
    e = _Engine([], broker)
    assert asyncio.run(e._reducing_qty_for_exit("NBIS.XNAS", "MANUAL-001", "LONG")) == Decimal(0)


@pytest.mark.parametrize("otype", ["stop", "stop_limit", "trailing_stop", "limit", "market"])
def test_EVERY_resting_sell_reserves_shares_whatever_its_type(otype):
    """Alpaca reserves against any resting reducing order, not only protective ones. Conflating COVERAGE
    with AVAILABILITY produced `PROT-SELL-OKTA: 403 insufficient qty available (requested: 68,
    available: 2)` — a take-profit LIMIT protects nothing on the way down and still holds the shares."""
    broker = _Broker([_row("some-order", "venue-1", otype=otype)], held=29.0)
    e = _Engine([], broker)
    assert asyncio.run(e._reducing_qty_for_exit("NBIS.XNAS", "MANUAL-001", "LONG")) == Decimal("29.0")


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE JOIN KEY (#269 regression, 2026-08-17)
#
# Operator: "u did it again. why? flatten works now peak broken again."
#
#     attach_manager REJECTED — the broker holds resting exit order(s) this system did not place
#     (379fcfb5-6e6d-4947-8666-55e061c20b02) — cancel them manually before arming PEAK
#
# That order was HIS OWN BRACKET's take-profit leg:
#
#     AEM sell limit qty=20 class=bracket coid=379fcfb5-6e6d-4947-8666-55e061c20b02
#
# We submit a bracket as ONE native Alpaca request and the VENUE creates the stop and take-profit legs,
# giving each a client order id of its own. `_submit_order_list` maps the legs back to our orders by
# VENUE id, so the cache holds the leg under OUR coid while Alpaca reports a UUID.
#
# The new broker-side guard joined the two by `client_order_id` alone. For a bracket leg that never
# matches — so this system's own take-profit looked invisible to the cache AND foreign to us, and PEAK
# refused to arm. That is #265 verbatim ("refusing to arm over a bracket told the operator to cancel it
# manually"), reintroduced by a join key rather than by a guard.
# ══════════════════════════════════════════════════════════════════════════════════════════════════


class _BracketLeg:
    """A bracket leg as it exists on BOTH sides: our client order id in the cache, Alpaca's at the venue.

    A double whose two ids were the same string could not express the bug — the whole defect is that
    they differ.
    """

    def __init__(self, our_coid: str, venue_id: str):
        self.instrument_id = "AEM.XNYS"
        self.strategy_id = "MANUAL-001"
        self.side = OrderSide.SELL
        self.order_type = OrderType.LIMIT
        self.client_order_id = ClientOrderId(our_coid)
        self.venue_order_id = venue_id
        self.leaves_qty = 20.0
        self.tags = ["bracket:entry-aem"]
        self.status = type("S", (), {"name": "ACCEPTED"})()
        self.is_closed = False


def _aem_bracket():
    leg = _BracketLeg("BR-ours-take-profit", "6f1bbfd1-0c25-483c-8edd-af27d157a876")
    row = _row("379fcfb5-6e6d-4947-8666-55e061c20b02", "6f1bbfd1-0c25-483c-8edd-af27d157a876",
               symbol="AEM", qty=20.0, otype="limit")
    return _Engine([leg], _Broker([row], held=20.0, symbol="AEM")), leg, row


def test_the_fixture_really_has_TWO_DIFFERENT_ids_for_one_order():
    """The premise. If the venue row carried our coid, a client-order-id join would match and every
    assertion below would pass with the bug fully reintroduced."""
    _e, leg, row = _aem_bracket()
    assert str(leg.client_order_id) != row["client_order_id"], "the two ids must differ, or there is no bug"
    assert str(leg.venue_order_id) == row["id"], "the venue id is the only thing linking them"


def test_our_own_BRACKET_LEG_is_not_reported_as_hidden():
    """The refusal the operator hit. The leg is in the cache and resting at the venue — it is not hidden at all,
    and reporting it as such is what refused the arm."""
    e, _leg, _row = _aem_bracket()

    hidden = asyncio.run(e._venue_only_reducing_rows("AEM.XNYS", "MANUAL-001", OrderSide.SELL))
    assert hidden == [], (
        "this system's own bracket leg was reported as an order it did not place — PEAK refuses to arm"
    )


def test_a_bracket_leg_is_NOT_cancelled_TWICE():
    """The same join, one step further, in the race that makes it reachable.

    A cancel is ASYNCHRONOUS: Alpaca keeps reporting the order as open until it confirms. So when the
    venue loop runs, Nautilus's cancel is in flight and the leg is still in the book — and the loop must
    recognise it as already handled. Joining on client order id alone it does not, and fires a second
    cancel at the broker behind Nautilus's back.

    The fixture asserts its own premise first: with the cancel unconfirmed the row MUST still be visible,
    or there is nothing for the skip-set to skip and this test cannot fail.
    """
    e, _leg, _row = _aem_bracket()
    e.native_cancel_confirms = False

    asyncio.run(e._cancel_reducing_leg("AEM.XNYS", "MANUAL-001", OrderSide.SELL))

    assert e.cancelled_natively == ["BR-ours-take-profit"], "the native cancel did not happen"
    assert len(asyncio.run(e._http.list_orders())) == 1, (
        "the fixture removed the row on request, so a double cancel is unreachable and this test is inert"
    )
    assert e._http.cancelled == [], (
        "the bracket leg was cancelled a SECOND time at the venue while Nautilus's cancel was in flight"
    )


def test_a_TRULY_foreign_resting_sell_still_refuses():
    """The half of the guard that must NOT be widened by the fix. An order that is neither in the cache
    nor one of ours is exactly the case where cancelling blind strips something the operator wanted."""
    stranger = _row("SOMEONE-ELSE", "venue-stranger", symbol="AEM", qty=20.0)
    e = _Engine([], _Broker([stranger], held=20.0, symbol="AEM"))

    hidden = asyncio.run(e._venue_only_reducing_rows("AEM.XNYS", "MANUAL-001", OrderSide.SELL))
    assert [r["client_order_id"] for r in hidden] == ["SOMEONE-ELSE"], (
        "an order this system cannot account for must still be reported"
    )
