"""One hand-placed IB order with no `orderRef` must not kill the whole order-status batch (#785).

THE DEFECT. `nautilus_trader==1.229.0`,
`adapters/interactive_brokers/execution.py:517` builds the report with an UNGUARDED
`ClientOrderId(ib_order.orderRef)`. Fifty-eight lines above, the same function writes

    :459  # Use venue_order_id as key since orderRef may be empty for external orders.
          client_order_id = ClientOrderId(ib_order.orderRef) if ib_order.orderRef else None

and `_on_open_order` guards it a third time at `:1779`. The adapter knows; one line forgot.
`ClientOrderId('')` is Cython and raises `ValueError: 'value' string was invalid, was ''`.

`generate_order_status_reports` loops over the venue's open orders with NO per-order try, so the
first refless order discards every report in the batch — including the ones already parsed.

MEASURED ON staging2, 2026-09-02T16:22:53Z -> 2026-09-03T14:53:58Z (22h): IB account DUPTEST02 held
18 open orders with `orderRef=''`, and the engine logged 12,117 `Failed to generate order status
reports`, 11,844 `Error in check_order_consistency`, and 1,217 `protection: venue orders unreadable
and no broker REST fallback exists — not placing anything`. Reconciliation and protection were both
blind for the whole session while 21 PROT orders sat ACCEPTED at the venue.

WHY THE DOUBLE LOOKS LIKE THIS. Every method under test IS the shipped vendor function, copied onto
a class whose attributes we can actually set — the Cython base makes `_log`, `_clock` and
`account_id` read-only, so `object.__new__` is not available. `IBOrder` is `ibapi.order.Order`, the
real one, and `ClientOrderId` is the real Cython identifier: the double REJECTS the empty string
exactly the way production does. A double that accepted `ClientOrderId('')` could not represent this
bug at all, which is the failure mode this repo has paid for six times.
"""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal

