"""Step 3 of #641: broker reads must not gate on the ALPACA REST client.

`self._http` is an `AlpacaHttpClient` built ONLY when `data_provider == "alpaca"`
(engine_node.py:993). ibkr-paper-retired has none — so every `if self._http is None: return` was a whole
subsystem silently OFF on IBKR while every surface said RUNNING: the protection reconciler never read
the broker, the reducing-order read answered UNKNOWN forever, and exits died waiting on a share
reservation the venue does not even have.

The venue-neutral path already exists: `generate_order_status_reports` and
`generate_position_status_reports` are Nautilus's own reads, implemented by the shipped Interactive
Brokers adapter (execution.py:532 / :830) and by our Alpaca client alike. These tests pin that the
gates now sit on THAT source, with the REST read as the loudly-labelled Alpaca-only fallback.

Every test here was seen RED against the `_http is None` gates before the fix existed.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import ClassVar

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Quantity

from api.engine_node import UiFeedStrategy

# --------------------------------------------------------------------------------------------------
# Doubles built from what production ACTUALLY emits: typed reports come out of the real Alpaca
# parser (the same one the exec client uses), and position reports are real Nautilus objects — a
# hand-invented report shape could not fail where the real one does.
# --------------------------------------------------------------------------------------------------


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
        _symbol_to_id: ClassVar[dict] = {
            "AEM": InstrumentId.from_str("AEM.XNYS"),
            "WPM": InstrumentId.from_str("WPM.XNYS"),
        }

        def _client_order_id_for(self, raw):
            return ClientOrderId(raw["client_order_id"])

    out = [AlpacaExecutionClient._parse_order_report(_Host(), r) for r in rows]
    assert all(r is not None for r in out), "fixture rows must all parse — a dropped row tests nothing"
    return out


def _row(coid, venue_id, *, symbol="AEM", qty="10", side="sell", otype="stop", status="new"):
    return {"id": venue_id, "client_order_id": coid, "symbol": symbol, "side": side,
            "type": otype, "status": status, "qty": qty, "filled_qty": "0", "stop_price": "50"}


def _pos_report(iid="AEM.XNYS", side=PositionSide.LONG, qty="54", avg="181.9"):
    return PositionStatusReport(
        account_id=AccountId("IB-DUTEST001"),   # an IBKR-shaped account: the venue under test
        instrument_id=InstrumentId.from_str(iid),
        position_side=side,
        quantity=Quantity.from_str(qty),
        report_id=UUID4(),
        ts_last=0,
        ts_init=0,
        avg_px_open=None if avg is None else Decimal(avg),
    )


class _ExecClient:
    """The registered execution client, answering BOTH typed reads — as the IB adapter does."""

    def __init__(self, order_reports=(), position_reports=()):
        self.order_reports = list(order_reports)
        self.position_reports = list(position_reports)

    async def generate_order_status_reports(self, _cmd):
        return list(self.order_reports)

    async def generate_position_status_reports(self, _cmd):
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
    def __init__(self, ts_ns=0):
        self._ts = ts_ns

    def timestamp_ns(self):
        return self._ts


def _host(exec_client, http=None):
    class _H:
        pass

    h = _H()
    h._exec_client, h._http, h.log, h.clock = exec_client, http, _Log(), _Clock()
    h._venue_order_reports = UiFeedStrategy._venue_order_reports.__get__(h)
    h._venue_position_reports = UiFeedStrategy._venue_position_reports.__get__(h)
    h._record_book_truth = UiFeedStrategy._record_book_truth.__get__(h)
    h._trader_id_str = UiFeedStrategy._trader_id_str.__get__(h)
    return h


# --------------------------------------------------------------------------------------------------
# _venue_reducing_orders: the typed path was already primary — the `_http is None` gate above it
# refused to even TRY on the one stack (ibkr-paper-retired) where the typed path is the only path.
# --------------------------------------------------------------------------------------------------


def test_reducing_orders_are_read_TYPED_when_there_is_no_rest_client():
    """The ibkr-paper-retired shape: an exec client, no AlpacaHttpClient. The venue holds one resting
    reducing order; the read must return it rather than answering UNKNOWN forever."""
    h = _host(_ExecClient(order_reports=_order_reports([_row("a", "v1")])), http=None)
    got = asyncio.run(UiFeedStrategy._venue_reducing_orders(h, "AEM.XNYS", "sell"))
    assert got is not None, (
        "an IBKR node (no REST client) answered UNKNOWN with a working execution client wired — "
        "the `_http is None` gate is refusing a typed path that resolves"
    )
    assert len(got) == 1 and got[0]["_remaining"] == 10.0, got


def test_no_reader_at_all_is_UNKNOWN_and_SAYS_SO():
    """No exec client AND no REST client: None is right, silence is not. A read that cannot happen
    must name the missing input rather than degrade indistinguishably from 'nothing resting'."""
    h = _host(None, http=None)
    got = asyncio.run(UiFeedStrategy._venue_reducing_orders(h, "AEM.XNYS", "sell"))
    assert got is None
    assert h.log.warnings, (
        "no execution client and no REST client, and the read said nothing — a permanently "
        "unreadable venue must be loud, or it reads exactly like a healthy empty book"
    )


# --------------------------------------------------------------------------------------------------
# _reconcile_protection_inner: it awaited `self._http.list_positions()` BEFORE the typed order read,
# so the whole reconciler was dead on IBKR however well the typed order path worked.
# --------------------------------------------------------------------------------------------------


def _reconciler_fake(monkeypatch, *, exec_client, http):
    from api.test_protection_reconciler import _Fake, _ns, _settings

    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=http)
    fake._exec_client = exec_client
    # Bind the real typed position accessor exactly as _Fake binds the order one — if the class
    # binding is added later this line is redundant, never wrong.
    fake._venue_position_reports = UiFeedStrategy._venue_position_reports.__get__(fake)
    fake._record_book_truth = UiFeedStrategy._record_book_truth.__get__(fake)
    fake._trader_id_str = UiFeedStrategy._trader_id_str.__get__(fake)
    # PRODUCTION'S CACHE KNOWS WHO HOLDS THE POSITION, and the dispatch now REFUSES to arm a stop it
    # cannot stamp with an owner (#748) — an unstamped stop carries MANUAL-001, its fill resolves to
    # a position that never existed, and the sale never reaches the cache. This double drives the
    # TYPED venue read with no REST client, so nothing seeds the book from broker rows; without this
    # the test would fail on the missing stamp rather than on the seam it exists to pin.
    from api.test_protection_reconciler import _pos_in_cache

    fake.cache._positions = [_pos_in_cache("AEM.XNYS", "MOMENTUM-002", 54.0)]
    return fake


def _run(fake):
    asyncio.run(UiFeedStrategy._reconcile_protection(fake))


def test_the_protection_reconciler_runs_from_TYPED_positions_with_no_rest_client(monkeypatch):
    """THE SEAM, driven end to end: exec client present, `_http` None, one naked long at the venue.
    The reconciler must read positions typed, derive the row, and rest a stop — today it returns at
    the `_http is None` gate and staging's book is permanently unprotected."""
    fake = _reconciler_fake(
        monkeypatch,
        exec_client=_ExecClient(position_reports=[_pos_report()]),
        http=None,
    )
    _run(fake)
    assert fake.built, (
        "a naked 54-share long was visible in the typed position report and the reconciler placed "
        "nothing — the position read still gates on the Alpaca REST client"
    )
    assert fake.built[0]["quantity"] == 54.0, fake.built
    # The broker's cost basis must survive the migration: it feeds the tiles and used to be read
    # from Alpaca's `avg_entry_price`; the typed report states the same fact as `avg_px_open`.
    assert fake._broker_avg_entry == {"AEM.XNYS": 181.9}, fake._broker_avg_entry


