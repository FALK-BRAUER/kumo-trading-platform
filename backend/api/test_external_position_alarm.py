"""A non-flat position booked under EXTERNAL must ALARM, not merely render (#639).

WHAT HAPPENED WITHOUT IT. Four phantom SHORTs on paper — MRVL, RBRK, TOST, VEEV — each mirroring a
real long, all under `StrategyId("EXTERNAL")`, minted by reconciliation after the 08-27 inert-node
restart. They sat for a DAY and were found by the operator noticing that a side label contradicted the
arithmetic on the same screen. `/health` said ok throughout.

The mechanism is documented in our own code (`strategies/momentum.py:762`): a position opened
DURING a session is unclaimed until the next restart, so the startup-held set is what
reconciliation flattens — "the case that actually bit us". **Every inert-node incident mints
phantoms. #613 reports the outage; nothing reported its aftermath.**

Detection already exists — `classify_external` feeds the external_activity UI plane. The gap is the
ALARM: a plane a human must open is not a detector. This wires the alerts service to it.

WHY CRITICAL AND WHY DEDUPED PER POSITION. The operator hazard is the adopt affordance: the
UNCLAIMED row invites "tap to move to a strategy", and adopting a phantom SHORT makes the adopting
lane's exit path BUY — doubling a real long at market. So the alarm must arrive before the tap.
Deduped on (instrument, side, qty-sign) so a standing phantom does not re-page every poll, but a
NEW one does.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


def _svc(positions):
    """The alerts service bound to an ENVIRONMENT double — node, notifier, clock faked; the checker
    itself is the real method. Not a host double: overriding the method under test is how three of
    the peer's tests ran green against nothing."""
    from api.alerts import AlertsService as AlertService

    sent: list[tuple[str, object]] = []

    class _N:
        async def send(self, key, alert):
            sent.append((key, alert))
            return True

    svc = object.__new__(AlertService)
    svc._node = SimpleNamespace(positions=lambda: positions)
    svc._n = _N()
    svc._announced = set()
    svc._external_alarmed = set()
    return svc, sent


def _pos(iid, side, qty, strat="EXTERNAL"):
    return SimpleNamespace(model_dump=lambda i=iid, s=side, q=qty, st=strat: {
        "instrument_id": i, "side": s, "quantity": q, "strategy_id": st})


def test_the_fixture_contains_the_real_shape():
    """FIXTURE PROPERTY FIRST: a non-flat EXTERNAL beside a flat one and a lane-owned one. If every
    fixture position alarmed, a checker with no filter would pass."""
    rows = [_pos("MRVL.XNAS", "SHORT", 14.0), _pos("AAPL.XNAS", "FLAT", 0.0),
            _pos("VEEV.XNYS", "LONG", 10.0, strat="TECHIVOL-005")]
    d = [p.model_dump() for p in rows]
    assert sum(1 for r in d if r["strategy_id"] == "EXTERNAL" and r["side"] != "FLAT") == 1


def test_a_NON_FLAT_external_position_ALARMS_critically():
    svc, sent = _svc([_pos("MRVL.XNAS", "SHORT", 14.0)])
    asyncio.run(svc._announce_external_positions())
    assert len(sent) == 1, "a non-flat EXTERNAL position produced no alarm (#639)"
    key, alert = sent[0]
    assert "MRVL.XNAS" in alert.body and "SHORT" in alert.body
    assert alert.critical, "the phantom that doubles a long on adoption is not critical"
    assert "adopt" in alert.body.lower() or "move to a strategy" in alert.body.lower(), (
        "the alarm does not warn about the adopt affordance, which is the actual hazard"
    )


def test_FLAT_external_and_LANE_positions_do_not_alarm():
    """24 of paper's 25 EXTERNAL rows are FLAT — history, not hazard. Alarming on them is the
    always-on-alarm trap (#644/#645) that trains the operator to ignore the channel."""
    svc, sent = _svc([_pos("AAPL.XNAS", "FLAT", 0.0),
                      _pos("VEEV.XNYS", "LONG", 10.0, strat="TECHIVOL-005")])
    asyncio.run(svc._announce_external_positions())
    assert sent == []


def test_a_STANDING_phantom_does_not_repage_but_a_NEW_one_does():
    svc, sent = _svc([_pos("MRVL.XNAS", "SHORT", 14.0)])
    asyncio.run(svc._announce_external_positions())
    asyncio.run(svc._announce_external_positions())
    assert len(sent) == 1, "the same standing phantom re-paged — the channel becomes wallpaper"
    svc._node = SimpleNamespace(positions=lambda: [
        _pos("MRVL.XNAS", "SHORT", 14.0), _pos("TOST.XNYS", "SHORT", 74.0)])
    asyncio.run(svc._announce_external_positions())
    assert len(sent) == 2 and "TOST.XNYS" in sent[1][1].body


def test_a_failing_read_is_COUNTED_not_swallowed():
    """The checker must report itself when broken (BROKEN_CHECK_POLLS exists for this). A poll that
    dies silently is indistinguishable from a clean poll — the #635 phantoms were only invisible
    because nothing was looking."""

    svc, sent = _svc([])
    failures = []
    svc._check_failed = lambda name, exc: failures.append(name)
    svc._check_ok = lambda name: None
    svc._node = SimpleNamespace(positions=lambda: (_ for _ in ()).throw(RuntimeError("db")))
    asyncio.run(svc._announce_external_positions())
    assert failures == ["external_positions"], "a broken check vanished instead of reporting itself"


def test_the_run_loop_actually_CALLS_it():
    """The wiring. _schedule_bar_drain, _lane_symbols, supplies_trading_calendar — three times today
    a correct method nothing invoked shipped green. Docstring stripped so prose cannot satisfy it."""
    import ast
    import inspect
    import textwrap

    from api.alerts import AlertsService as AlertService

    tree = ast.parse(textwrap.dedent(inspect.getsource(AlertService.run)))
    fn = tree.body[0]
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    assert "_announce_external_positions" in ast.unparse(fn), (
        "run() never calls the external-position check — the alarm exists and never fires (#639)"
    )
