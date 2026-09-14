"""#807 item 3 — an UNCLAIMED row the broker holds NONE of must say so, and must be marked from its
OWN position.

Measured 2026-09-09 05:48 UTC on paper: CRM 10, HALO 10, WDAY 11, PATH 192 rendered as "reconciled —
tap to move to a strategy" while the broker held 0, 0, 8 and 126; and every `mkt` on them carried the
SIGN of a different position (`-$2,484` on a LONG), because `_enrich_position_financials` kept one
position per INSTRUMENT and the EXTERNAL row was marked off the lane's mirrored short.

Three states, never two: `venue_qty` is the broker's quantity for the symbol, 0.0 when the broker
answered and holds none, None when the broker has not answered at all.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from nautilus_trader.model.identifiers import InstrumentId, StrategyId


class _Pos:
    """Shaped like the Nautilus Position where the two production methods touch it. `quantity` is
    UNSIGNED and `signed_qty` signed — Nautilus's real contract."""

    def __init__(self, instrument_id: str, strategy_id: str, signed_qty: float, avg_px: float):
        self.instrument_id = InstrumentId.from_str(instrument_id)
        self.strategy_id = StrategyId(strategy_id)  # a real one: `_position_origin` asks it `is_external()`
        self.signed_qty = signed_qty
        self.quantity = abs(signed_qty)
        self.avg_px_open = avg_px
        self.is_open = signed_qty != 0
        self.side = types.SimpleNamespace(name="LONG" if signed_qty > 0 else "SHORT")
        self.account_id = "ALPACA-x"
        self.realized_pnl = "0.00 USD"
        self.ts_last = 1
        self.opening_order_id = None  # `_position_origin` looks the opener up; None → not cockpit-placed


class _Row:
    """The POSITION row `_enrich_position_financials` mutates — with the identity fields production
    rows carry (`ExternalActivityDTO.strategy_id`, `side`)."""

    def __init__(self, instrument_id: str, strategy_id: str, side: str, avg_px: float, last_px: float):
        self.source = "POSITION"
        self.instrument_id = instrument_id
        self.strategy_id = strategy_id
        self.side = side
        self.avg_px = avg_px
        self.last_px = last_px
        self.market_value = None
        self.unrealized_pl = None
        self.unrealized_plpc = None
        self.basis_contested = None


# --------------------------------------------------------------------------------------------------
# C. Marked from its OWN position
# --------------------------------------------------------------------------------------------------


def _enrich(rows, positions, last_px):
    from api.engine_node import UiFeedStrategy

    node = types.SimpleNamespace(
        cache=types.SimpleNamespace(positions_open=lambda: positions),
        _last_close={p.instrument_id: last_px for p in positions},
        _venue_has_account=lambda venue: False,
        _marking_basis=lambda instrument_id, avg: (avg, False),
    )
    UiFeedStrategy._enrich_position_financials(node, rows)
    return rows


def test_fixture_the_mirror_pair_is_two_positions_on_one_instrument():
    ext = _Pos("CRM.XNYS", "EXTERNAL", +10.0, 250.33)
    lane = _Pos("CRM.XNYS", "TECHIVOL-005", -10.0, 250.505)
    assert ext.instrument_id == lane.instrument_id and ext.signed_qty == -lane.signed_qty


def test_the_EXTERNAL_row_is_marked_from_the_EXTERNAL_position_not_the_lanes_short():
    """The live CRM numbers: EXTERNAL LONG 10 @ 250.33, TECHIVOL SHORT 10 @ 250.505, last 248.40.
    The LONG row's market value is +2,484.00 and its P&L −19.30; the short's is the mirror image."""
    ext = _Pos("CRM.XNYS", "EXTERNAL", +10.0, 250.33)
    lane = _Pos("CRM.XNYS", "TECHIVOL-005", -10.0, 250.505)
    ext_row = _Row("CRM.XNYS", "EXTERNAL", "LONG", 250.33, 248.40)
    lane_row = _Row("CRM.XNYS", "TECHIVOL-005", "SHORT", 250.505, 248.40)
    # Positions listed SHORT-last, the order that put the short's sign on the long's row.
    _enrich([ext_row, lane_row], [ext, lane], 248.40)
    assert ext_row.market_value == pytest.approx(+2484.00, abs=0.01), ext_row.market_value
    assert ext_row.unrealized_pl == pytest.approx(-19.30, abs=0.01)
    assert ext_row.avg_px == pytest.approx(250.33)
    assert lane_row.market_value == pytest.approx(-2484.00, abs=0.01)
    assert lane_row.unrealized_pl == pytest.approx(+21.05, abs=0.01)
    assert lane_row.avg_px == pytest.approx(250.505)


def test_a_row_with_no_matching_position_is_left_unmarked_not_borrowed():
    """A third row on the instrument with a lane nobody holds gets NOTHING — not the neighbour's numbers."""
    ext = _Pos("CRM.XNYS", "EXTERNAL", +10.0, 250.33)
    stray = _Row("CRM.XNYS", "MOMENTUM-002", "LONG", 1.0, 248.40)
    _enrich([stray], [ext], 248.40)
    assert stray.market_value is None and stray.unrealized_pl is None


# --------------------------------------------------------------------------------------------------
# B. The broker's quantity rides on the row
# --------------------------------------------------------------------------------------------------