def test_the_reconciler_refuses_LOUDLY_when_nothing_can_read_the_broker(monkeypatch):
    """Enabled, and yet NO source of broker truth exists (no exec client, no REST client). Placing
    nothing is right; placing nothing SILENTLY is the #298 failure — a subsystem that is off reads
    exactly like a subsystem with nothing to do."""
    fake = _reconciler_fake(monkeypatch, exec_client=None, http=None)
    _run(fake)
    assert not fake.built and not fake.submitted
    assert any("no execution client" in w and "REST" in w for w in fake.log.warnings), (
        f"protection is enabled and completely blind, and said nothing: {fake.log.warnings!r}"
    )


def test_the_typed_and_rest_position_sources_produce_the_SAME_rows(monkeypatch):
    """Verification by disagreement: one book, both sources, identical rows into `plan_protection`.

    The Alpaca row states `market_value`; the typed report cannot (PositionStatusReport carries no
    such field — see test_broker_read_gaps.py), so the typed path derives it as signed qty x last
    price. The fixture price is chosen so the broker's stated 5400.0 IS 54 x 100 — the two paths must
    then agree exactly, and a sign or side slip in either shows up as a mismatch.
    """
    from api.protection import broker_rows, position_rows_from_reports

    typed = position_rows_from_reports(
        [_pos_report(), _pos_report(iid="WPM.XNYS", side=PositionSide.SHORT, qty="88", avg=None)],
        lambda iid: {"AEM.XNYS": 100.0, "WPM.XNYS": 60.0}.get(iid),
    )
    rest, unresolved = broker_rows(
        [
            {"symbol": "AEM", "qty": "54", "market_value": "5400.0", "side": "long"},
            {"symbol": "WPM", "qty": "-88", "market_value": "-5280.0", "side": "short"},
        ],
        lambda s: f"{s}.XNYS",
    )
    assert unresolved == []
    assert typed == rest, f"typed rows {typed!r} != REST rows {rest!r}"