import pytest
from ibapi.order import Order as IBOrder
from ibapi.order_state import OrderState as IBOrderState
from nautilus_trader.adapters.interactive_brokers.execution import (
    InteractiveBrokersExecutionClient as _Vendor,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.test_kit.providers import TestInstrumentProvider

INSTRUMENT = TestInstrumentProvider.equity(symbol="ARKK", venue="BATS")
ACCOUNT = "DUPTEST02"
#: A SECOND IB account on the same login. No hyphen: `AccountId.get_id()` splits on the first one,
#: so "DU-OTHER" would resolve to "DU" and the two clients would not be distinguishable at all.
OTHER_ACCOUNT = "DUPTEST99"

#: Eighteen venue ids, the count staging2 actually choked on over the 22h log. The SHAPE of the batch
#: is what this test is about — eighteen refless orders around the good ones — so the count is real and
#: the numbers are synthetic: the venue's own permIds were replaced before publication (#1045).
REFLESS_PERM_IDS = tuple(900_000_001 + i for i in range(18))


def ib_order(order_ref: str, order_id: int, perm_id: int) -> IBOrder:
    """A real `ibapi.order.Order`, filled the way IB fills one for a resting stop."""
    o = IBOrder()
    o.orderId = order_id
    o.permId = perm_id
    o.orderRef = order_ref
    o.action = "SELL"
    o.orderType = "STP"
    o.tif = "GTC"
    o.totalQuantity = Decimal("10")
    o.filledQuantity = Decimal("0")
    o.auxPrice = 50.0
    o.account = ACCOUNT
    o.contract = INSTRUMENT.id
    o.order_state = IBOrderState()
    o.order_state.status = "Submitted"
    return o


class _Log:
    """RECORDS THE LEVEL, not just the text.

    The first version collapsed `warning = info = error = debug`, so every assertion about "this is
    an ERROR" was an assertion about nothing — codex's finding, and the same undiscriminating shape
    this repo keeps paying for. `lines` is kept for the tests that only care what was said.
    """

    _LEVELS = ("warning", "info", "error", "debug", "exception")

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def __getattr__(self, name):
        if name in _Log._LEVELS:
            def _rec(msg, *a, **k):
                self.records.append((name, str(msg)))
            return _rec
        raise AttributeError(name)

    @property
    def lines(self) -> list[str]:
        return [msg for _level, msg in self.records]

    def at(self, level) -> list[str]:
        return [msg for lv, msg in self.records if lv == level]


class _Clock:
    def timestamp_ns(self):
        return 1_700_000_000_000_000_000


class _MsgBus:
    """The health plane. `UNRECONCILED_TOPIC` is where Alpaca already publishes the rows its own
    reconciler had to skip (#643) — same topic, same shape, or the two drift."""

    def __init__(self, fail: bool = False):
        self.published: list[tuple] = []
        self._fail = fail

    def publish(self, topic, payload):
        if self._fail:
            raise RuntimeError("the health plane is down")
        self.published.append((topic, payload))

    def rows(self):
        from api.bus_topics import UNRECONCILED_TOPIC

        return [p["orders"] for t, p in self.published if t == UNRECONCILED_TOPIC]


class _InstrumentProvider:
    async def get_instrument(self, contract):
        return INSTRUMENT

    def get_price_magnifier(self, instrument_id):
        return 1


class _IBClient:
    """Stands in for `InteractiveBrokersClient` — the socket, and the object we wrap.

    IT FILTERS BY ACCOUNT, because production does: `client/order.py:164` returns
    `[o for o in all_orders if o.account == account_id]`. A double that skipped that would let a
    filter which accidentally widened the book — returning orders from another IB account under the
    same login — pass unnoticed, and this repo has shipped four defects behind doubles that could
    not represent what production refuses.
    """

    def __init__(self, orders, positions=()):
        self._orders = orders
        self._positions = positions
        #: Set to return something the vendor's own annotation forbids, bypassing every shaping this
        #: double does. See `test_a_NON_LIST_return_RAISES_rather_than_becoming_a_SHORT_BOOK`.
        self._raw = None
        self.calls = 0
        self._order_id_to_order_ref: dict = {}
        self._log = _Log()

    async def get_open_orders(self, account_id: str):
        self.calls += 1
        # `None` means the socket dropped mid-request. Returned as-is, deliberately: the vendor
        # turns it into a ConnectionError and refuses to reconcile, and that distinction is the
        # subject of `test_a_disconnect_is_NOT_flattened_into_an_empty_book`.
        if self._raw is not None:
            return self._raw
        if self._orders is None:
            return None
        return [o for o in self._orders if o.account == account_id]

    async def get_positions(self, account_id: str):
        # `None` here is a disconnect too, and the vendor raises ConnectionError on it at
        # `execution.py:587`. The batch path reads positions whenever `open_only=False`, which is
        # the mode the cockpit's own `_venue_order_reports` uses.
        return None if self._positions is None else list(self._positions)


class _ExecClient:
    """The shipped vendor methods, running on attributes a test can set.

    Not a reimplementation: the loop below copies every function and property defined on
    `InteractiveBrokersExecutionClient` onto this class, so `generate_order_status_reports`,
    `_parse_ib_order_to_order_status_report` and `_cached_order_filled_qty` are the vendor's own
    code. If the vendor changes, this changes with it.
    """

    for _name, _attr in vars(_Vendor).items():
        if inspect.isfunction(_attr) or isinstance(_attr, property):
            locals()[_name] = _attr
    del _name, _attr

    def __init__(self, orders, positions=(), msgbus=None, account=ACCOUNT, ib_client=None):
        # `ib_client` is injectable because Nautilus CACHES the transport per
        # `(host, port, client_id)` (`factories.py:120`), so two execution clients really can share
        # one — and that is the case the reporting has to get right.
        self._client = ib_client if ib_client is not None else _IBClient(orders, positions)
        self._log = _Log()
        self._clock = _Clock()
        self._msgbus = msgbus if msgbus is not None else _MsgBus()
        self._instrument_provider = _InstrumentProvider()
        self._order_filled_qty: dict = {}
        self._filter_sec_types: set = set()
        self.account_id = AccountId(f"INTERACTIVE_BROKERS-{account}")


def _reports_command(open_only: bool) -> GenerateOrderStatusReports:
    return GenerateOrderStatusReports(
        instrument_id=None, start=None, end=None, open_only=open_only,
        command_id=UUID4(), ts_init=0,
    )


def _run(client, *, open_only: bool = True):
    return asyncio.run(client.generate_order_status_reports(_reports_command(open_only)))


def _filtered(orders, positions=(), msgbus=None, account=ACCOUNT, ib_client=None):
    from api.providers.ib_refless_orders import install_refless_order_filter

    client = _ExecClient(orders, positions, msgbus, account, ib_client)
    install_refless_order_filter(client)
    return client


GOOD = "PROT-SELL-ARKK-BATS-de398844"


# ---------------------------------------------------------------------------------------------
# FIXTURE PROPERTIES FIRST. Asserting that the batch survives proves nothing unless the fixture can
# express a batch that dies. Both tests below must pass BEFORE and AFTER the fix — they pin the
# defect and the double, not the repair.
# ---------------------------------------------------------------------------------------------

def test_the_fixture_can_express_the_bug__ClientOrderId_rejects_the_empty_string():
    """The Cython identifier is what raises. A double that accepted `''` here would make every
    assertion in this file vacuous."""
    with pytest.raises(ValueError) as exc:
        ClientOrderId("")
    assert "was ''" in str(exc.value)


def test_the_fixture_can_express_the_bug__an_UNFILTERED_batch_dies_on_ONE_refless_order():
    """#785 itself, reproduced through the vendor's own entry point.

    Note WHICH order is lost: the good one is parsed and appended FIRST, and is discarded anyway,
    because the exception escapes the loop and the whole return value goes with it. That is why
    staging2's `venue_reported_ids` was empty rather than short by 18.
    """
    client = _ExecClient([ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])])
    with pytest.raises(ValueError) as exc:
        _run(client)
    assert "was ''" in str(exc.value)
    # FROM THE VENDOR'S PARSER, not from somewhere incidental. Without this the test would pass on
    # any ValueError the fixture happened to raise, and would keep passing over a repaired adapter.
    frames = [(str(e.path), e.name) for e in exc.traceback]
    assert any(
        path.endswith("interactive_brokers/execution.py")
        and name == "_parse_ib_order_to_order_status_report"
        for path, name in frames
    ), frames


