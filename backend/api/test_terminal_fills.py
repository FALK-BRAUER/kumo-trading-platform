"""#807 item 4 — a fill arriving for an order the cache holds TERMINAL is the exact second the book
starts lying, and today it is one WARN line among 237,216 identical ones.

Nautilus 1.229 `execution/engine.pyx:1586` applies such a fill to the POSITION after the order refuses
it (`InvalidStateTrigger`), so the position plane diverges from the order plane with nothing on any
surface. This pins that the engine RECORDS it, that the health frame CARRIES it (the Pydantic-drop
class: #233/#322/#336), and that the alert plane SAYS it.

DRIVEN AT THE SEAM with production bytes: the order is PATH's real corpse replayed to REJECTED through
`OrderUnpacker`, the fill is its real 80-share live fill — both from `fixtures/repair_807`.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from api import cache_repair as cr
from api.observation import Observations

# READS A PRODUCTION CAPTURE (#1045): skipped by name where api/fixtures/repair_807/ is absent (public tree).
pytestmark = pytest.mark.captured_fixture("repair_807")

FIXTURES = Path(__file__).parent / "fixtures" / "repair_807"
PATH_CORPSE = "PROT-SELL-PATH-XNYS-b3ea77ee"
PATH_LANE = "PATH.XNYS-TECHIVOL-005"


@pytest.fixture(scope="module")
def image() -> cr.CacheImage:
    return cr.CacheImage.from_fixture(FIXTURES / "redis.json")


@pytest.fixture(scope="module")
def corpse_order(image):
    return cr.replay_order(image.orders[PATH_CORPSE])


@pytest.fixture(scope="module")
def live_fill(image):
    """The 80-share fill Alpaca delivered at 13:32:01 on 2026-09-04 — `reconciliation=False`."""
    fills = [cr.to_obj(e) for e in image.positions[PATH_LANE]]
    live = [f for f in fills if not f.reconciliation]
    assert len(live) == 1 and str(live[0].last_qty) == "80"
    return live[0]


def _node(order):
    """Duck-typed `self` for `UiFeedStrategy._publish_fill` — the REAL method runs; only what it reaches
    for is substituted, and the cache answers with a REAL Nautilus order."""
    from api.book_truth import InferredFills, TerminalFills

    obs = Observations()
    obs.declare("inferred_fills", "fills_on_terminal_orders")
    published: list[tuple[str, dict]] = []
    return types.SimpleNamespace(
        cache=types.SimpleNamespace(
            positions_open=lambda: [],
            order=lambda coid: order if str(coid) == str(order.client_order_id) else None,
        ),
        _observations=obs,
        _inferred_fills=InferredFills(),
        _terminal_fills=TerminalFills(),
        _safe_now=lambda: 0,
        _publish=lambda name, frame: published.append((name, frame)),
        _published=published,
    )


def test_fixture_the_order_is_terminal_and_the_fill_is_its_own(corpse_order, live_fill):
    assert corpse_order.status_string() == "REJECTED" and corpse_order.is_closed
    assert str(live_fill.client_order_id) == str(corpse_order.client_order_id)


def test_a_fill_on_a_REJECTED_order_is_recorded_with_the_orders_status(corpse_order, live_fill):
    from api.engine_node import UiFeedStrategy

    node = _node(corpse_order)
    UiFeedStrategy._publish_fill(node, live_fill)
    rows = node._terminal_fills.as_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["client_order_id"] == PATH_CORPSE
    assert row["order_status"] == "REJECTED"
    assert row["instrument_id"] == "PATH.XNYS" and row["strategy_id"] == "TECHIVOL-005"
    assert row["quantity"] == 80.0 and row["count"] == 1
    # The fill frame the UI reads still goes out — recording is beside the publish, not instead of it.
    assert [name for name, _ in node._published] == ["fill"]


def test_a_fill_on_an_ACCEPTED_order_is_not_a_finding(image, live_fill):
    """The same corpse REVIVED (what #809 does) is ACCEPTED; its fill is ordinary and records nothing."""
    from api.engine_node import UiFeedStrategy

    venue = cr.VenueTruth.from_fixture(FIXTURES / "venue.json")
    instrument = cr.to_obj(image.instruments["PATH.XNYS"])
    vo = venue.orders[PATH_CORPSE]
    resting = cr.VenueOrder(vo.client_order_id, "new", cr.Decimal(0), None, None, vo.venue_order_id)
    revived = cr.replay_order(cr.revive_order(image, PATH_CORPSE, resting, instrument).events)
    assert revived.status_string() == "ACCEPTED"  # fixture property: the contrast is real
    # THE MOMENT THE HOOK RUNS: Nautilus has already applied a legitimate fill to the order, so the
    # order plane carries the trade id. That is what a refused fill never gets (see the corpse test).
    revived.apply(live_fill)
    assert revived.status_string() == "PARTIALLY_FILLED" and live_fill.trade_id in revived.trade_ids
    node = _node(revived)
    UiFeedStrategy._publish_fill(node, live_fill)
    assert node._terminal_fills.as_rows() == []


def test_fixture_the_corpse_REFUSES_the_fill_so_its_trade_id_never_joins_the_order(corpse_order, live_fill):
    """The mechanism the detector keys on, pinned on the installed package: `Order.apply` raises
    `InvalidStateTrigger` for REJECTED -> PARTIALLY_FILLED, and the trade id stays absent."""
    from nautilus_trader.core.fsm import InvalidStateTrigger

    with pytest.raises(InvalidStateTrigger):
        corpse_order.apply(live_fill)
    assert live_fill.trade_id not in corpse_order.trade_ids


def test_an_ordinary_final_fill_on_a_FILLED_order_is_NOT_a_finding(image):
    """Nautilus applies the fill to the order BEFORE publishing it, so this hook sees `FILLED` on every
    complete fill (codex review). The real BUY 126 (`kumo-8b68…`) with its own fill must record nothing;
    what makes a corpse's fill a finding is that the ORDER never carried it."""
    from api.engine_node import UiFeedStrategy

    events = image.orders["kumo-8b68fc87e100dbcb6e07"]
    order = cr.replay_order(events)
    assert order.status_string() == "FILLED" and order.is_closed  # the shape that used to trip it
    own_fill = next(cr.to_obj(e) for e in events if cr.to_dict(e)["type"] == "OrderFilled")
    assert own_fill.trade_id in order.trade_ids
    node = _node(order)
    UiFeedStrategy._publish_fill(node, own_fill)
    assert node._terminal_fills.as_rows() == []


def test_the_health_frame_carries_the_rows_and_the_dropped_count():
    """Pin the producer: the frame reads `_terminal_fills.as_rows()` and `.dropped` — a registry that
    is never read is a detector that never fires."""
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy)
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert '"fills_on_terminal_orders": self._terminal_fills.as_rows()' in code
    assert '"fills_on_terminal_orders_dropped": self._terminal_fills.dropped' in code


