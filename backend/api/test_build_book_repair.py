"""The seam #771 was missing: a node method that PLANS the repair from its own book (#771).

WHY A SEAM TEST AND NOT UNIT TESTS. `plan_contra_closes` and `prepare_execution` were both correct
and both fully tested, and the repair still could not run on any node — because NOTHING CALLED THEM.
A green helper says nothing about whether anything invokes it correctly, which is the shape that
broke production five times on 2026-08-14. So these drive `build_book_repair` itself.

NO pytest-asyncio IN THIS REPO, so the coroutine is driven with `asyncio.run` explicitly rather
than through a marker that would silently not run (`needs_services` tests error out and nothing
reports it).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from api.engine_node import UiFeedStrategy


@dataclass
class _Report:
    """Shaped from a Nautilus PositionStatusReport. `signed_decimal_qty` is the field the production
    read uses (book_truth.py:187) — a double carrying `quantity` instead would test nothing."""
    instrument_id: str
    signed_decimal_qty: float


@dataclass
class _Pos:
    instrument_id: str
    strategy_id: str
    signed_qty: float
    avg_px: float
    pos_id: str
    is_open: bool = True
    ts_opened: int = 1_000

    @property
    def id(self):
        return self.pos_id

    @property
    def avg_px_open(self):
        return self.avg_px

    @property
    def side(self):
        class _S:
            name = "LONG" if self.signed_qty > 0 else "SHORT"
        return _S()


class _Order:
    def __init__(self, iid):
        self.instrument_id = iid


class _Cache:
    def __init__(self, positions, orders=(), instruments=()):
        self._p, self._o, self._i = positions, orders, instruments

    def positions_open(self):
        return list(self._p)

    def positions_closed(self):
        return []

    def orders_open(self):
        return list(self._o)

    def instrument_ids(self):
        return list(self._i)


class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, msg, *a):
        self.lines.append(str(msg))

    warning = info = error = debug = _rec

    def exception(self, msg, *a):
        self.lines.append(str(msg))


class _Node:
    """Only the surface `build_book_repair` touches, bound to the REAL method."""

    build_book_repair = UiFeedStrategy.build_book_repair

    def __init__(self, positions, reports, orders=()):
        self.cache = _Cache(positions, orders, [p.instrument_id for p in positions])
        self.log = _Log()
        self._reports = reports
        self.ran = []

    async def _venue_position_reports(self):
        return self._reports

    def run_book_repair(self, execution_plan, *, arm=False):
        self.ran.append((execution_plan, arm))
        return {"booked": 0, "refused": len(execution_plan.refused)}


def _offset_book(inst="MRVL.XNAS"):
    """The live paper shape: lane long vs EXTERNAL short, equal size, identical basis, net ZERO."""
    return [
        _Pos(inst, "QC345-003", 14.0, 224.55, f"{inst}-QC345-003"),
        _Pos(inst, "EXTERNAL", -14.0, 224.55, f"{inst}-EXTERNAL"),
    ]


def test_the_fixture_is_invisible_to_the_venue():
    """FIXTURE PROPERTY FIRST. If the pair did not net to zero, a venue reporting only non-zero
    positions would still mention it, the completeness flag would never bind, and every assertion
    below would pass with the mechanism removed."""
    assert sum(p.signed_qty for p in _offset_book()) == 0.0


def test_an_unreadable_venue_REFUSES_and_never_reaches_the_booking_step():
    """None is unreadable, not flat. The repair must not be planned against a book it cannot see —
    and must not quietly return a clean-looking zero either."""
    node = _Node(_offset_book(), reports=None)
    out = asyncio.run(node.build_book_repair())
    assert node.ran == [], "run_book_repair was reached on an unreadable venue"
    assert "venue unreadable" in out["error"]
    assert any("REFUSING to plan" in line for line in node.log.lines)


def test_a_complete_snapshot_PLANS_the_offset_pair_end_to_end():
    """The whole point: an empty report LIST is a complete snapshot, so the pair the venue never
    mentions is planned and handed to the booking step."""
    node = _Node(_offset_book(), reports=[])
    out = asyncio.run(node.build_book_repair())
    assert out["planned"] == 1
    assert out["not_planned"] == 0
    assert len(node.ran) == 1
    execution_plan, arm = node.ran[0]
    assert arm is False, "must default to report-only"
    assert len(execution_plan.orders) == 1
    order = execution_plan.orders[0]
    assert order.instrument_id == "MRVL.XNAS"
    assert {leg.position_id for leg in order.legs} == {
        "MRVL.XNAS-QC345-003", "MRVL.XNAS-EXTERNAL"}


def test_the_venue_quantity_is_read_from_signed_decimal_qty():
    """The production field, not `quantity`. A venue that DOES report the instrument must contradict
    the cache and refuse — which only happens if the number was actually read."""
    node = _Node(_offset_book(), reports=[_Report("MRVL.XNAS", 99.0)])
    out = asyncio.run(node.build_book_repair())
    assert out["planned"] == 0
    assert out["not_planned"] == 1


def test_working_orders_are_handed_over_as_INSTRUMENT_IDS():
    """`_validated_working` RAISES on a bare symbol rather than never matching. Passing `MRVL` here
    instead of `MRVL.XNAS` would take the whole repair down — the #239 guard failing OPEN is what
    that validator exists to prevent, so the seam must speak the position book's vocabulary."""
    node = _Node(_offset_book(), reports=[], orders=[_Order("MRVL.XNAS")])
    out = asyncio.run(node.build_book_repair())
    # Not planned — a resting order is working on it, which is the #239 refusal, NOT a crash.
    assert out["planned"] == 0
    assert out["not_planned"] == 1
    assert any("resting order" in line for line in node.log.lines)