# ---------------------------------------------------------------------------------------------
# THE PROPERTY. One bad order must never cost the batch.
# ---------------------------------------------------------------------------------------------

def test_one_refless_order_does_not_kill_the_batch():
    reports = _run(_filtered([ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])]))
    assert [str(r.client_order_id) for r in reports] == [GOOD]


def test_the_REAL_staging2_book_reports_every_one_of_its_21_live_orders():
    """The exact shape that was blind for 22h: 21 PROT orders the engine placed, 18 hand-placed
    orders with no `orderRef`, refless ones interleaved so the failure cannot depend on ordering."""
    good = [ib_order(f"PROT-SELL-{i}", 100 + i, 900 + i) for i in range(21)]
    refless = [ib_order("", 200 + i, p) for i, p in enumerate(REFLESS_PERM_IDS)]
    book = [x for pair in zip(good, refless + [None] * 3) for x in pair if x is not None]

    reports = _run(_filtered(book))

    assert len(reports) == 21, f"expected all 21 engine orders, got {len(reports)}"
    assert {str(r.client_order_id) for r in reports} == {f"PROT-SELL-{i}" for i in range(21)}


def test_a_book_of_ONLY_refless_orders_reports_nothing_AND_SAYS_the_book_was_not_empty():
    """The degenerate case, and the one that could become a silent short read.

    `[]` is a true answer — the venue holds no order this node can name — but it is INDISTINGUISHABLE
    from "the venue holds nothing" unless the drop is recorded. Three states, never two: nothing
    there, 18 dropped, and a disconnect are three different facts. Killed by dropping silently.
    """
    client = _filtered([ib_order("", 200 + i, pid) for i, pid in enumerate(REFLESS_PERM_IDS)])
    assert _run(client) == []
    lines = client._client._log.lines + client._log.lines
    assert any("18" in ln for ln in lines), lines


def test_a_clean_book_passes_through_UNCHANGED():
    """A filter that drops something it should not is a silent short read — the same blindness with
    a different cause. Killed by a predicate that drops on anything but an empty `orderRef`."""
    book = [ib_order(f"PROT-SELL-{i}", 100 + i, 900 + i) for i in range(21)]
    reports = _run(_filtered(book))
    assert {str(r.client_order_id) for r in reports} == {f"PROT-SELL-{i}" for i in range(21)}


def test_open_only_FALSE_survives_too():
    """`api/engine_node.py:4483 _venue_order_reports` — the cockpit's OWN call, feeding the
    protection reconciler and `reserving_orders` — passes `open_only=False`. That branch also walks
    the positions book, and it is the path that logged 1,217 refusals to arm protection.
    """
    reports = _run(
        _filtered([ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])]),
        open_only=False,
    )
    assert [str(r.client_order_id) for r in reports] == [GOOD]


def _single(client, *, client_order_id=None, venue_order_id=None):
    return asyncio.run(
        client.generate_order_status_report(
            GenerateOrderStatusReport(
                instrument_id=None,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                command_id=UUID4(),
                ts_init=0,
            ),
        ),
    )


def test_the_TARGETED_query_by_VENUE_ORDER_ID_reaches_the_refless_order_and_must_not_raise():
    """`generate_order_status_report` (singular, `:419`) reads the same list and parses whatever it
    matches. It matches on `str(ib_order.orderId)` — NOT the PERM id — so a venue_order_id query CAN
    select a refless order, and today it raises the same ValueError from the same line.

    A `client_order_id` query cannot reach it (an empty `orderRef` never equals a real id), so
    asserting that path would have been asserting the accident. This asserts the reachable one.
    """
    book = [ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])]

    # FIXTURE PROPERTY: unfiltered, this query really does reach line 517.
    with pytest.raises(ValueError):
        _single(_ExecClient(book), venue_order_id=VenueOrderId("2"))

    # None is the honest answer once it is filtered out: the venue holds no order this node can
    # name by that id. The vendor logs "not found ... leaving order state unchanged" and returns.
    assert _single(_filtered(book), venue_order_id=VenueOrderId("2")) is None


def test_the_TARGETED_query_still_FINDS_a_real_order_in_a_book_that_contains_a_refless_one():
    """The other half. A filter that emptied the list would satisfy the test above and break this
    one — "found nothing" and "cannot look" must not become the same answer."""
    book = [ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])]
    report = _single(_filtered(book), client_order_id=ClientOrderId(GOOD))
    assert report is not None and str(report.client_order_id) == GOOD


# ---------------------------------------------------------------------------------------------
# THE SIBLINGS. Each of these is a way the fix could itself become the outage.
# ---------------------------------------------------------------------------------------------