def _publish_external(positions, broker_qty):
    from api.engine_node import UiFeedStrategy

    frames = []
    node = types.SimpleNamespace(
        _exec_client_id="ALPACA",
        _trade_cycles={},
        id="MANUAL-001",
        cache=types.SimpleNamespace(
            positions_open=lambda: positions,
            orders_open=lambda: [],
            position=lambda pid: None,
            order=lambda coid: None,
        ),
        _broker_qty=broker_qty,
        _enrich_position_financials=lambda rows: None,
        _publish=lambda name, frame: frames.append((name, frame)),
        clock=types.SimpleNamespace(timestamp_ns=lambda: 0),
        log=types.SimpleNamespace(error=lambda msg: (_ for _ in ()).throw(AssertionError(msg))),
    )
    UiFeedStrategy._publish_external(node)
    assert frames and frames[0][0] == "external_activity"
    return {r["instrument_id"]: r for r in frames[0][1]["external"] if r["source"] == "POSITION"}


def test_a_phantom_external_row_carries_venue_qty_ZERO_when_the_broker_answered():
    rows = _publish_external([_Pos("CRM.XNYS", "EXTERNAL", +10.0, 250.33)], broker_qty={"PATH": 126.0})
    assert rows["CRM.XNYS"]["venue_qty"] == 0.0


def test_a_backed_external_row_carries_the_brokers_quantity():
    rows = _publish_external([_Pos("PATH.XNYS", "EXTERNAL", +192.0, 15.78)], broker_qty={"PATH": 126.0})
    assert rows["PATH.XNYS"]["venue_qty"] == 126.0


def test_a_dotted_symbol_keys_on_the_whole_symbol_not_its_first_token():
    """BRK.B.XNYS: the venue never carries a dot, the symbol can. `split(".")[0]` would ask the broker
    about "BRK" and read BRK.B as a phantom (codex review)."""
    rows = _publish_external([_Pos("BRK.B.XNYS", "EXTERNAL", +3.0, 400.0)], broker_qty={"BRK.B": 3.0})
    assert rows["BRK.B.XNYS"]["venue_qty"] == 3.0


def test_no_broker_answer_yet_is_None_not_zero():
    """Absence must not be readable as "holds none" — a boot before the first reconcile poll."""
    rows = _publish_external([_Pos("CRM.XNYS", "EXTERNAL", +10.0, 250.33)], broker_qty=None)
    assert rows["CRM.XNYS"]["venue_qty"] is None


def test_the_dto_declares_venue_qty():
    from api.models import ExternalActivityDTO

    dto = ExternalActivityDTO(account_id="a", client_id="c", instrument_id="CRM.XNYS", source="POSITION",
                              strategy_id="EXTERNAL", origin="RECONCILIATION", side="LONG", quantity=10.0,
                              ts_last=0, venue_qty=0.0)
    assert dto.model_dump()["venue_qty"] == 0.0
    assert ExternalActivityDTO.model_fields["venue_qty"].default is None


# --------------------------------------------------------------------------------------------------
# B. The exec client publishes the broker's map on the reconcile snapshot
# --------------------------------------------------------------------------------------------------


def _drift_snapshot(broker_positions, cache_positions):
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    published = []

    async def list_positions():
        return broker_positions

    client = types.SimpleNamespace(
        _http=types.SimpleNamespace(list_positions=list_positions),
        _cache=types.SimpleNamespace(positions_open=lambda: cache_positions),
        _msgbus=types.SimpleNamespace(publish=lambda topic, msg: published.append((topic, msg))),
        _clock=types.SimpleNamespace(timestamp_ns=lambda: 7),
        _unrealized_totals={},
    )
    asyncio.run(AlpacaExecutionClient._report_reconcile_drift(client))
    assert len(published) == 1 and published[0][0] == "broker.reconcile"
    return published[0][1]


def test_the_reconcile_snapshot_carries_the_brokers_per_symbol_quantity():
    broker = [
        {"symbol": "PATH", "qty": "126", "side": "long", "unrealized_pl": "1.0", "unrealized_intraday_pl": "0.5"},
        {"symbol": "WDAY", "qty": "8", "side": "long", "unrealized_pl": "1.0", "unrealized_intraday_pl": "0.5"},
    ]
    cache = [_Pos("PATH.XNYS", "EXTERNAL", +192.0, 15.78), _Pos("PATH.XNYS", "TECHIVOL-005", -192.0, 15.78)]
    snap = _drift_snapshot(broker, cache)
    assert snap["broker_qty"] == {"PATH": 126.0, "WDAY": 8.0}
    assert snap["drift"] == [{"symbol": "PATH", "broker_qty": 126.0, "cockpit_qty": 0.0},
                             {"symbol": "WDAY", "broker_qty": 8.0, "cockpit_qty": 0.0}]


def test_a_short_at_the_broker_is_negative_in_the_map():
    snap = _drift_snapshot([{"symbol": "X", "qty": "5", "side": "short", "unrealized_pl": "0", "unrealized_intraday_pl": "0"}], [])
    assert snap["broker_qty"] == {"X": -5.0}


def test_an_empty_book_is_an_empty_map_not_a_missing_one():
    """{} means the broker answered and holds nothing; the ENGINE turns a missing key into None."""
    snap = _drift_snapshot([], [])
    assert snap["broker_qty"] == {}


def test_the_engine_reads_the_map_off_the_snapshot_and_None_when_it_is_absent():
    from api.engine_node import UiFeedStrategy

    node = types.SimpleNamespace(_reconcile_drift=[], _broker_qty=None)
    UiFeedStrategy._on_broker_reconcile(node, {"drift": [], "broker_qty": {"PATH": 126.0}, "ts": 1})
    assert node._broker_qty == {"PATH": 126.0}
    UiFeedStrategy._on_broker_reconcile(node, {"drift": [], "ts": 2})  # an older producer, or IBKR
    assert node._broker_qty is None, "a snapshot without the map must not be read as 'holds none'"