def test_a_position_with_no_last_price_is_KEPT_with_zero_market_value():
    """A missing price must not drop the position from the audit — `plan_protection` already refuses
    that leg as `no_price`, and a dropped row is a position the audit implicitly calls protected.
    Zero notional on the refusal understates exposure; absence would misstate protection."""
    from api.protection import position_rows_from_reports

    rows = position_rows_from_reports([_pos_report()], lambda iid: None)
    assert len(rows) == 1
    assert rows[0]["market_value"] == 0.0
    assert rows[0]["quantity"] == 54.0


def test_a_flat_report_row_produces_no_position_row():
    """Adapters may report FLAT rows; a zero-quantity 'position' must not reach the planner where it
    would key a leg and dilute the covered/naked accounting."""
    from api.protection import position_rows_from_reports

    rows = position_rows_from_reports(
        [_pos_report(side=PositionSide.FLAT, qty="0", avg=None)], lambda iid: 100.0
    )
    assert rows == []


# --------------------------------------------------------------------------------------------------
# _refresh_realized_periods: NOT ported (the activities ledger has no Nautilus equivalent — see
# test_broker_read_gaps.py). But cosmetic-and-off must be LOUD, not bare-return.
# --------------------------------------------------------------------------------------------------


def test_realized_periods_degradation_NAMES_the_venue_that_cannot_supply_activities():
    from nautilus_trader.model.identifiers import ClientId

    class _H:
        pass

    h = _H()
    h._http, h.log, h._exec_client_id = None, _Log(), ClientId("INTERACTIVE_BROKERS")
    h._realized_periods_degraded = None
    asyncio.run(UiFeedStrategy._refresh_realized_periods(h))
    assert any("INTERACTIVE_BROKERS" in w for w in h.log.warnings), (
        f"multi-period realized P&L is silently off — the degraded state must say WHICH venue has no "
        f"activity ledger this engine can read, got {h.log.warnings!r}"
    )
    # Once per process, not once per sweep: this is a permanent property of the stack, and a warning
    # every 60s about a known, accepted degradation is the cry-wolf alarm operators learn to skip.
    before = len(h.log.warnings)
    asyncio.run(UiFeedStrategy._refresh_realized_periods(h))
    assert len(h.log.warnings) == before, "the degradation warning repeats every sweep — cry-wolf"