def test_a_disconnect_is_NOT_flattened_into_an_empty_book():
    """`get_open_orders` returns None when the socket dropped mid-request, and the vendor's own
    docstring says callers must not read None as "confirmed zero open orders". A filter that
    returned `[]` instead would hand reconciliation an empty `venue_reported_ids` — which is
    EXACTLY the state #785 produced, reached by a new route. Killed by `list(orders or [])`.
    """
    with pytest.raises(ConnectionError):
        _run(_filtered(None))


def test_a_POSITIONS_disconnect_is_still_a_disconnect_on_open_only_FALSE():
    """`open_only=False` reads the positions book too, and the vendor raises ConnectionError when
    that read comes back None (`execution.py:587`) for the same reason as the orders read: None is
    "the socket dropped", never "confirmed zero".

    In review, the double returned `[]` for positions unconditionally, so this whole branch was
    unrepresentable — the fixture could not express the failure, which is the vacuity rule one level
    out. Killed by a filter that touches the positions read, or by a double that cannot say None.
    """
    with pytest.raises(ConnectionError):
        _run(_filtered([ib_order(GOOD, 1, 111)], positions=None), open_only=False)


def test_an_order_belonging_to_ANOTHER_ACCOUNT_is_still_not_reported():
    """Production's `get_open_orders` filters `order.account == account_id` (`client/order.py:164`).
    A wrap that widened the book — returning every order on the login rather than this account's —
    would be a new way to be wrong about the venue. Killed by a filter that rebuilds the list from
    an unfiltered source."""
    theirs = ib_order("SOMEONE-ELSES-ORDER", 9, 999)
    theirs.account = "DU-SOMEONE-ELSE"
    reports = _run(_filtered([ib_order(GOOD, 1, 111), theirs, ib_order("", 2, REFLESS_PERM_IDS[0])]))
    assert [str(r.client_order_id) for r in reports] == [GOOD]


def test_the_filter_SAYS_what_it_DROPPED():
    """Absence must be readable. A filter that silently removes 18 orders leaves an operator unable
    to tell a filtered book from a short one, and this repo has paid for that three times. The count
    and the venue ids must both appear."""
    client = _filtered([ib_order(GOOD, 1, 111)] + [
        ib_order("", 200 + i, p) for i, p in enumerate(REFLESS_PERM_IDS)
    ])
    _run(client)
    lines = client._client._log.lines + client._log.lines
    assert any("18" in ln for ln in lines), lines
    assert any(str(REFLESS_PERM_IDS[0]) in ln for ln in lines), lines


def test_installing_twice_does_not_double_the_filter():
    """Idempotent, as `install_budget_gate` is. A second install wraps the wrapper: harmless for the
    verdict, but it publishes the skipped rows TWICE per batch and doubles the drop log, which makes
    the count on the health plane meaningless.

    ASSERTS THE PUBLISHED BATCHES, not the log text — a log-shaped assertion is one rewording away
    from vacuous, and the published count is the number an operator actually reads.
    """
    from api.providers.ib_refless_orders import install_refless_order_filter

    bus = _MsgBus()
    client = _ExecClient([ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])], msgbus=bus)
    install_refless_order_filter(client)
    install_refless_order_filter(client)
    _run(client)
    assert len(bus.rows()) == 1, bus.rows()


def test_the_filter_RETURNS_the_client_so_a_factory_can_chain_it():
    """`BudgetGatedIBExecClientFactory.create` returns `install_budget_gate(...)` in one line; this
    must compose the same way or the wiring below cannot be written without a temporary."""
    from api.providers.ib_refless_orders import install_refless_order_filter

    client = _ExecClient([])
    assert install_refless_order_filter(client) is client


# ---------------------------------------------------------------------------------------------
# THE SEAM. A correct filter that nothing installs is the defect with extra steps — this repo has
# shipped that exact shape (#606 wired a fix into one of two providers; `_lane_symbols` was defined
# and never called). Aim at the CLASS of IB execution clients, not at today's one.
# ---------------------------------------------------------------------------------------------