def test_arm_is_forwarded_rather_than_swallowed():
    node = _Node(_offset_book(), reports=[])
    asyncio.run(node.build_book_repair(arm=True))
    assert node.ran[0][1] is True


# --------------------------------------------------------------------------------------------
# The COMMAND seam: how a repair is actually triggered on a running node (#771)
# --------------------------------------------------------------------------------------------


class _CmdNode:
    """The command handler bound to a stub `build_book_repair`, so these test DISPATCH and ARMING
    rather than re-testing the planner."""

    _handle_book_repair_command = UiFeedStrategy._handle_book_repair_command

    def __init__(self, result=None):
        self.log = _Log()
        self.calls = []
        self.pairs = []
        self._result = result or {"planned": 1, "not_planned": 0, "booked": 0, "refused": 0}

    async def build_book_repair(self, *, arm=False, pair=None):
        # MIRRORS PRODUCTION'S SIGNATURE. A double that cannot accept what the caller passes would
        # fail for the wrong reason and hide whether the gate under test works.
        self.calls.append(arm)
        self.pairs.append(pair)
        return self._result


def test_the_repair_command_is_REPORT_ONLY_unless_arm_is_sent():
    node = _CmdNode()
    assert asyncio.run(node._handle_book_repair_command("c1", {})) == ("ok", "")
    assert node.calls == [False]


def test_arm_must_be_the_BOOLEAN_true_not_a_truthy_string():
    """`bool(payload.get("arm"))` would arm on the STRING "false" — the value a hand-written JSON
    payload most easily produces. Every gate in this repo defaults off, and a gate a typo can open
    is not a gate."""
    node = _CmdNode()
    for value in ("false", "true", 1, "1", [], None):
        node.calls.clear()
        asyncio.run(node._handle_book_repair_command("c", {"arm": value}))
        assert node.calls == [False], f"arm={value!r} must NOT arm the repair"
    node.calls.clear()
    asyncio.run(node._handle_book_repair_command("c", {"arm": True}))
    assert node.calls == [True]


def test_an_unreadable_venue_comes_back_as_an_ERROR_ack_not_a_clean_one():
    """The refusal must reach the operator as an error. A repair that planned nothing because the
    venue could not be read looks exactly like one with nothing to do."""
    node = _CmdNode(result={"error": "venue unreadable — repair not planned"})
    status, error = asyncio.run(node._handle_book_repair_command("c2", {}))
    assert status == "error"
    assert "venue unreadable" in error


