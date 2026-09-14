"""The `on_emergency_exit` adapter — a callback target over `liquidate_lane` (#922, kumo-strategies a8504dc).

The lane DECIDES (kumo-strategies `strategies/emergency.py`); the owner of the positions ACTS. Cockpit
supplies `on_emergency_exit(*, lane, verdict, reasons) -> EmergencyOutcome` and the lane calls it. The
outcome is the ONE durable fact the lane journals beside its decision: "called for liquidation, owner
accepted N". Three rules from the interface, each a test here:
  * it NEVER raises and NEVER returns None — the lane renders both as UNREACHABLE, and a completed
    liquidation must not read as one that could not be reached;
  * REFUSED carries a reason (construction raises without one upstream);
  * ACCEPTED carries an honest count — zero is a legitimate ACCEPTED (the lane held nothing), a partial
    is ACCEPTED with what was submitted and the failures in `detail`.
CALLABLE AND UNREFERENCED (coordinator): nothing upstream calls it yet and the trigger decision is
the operator's; no synthetic caller is built here to make it "exercised".

The outcome type lives upstream. Until it lands on kumo-strategies main the adapter builds the outcome
through a call-time import and these tests run against a cockpit double with the SAME constructor
contract (status/positions/reason/detail, refusing REFUSED without a reason); the installed-shape guard
at the bottom skips by name until the real class is importable and then proves the double matches it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from api import emergency_exit as ee

ACCEPTED, PARTIAL, REFUSED, UNREACHABLE = "accepted", "partial", "refused", "unreachable"


@dataclass(frozen=True)
class _Outcome:
    """The a8504dc contract, as a double that REJECTS what upstream rejects."""
    status: str
    positions: int = 0
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in (ACCEPTED, PARTIAL, REFUSED, UNREACHABLE):
            raise ValueError(f"status {self.status!r}")
        if self.status in (PARTIAL, REFUSED, UNREACHABLE) and not self.reason:
            raise ValueError(f"a {self.status} outcome must carry a reason")
        if self.positions < 0:
            raise ValueError("positions cannot be negative")

    @property
    def acted(self) -> bool:   # the real class's name (e600383): `acted`, not `acts`
        return self.status == ACCEPTED


class _Verdict:
    def __init__(self, state="yes", reasons=("index below its 50-session average",)):
        self.state, self.reasons = state, reasons

    @property
    def acts(self):
        return self.state == "yes"


class _Node:
    """What the adapter is handed: the engine's liquidate entry point, scripted."""

    def __init__(self, outcome, book=None):
        from types import SimpleNamespace
        self.calls: list[tuple[str, dict]] = []
        self._outcome = outcome
        rows = book if book is not None else [("GLD.ARCX", 39), ("NVDA.XNAS", 61)]
        # the leash reads the engine's LIVE book through the Nautilus kwarg the sweep uses
        self.cache = SimpleNamespace(positions_open=lambda strategy_id=None, **kw: [
            SimpleNamespace(instrument_id=iid, quantity=qty, strategy_id=strategy_id) for iid, qty in rows])

    async def liquidate_lane(self, cid, payload):
        self.calls.append((cid, payload))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _adapter(node, **kw):
    return ee.build_on_emergency_exit(node, outcome_type=_Outcome, **kw)


def _call(hook, **kw):
    kw.setdefault("lane", "TECHIVOL-005"); kw.setdefault("verdict", _Verdict()); kw.setdefault("reasons", ("index below its 50-session average",))
    return asyncio.run(hook(**kw))


# -- fixture property: the double rejects what upstream rejects -----------------------------------------

def test_fixture_property_the_outcome_double_refuses_a_REFUSED_without_a_reason_and_a_bad_status():
    with pytest.raises(ValueError):
        _Outcome(REFUSED)
    with pytest.raises(ValueError):
        _Outcome("done")
    assert _Outcome(ACCEPTED, 0).acted and not _Outcome(REFUSED, reason="x").acted


# -- the mapping ----------------------------------------------------------------------------------------

def test_ok_maps_to_ACCEPTED_with_the_submitted_count_and_the_lanes_reasons_travel_as_provenance():
    node = _Node(("ok", {"state": "ok", "held": 8, "submitted": 8, "failed": [], "remainder": [], "invoked_by": "hook:TECHIVOL-005"}))
    out = _call(_adapter(node), reasons=("index 0.91 below 0.97", "dwell 3 of 3"))
    assert out.status == ACCEPTED and out.positions == 8 and out.acted
    cid, payload = node.calls[0]
    assert payload["strategy_id"] == "TECHIVOL-005"
    assert payload["invoked_by"] == "hook:TECHIVOL-005" and payload["reason"] == "index 0.91 below 0.97 | dwell 3 of 3"
    assert "expected_positions" in payload and "expected_total_qty" in payload, "the leash is supplied from the live book"


def test_held_nothing_maps_to_ACCEPTED_with_zero_never_REFUSED():
    node = _Node(("ok", {"state": "held_nothing", "held": 0, "submitted": 0, "failed": [], "remainder": []}))
    out = _call(_adapter(node))
    assert out.status == ACCEPTED and out.positions == 0