def test_the_registry_coalesces_repeats_and_caps_its_size(corpse_order, live_fill):
    """PATH took five fills on one corpse; one row with count 5, not five rows. And a cap, counted."""
    from api.engine_node import UiFeedStrategy
    from api.book_truth import TerminalFills

    node = _node(corpse_order)
    for _ in range(5):
        UiFeedStrategy._publish_fill(node, live_fill)
    rows = node._terminal_fills.as_rows()
    assert len(rows) == 1 and rows[0]["count"] == 5 and rows[0]["quantity"] == 400.0
    reg = TerminalFills(cap=2)
    for i in range(4):
        reg.record(f"O-{i}", "REJECTED", "X.XNAS", "L", 1.0, ts_ns=i)
    assert len(reg.as_rows()) == 2 and reg.dropped == 2


def test_the_health_dto_declares_the_field_so_it_cannot_be_dropped():
    """The engine publishes it, `models.py` must DECLARE it (#233/#322/#336 — third and fourth time)."""
    from api.models import HealthResponse

    row = {"client_order_id": PATH_CORPSE, "order_status": "REJECTED", "count": 1}
    dto = HealthResponse(status="ok", subsystems=[], feed_last_tick_ts=0, fills_on_terminal_orders=[row],
                         fills_on_terminal_orders_dropped=3)
    assert dto.fills_on_terminal_orders == [row]
    assert dto.model_dump()["fills_on_terminal_orders"] == [row]
    assert dto.model_dump()["fills_on_terminal_orders_dropped"] == 3


def test_the_alert_plane_says_it_once_and_names_the_order():
    from api.alerts import AlertsService
    from api.notify import Alert, Notifier  # noqa: F401 — the real Alert type reaches the transport
    from api.test_alerts import ON, FakeTx

    rows = [{"client_order_id": PATH_CORPSE, "order_status": "REJECTED", "instrument_id": "PATH.XNYS",
             "strategy_id": "TECHIVOL-005", "quantity": 80.0, "count": 1, "ts_ns": 0}]
    node = types.SimpleNamespace(health=lambda: {"fills_on_terminal_orders": rows}, positions=lambda: [])
    tx = FakeTx()
    svc = AlertsService(node, notifier=Notifier(transport=tx, dedupe_backend="memory", settings=dict(ON)))
    asyncio.run(svc._announce_terminal_fills())
    assert tx.sent, "a fill on a terminal order raised no alert"
    said = " ".join(f"{a.title} {a.body}" for a in tx.sent)
    assert PATH_CORPSE in said and "REJECTED" in said and "PATH" in said
    assert any(a.critical for a in tx.sent)
    asyncio.run(svc._announce_terminal_fills())
    assert len(tx.sent) == 1, "the same finding paged twice"


def test_the_poll_loop_actually_calls_the_announcer():
    """A check that exists and is never called is what left eight shorts unannounced for nine days
    (#437's `short_violations`). Pin the WIRING, not the unit."""
    import inspect

    from api.alerts import AlertsService

    src = inspect.getsource(AlertsService.run)
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert "_announce_terminal_fills()" in code
    assert "_announce_short_violations()" in code  # the sibling it sits beside — same loop, same cadence