def test_EVERY_IB_exec_client_this_repo_hands_NAUTILUS_filters_refless_orders():
    """Resolves the factory off the spec and asks the object, rather than grepping the module for a
    name — a source scan is satisfied by a dead class definition left behind, which is the evadable
    shape `test_every_exec_client_is_gated` was already rewritten to avoid.

    Killed by reverting `ibkr.py`'s spec to the shipped
    `InteractiveBrokersLiveExecClientFactory`, or by dropping the install from `create`.
    """
    import ast
    import importlib
    import pkgutil

    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveExecClientFactory,
    )

    import api.providers as providers

    checked = []
    for mod in pkgutil.iter_modules(providers.__path__):
        m = importlib.import_module(f"api.providers.{mod.name}")
        try:
            tree = ast.parse(inspect.getsource(m))
        except (OSError, TypeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ExecClientSpec"):
                continue
            kw = next((k for k in node.keywords if k.arg == "factory"), None)
            assert kw is not None, f"api.providers.{mod.name}: ExecClientSpec with no factory"
            name = getattr(kw.value, "id", None) or getattr(kw.value, "attr", None)
            factory = getattr(m, name, None)
            assert factory is not None, f"api.providers.{mod.name}: cannot resolve factory {name}"
            if not (isinstance(factory, type)
                    and issubclass(factory, InteractiveBrokersLiveExecClientFactory)):
                continue  # not an IB client; `orderRef` is an IB field and means nothing elsewhere
            checked.append(f"{mod.name}.{name}")
            assert getattr(factory, "filters_refless_orders", False), (
                f"api.providers.{mod.name} hands Nautilus {name}, which does NOT drop orders with "
                f"an empty orderRef — one hand-placed order at the broker then kills every order "
                f"status report the node makes (#785)."
            )
    assert checked, (
        "no provider module built an ExecClientSpec with an IB factory — this guard matched "
        "NOTHING, which is how it would pass over a repo that had lost the wiring entirely"
    )


def test_the_installed_filter_wraps_the_method_the_VENDOR_actually_calls():
    """`generate_order_status_reports` reads `self._client.get_open_orders`. Wrapping anything else
    — the parser, a helper of our own — leaves the vendor loop reading the raw book.

    ASSERTS BEHAVIOUR, NOT IDENTITY. The first version of this test asserted
    `client._client.get_open_orders is not raw`, which passes with NOTHING installed: re-reading a
    bound method builds a new object every time, so `a.m is not a.m` is true of any object in
    Python. That is the vacuous shape codex caught in review, and it is exactly the "detector aimed
    one level away" failure this repo keeps paying for.
    """
    book = [ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])]

    unwrapped = asyncio.run(_ExecClient(book)._client.get_open_orders(ACCOUNT))
    assert [o.orderRef for o in unwrapped] == [GOOD, ""], "the bare double already filtered"

    wrapped = asyncio.run(_filtered(book)._client.get_open_orders(ACCOUNT))
    assert [o.orderRef for o in wrapped] == [GOOD]


def test_the_REPO_FACTORY_returns_a_client_whose_BATCH_SURVIVES_a_refless_order():
    """THE SEAM, driven rather than marked.

    The guard above resolves `filters_refless_orders` off the factory CLASS, and a marker set on a
    factory whose `create` forgot to install anything satisfies it — codex's finding, and the same
    "declared but not connected" shape as `_lane_symbols` (defined, never called) and #606 (wired
    into one of two providers). This drives `BudgetGatedIBExecClientFactory.create` for real, with
    the vendor's own factory patched to hand back a client we can feed a book to, and asks the
    RETURNED OBJECT whether the batch survives.

    Killed by: dropping the install from `create`, installing it on the wrong attribute, setting the
    marker without wrapping, or wrapping a copy rather than the instance the factory returns.
    """
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveExecClientFactory as _VendorFactory,
    )

    from api.providers.ibkr import BudgetGatedIBExecClientFactory

    built = _ExecClient([ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])])
    original = _VendorFactory.__dict__["create"]
    _VendorFactory.create = staticmethod(lambda **kw: built)
    try:
        client = BudgetGatedIBExecClientFactory.create(
            loop=None, name=None, config=None, msgbus=None, cache=None, clock=None,
        )
    finally:
        _VendorFactory.create = original

    assert client is built, "the factory must wrap the vendor's instance, not replace it"
    assert [str(r.client_order_id) for r in _run(client)] == [GOOD]
    # BOTH wraps compose. #782's gate and this filter live on the same client, and an install that
    # replaced rather than added would silently un-gate execution while fixing reconciliation.
    assert getattr(client, "_budget_gate_installed", False), "the budget gate was lost"


def test_the_COCKPIT_read_that_FEEDS_PROTECTION_comes_back_with_reports_not_None():
    """The consequence the ticket is actually about.

    `UiFeedStrategy._venue_order_reports` (`api/engine_node.py:4483`) wraps the same vendor call and
    returns None when it raises. Its caller at `:1988` then refuses to arm any protection at all,
    because half a broker read is worse than none — 1,217 refusals on staging2 in 22h. That refusal
    is CORRECT; the read being unreadable is the defect.

    Driven through the real `_venue_order_reports` function, bound to a stand-in that carries only
    what it touches, because constructing a `UiFeedStrategy` needs a whole Nautilus kernel. `_run`
    above proves the vendor method; this proves the cockpit consumer sees the result.
    """
    from api.engine_node import UiFeedStrategy

    class _Node:
        _venue_order_reports = UiFeedStrategy._venue_order_reports

        def __init__(self, client):
            self._exec_client = client
            self.clock = _Clock()
            self.log = _Log()

    book = [ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])]

    # FIXTURE PROPERTY FIRST: unfiltered, this really does come back None — the state that made
    # staging2 refuse to place protection. Without this the assertion below could not fail.
    assert asyncio.run(_Node(_ExecClient(book))._venue_order_reports()) is None

    reports = asyncio.run(_Node(_filtered(book))._venue_order_reports())
    assert reports is not None, "protection would still refuse to arm"
    assert [str(r.client_order_id) for r in reports] == [GOOD]


