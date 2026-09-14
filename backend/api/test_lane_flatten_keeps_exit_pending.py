"""A lane flatten whose cancel-confirm times out must QUEUE the close, never strand the position (#646).

THE SEQUENCE THAT STRANDS
-------------------------
The flatten path is cancel -> CONFIRM -> close, and the cancel-first order is forced by the venue: on a
venue that reserves shares against resting stops (Alpaca), the close is rejected on `available: 0` while
the protection rests, which is how five protective stops were lost on 2026-08-12. So by the time the
confirm WAIT times out, the cancels have already flown and cannot be recalled.

For that exact case the handler queues the close as a `deferred_flatten` manager — the exit stays pending
and replays until it completes. Except `_reserve_attach_command` categorically refused any
`strategy_id != MANUAL-001`. So for any LANE position (MOMENTUM-002, BCTROT-004, ...) the fallback was
structurally unreachable:

    stops cancelled or in flight, NO queued close, and the operator shown
    "this engine process cannot attach a manager for strategy 'MOMENTUM-002'"

which reads as a routing problem, not "your protection is gone and nothing is closing this". The
off-hours QUEUE branch failed identically, so deferred flatten for lane positions did not exist at all.

WHY THESE TESTS DRIVE THE REAL ENTRY POINT
------------------------------------------
The defect is pure WIRING: `_handle_flatten_command`'s timeout branch calling an attach gate that
refuses the very strategy ids the flatten path itself routes for. Every unit involved was individually
correct. So the tests bind the REAL `_handle_flatten_command`, the REAL `_handle_attach_manager` and the
REAL `_reserve_attach_command` (the guard under test) to a duck-typed host, stubbing only the DB-commit
half and the venue waits — the same discipline as `test_exit_release.py`.

Nautilus-native check (mandated): `Strategy.close_position` (strategy.pyx:1351) builds a plain closing
MarketOrder and submits with `position_id=position.id` — it cancels NOTHING and links nothing. The
OrderManager behind `manage_contingent_orders` acts only on orders created with
`contingency_type != NO_CONTINGENCY` and `linked_order_ids` set at CREATION (execution/manager.pyx:332+);
our protective stops are standalone (`NO_CONTINGENCY`), and Nautilus offers no way to link a resting
order to a new close after the fact. So cancel-then-close stands, and the fix is the fallback: the
queued close must be REACHABLE for lane positions, and a failure after the cancels flew must be LOUD.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from api.command_ledger import Reserve, ReserveResult
from api.engine_node import UiFeedStrategy, _DeferredFlatten

# Wednesday 2026-08-26 15:00 ET — regular hours, so decide() says EXECUTE and the cancel branch runs.
_RTH_NS = int(datetime(2026, 8, 26, 15, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1e9)


class _Id:
    def __init__(self, value: str):
        self.value = value

    def __str__(self) -> str:
        return self.value


class _Side:
    def __init__(self, name: str):
        self.name = name


class _Position:
    def __init__(self, instrument_id: str, strategy_id: str):
        self.instrument_id = _Id(instrument_id)
        self.strategy_id = _Id(strategy_id)
        self.side = _Side("LONG")
        self.quantity = 136
        self.id = _Id(f"{instrument_id}-{strategy_id}")
        self.account_id = _Id("ALPACA-001")


class _Clock:
    def timestamp_ns(self) -> int:
        return _RTH_NS


class _Ledger:
    """First sight for every id — the reserve half must run for REAL so the guard inside it is what the
    test exercises, not a stub standing where the bug lives."""

    def __init__(self):
        self.reserved: list[str] = []

    async def reserve(self, cid, ctype, coid, phash, entry_id):
        self.reserved.append(cid)
        return ReserveResult(Reserve.RESERVED)

    async def mark(self, cid, status, error=None):
        return None


class _Owner:
    """The registered sibling strategy instance — what `register_strategy` records."""

    def __init__(self):
        self.closed: list[tuple[object, list]] = []

    def close_position(self, position, tags=None, time_in_force=None):  # what the REAL signature accepts (#924)
        self.closed.append((position, tags or []))


class _Cache:
    def positions_open(self):
        return []


class _Host:
    """Duck-typed `self` for the REAL flatten + attach chain. Anything it does not declare raises,
    rather than silently passing (house rule: the double must reject what production rejects)."""

    def __init__(self, *, ledger=None):
        self.id = _Id("MANUAL-001")
        self.clock = _Clock()
        self.cache = _Cache()
        self._orders_armed = True
        self._exec_client_id = _Id("ALPACA")
        self._seen_orders: set[str] = set()
        self._sibling_strategies: dict[str, object] = {}
        self._cmd_ledger = ledger
        self.cancel_calls: list[tuple[str, str, object]] = []
        self.attaches: list[dict] = []
        self.commit_outcome: tuple[str, str] = ("ok", "deferred_flatten queued for the next regular-hours open")
        self.submitted: list[object] = []

        # The REAL methods under test, bound to this host.
        self._handle_flatten_command = UiFeedStrategy._handle_flatten_command.__get__(self)
        self._handle_attach_manager = UiFeedStrategy._handle_attach_manager.__get__(self)
        self._reserve_attach_command = UiFeedStrategy._reserve_attach_command.__get__(self)
        self._canon_manager_params = UiFeedStrategy._canon_manager_params.__get__(self)

    # --- live-state reads ---------------------------------------------------------------------------
    def _position_for(self, instrument_id, strategy_id, expected_side):
        return _Position(instrument_id, strategy_id)

    async def _reducing_qty_for_exit(self, instrument_id, strategy_id, side_name):
        return Decimal(136)  # a protective stop rests -> cancel_resting_first is set

    # --- venue interactions -------------------------------------------------------------------------
    async def _cancel_reducing_leg(self, instrument_id, strategy_id, reducing_side):
        self.cancel_calls.append((instrument_id, strategy_id, reducing_side))

    async def _await_reducing_orders_clear(self, instrument_id, strategy_id, reducing_side):
        return False  # THE TIMEOUT — the cancels are in flight and the venue never confirmed

    async def _await_shares_available(self, instrument_id, qty):
        raise AssertionError("unreachable after a clear-timeout — the close must not proceed")

    def _build_order(self, payload):
        raise AssertionError("no immediate close may be built after the cancel-confirm timed out")

    def _submit(self, order, position_id=None):
        raise AssertionError("no immediate close may be submitted after the cancel-confirm timed out")

    # --- the DB half, stubbed: attach persistence is pinned elsewhere (test_managers.py) ------------
    async def _commit_attach_command(self, cid, ledger_cid, *, kind, account_id, instrument_id,
                                     strategy_id, cycle_id, leash, params):
        self.attaches.append({"kind": kind, "strategy_id": strategy_id, "instrument_id": instrument_id,
                              "params": params})
        return self.commit_outcome


def _flatten(host: _Host, strategy_id: str) -> tuple[str, str]:
    payload = {
        "instrument_id": "AEM.XNYS",
        "strategy_id": strategy_id,
        "expected_side": "LONG",
        "expected_qty": 136,
    }
    return asyncio.run(host._handle_flatten_command("c" * 32, payload, "1-1"))


# --- fixture properties first: if the harness cannot express the bug, every assertion below is vacuous


def test_fixture_the_timeout_branch_is_reached_and_attach_works_for_the_feeds_own_position():
    """The harness's own properties: the cancel flies, the confirm times out, and the SAME attach chain
    (real reserve + stubbed commit) queues a deferred_flatten for MANUAL-001. Only then can a lane
    refusal below mean what it claims — otherwise the guard was never on the path at all."""
    host = _Host(ledger=_Ledger())
    status, detail = _flatten(host, "MANUAL-001")

    assert host.cancel_calls, "the cancel never flew — the timeout branch was not reached"
    assert status == "ok", f"the feed's own position must queue on timeout, got {(status, detail)!r}"
    assert [a["kind"] for a in host.attaches] == ["deferred_flatten"]
    assert host._cmd_ledger.reserved, "the REAL reserve half never ran — the guard under test was bypassed"


def test_a_lane_flatten_whose_cancel_confirm_times_out_QUEUES_the_close_rather_than_stranding():
    """#646, the headline. MOMENTUM-002's owner IS registered on this node — the immediate path would
    route the close through it. The timeout fallback must be attachable for exactly the same set of
    strategy ids, or the fallback exists only for the one strategy that needed it least."""
    host = _Host(ledger=_Ledger())
    host._sibling_strategies["MOMENTUM-002"] = _Owner()

    status, detail = _flatten(host, "MOMENTUM-002")

    assert host.cancel_calls, "the cancel never flew — the timeout branch was not reached"
    assert status == "ok", (
        f"stops cancelled or in flight, and the close was NOT queued: {(status, detail)!r} — the "
        f"position is left naked with the operator told about a routing problem"
    )
    assert [a["strategy_id"] for a in host.attaches] == ["MOMENTUM-002"], (
        "no deferred_flatten was attached for the lane — the exit evaporated while its cancels survive"
    )


def test_an_attach_failure_after_the_cancels_flew_reports_the_naked_position_as_its_own_condition():
    """When the queue genuinely cannot happen (ledger down, here), the error must say what the operator
    is actually looking at — protection cancelled or cancelling, NO close pending — never read as a
    routing or infrastructure detail. A fallback that degrades must degrade LOUDLY."""
    host = _Host(ledger=None)  # the attach's reserve half refuses: idempotency ledger unavailable
    status, detail = _flatten(host, "MANUAL-001")

    assert host.cancel_calls, "the cancel never flew — the timeout branch was not reached"
    assert status == "error"
    assert "UNPROTECTED" in detail and "cancel" in detail.lower(), (
        f"the operator is told {detail!r} — nothing says the protection is already gone and no close "
        f"is pending, which is the one fact that decides whether they must act right now"
    )


# --- the queued replay must route like the immediate path (same class, one level later) --------------


class _ApplyStrategy:
    """Duck-typed `strategy` for the REAL `_DeferredFlatten.apply` — no resting exits, so the replay goes
    straight to submission, which is the seam under test."""

    def __init__(self):
        self.id = _Id("MANUAL-001")
        self.cache = _Cache()
        self._trade_cycles = {}
        self._seen_orders: set[str] = set()
        self._sibling_strategies: dict[str, object] = {}
        self.submits: list[tuple[object, object]] = []
        self.dropped_claims: list[tuple[str, str]] = []  # #923: the flatten drops the lane's claim

    async def _drop_claim(self, strategy_id, symbol):
        self.dropped_claims.append((strategy_id, symbol))

    def _position_for(self, instrument_id, strategy_id, expected_side):
        return _Position(instrument_id, strategy_id)

    async def _reducing_qty_for_exit(self, instrument_id, strategy_id, side_name):
        return Decimal(0)

    def _build_order(self, payload):
        return payload

    def _submit(self, order, position_id=None):
        self.submits.append((order, position_id))


class _Row:
    def __init__(self, strategy_id: str):
        self.instrument_id = "AEM.XNYS"
        self.strategy_id = strategy_id
        self.params = {"expected_side": "LONG", "expected_qty": 136}
        self.cycle_id = None
        self.manager_id = "0f2c4a6e-0000-0000-0000-000000000646"


def test_the_deferred_replay_submits_through_the_OWNER_for_a_lane_position():
    """Once a lane deferred_flatten can exist, its replay must close the way the immediate path does:
    BY the owning strategy. A self-submitted order without the owner's position id is attributed to
    MANUAL-001 and OPENS a short beside the position — the WHD 2026-08-19 incident, replayed at the
    open with nobody watching."""
    strategy = _ApplyStrategy()
    owner = _Owner()
    strategy._sibling_strategies["MOMENTUM-002"] = owner

    state, detail = asyncio.run(_DeferredFlatten().apply(strategy, _Row("MOMENTUM-002")))

    assert state == "APPLIED", (state, detail)
    assert owner.closed, "the owner never submitted — the close went out attributed to MANUAL-001"
    assert not strategy.submits, (
        "the replay submitted from the feed strategy for a lane position — under NETTING that opens a "
        "MANUAL short beside the lane's position instead of closing it"
    )


def test_the_deferred_replay_refuses_an_unreachable_owner_BEFORE_cancelling_anything():
    """Same discipline as the immediate path: the outcome is decided before any side effect. An owner
    this node does not run cannot be closed for — say so, without having touched the protection."""
    strategy = _ApplyStrategy()  # no siblings registered

    async def _boom(*a):
        raise AssertionError("nothing may be cancelled for a position whose owner is unreachable")

    strategy._cancel_reducing_leg = _boom
    state, detail = asyncio.run(_DeferredFlatten().apply(strategy, _Row("BCTROT-004")))

    assert state == "FAILED"
    assert "does not run" in (detail or ""), (state, detail)
    assert not strategy.submits


def test_the_deferred_replay_pins_the_position_id_for_its_own_position():
    """`risk/engine.pyx:425` gates the entire reduce-only check behind `command.position_id is not None`
    — the immediate path passes it, and the replay of the same intent must not silently drop it."""
    strategy = _ApplyStrategy()

    state, detail = asyncio.run(_DeferredFlatten().apply(strategy, _Row("MANUAL-001")))

    assert state == "APPLIED", (state, detail)
    assert len(strategy.submits) == 1
    _, position_id = strategy.submits[0]
    assert position_id is not None and str(position_id) == "AEM.XNYS-MANUAL-001", (
        "the replayed close names no position — reduce-only is decoration and a stale quantity can "
        "flip the position instead of closing it"
    )
