"""Step 4 of #641: share availability is DERIVED, and a venue that never reserves never waits.

`qty_available` is an ALPACA reservation concept: `grep -rn "qty_available"` over the installed
nautilus_trader returns zero hits (pinned in test_broker_read_gaps.py), so no typed read can supply
it and reading it kept `_venue_shares_available` broker-specific forever. The venue-neutral truth is
arithmetic over facts every adapter reports:

    available = |net position quantity| − Σ resting reducing-order quantity

Two consequences, both pinned here:

  * SHORTS WORK. Alpaca reports `qty_available` NEGATIVE for a short (measured -88,
    engine_node.py's own comment), so the BUY branch could never be satisfied. The derived form
    answers in absolute claimable shares on the position's own reducing side.

  * A VENUE THAT NEVER RESERVES SKIPS THE WAIT. IB has no reservation concept
    (api/venues/facts.py, #430). `_await_shares_available` used to poll the full timeout and return
    False when the answer was None — which is exactly why every staging-ibkr exit died: "no
    reservation concept" must mean SKIP, not "wait then fail". The fact is DECLARED on the provider's
    `ExecClientSpec` (never inferred from a provider name — the #619 rule) and pinned equal to the
    measured venue catalogue, because two derivations of one fact drift.

Every behaviour-changing test here was seen RED before the fix existed.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import ClassVar

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Quantity

from api.engine_node import UiFeedStrategy


def _order_reports(rows):
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    class _Clk:
        def timestamp_ns(self):
            return 1_800_000_000_000_000_000

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

    out = [AlpacaExecutionClient._parse_order_report(_Host(), r) for r in rows]
    assert all(r is not None for r in out), "fixture rows must all parse — a dropped row tests nothing"
    return out


def _row(coid, venue_id, *, qty, side, otype="stop", status="new"):
    return {"id": venue_id, "client_order_id": coid, "symbol": "AEM", "side": side,
            "type": otype, "status": status, "qty": qty, "filled_qty": "0", "stop_price": "50"}


def _pos_report(side, qty):
    return PositionStatusReport(
        account_id=AccountId("IB-DUTEST001"),
        instrument_id=InstrumentId.from_str("AEM.XNYS"),
        position_side=side,
        quantity=Quantity.from_str(qty),
        report_id=UUID4(),
        ts_last=0,
        ts_init=0,
    )


class _ExecClient:
    def __init__(self, order_reports=(), position_reports=(), *,
                 orders_readable=True, positions_readable=True):
        self.order_reports = list(order_reports)
        self.position_reports = list(position_reports)
        self.orders_readable = orders_readable
        self.positions_readable = positions_readable

    async def generate_order_status_reports(self, _cmd):
        if not self.orders_readable:
            raise RuntimeError("venue order read failed")
        return list(self.order_reports)

    async def generate_position_status_reports(self, _cmd):
        if not self.positions_readable:
            raise RuntimeError("venue position read failed")
        return list(self.position_reports)


class _Log:
    def __init__(self):
        self.warnings, self.errors, self.infos = [], [], []

    def warning(self, msg):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.errors.append(str(msg))

    def info(self, msg):
        self.infos.append(str(msg))

    def exception(self, msg, ex=None):
        if ex is None:
            raise TypeError("Logger.exception requires the exception — Condition.not_none(ex, 'ex')")
        self.warnings.append(f"EXC {msg}")


class _Clock:
    def timestamp_ns(self):
        return 0


class _EmptyCache:
    def positions_open(self):
        return []


def _host(exec_client, http=None, cache=None):
    class _H:
        pass

    h = _H()
    h._exec_client, h._http, h.log, h.clock = exec_client, http, _Log(), _Clock()
    h.cache = cache if cache is not None else _EmptyCache()
    for name in ("_venue_order_reports", "_venue_position_reports", "_venue_net_position",
                 "_venue_reducing_orders", "_venue_shares_available"):
        setattr(h, name, getattr(UiFeedStrategy, name).__get__(h))
    return h


def _available(h, iid="AEM.XNYS"):
    return asyncio.run(h._venue_shares_available(iid))


# --------------------------------------------------------------------------------------------------
# The derivation.
# --------------------------------------------------------------------------------------------------


def test_long_availability_is_net_minus_resting_reduces():
    """LONG 100, a 40-share sell resting: 60 claimable. The number Alpaca would state as
    `qty_available`, now derived from reads every adapter can answer."""
    h = _host(_ExecClient(
        order_reports=_order_reports([_row("a", "v1", qty="40", side="sell")]),
        position_reports=[_pos_report(PositionSide.LONG, "100")],
    ))
    assert _available(h) == Decimal(60)


def test_short_availability_counts_resting_BUYS_and_answers_in_absolute_shares():
    """The engine_node comment records the landmine: Alpaca reports `qty_available` NEGATIVE for a
    short (measured -88), so a wait on the BUY side could never be satisfied. Derived: SHORT 88 with
    a 30-share buy-to-cover resting leaves 58 claimable — positive, on the position's own reducing
    side. The resting SELL on the same name must NOT count: for a short it is the ENTRY side."""
    h = _host(_ExecClient(
        order_reports=_order_reports([
            _row("a", "v1", qty="30", side="buy"),
            _row("b", "v2", qty="7", side="sell"),   # entry side for a short — not a reservation
        ]),
        position_reports=[_pos_report(PositionSide.SHORT, "88")],
    ))
    assert _available(h) == Decimal(58)


def test_flat_at_the_venue_is_ZERO_not_unknown():
    """No position report for the instrument is a real answer — nothing held, nothing claimable —
    exactly as the old read returned Decimal(0) when the symbol was absent from /v2/positions."""
    h = _host(_ExecClient(order_reports=[], position_reports=[]))
    assert _available(h) == Decimal(0)


def test_unreadable_RESTING_ORDERS_is_UNKNOWN_never_zero_reserved():
    """The dangerous direction: with the position readable and the order book NOT, assuming zero
    reserved authorises an exit straight into a protective stop's shares — #245. None means the
    caller sends nothing."""
    h = _host(_ExecClient(
        order_reports=(), position_reports=[_pos_report(PositionSide.LONG, "100")],
        orders_readable=False,
    ))
    assert _available(h) is None


def test_net_position_falls_back_to_the_CACHE_when_no_report_source_resolves():
    """The task's own contract: net quantity comes from the typed report OR the cache. A node whose
    position read fails but whose order read works (and no REST client, so no third source) still
    answers from the one book it has."""

    class _Pos:
        instrument_id = InstrumentId.from_str("AEM.XNYS")

        def signed_decimal_qty(self):
            return Decimal(29)

    class _Cache:
        def positions_open(self):
            return [_Pos()]

    h = _host(
        _ExecClient(order_reports=[], positions_readable=False),
        cache=_Cache(),
    )
    assert _available(h) == Decimal(29)


def test_reserving_more_than_held_floors_at_zero():
    """A stop covering more than the position (the oversize defect the reconciler corrects) must
    read as 0 claimable, never negative — `_await_shares_available` compares with >=."""
    h = _host(_ExecClient(
        order_reports=_order_reports([_row("a", "v1", qty="120", side="sell")]),
        position_reports=[_pos_report(PositionSide.LONG, "100")],
    ))
    assert _available(h) == Decimal(0)


# --------------------------------------------------------------------------------------------------
# The wait, on a venue with no reservation concept.
# --------------------------------------------------------------------------------------------------


def _wait_host(*, reserves: bool, available):
    class _H:
        pass

    h = _H()
    h.log, h.clock = _Log(), _Clock()
    h._exec_reserves_shares = reserves
    h.venue_reads = 0

    async def _shares(instrument_id):
        h.venue_reads += 1
        return available

    h._venue_shares_available = _shares
    h._await_shares_available = UiFeedStrategy._await_shares_available.__get__(h)
    return h


def test_a_venue_that_never_reserves_SKIPS_the_wait():
    """Exits are dead on staging-ibkr precisely here: the venue reserves nothing, the availability
    read has nothing to say, and the wait burned the full timeout then answered False — so every
    exit was refused on a constraint the venue does not have. No reservation concept = no wait."""
    h = _wait_host(reserves=False, available=None)
    t0 = time.monotonic()
    ok = asyncio.run(h._await_shares_available("AEM.XNYS", Decimal(29), timeout_s=2.0))
    took = time.monotonic() - t0
    assert ok is True, (
        "the venue does not reserve shares against resting orders, and the wait still refused the "
        "exit — 'no reservation concept' must mean SKIP THE WAIT, not 'wait then fail'"
    )
    assert h.venue_reads == 0, "nothing to wait on means nothing to poll — the read still ran"
    assert took < 1.0, f"the skip still burned {took:.2f}s of a 2s timeout"


def test_a_venue_that_DOES_reserve_still_waits_and_still_refuses():
    """The other direction, so the skip cannot rot into 'never wait anywhere': on a reserving venue
    an unsatisfiable availability must still time out to False — send nothing, keep the stop."""
    h = _wait_host(reserves=True, available=Decimal(0))
    ok = asyncio.run(h._await_shares_available("AEM.XNYS", Decimal(29), timeout_s=0.05))
    assert ok is False
    assert h.venue_reads > 0, "the reserving venue was never polled — the skip is unconditional"


# --------------------------------------------------------------------------------------------------
# The declaration: on the provider spec, agreeing with the measured venue catalogue.
# --------------------------------------------------------------------------------------------------


def test_each_exec_provider_DECLARES_whether_its_venue_reserves(monkeypatch):
    """No `if venue == ...` above api/providers/: the venue difference is a field on ExecClientSpec,
    REQUIRED so a new provider must measure and declare rather than inherit a guess. Pinned equal to
    api/venues/facts.py because two derivations of one fact drift — and this one decides whether
    exits wait."""
    from api import feed_config as fc
    from api.providers import ibkr
    from api.providers.alpaca import build_exec
    from api.venues.facts import ALPACA, IBKR

    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    alpaca_spec = build_exec({})

    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DUTEST001")
    real = fc.load_feed_config

    def _fake(*a, **k):
        cfg = real()
        return cfg.__class__(**{**cfg.__dict__, "symbols": ("AEM",)})

    monkeypatch.setattr(fc, "load_feed_config", _fake)
    ibkr_spec = ibkr.build({"account_id_env": "IBKR_ACCOUNT_ID", "ibg_client_id": 1})

    assert alpaca_spec.reserves_shares_against_resting_stop is True
    assert ibkr_spec.reserves_shares_against_resting_stop is False
    assert alpaca_spec.reserves_shares_against_resting_stop == \
        ALPACA["reserves_shares_against_resting_stop"]
    assert ibkr_spec.reserves_shares_against_resting_stop == \
        IBKR["reserves_shares_against_resting_stop"]


def test_the_builder_hands_the_reservation_fact_to_the_feed():
    """THE SEAM. A declared spec field nobody wires is the #574 shape — declared in the file, listed
    in the README, consulted by nothing. The builder must assign `feed._exec_reserves_shares` from
    the spec's own field; the class default (True: wait, refuse, keep the stop) covers only the node
    with no exec provider at all."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    tree = ast.parse(src)
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_exec_reserves_shares"
                and getattr(t.value, "id", None) == "feed" for t in n.targets)
    ]
    assert assigns, (
        "nothing assigns `feed._exec_reserves_shares`, so every venue is treated as reserving and "
        "staging-ibkr's exits keep waiting on a reservation that does not exist"
    )
    sources = [
        node.attr for a in assigns for node in ast.walk(a.value) if isinstance(node, ast.Attribute)
    ]
    assert "reserves_shares_against_resting_stop" in sources, (
        f"the wiring does not read the spec's declared field (reads {sources!r}) — a second "
        f"derivation of the venue fact is exactly what this field exists to prevent"
    )


def test_the_class_default_is_the_conservative_direction():
    """With no exec provider declared, the safe error is to WAIT: a venue wrongly treated as
    reserving refuses an exit (recoverable — the stop stays); the reverse submits into a live
    reservation and is rejected at the venue. This is the one place a default is genuinely as
    correct as a declaration, and this test is the comment that says so."""
    assert UiFeedStrategy._exec_reserves_shares is True