def test_a_BLANK_orderRef_is_dropped_too__the_predicate_is_ClientOrderId_ITSELF():
    """`ClientOrderId(' ')` and `ClientOrderId('\\t')` raise exactly as `ClientOrderId('')` does —
    `Condition.valid_string` rejects whitespace, not just emptiness. A hand-written `if not ref`
    guard agrees with the Cython rule on `''` and DISAGREES on `' '`, and two derivations of one
    rule drift; this repo has paid for that on leash validation, manager-armed derivation and the
    percent->bps rounding seam.

    So the filter must ask `ClientOrderId` itself. Killed by any blank test of our own.
    """
    for blank in (" ", "\t"):
        with pytest.raises(ValueError):
            ClientOrderId(blank)  # FIXTURE PROPERTY: these really are rejected by production

    book = [ib_order(GOOD, 1, 111), ib_order(" ", 2, REFLESS_PERM_IDS[0])]
    with pytest.raises(ValueError):
        _run(_ExecClient(book))  # and really do kill an unfiltered batch
    assert [str(r.client_order_id) for r in _run(_filtered(book))] == [GOOD]


def test_the_NATIVE_config_knob_does_NOT_avoid_this__measured_not_assumed():
    """CLAUDE.md: prove Nautilus does not already do it, by reading the CONFIG OBJECT'S FIELDS.

    `InteractiveBrokersExecClientConfig` has exactly one field that changes which orders are
    fetched: `fetch_all_open_orders`. False selects `reqOpenOrders` (this client id only) and True
    selects `reqAllOpenOrders` (every API client, plus the TWS GUI) — `client/order.py:138`.

    It DEFAULTS TO FALSE and `api/providers/ibkr.py` does not set it, so staging2 was already on the
    narrow read and saw all 18 refless orders anyway. The knob is not the fix, and narrowing further
    is not available. Pinned here so nobody re-proposes it: if a future Nautilus changes the default,
    this test says so rather than leaving the claim to a docstring.
    """
    from nautilus_trader.adapters.interactive_brokers.config import (
        InteractiveBrokersExecClientConfig,
    )

    import api.providers.ibkr as ibkr

    # A msgspec Struct: the class attribute is a member descriptor, so the DEFAULT has to be read
    # off `__struct_defaults__`, which aligns with the TAIL of `__struct_fields__`.
    fields = InteractiveBrokersExecClientConfig.__struct_fields__
    defaults = InteractiveBrokersExecClientConfig.__struct_defaults__
    declared = dict(zip(fields[-len(defaults):], defaults))
    assert declared["fetch_all_open_orders"] is False, declared
    assert "fetch_all_open_orders" not in inspect.getsource(ibkr), (
        "ibkr.py now sets fetch_all_open_orders — re-read whether this changes the refless story"
    )


def test_an_orderRef_of_NONE_is_dropped_too__ValueError_alone_is_not_the_predicate():
    """`ClientOrderId(None)` raises **TypeError**, not ValueError — the Cython signature rejects the
    type before `valid_string` ever runs. A filter written as `except ValueError` would let a None
    ref straight through to `execution.py:517` and the batch would die exactly as before, while
    every other test in this file passed.

    The vendor's own truthiness guards at `:459` and `:1779` treat None as absent, so None is a
    shape it expects. Killed by narrowing the except clause to ValueError.
    """
    with pytest.raises(TypeError):
        ClientOrderId(None)  # FIXTURE PROPERTY: it is a DIFFERENT exception type

    nullref = ib_order("", 2, REFLESS_PERM_IDS[0])
    nullref.orderRef = None
    book = [ib_order(GOOD, 1, 111), nullref]

    with pytest.raises(TypeError):
        _run(_ExecClient(book))  # and it really does kill an unfiltered batch
    assert [str(r.client_order_id) for r in _run(_filtered(book))] == [GOOD]


# ---------------------------------------------------------------------------------------------
# THE HEALTH PLANE. #785 is #643 on the IBKR side: "a row reconciliation cannot represent must not
# kill the batch, and must not vanish either". Alpaca already publishes its skipped rows on
# `UNRECONCILED_TOPIC` every batch, empty included, and `engine_node.py:1282` subscribes and folds
# them into status. An IB filter that only logged would be the SAME rule derived twice — one visible
# on the health plane, one visible only to whoever greps the container.
# ---------------------------------------------------------------------------------------------

