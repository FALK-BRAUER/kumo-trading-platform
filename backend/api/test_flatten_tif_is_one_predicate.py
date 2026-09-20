"""Every flatten close goes out DAY — the owner-routed branch included (#924).

MEASURED 2026-09-11 while mapping #922: the self-owned flatten builds `"time_in_force": "day"`
(engine_node.py ~3690) while the owner-routed branch calls `owner.close_position(pos, tags=[...])` and
inherits Nautilus's default — `Strategy.close_position(..., time_in_force=TimeInForce.GTC, ...)`
(trading/strategy.pyx:1351). Same for `_DeferredFlatten.apply`. A GTC market close that does not fill
in-session (halted name, after-hours submit) RESTS at the venue overnight against a book the operator
believes is flat; the DAY branch expires. Two derivations of one intent, different behaviour — and #922's
lane-wide liquidation builds on `close_position`, so it would inherit GTC on day one.

ONE PREDICATE: every flatten submission in engine_node.py is DAY, whichever branch built it.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
from pathlib import Path

from nautilus_trader.model.enums import TimeInForce
import nautilus_trader

from api import engine_node
from api.engine_node import UiFeedStrategy
from api.test_lane_flatten_keeps_exit_pending import _Host, _Id, _Ledger, _Position

ENGINE = Path(engine_node.__file__)
STRATEGY_PYX = Path(nautilus_trader.__file__).parent / "trading" / "strategy.pyx"


# -- fixture property first: omitting the kwarg IS GTC on the installed package --------------------------

def test_fixture_property_the_installed_close_position_defaults_to_GTC():
    """If Nautilus defaulted to DAY the omission would be harmless and this file would pin nothing."""
    src = STRATEGY_PYX.read_text()
    m = re.search(r"cpdef void close_position\((.*?)\)", src, re.S)
    assert m, "close_position signature not found in the installed strategy.pyx"
    assert re.search(r"time_in_force\s*=\s*TimeInForce\.GTC", m.group(1)), m.group(1)


# -- the predicate, at the source: every close_position call in the engine names DAY ----------------------

def _close_position_calls():
    tree = ast.parse(ENGINE.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "close_position":
            kws = {k.arg: ast.unparse(k.value) for k in node.keywords}
            out.append((node.lineno, kws))
    return out


def test_fixture_property_the_engine_calls_close_position_on_both_flatten_branches():
    calls = _close_position_calls()
    assert len(calls) >= 2, f"expected the immediate and the deferred flatten to call close_position; found {calls}"


def test_every_close_position_call_in_the_engine_is_DAY_not_the_GTC_default():
    offenders = [(ln, kws) for ln, kws in _close_position_calls()
                 if kws.get("time_in_force") != "TimeInForce.DAY"]
    assert offenders == [], (
        f"these close_position calls inherit Nautilus's GTC default — a liquidation must not leave a "
        f"resting order at the venue overnight: {offenders}")


def test_the_self_owned_flatten_builds_DAY_too_so_both_branches_share_one_answer():
    src = ENGINE.read_text()
    assert '"time_in_force": "day"' in src, "the self-owned flatten no longer says DAY — both branches must"


# -- the seam: the owner double records what production actually passes ---------------------------------

class _RecordingOwner:
    """Accepts what the REAL `Strategy.close_position` accepts — a double that raised on `time_in_force`
    would fail the fix, not the bug."""

    def __init__(self):
        self.calls: list[dict] = []

    def close_position(self, position, client_id=None, tags=None, time_in_force=TimeInForce.GTC,
                       reduce_only=True, quote_quantity=False, params=None):
        self.calls.append({"position": position, "tags": tags, "time_in_force": time_in_force})


class _ClearHost(_Host):
    """The immediate path reaches the close: cancels confirm and shares are available."""

    async def _await_reducing_orders_clear(self, instrument_id, strategy_id, reducing_side):
        return True

    async def _await_shares_available(self, instrument_id, qty):
        return True

    def _build_order(self, payload):
        # The immediate path builds the closing order BEFORE branching to the owner (engine_node.py
        # ~3688); on the owner branch that object is not submitted. Record the payload so the self-branch
        # TIF is visible too, and refuse a self-submit — the owner must be the one that closes.
        self.built = payload
        from types import SimpleNamespace
        return SimpleNamespace(client_order_id=payload["client_order_id"])

    def _submit(self, order, position_id=None):
        raise AssertionError("an owner-routed flatten must not self-submit")


def test_an_owner_routed_flatten_submits_the_close_DAY():
    host = _ClearHost(ledger=_Ledger())
    owner = _RecordingOwner()
    host._sibling_strategies["MOMENTUM-002"] = owner
    status, detail = asyncio.run(host._handle_flatten_command("d" * 32, {
        "instrument_id": "AEM.XNYS", "strategy_id": "MOMENTUM-002",
        "expected_side": "LONG", "expected_qty": 136}, "1-1"))
    assert owner.calls, f"the owner never closed: {status} {detail}"
    assert owner.calls[0]["time_in_force"] is TimeInForce.DAY, owner.calls[0]
    assert owner.calls[0]["tags"] and owner.calls[0]["tags"][0].startswith("flatten:")