def test_an_EVICTION_is_counted_separately_from_pairs_in_the_operator_reply():
    """#779. `planned` counts pairs; merging evictions into it would hide WHICH mechanism ran, and a
    reply reading `planned=0 booked=1` reads as a repair nobody planned."""
    inst = "GMAB.XNAS"
    book = [
        _Pos(inst, "BCTROT-004", 59.0, 33.41, f"{inst}-BCTROT-004"),
        _Pos(inst, "MOMENTUM-002", 59.0, 33.94, f"{inst}-MOMENTUM-002"),
        _Pos(inst, "EXTERNAL", -59.0, 33.68, f"{inst}-EXTERNAL"),
    ]
    node = _Node(book, reports=[])
    out = asyncio.run(node.build_book_repair())
    assert out["planned"] == 0, "an eviction is not a pair"
    assert out["evictions"] == 1
    assert len(node.ran) == 1
    execution_plan, _arm = node.ran[0]
    assert len(execution_plan.orders) == 1
    assert len(execution_plan.orders[0].legs) == 3


def test_an_OPERATOR_DIRECTED_pair_reaches_the_booking_path():
    """#784. HALO is what the automatic planner refuses; naming the pairing must produce exactly one
    order with the two legs the operator chose, and nothing else."""
    inst = "HALO.XNAS"
    book = [
        _Pos(inst, "BCTROT-004", 55.0, 106.42, f"{inst}-BCTROT-004"),
        _Pos(inst, "MOMENTUM-002", 1.0, 104.35, f"{inst}-MOMENTUM-002"),
        _Pos(inst, "EXTERNAL", -37.0, 111.78, f"{inst}-EXTERNAL"),
    ]
    node = _Node(book, reports=[_Report(inst, 19.0)])
    out = asyncio.run(node.build_book_repair(pair={
        "instrument_id": inst, "long_strategy": "BCTROT-004",
        "short_strategy": "EXTERNAL", "quantity": 37.0,
    }))
    assert out["planned"] == 1
    execution_plan, arm = node.ran[0]
    assert arm is False
    assert len(execution_plan.orders) == 1
    order = execution_plan.orders[0]
    assert {(l.strategy_id, l.side, l.quantity) for l in order.legs} == {
        ("BCTROT-004", "SELL", 37.0), ("EXTERNAL", "BUY", 37.0)}
    assert any("OPERATOR-DIRECTED" in line for line in node.log.lines)


def test_WITHOUT_a_named_pair_the_same_book_is_still_REFUSED():
    """The automatic refusal is not relaxed by the feature existing. Killed by routing the automatic
    path through the operator planner."""
    inst = "HALO.XNAS"
    book = [
        _Pos(inst, "BCTROT-004", 55.0, 106.42, f"{inst}-BCTROT-004"),
        _Pos(inst, "MOMENTUM-002", 1.0, 104.35, f"{inst}-MOMENTUM-002"),
        _Pos(inst, "EXTERNAL", -37.0, 111.78, f"{inst}-EXTERNAL"),
    ]
    node = _Node(book, reports=[_Report(inst, 19.0)])
    out = asyncio.run(node.build_book_repair())
    assert out["planned"] == 0 and out["evictions"] == 0
    assert out["not_planned"] == 1


def test_the_COMMAND_forwards_the_operator_pair_rather_than_dropping_it():
    """THE SEAM. The other operator test drives `build_book_repair` directly, so it passes even when
    the command never reads `payload["pair"]` — measured: that mutation survived. The command is what
    an operator actually uses, so it is what must be pinned.

    Killed by `pair = None` in `_handle_book_repair_command`.
    """
    node = _CmdNode()
    spec = {"instrument_id": "HALO.XNAS", "long_strategy": "BCTROT-004",
            "short_strategy": "EXTERNAL", "quantity": 37.0}
    asyncio.run(node._handle_book_repair_command("c9", {"pair": spec}))
    assert node.pairs == [spec], f"the command dropped the operator's pairing: {node.pairs}"


def test_a_command_with_no_pair_asks_for_the_AUTOMATIC_sweep():
    """Absent must mean the ordinary sweep, and the two must not be confusable."""
    node = _CmdNode()
    asyncio.run(node._handle_book_repair_command("c10", {}))
    assert node.pairs == [None]