def test_every_dropped_order_is_PUBLISHED_on_the_health_plane_not_just_logged():
    from api.bus_topics import UNRECONCILED_TOPIC

    bus = _MsgBus()
    client = _filtered(
        [ib_order(GOOD, 1, 111)]
        + [ib_order("", 200 + i, pid) for i, pid in enumerate(REFLESS_PERM_IDS)],
        msgbus=bus,
    )
    assert [str(r.client_order_id) for r in _run(client)] == [GOOD]

    batches = bus.rows()
    assert batches, f"nothing was published on {UNRECONCILED_TOPIC}"
    rows = batches[-1]
    assert len(rows) == 18, rows
    # THE SAME ROW SHAPE Alpaca publishes (`providers/alpaca/exec_client.py:1300-1305`), so the one
    # consumer at `engine_node.py:8028` and the alerts that read it need no broker special case.
    assert set(rows[0]) >= {"venue_order_id", "symbol", "client_order_id", "reason"}, rows[0]
    assert any(str(REFLESS_PERM_IDS[0]) in str(r["venue_order_id"]) for r in rows), rows


def test_a_CLEAN_batch_publishes_an_EMPTY_LIST_so_a_FIXED_offender_CLEARS():
    """Three states, never two. `[]` states KNOWN-CLEAN; a topic never published is NEVER ASKED. If
    the filter published only when it dropped something, the last bad frame would stand forever and
    a repaired book would look identical to a broken one. Killed by `if dropped: publish(...)`."""
    bus = _MsgBus()
    _run(_filtered([ib_order(GOOD, 1, 111)], msgbus=bus))
    assert bus.rows() == [[]], bus.published


def test_the_PUBLISH_cannot_abort_the_batch_it_exists_to_PROTECT():
    """A guard whose own reporting can raise is a new abort path with a reassuring name — and this
    repo shipped exactly that in `_record_book_truth`, whose except branch called
    `self.clock.timestamp_ns()` on an object with no clock. Alpaca's publish is wrapped for the same
    reason (`exec_client.py:1318-1323`). Killed by removing the try around the publish."""
    client = _filtered([ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])],
                       msgbus=_MsgBus(fail=True))
    assert [str(r.client_order_id) for r in _run(client)] == [GOOD]


# ---------------------------------------------------------------------------------------------
# THE FILTER MUST NOT BECOME A NEW WAY TO BE WRONG ABOUT THE VENUE. Every test below is a shape in
# which a careless filter turns a LOUD failure into a SHORT BOOK — the same outage #785 is, reached
# through the repair. All eight came out of codex's review of the implementation.
# ---------------------------------------------------------------------------------------------

def test_an_order_with_NO_orderRef_ATTRIBUTE_RAISES_rather_than_being_quietly_skipped():
    """An absent ATTRIBUTE is the adapter having changed under us; an absent VALUE is a hand-placed
    order. They must not be the same answer.

    `getattr(order, "orderRef", None)` collapses them: the missing attribute becomes None, None
    raises TypeError, and TypeError is skipped — so an ibapi change would silently shorten every
    batch while this suite stayed green. The unpatched vendor raises AttributeError at
    `execution.py:517`; so must we.
    """
    class _Alien:
        account = ACCOUNT  # passes the account filter, so it really does reach the predicate

    with pytest.raises(AttributeError):
        _run(_filtered([ib_order(GOOD, 1, 111), _Alien()]))


def test_a_NON_LIST_return_RAISES_rather_than_becoming_a_SHORT_BOOK():
    """The vendor annotates `get_open_orders` as `list[IBOrder] | None` (`client/order.py:127`).
    Anything else means the adapter changed. Iterating it — a dict yields its KEYS — would turn each
    key into a "skipped order" and return `[]`, emptying `venue_reported_ids`: #785 again, by the
    repair's own hand. Killed by dropping the isinstance check."""
    client = _filtered([])
    # STRAIGHT PAST the double's own account filter, because the point is what the WRAPPER does with
    # a shape it was never promised — a double that could only ever hand back a list could not
    # express this at all.
    client._client._raw = {"not": "a list"}
    with pytest.raises(TypeError, match="not a list"):
        _run(client)


def test_a_RAISING_contract_accessor_does_not_kill_the_batch_from_the_DIAGNOSTIC_path():
    """`_describe` exists only to say WHY a row was dropped. An observation that can break the live
    path is the `_record_book_truth` defect — whose except branch called a clock the object did not
    have, and so aborted the very protection pass it was watching.

    It must also degrade LOUDLY: a null `venue_order_id` is obviously anomalous, and the reason names
    both failures. Substituting an id in a different format would silently change alert identity,
    because `alerts.py:114` keys on that field.
    """
    class _Exploding:
        account = ACCOUNT
        orderRef = ""
        orderId = 2

        @property
        def permId(self):
            raise RuntimeError("ibapi field blew up")

    bus = _MsgBus()
    client = _filtered([ib_order(GOOD, 1, 111), _Exploding()], msgbus=bus)
    assert [str(r.client_order_id) for r in _run(client)] == [GOOD]

    row = bus.rows()[-1][0]
    assert row["venue_order_id"] is None, row
    assert "could not be described" in row["reason"] and "blew up" in row["reason"], row