def test_partial_maps_to_PARTIAL_not_ACCEPTED_with_the_honest_count_and_a_reason_naming_the_failures():
    """`acts` is the ONE thing the lane branches on; a partial must not read as handled."""
    node = _Node(("partial", {"state": "partial", "held": 5, "submitted": 3, "failed": [{"symbol": "XLV", "qty": 40, "error": "x"}], "remainder": [{"symbol": "LNG", "qty": 12}]}))
    out = _call(_adapter(node))
    assert out.status == PARTIAL and out.positions == 3 and not out.acted
    assert out.reason == "1 of 5 failed: XLV; 1 remained open after the sweep"
    assert out.detail["failed"] == [{"symbol": "XLV", "qty": 40, "error": "x"}] and out.detail["remainder"] == [{"symbol": "LNG", "qty": 12}]


def test_refused_maps_to_REFUSED_with_the_handlers_why_as_the_reason():
    node = _Node(("refused", {"why": "orders disarmed (KUMO_ORDERS_ARMED off)"}))
    out = _call(_adapter(node))
    assert out.status == REFUSED and out.reason == "orders disarmed (KUMO_ORDERS_ARMED off)" and not out.acted


def test_deferred_to_next_open_is_REFUSED_with_the_deferral_named_the_lane_must_escalate_not_believe_a_sale():
    node = _Node(("deferred_to_next_open", {"state": "deferred_to_next_open", "held": 5, "submitted": 0, "positions": []}))
    out = _call(_adapter(node))
    assert out.status == REFUSED and out.positions == 0
    assert "deferred_to_next_open" in out.reason and "LIQUIDATING" in out.reason
    assert out.detail["state"] == "deferred_to_next_open"


def test_duplicate_is_REFUSED_by_name():
    node = _Node(("duplicate", {"why": "duplicate command id"}))
    out = _call(_adapter(node))
    assert out.status == REFUSED and "duplicate" in out.reason


# -- never raise, never None ---------------------------------------------------------------------------

def test_a_raising_engine_is_UNREACHABLE_with_the_exception_named_never_raised_through_never_None():
    """A refusal is a decision by a working system; an exception is a system that is not — the lane
    escalates them to different people, so they must not read the same."""
    node = _Node(RuntimeError("bus down"))
    out = _call(_adapter(node))
    assert out is not None and out.status == UNREACHABLE and "RuntimeError: bus down" in out.reason and not out.acted


def test_an_unknown_status_from_the_engine_is_REFUSED_never_ACCEPTED():
    node = _Node(("weird", {"why": "?"}))
    out = _call(_adapter(node))
    assert out.status == REFUSED and "weird" in out.reason


def test_a_verdict_that_does_not_act_is_REFUSED_before_touching_the_engine():
    """Rule 4 upstream: nothing acts on UNKNOWN. The adapter enforces it too — two derivations."""
    node = _Node(("ok", {"state": "ok", "submitted": 8}))
    out = _call(_adapter(node), verdict=_Verdict(state="unknown", reasons=("no price panel",)))
    assert out.status == REFUSED and "unknown" in out.reason and node.calls == []


# -- the leash comes from the live book, not from the lane --------------------------------------------

def test_the_adapter_reads_the_leash_from_the_engines_book_so_the_handler_can_check_it():
    node = _Node(("ok", {"state": "ok", "submitted": 2}))
    book = {"TECHIVOL-005": [("GLD.ARCX", 39), ("NVDA.XNAS", 61)]}
    out = _call(_adapter(node, read_book=lambda lane: book.get(lane, [])))
    assert node.calls[0][1]["expected_positions"] == 2 and node.calls[0][1]["expected_total_qty"] == 100


# -- unique command ids per call (a retry after a partial is a NEW command) ----------------------------

def test_each_call_is_a_new_command_id():
    node = _Node(("ok", {"state": "ok", "submitted": 1}))
    hook = _adapter(node)
    _call(hook); _call(hook)
    assert node.calls[0][0] != node.calls[1][0]


# -- installed shape: the double matches the real class once it lands ----------------------------------

def test_installed_shape_the_real_EmergencyOutcome_matches_the_double():
    em = pytest.importorskip("kumo_strategies.strategies.emergency",
                             reason="strategies/emergency.py (a8504dc, PR #169) not on the installed kumo-strategies yet — adapter builds through a call-time import")
    for name in ("ACCEPTED", "PARTIAL", "REFUSED", "UNREACHABLE"):
        assert hasattr(em, name)
    with pytest.raises(ValueError):
        em.EmergencyOutcome(status=em.PARTIAL, positions=1)   # PARTIAL requires a reason upstream too
    real = em.EmergencyOutcome(status=em.ACCEPTED, positions=3, detail={"x": 1})
    assert real.acted and real.positions == 3
    with pytest.raises(ValueError):
        em.EmergencyOutcome(status=em.REFUSED)
    node = _Node(("ok", {"state": "ok", "submitted": 3}))
    out = _call(ee.build_on_emergency_exit(node))     # no outcome_type → the REAL class
    assert isinstance(out, em.EmergencyOutcome) and out.acted and out.positions == 3
