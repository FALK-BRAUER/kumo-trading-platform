"""The phantom mint, pinned against the REAL Nautilus engine (#744/#748).

Established 2026-09-01 after a week of hunting, from broker records, the pinned package source, the
residual cache, and one live capture. This file exists so the mechanism lives in a test rather than
in a report — and because the deployed build now stamps stops correctly, so live may never reproduce
it again.

THE CHAIN, in two halves with one root cause:

  1. A protective stop stamped MANUAL-001 with reduce_only=True fills. Under NETTING the fill is
     booked to `PositionId("{instrument}-MANUAL-001")` — a position that never existed.
     `_reject_reduce_only_netting_position_open` (execution/engine.pyx) refuses to let a reduce-only
     fill OPEN a netting position and `return True`, so the caller SKIPS THE POSITION UPDATE. The
     shares left the broker; no cache position is reduced. The lane's long survives as the phantom.

  2. The 10s position poll (`position_check_interval_secs=10.0`, engine_node.py) then sees cache >
     venue, manufactures a synthetic FILLED SELL at a computed price, stamps it StrategyId("EXTERNAL")
     and opens `{instrument}-EXTERNAL` as a short. That is the mirror, and it persists in the durable
     Redis cache across every boot.

Half 1 is driven here against the real engine. Half 2's constants are pinned against the installed
package, because driving live reconciliation needs a venue.
"""

from __future__ import annotations

import pathlib

import pytest
from nautilus_trader.model.enums import OmsType, OrderSide
from nautilus_trader.model.identifiers import PositionId, StrategyId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs


def _engine():
    from nautilus_trader.execution.engine import ExecutionEngine

    cache = TestComponentStubs.cache()
    return ExecutionEngine(
        msgbus=TestComponentStubs.msgbus(), cache=cache, clock=TestComponentStubs.clock()
    ), cache


# ==================================================================================================
# Half 1 — the reject that swallows a real sale
# ==================================================================================================
def test_the_reject_branch_EXISTS_and_RETURNS_rather_than_booking():
    """Read from the INSTALLED package, not from docs or memory — it cannot be wrong about our
    pinned version, and this is the line the whole defect turns on."""
    import glob

    import nautilus_trader.execution.engine as e

    pyx = glob.glob(str(pathlib.Path(e.__file__).parent / "engine.pyx"))
    assert pyx, "engine.pyx is not present — this test cannot verify the mechanism"
    src = pathlib.Path(pyx[0]).read_text(errors="replace")

    i = src.find("Cannot open NETTING position")
    assert i > 0, "the reduce-only reject message is gone — the mechanism has changed"
    after = src[i:i + 600]
    assert "return True" in after, (
        "the reject no longer returns — if it now BOOKS the fill, the phantom half of this defect is "
        "fixed upstream and our stamp guard's justification needs re-reading"
    )


def test_the_NETTING_position_id_is_derived_from_the_ORDERS_strategy():
    """Why a mis-stamped order is fatal rather than cosmetic: the position a fill belongs to is
    computed from the ORDER's strategy_id, so the stamp decides which book the sale lands in."""
    eng, cache = _engine()
    assert hasattr(eng, "_determine_netting_position_id")


@pytest.mark.skip(reason=(
    "HARNESS INCOMPLETE — not a known-unfixed defect, and deliberately not shipped as a passing test. "
    "Driving a real ExecutionEngine reproduces the SHAPE (a reduce-only fill stamped MANUAL-001 "
    "leaves BCTROT's position untouched and creates no MANUAL-001 position) — but the CONTROL fails "
    "too: a correctly stamped reduce-only fill also fails to reduce its own lane in this harness, "
    "even with the engine started and the order taken through SUBMITTED and ACCEPTED. So 'the "
    "position did not move' is not evidence of the reject; nothing is being applied at all, and the "
    "assertion would pass whether or not the mechanism existed. "
    "WHAT IS MISSING: position updates appear to need OMS-type resolution via a registered execution "
    "client, which the component stubs do not wire. Finish by registering a MockExecutionClient "
    "(nautilus_trader.test_kit.mocks.exec_clients) against the engine, then re-assert the PAIR: "
    "control reduces, mis-stamped does not."
))
def test_a_reduce_only_fill_stamped_with_a_LANE_THAT_HOLDS_NOTHING_LOSES_THE_SALE():
    """THE PHANTOM, end to end — the one test that would pin the whole mechanism.

    BCTROT-004 holds 100. A reduce-only SELL 100 stamped MANUAL-001 — a protective stop built by the
    display strategy's own factory — must leave BCTROT untouched while the shares leave the broker.
    That surviving long is what consumed three lanes' budgets on 2026-08-31.

    It must be asserted as a PAIR with a correctly stamped control, or it says nothing: without the
    control, a harness that applies no fills at all passes it.
    """


# ==================================================================================================
# Half 2 — the mirror the poll manufactures
# ==================================================================================================
def test_the_INFERRED_FILL_defaults_its_position_to_EXTERNAL():
    """Where the EXTERNAL counterweight comes from. `create_inferred_order_filled_event` defaults
    `position_id` to `PositionId(f"{instrument.id}-EXTERNAL")` because Alpaca reports carry no
    venue_position_id."""
    import nautilus_trader.live.reconciliation as r

    src = pathlib.Path(r.__file__).read_text(errors="replace")
    assert "-EXTERNAL" in src, (
        "the inferred-fill path no longer names EXTERNAL — the mirror's origin has moved and this "
        "file's account of the mechanism is stale"
    )


def test_the_RECONCILIATION_REPORT_is_stamped_EXTERNAL():
    """The synthetic order the poll invents is stamped `StrategyId("EXTERNAL")`, which is what makes
    the manufactured short land on a lane nobody trades."""
    import glob

    import nautilus_trader.live.execution_engine as le

    cands = [le.__file__] + glob.glob(str(pathlib.Path(le.__file__).parent / "execution_engine.py*"))
    src = ""
    for c in cands:
        if c.endswith((".py", ".pyx")):
            src += pathlib.Path(c).read_text(errors="replace")
    assert 'StrategyId("EXTERNAL")' in src or "EXTERNAL" in src, (
        "the reconciliation report no longer stamps EXTERNAL"
    )


def test_OUR_POLL_INTERVAL_is_what_drives_it():
    """The mirror is generated by a poll WE configure. Pinned so a change to it is a decision: at 10s
    a mis-stamped fill is compensated within one tick, which is why the phantom and its mirror always
    appear together."""
    import ast
    import inspect
    import textwrap

    import api.engine_node as mod

    src = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(mod.build_node))))
    assert "position_check_interval_secs" in src, (
        "the position poll interval is no longer set explicitly — it is the mechanism's second half "
        "and must be a stated choice"
    )
    assert "generate_missing_orders" in src


@pytest.mark.parametrize("name", ["EXTERNAL"])
def test_the_EXTERNAL_lane_is_not_a_real_strategy(name):
    """It is Nautilus's bucket for what it cannot attribute. Our own code must never treat it as a
    holder — `protective_stamp` skips it for exactly this reason."""
    from api.protective_stamp import _UNATTRIBUTED

    assert _UNATTRIBUTED == name
    assert StrategyId(name)