def test_TWO_exec_clients_over_ONE_transport_each_report_to_their_OWN_health_plane():
    """Nautilus caches the `InteractiveBrokersClient` per `(host, port, client_id)`
    (`factories.py:120`), so two execution clients CAN share one transport. The first version of
    this filter put the idempotence flag on the transport but captured the FIRST exec client's
    `_msgbus` in the closure — so the second account's skipped rows would have been published onto
    the first account's health plane. Wrong everywhere, visible nowhere.

    `get_open_orders(account_id)` carries the account, and each exec client owns exactly one, so the
    report follows the account. Killed by capturing a single client in the wrapper.
    """
    mine = ib_order(GOOD, 1, 111)
    my_bad = ib_order("", 2, REFLESS_PERM_IDS[0])
    theirs_bad = ib_order("", 3, REFLESS_PERM_IDS[1])
    theirs_bad.account = OTHER_ACCOUNT

    shared = _IBClient([mine, my_bad, theirs_bad])
    bus_a, bus_b = _MsgBus(), _MsgBus()
    a = _filtered(None, msgbus=bus_a, account=ACCOUNT, ib_client=shared)
    b = _filtered(None, msgbus=bus_b, account=OTHER_ACCOUNT, ib_client=shared)

    assert [str(r.client_order_id) for r in _run(a)] == [GOOD]
    assert _run(b) == []

    assert [r["venue_order_id"] for r in bus_a.rows()[-1]] == [f"PERM-{REFLESS_PERM_IDS[0]}"]
    assert [r["venue_order_id"] for r in bus_b.rows()[-1]] == [f"PERM-{REFLESS_PERM_IDS[1]}"]


def test_the_published_row_carries_the_RAW_orderRef_the_way_the_ALPACA_side_does():
    """`alpaca/exec_client.py:1300` publishes the broker's own `client_order_id`. A `repr` here
    would put the STRINGS `"''"` and `"None"` on a topic whose single consumer
    (`engine_node.py:8028`) and alert reader (`alerts.py:114`) are shared between both brokers —
    one rule, two derivations, which is the drift these tickets are about.

    Also pins the venue id FORMAT, which is `get_venue_order_id`'s, the same derivation
    `execution.py:503` uses for a report that succeeds.
    """
    bus = _MsgBus()
    _run(_filtered([ib_order("", 2, REFLESS_PERM_IDS[0])], msgbus=bus))
    row = bus.rows()[-1][0]
    assert row == {
        "venue_order_id": f"PERM-{REFLESS_PERM_IDS[0]}",
        "symbol": "ARKK",
        "client_order_id": "",
        "reason": row["reason"],
    }, row
    assert "was ''" in row["reason"], row


def test_the_SINGULAR_path_publishes_the_SAME_rows_as_the_BATCH_path():
    """Alpaca publishes only from `generate_order_status_reports`; this fires from
    `get_open_orders`, which the vendor's singular query (`execution.py:419`) also calls — a denser
    cadence. That is acceptable ONLY while the CONTENT cannot disagree, because both callers read
    the same whole open-orders book and `engine_node.py:8028` stores whichever payload arrived last.

    Pinned here so a future narrowing of either read cannot make the health plane alternate between
    two different truths.
    """
    bus = _MsgBus()
    book = [ib_order(GOOD, 1, 111), ib_order("", 2, REFLESS_PERM_IDS[0])]
    client = _filtered(book, msgbus=bus)

    _run(client)
    from_batch = bus.rows()[-1]
    _single(client, client_order_id=ClientOrderId(GOOD))
    from_singular = bus.rows()[-1]

    assert from_singular == from_batch, (from_singular, from_batch)


def test_the_change_line_is_an_ERROR_and_fires_ONLY_when_the_ROWS_change():
    """Two properties, and the second is why the fingerprint is over the WHOLE ROW.

    Cadence: this runs every ~6s on staging2. An ERROR per offender per tick is 18 lines every six
    seconds, which buries the log the way the outage did — so the line fires on CHANGE.

    But keying on the venue ids ALONE would mean the same orders with a different REASON produce no
    new line, and `alerts.py:123` dedupes by venue id too, so nothing anywhere would tell an
    operator the situation had changed. Killed by fingerprinting the ids instead of the rows.
    """
    bad = ib_order("", 2, REFLESS_PERM_IDS[0])
    client = _filtered([ib_order(GOOD, 1, 111), bad])

    _run(client)
    assert len(client._log.at("error")) == 1, client._log.records
    _run(client)
    assert len(client._log.at("error")) == 1, "an unchanged book logged twice"

    bad.orderRef = "  "  # same order, same venue id, DIFFERENT reason
    _run(client)
    assert len(client._log.at("error")) == 2, client._log.records


def test_a_book_that_becomes_CLEAN_says_so_rather_than_going_quiet():
    """The recovery half. `[]` on the topic clears the health plane, but a silent recovery leaves
    the last ERROR standing as the newest thing anyone reads in the log."""
    bad = ib_order("", 2, REFLESS_PERM_IDS[0])
    client = _filtered([ib_order(GOOD, 1, 111), bad])
    _run(client)
    client._client._orders = [ib_order(GOOD, 1, 111)]
    _run(client)
    assert any("usable orderRef again" in ln for ln in client._log.at("info")), client._log.records
