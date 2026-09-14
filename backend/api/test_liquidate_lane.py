"""`liquidate_lane` — the invocable-now liquidation cockpit did not have (#922). Red before it exists.

MEASURED 2026-09-11 (#922): nothing in cockpit writes LIQUIDATING (operator_only upstream, no route, no
command); `daily_loss → HALT` sells nothing and a HALTED lane cannot exit; pool-exclude sells at the next
session; the one lane-attributed close is `owner.close_position` — per position, no lane sweep. the operator's
LIQUIDATE condition ("emergency, act now") had no mechanism.

SCOPE, as reviewed (coordinator, 03:01Z):
  1. write LIQUIDATING FIRST, verify it reads back, THEN sweep — a slot firing between the last close
     and the state write re-enters what was just sold; the safe failure is LIQUIDATING + a partial book.
  2. a HALTED lane must liquidate — that is the PRIMARY case (daily_loss halts, the book is held, the
     lane itself can no longer exit).
  3. every close is DAY (#924) and carries the lane's StrategyId + position_id (NETTING).
  4. partial failure is a REPORT, not a rollback: which positions did not close, by symbol and qty.
  5. refuse by name; expected-count/qty leash re-validated against the live cache BEFORE any order;
     ORDERS_ARMED re-checked INSIDE; idempotent ledger; provenance (invoked_by, reason) on the response
     and the row; three-state response (closed N / refused why / held nothing).
  6. drop the lane's claim per closed symbol (the #923 gap must not be inherited).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from nautilus_trader.model.enums import TimeInForce

from api.engine_node import UiFeedStrategy
from api.test_lane_flatten_keeps_exit_pending import _Id, _Ledger, _Position, _Side
from api.command_ledger import Reserve, ReserveResult

CID = "e" * 32


class _Pos(_Position):
    def __init__(self, instrument_id, strategy_id, qty):
        super().__init__(instrument_id, strategy_id)
        self.quantity = qty


class _Order:
    def __init__(self, coid, instrument_id, strategy_id, side="BUY", uncancellable=False):
        self.client_order_id = _Id(coid)
        self.instrument_id = _Id(instrument_id)
        self.strategy_id = _Id(strategy_id)
        self.side = _Side(side)
        self.uncancellable = uncancellable


class _Cache:
    """`positions_open(strategy_id=...)` / `orders_open(strategy_id=...)` are the Nautilus kwargs the sweep
    must use; an unscoped read — the account's whole book — is COUNTED and is a failure of ownership.
    `scoped_reads` counts the lane-scoped position reads, so a test can prove the leash and the sweep
    used ONE snapshot and that the handler re-read the book AFTER the sweep."""

    def __init__(self, positions, orders=()):
        self._p = list(positions)
        self._o = list(orders)
        self.unscoped_reads = 0
        self.scoped_reads = 0
        self.on_read = None            # a hook the mid-sweep-fill test uses to mutate the book

    def positions_open(self, strategy_id=None, **kw):
        if strategy_id is None:
            self.unscoped_reads += 1
            return list(self._p)
        self.scoped_reads += 1
        out = [p for p in self._p if str(p.strategy_id) == str(strategy_id)]
        if self.on_read:
            self.on_read(self.scoped_reads)
        return out

    def orders_open(self, strategy_id=None, **kw):
        if strategy_id is None:
            self.unscoped_reads += 1
            return list(self._o)
        return [o for o in self._o if str(o.strategy_id) == str(strategy_id)]


class _Owner:
    def __init__(self, fail_on=()):
        self.calls: list[dict] = []
        self.fail_on = set(fail_on)

    def close_position(self, position, client_id=None, tags=None, time_in_force=TimeInForce.GTC,
                       reduce_only=True, quote_quantity=False, params=None):
        if str(position.instrument_id) in self.fail_on:
            raise RuntimeError(f"venue refused {position.instrument_id}")
        self.calls.append({"position": position, "tags": tags or [], "time_in_force": time_in_force})


class _Host:
    """Duck-typed self for the REAL `_handle_liquidate_lane_command`. Anything undeclared raises."""

    def __init__(self, positions, *, lifecycle="TRADING", ledger=None, armed=True, orders=(), now_ns=None):
        self.id = _Id("MANUAL-001")
        self.cache = _Cache(positions, orders)
        from api.test_lane_flatten_keeps_exit_pending import _RTH_NS
        self.clock = type("_Clock", (), {"timestamp_ns": staticmethod(lambda: now_ns if now_ns is not None else _RTH_NS)})()
        self.cancelled_orders: list[str] = []
        self._orders_armed = armed
        self._sibling_strategies: dict[str, object] = {}
        self._cmd_ledger = ledger if ledger is not None else _Ledger()
        self.sequence: list[tuple] = []          # the ORDER of side effects — the TOCTOU pin reads it
        self.lifecycle_rows: dict[str, str] = {"MOMENTUM-002": lifecycle, "BCTROT-004": "TRADING"}
        self.lifecycle_write_outcome = "SAVED"
        self.dropped_claims: list[tuple[str, str]] = []
        self.cancels: list[tuple] = []
        self.journal: list[dict] = []
        self._handle_liquidate_lane_command = UiFeedStrategy._handle_liquidate_lane_command.__get__(self)
        self._liquidate_lane = UiFeedStrategy._liquidate_lane.__get__(self)  # the REAL sweep, bound

    async def _read_lifecycle(self, strategy_id):
        return self.lifecycle_rows.get(strategy_id)

    async def _write_lifecycle(self, strategy_id, state, *, reason, by):
        self.sequence.append(("lifecycle", strategy_id, state, by))
        if self.lifecycle_write_outcome == "SAVED":
            self.lifecycle_rows[strategy_id] = state
        return self.lifecycle_write_outcome

    async def _cancel_reducing_leg(self, instrument_id, strategy_id, reducing_side):
        self.sequence.append(("cancel", instrument_id, strategy_id))
        self.cancels.append((instrument_id, strategy_id))

    async def _await_reducing_orders_clear(self, instrument_id, strategy_id, reducing_side):
        return True

    async def _cancel_working_order(self, order):
        """Cancel one of the lane's WORKING orders (an entry still at the venue). Returns True when the
        venue confirmed the cancel; an uncancellable order is a `failed` entry, never a silent skip."""
        self.sequence.append(("cancel_order", str(order.client_order_id)))
        if order.uncancellable:
            return False
        self.cancelled_orders.append(str(order.client_order_id))
        self.cache._o = [o for o in self.cache._o if o is not order]
        return True

    async def _drop_claim(self, strategy_id, symbol):
        self.sequence.append(("drop_claim", strategy_id, symbol))
        self.dropped_claims.append((strategy_id, symbol))

    async def _journal_liquidation(self, strategy_id, row: dict):
        self.journal.append(dict(row, strategy_id=strategy_id))

    def _submit(self, order, position_id=None):
        raise AssertionError("liquidate_lane must route every close through the OWNER, never self-submit")


def _owner_that_records(host, lane="MOMENTUM-002", **kw):
    o = _Owner(**kw)
    original = o.close_position

    def recording(position, **kwargs):
        host.sequence.append(("close", str(position.instrument_id), str(position.strategy_id)))
        return original(position, **kwargs)
    o.close_position = recording
    host._sibling_strategies[lane] = o
    return o


def _book():
    return [_Pos("AEM.XNYS", "MOMENTUM-002", 136), _Pos("XLV.ARCX", "MOMENTUM-002", 40),
            _Pos("AEM.XNYS", "BCTROT-004", 50)]


def _payload(**over):
    p = {"strategy_id": "MOMENTUM-002", "expected_positions": 2, "expected_total_qty": 176,
         "invoked_by": "operator", "reason": "market broke — get out"}
    p.update(over)
    return p


def _run(host, payload):
    return asyncio.run(host._handle_liquidate_lane_command(CID, payload, "1-1"))


# -- fixture properties first ---------------------------------------------------------------------------

def test_fixture_property_two_lanes_share_an_instrument_and_the_cache_scopes_by_lane():
    c = _Cache(_book())
    assert {str(p.strategy_id) for p in c.positions_open()} == {"MOMENTUM-002", "BCTROT-004"}
    assert [str(p.instrument_id) for p in c.positions_open(strategy_id="MOMENTUM-002")] == ["AEM.XNYS", "XLV.ARCX"]
    assert c.unscoped_reads == 1, "the unscoped read is COUNTED — the sweep must never make one"


def test_fixture_property_the_host_refuses_a_self_submit_and_records_order_of_effects():
    host = _Host(_book())
    with pytest.raises(AssertionError):
        host._submit(object(), position_id="x")
    assert host.sequence == []


# -- the happy path: state first, then the sweep, DAY, owner-routed, claims dropped ---------------------

def test_liquidate_writes_LIQUIDATING_before_the_first_close_and_closes_only_that_lane():
    host = _Host(_book()); owner = _owner_that_records(host)
    status, detail = _run(host, _payload())
    assert status == "ok", detail
    kinds = [s[0] for s in host.sequence]
    assert kinds.index("lifecycle") < kinds.index("close"), "LIQUIDATING must be true BEFORE any close (TOCTOU)"
    assert host.lifecycle_rows["MOMENTUM-002"] == "LIQUIDATING"
    assert [str(c["position"].instrument_id) for c in owner.calls] == ["AEM.XNYS", "XLV.ARCX"]
    assert all(str(c["position"].strategy_id) == "MOMENTUM-002" for c in owner.calls)
    assert host.cache.unscoped_reads == 0, "the sweep read the ACCOUNT's book"
    assert host.lifecycle_rows["BCTROT-004"] == "TRADING"


def test_every_close_is_DAY_and_tagged_with_the_command():
    host = _Host(_book()); owner = _owner_that_records(host)
    _run(host, _payload())
    assert all(c["time_in_force"] is TimeInForce.DAY for c in owner.calls)
    assert all(any(t.startswith("liquidate:") for t in c["tags"]) for c in owner.calls)


def test_the_resting_stop_is_released_before_each_close_and_the_claim_dropped_after():
    host = _Host(_book()); _owner_that_records(host)
    _run(host, _payload())
    for sym in ("AEM.XNYS", "XLV.ARCX"):
        k = [s for s in host.sequence if len(s) > 1 and s[1] == sym]
        assert [s[0] for s in k] == ["cancel", "close"], k
    assert sorted(host.dropped_claims) == [("MOMENTUM-002", "AEM"), ("MOMENTUM-002", "XLV")]


def test_a_HALTED_lane_liquidates_this_is_the_primary_case():
    host = _Host(_book(), lifecycle="HALTED"); owner = _owner_that_records(host)
    status, detail = _run(host, _payload())
    assert status == "ok", detail
    assert len(owner.calls) == 2 and host.lifecycle_rows["MOMENTUM-002"] == "LIQUIDATING"


def test_the_response_and_the_row_carry_counts_and_provenance():
    host = _Host(_book()); _owner_that_records(host)
    status, detail = _run(host, _payload())
    assert detail["submitted"] == 2 and detail["failed"] == [] and detail["held"] == 2 and detail["remainder"] == []
    assert detail["basis"] == "accepted-by-nautilus-locally, not venue-confirmed", "SUBMITTED is not FILLED (#165: an expiry writes no row)"
    assert detail["invoked_by"] == "operator" and detail["reason"] == "market broke — get out"
    assert host.journal and host.journal[0]["invoked_by"] == "operator" and host.journal[0]["submitted"] == 2


# -- three states, never two ----------------------------------------------------------------------------

def test_a_lane_that_holds_nothing_still_goes_LIQUIDATING_and_says_held_zero_not_closed_zero():
    host = _Host([_Pos("AEM.XNYS", "BCTROT-004", 50)]); owner = _owner_that_records(host)
    status, detail = _run(host, _payload(expected_positions=0, expected_total_qty=0))
    assert status == "ok" and detail["held"] == 0 and detail["submitted"] == 0
    assert detail["state"] == "held_nothing"
    assert host.lifecycle_rows["MOMENTUM-002"] == "LIQUIDATING" and owner.calls == []


def test_partial_failure_is_a_REPORT_not_a_rollback():
    host = _Host(_book()); owner = _owner_that_records(host, fail_on={"XLV.ARCX"})
    status, detail = _run(host, _payload())
    assert status == "partial", detail
    assert detail["submitted"] == 1 and detail["failed"] == [{"symbol": "XLV", "qty": 40, "error": "RuntimeError: venue refused XLV.ARCX"}]
    assert host.lifecycle_rows["MOMENTUM-002"] == "LIQUIDATING", "the safe failure: no re-entry, a partial book"
    assert host.dropped_claims == [("MOMENTUM-002", "AEM")], "the failed close keeps its claim"
    assert host.journal[0]["failed"] == detail["failed"]


# -- refusals, each by name, before any order ------------------------------------------------------------

@pytest.mark.parametrize("over, reason", [
    ({"strategy_id": "NOBODY-009"}, "not registered"),
    ({"expected_positions": 1}, "expected 1 positions"),
    ({"expected_total_qty": 999}, "expected total 999"),
    ({"invoked_by": ""}, "invoked_by"),
    ({"reason": ""}, "reason"),
])
def test_refuses_by_name_with_no_close_and_no_state_write(over, reason):
    host = _Host(_book()); owner = _owner_that_records(host)
    status, detail = _run(host, _payload(**over))
    assert status == "refused", (status, detail)
    assert reason in detail["why"], detail
    assert owner.calls == [] and host.lifecycle_rows["MOMENTUM-002"] == "TRADING" and host.sequence == []


def test_refuses_when_orders_are_not_armed_checked_INSIDE_the_handler():
    host = _Host(_book(), armed=False); owner = _owner_that_records(host)
    status, detail = _run(host, _payload())
    assert status == "refused" and "armed" in detail["why"] and owner.calls == [] and host.sequence == []


def test_refuses_when_the_state_write_does_not_read_back_as_LIQUIDATING():
    host = _Host(_book()); owner = _owner_that_records(host)
    host.lifecycle_write_outcome = "OPERATOR_WON"
    status, detail = _run(host, _payload())
    assert status == "refused" and "LIQUIDATING" in detail["why"] and owner.calls == []


def test_a_replayed_command_returns_the_ledger_entry_and_closes_nothing():
    class _Dup(_Ledger):
        async def reserve(self, cid, ctype, coid, phash, entry_id):
            return ReserveResult(Reserve.DUPLICATE_COMMAND, existing_status="ok")
    host = _Host(_book(), ledger=_Dup()); owner = _owner_that_records(host)
    status, detail = _run(host, _payload())
    assert owner.calls == [] and host.sequence == []
    assert status in ("ok", "duplicate") and "duplicate" in str(detail).lower()


# -- coverage review round 1 (coordinator): six surrounding cases ----------------------------------------

def test_the_lanes_WORKING_orders_are_cancelled_before_any_close_and_an_uncancellable_one_is_a_failure():
    """LIQUIDATING stops the NEXT decision; it does not cancel an entry already at the venue. A working
    BUY that fills ten seconds after the sweep re-opens the book we just closed."""
    orders = [_Order("kumo-buy-1", "LNG.XNYS", "MOMENTUM-002"), _Order("kumo-buy-2", "CF.XNYS", "MOMENTUM-002"),
              _Order("kumo-bct-1", "DIA.ARCX", "BCTROT-004")]
    host = _Host(_book(), orders=orders); owner = _owner_that_records(host)
    status, detail = _run(host, _payload())
    kinds = [x[0] for x in host.sequence]
    assert kinds.index("cancel_order") < kinds.index("close"), "working orders are cancelled BEFORE the closes"
    assert sorted(host.cancelled_orders) == ["kumo-buy-1", "kumo-buy-2"], "only this lane's working orders"
    assert detail["cancelled_orders"] == 2 and len(owner.calls) == 2
    # the uncancellable one
    host2 = _Host(_book(), orders=[_Order("kumo-stuck", "LNG.XNYS", "MOMENTUM-002", uncancellable=True)])
    _owner_that_records(host2)
    status2, detail2 = _run(host2, _payload())
    assert status2 == "partial"
    assert {"order": "kumo-stuck", "error": "cancel not confirmed"} in detail2["failed"]


def test_outside_regular_hours_writes_LIQUIDATING_submits_NOTHING_and_returns_deferred_to_next_open():
    """"The market exploded" also happens on an overnight gap or a Sunday headline. A DAY close there would
    expire unfilled and write no row (#165), so SUBMITTING is useless — but refusing the whole command would
    leave the lane free to ADD at the open. Three states on the command: DONE / DEFERRED / REFUSED. Deferred
    = the lane is stopped from trading, its book is still under its resting stops, nothing has been sold."""
    from api.test_lane_flatten_keeps_exit_pending import _RTH_NS
    after_hours = _RTH_NS + 8 * 3_600 * 10**9
    host = _Host(_book(), now_ns=after_hours); owner = _owner_that_records(host)
    assert host.clock.timestamp_ns() != _RTH_NS
    status, detail = _run(host, _payload())
    assert status == "deferred_to_next_open", (status, detail)
    assert host.lifecycle_rows["MOMENTUM-002"] == "LIQUIDATING", "the lane must be unable to add at the open"
    assert owner.calls == [] and detail["submitted"] == 0
    assert [(p["symbol"], p["qty"]) for p in detail["positions"]] == [("AEM", 136), ("XLV", 40)], "what is still held, listed"
    assert host.cancelled_orders == [], "nothing is sent to the venue after hours — not even cancels"
    # a later IN-HOURS invocation for the same lane is ACCEPTED: the deferred command submitted nothing
    host.clock = type("_C", (), {"timestamp_ns": staticmethod(lambda: _RTH_NS)})()
    status2, detail2 = asyncio.run(host._handle_liquidate_lane_command("a" * 32, _payload(), "1-2"))
    assert status2 == "ok", (status2, detail2)
    assert len(owner.calls) == 2 and detail2["submitted"] == 2


def test_a_fill_landing_MID_SWEEP_is_reported_as_remainder_not_discarded():
    """An order submitted before the LIQUIDATING write can fill DURING the sweep; the position is not in
    the snapshot. After the sweep the handler RE-READS the lane's book and reports what is still open."""
    host = _Host(_book()); owner = _owner_that_records(host)

    def land_a_fill(read_no):
        if read_no == 1:  # right after the snapshot the sweep will use
            host.cache._p.append(_Pos("LNG.XNYS", "MOMENTUM-002", 12))
    host.cache.on_read = land_a_fill
    status, detail = _run(host, _payload())
    assert status == "partial", detail
    assert [str(c["position"].instrument_id) for c in owner.calls] == ["AEM.XNYS", "XLV.ARCX"], "the snapshot's two"
    assert detail["remainder"] == [{"symbol": "LNG", "qty": 12}]
    assert host.cache.scoped_reads >= 2, "the book was re-read AFTER the sweep"


def test_after_a_partial_a_NEW_command_acts_on_the_remainder_only_while_the_SAME_command_is_a_duplicate():
    host = _Host(_book()); owner = _owner_that_records(host, fail_on={"XLV.ARCX"})
    status, detail = _run(host, _payload())
    assert status == "partial" and len(owner.calls) == 1
    host.cache._p = [p for p in host.cache._p if not (str(p.instrument_id) == "AEM.XNYS" and str(p.strategy_id) == "MOMENTUM-002")]
    owner.fail_on = set()
    # the operator retries with a NEW command id and the remainder as the leash
    status2, detail2 = asyncio.run(host._handle_liquidate_lane_command("f" * 32, _payload(expected_positions=1, expected_total_qty=40), "1-2"))
    assert status2 == "ok", detail2
    assert [str(c["position"].instrument_id) for c in owner.calls] == ["AEM.XNYS", "XLV.ARCX"]
    assert detail2["submitted"] == 1


def test_a_SHORT_position_is_closed_through_the_owner_and_counted_by_absolute_quantity():
    short = _Pos("IONQ.XNYS", "MOMENTUM-002", 30); short.side = _Side("SHORT")
    host = _Host([short]); owner = _owner_that_records(host)
    status, detail = _run(host, _payload(expected_positions=1, expected_total_qty=30))
    assert status == "ok", detail
    assert owner.calls[0]["position"] is short, "Nautilus's close_position derives the covering side from the position"
    assert detail["submitted"] == 1 and detail["positions"][0]["side"] == "SHORT"


def test_the_leash_and_the_sweep_use_ONE_snapshot_a_book_that_moves_between_them_cannot_pass_by_agreeing_with_itself():
    """The caller's expected count comes from the same cache the sweep reads; if the handler read twice a
    stale first read could satisfy the leash while the sweep acted on a different book."""
    host = _Host(_book()); owner = _owner_that_records(host)
    reads_before_first_close = []

    def watch(read_no):
        if not any(x[0] == "close" for x in host.sequence):
            reads_before_first_close.append(read_no)
        if read_no == 1:
            host.cache._p.append(_Pos("LNG.XNYS", "MOMENTUM-002", 12))  # the book moves after the snapshot
    host.cache.on_read = watch
    status, detail = _run(host, _payload())   # leash says 2 / 176 — true of the snapshot
    assert reads_before_first_close == [1], "exactly ONE lane-scoped read before the first close"
    assert len(owner.calls) == 2 and detail["remainder"] == [{"symbol": "LNG", "qty": 12}]
    # and a leash that disagrees with the snapshot refuses before anything
    host3 = _Host(_book()); owner3 = _owner_that_records(host3)
    status3, detail3 = _run(host3, _payload(expected_total_qty=175))
    assert status3 == "refused" and owner3.calls == []


# -- implementation review round 1: the four lane families name their journal/sessionmaker differently ----

def test_every_lane_family_is_reachable_by_the_resolver():
    """Walk the REAL gateway/runner sources: each must assign a journal under a name the resolver knows,
    and a sessionmaker under a name it knows OR carry a PgJournal (which has `.sessionmaker`)."""
    import ast, pathlib
    from api.engine_node import UiFeedStrategy as U
    from kumo_strategies.runtime.executor import qc27_runner, pgjournal
    sources = {
        "momentum": pathlib.Path("strategies/momentum.py"), "qc345": pathlib.Path("strategies/qc345.py"),
        "crsi_short": pathlib.Path("strategies/crsi_short.py"), "qc27_runner": pathlib.Path(qc27_runner.__file__)}
    assert hasattr(pgjournal.PgJournal, "__dataclass_fields__") and "sessionmaker" in pgjournal.PgJournal.__dataclass_fields__
    for name, path in sources.items():
        tree = ast.parse(path.read_text())
        assigned = {t.attr for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in (n.targets[0].elts if isinstance(n.targets[0], ast.Tuple) else [n.targets[0]])
                    if isinstance(t, ast.Attribute)}
        fields = {n.target.id for n in ast.walk(tree) if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
        names = assigned | fields
        assert names & set(U._JOURNAL_ATTRS), f"{name}: journal is kept under none of {U._JOURNAL_ATTRS}: {sorted(names)[:12]}"


class _ResolverHost:
    """The REAL resolvers bound to a minimal self."""
    _JOURNAL_ATTRS = UiFeedStrategy._JOURNAL_ATTRS
    _SESSIONMAKER_ATTRS = UiFeedStrategy._SESSIONMAKER_ATTRS

    def __init__(self, siblings):
        self._sibling_strategies = siblings
        for name in ("_lane_runner", "_lane_journal", "_lane_sessionmaker"):
            setattr(self, name, getattr(UiFeedStrategy, name).__get__(self))


@pytest.mark.parametrize("shape", ["rotation", "qc27", "crsi"])
def test_the_resolver_finds_the_journal_and_sessionmaker_in_each_family_shape(shape):
    from types import SimpleNamespace
    from api.engine_node import UiFeedStrategy as U
    sm = object(); journal = SimpleNamespace(sessionmaker=sm)
    runner = {"rotation": SimpleNamespace(_sm=sm, _journal=journal),
              "qc27": SimpleNamespace(journal=journal),
              "crsi": SimpleNamespace(journal=journal)}[shape]
    host = _ResolverHost({"L": SimpleNamespace(_runner=runner)})
    assert host._lane_journal("L") is journal
    assert host._lane_sessionmaker("L") is sm


def test_the_resolver_refuses_by_name_when_a_runner_exposes_neither():
    from types import SimpleNamespace
    from api.engine_node import UiFeedStrategy as U
    host = _ResolverHost({"L": SimpleNamespace(_runner=SimpleNamespace())})
    with pytest.raises(RuntimeError, match="journal"):
        host._lane_journal("L")
    with pytest.raises(RuntimeError, match="sessionmaker"):
        host._lane_sessionmaker("L")


def test_an_order_unknown_to_the_cache_is_NOT_a_confirmed_cancel():
    from types import SimpleNamespace
    from api.engine_node import UiFeedStrategy as U
    owner = SimpleNamespace(cancel_order=lambda o: None)
    host = SimpleNamespace(_sibling_strategies={"MOMENTUM-002": owner}, cache=SimpleNamespace(order=lambda coid: None))
    order = SimpleNamespace(client_order_id=_Id("kumo-x"), strategy_id=_Id("MOMENTUM-002"))
    import api.engine_node as en
    original = en.asyncio.sleep
    en.asyncio.sleep = lambda *_: original(0)  # do not wait 5 s in a test
    try:
        assert asyncio.run(U._cancel_working_order(host, order)) is False
    finally:
        en.asyncio.sleep = original
