"""Which strategies a position may be CLAIMED into (#781).

WHY A SEAM TEST. `transfers.validate` is correct and fully tested; the defect was that the CALL SITE
handed it a constant. A test on the validator passes with the bug. This drives
`_handle_transfer_command` itself and captures the set it actually passes.

THE SIBLING CASE IS THE ONE THAT CARRIES INFORMATION. A test that only pins MANUAL-001 passes today
AND passes with the bug reintroduced, because at that single value the stale literal and the truth
AGREE — the QC27 `ALLOCATED_EQUITY = 20_000.0` shape.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass

import api.db.engine  # noqa: F401 — imported so the stub can replace it in sys.modules
import api.transfers  # noqa: F401 — same
from api.engine_node import UiFeedStrategy


@dataclass
class _Pos:
    quantity: float = 25.0
    avg_px_open: float = 10.0
    ts_last: int = 1_000
    account_id: str = "ACC-1"


class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, msg, *a):
        self.lines.append(str(msg))

    warning = info = error = debug = exception = _rec


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Node:
    """Only the surface `_handle_transfer_command` touches, bound to the REAL method."""

    _handle_transfer_command = UiFeedStrategy._handle_transfer_command

    def __init__(self, siblings):
        self.id = "MANUAL-001"
        self._sibling_strategies = dict.fromkeys(siblings, object())
        self.log = _Log()
        self._exec_client_id = "ALPACA"

    def _position_for(self, instrument_id, strategy_id, side):
        return _Pos()

    def _mark_px(self, instrument_id):
        return 10.0

    def _reducing_order_qty(self, instrument_id, strategy_id, side):
        from decimal import Decimal
        return Decimal("0")

    def _opposite_side_qty(self, instrument_id, strategy_id, side):
        from decimal import Decimal
        return Decimal("0")


def _run(target, siblings=("BCTROT-004", "MOMENTUM-002")):
    """Drive the real command and return (manageable_set_passed, status, error)."""
    captured = {}
    real = sys.modules["api.transfers"]

    stub = types.ModuleType("api.transfers")
    stub.resolve_transfer_px = real.resolve_transfer_px
    stub.TransferRequest = real.TransferRequest

    async def already_applied(session, cid):
        return False

    def validate(**kw):
        captured.update(kw)
        # The REAL validator decides, so its rules still apply and this cannot drift from
        # production. Then SHORT-CIRCUIT: returning a sentinel makes the command return before any
        # persistence, so these tests never need a database.
        captured["verdict"] = real.validate(**kw)
        return captured["verdict"] or "STOP-AFTER-VALIDATE"

    async def record(session, req, state):
        return None

    stub.already_applied = already_applied
    stub.validate = validate
    stub.record = record

    db = types.ModuleType("api.db.engine")
    db.session_factory = lambda: _Session()

    node = _Node(siblings)
    # BOTH, and that distinction is the point: `_handle_transfer_command` does
    # `from api import transfers as tr`, which reads the ATTRIBUTE on the `api` package rather than
    # sys.modules. Replacing only sys.modules leaves the real module bound and the stub never runs —
    # a double that cannot represent production, silently.
    import api as api_pkg
    real_attr = api_pkg.transfers
    sys.modules["api.transfers"] = stub
    api_pkg.transfers = stub
    old_db = sys.modules.get("api.db.engine")
    sys.modules["api.db.engine"] = db
    try:
        status, error = asyncio.run(node._handle_transfer_command("cmd-1", {
            "instrument_id": "TRT.AMEX",
            "side": "LONG",
            "target_strategy_id": target,
            "quantity": "25",
            "source_ts_last": 1_000,
        }))
    finally:
        sys.modules["api.transfers"] = real
        api_pkg.transfers = real_attr
        if old_db is not None:
            sys.modules["api.db.engine"] = old_db
    return captured.get("manageable_strategies"), captured.get("verdict"), error


def test_the_fixture_registers_siblings_that_are_NOT_self():
    """FIXTURE PROPERTY FIRST. If the only registered strategy were MANUAL-001, the stale literal and
    the derived set would be identical and every assertion below would pass with the bug."""
    node = _Node(("BCTROT-004", "MOMENTUM-002"))
    assert set(node._sibling_strategies) - {node.id}


def test_a_REGISTERED_SIBLING_is_a_legal_claim_target():
    """THE TEST THAT CARRIES THE INFORMATION. Killed by reverting to `{str(self.id)}`."""
    manageable, verdict, _e = _run("BCTROT-004")
    assert "BCTROT-004" in (manageable or set()), f"call site passed {manageable}"
    assert verdict is None, f"the validator refused a registered sibling: {verdict}"


def test_MANUAL_itself_remains_a_legal_target():
    """The old behaviour must survive the widening — and note this assertion alone passes WITH the
    bug, which is exactly why it is not the test that proves the fix."""
    manageable, verdict, _e = _run("MANUAL-001")
    assert "MANUAL-001" in (manageable or set())
    assert verdict is None, verdict


def test_a_strategy_that_is_NEITHER_self_NOR_registered_is_still_REFUSED():
    """Widening must not become 'anything goes'. A target this node cannot manage would strand the
    position with no owner able to close it."""
    manageable, verdict, _e = _run("GHOST-999")
    assert "GHOST-999" not in (manageable or set())
    assert verdict and "not a registered strategy" in verdict


def test_the_manageable_set_is_DERIVED_from_the_registry_not_a_second_list():
    """A hand-maintained list would drift from `register_strategy`, and drift is the whole subject.
    Registering a new lane must widen the set with no other edit."""
    manageable, _v, _e = _run("QC345-003", siblings=("BCTROT-004", "QC345-003"))
    assert manageable == {"MANUAL-001", "BCTROT-004", "QC345-003"}
