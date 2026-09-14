"""`judge_slot` would have caught QC345 a week ago. Nothing has ever called it.

Operator, 2026-08-23, on being told QC345 has never placed an order: "why is that news to you. we are
trying to fix it since about a week."

THE ANSWER, and it is not "nobody ran the query". The query exists, correct and tested:

    if c.decisions > 0 and c.intents == 0 and may_submit:
        return SlotVerdict(Verdict.DECIDED_NEVER_ATTEMPTED, ...)

QC345 on 2026-08-21 was `decisions 1, intents 0`, lifecycle TRADING so `may_submit` True. That is the
verdict, exactly. `SlotVerdict.alert_key` even carries the `strategy_degraded` prefix so the Notifier
routes it to `notify_strategy_degraded`.

`grep scan_slots` across api/ and scripts/ returns its own definition and its own tests. No route
serves it, no loop drives it, no monitor reads it. The failure was invisible BY CONSTRUCTION, and each
of the four tickets filed about QC345 (#385 crash, #383 no venue outcome, #459 cannot exit, #334
allocation) was a symptom found by hand.

SEVENTH unwired mechanism in this session: #439 detector, #462 caller argument, #467 acceptor, #440
boot gate, #345 `open_lots_after`, #437 `build_breaches`, and this. That is not seven coincidences. A
mechanism whose only witness is itself never gets called, because nothing fails when it isn't.
"""

from __future__ import annotations

import inspect


def test_the_predicate_would_have_caught_QC345() -> None:
    """Prove the detector is right BEFORE complaining that nothing calls it. If the predicate missed
    this case, wiring it would change nothing and the fix would be somewhere else entirely."""
    from api.slot_outcome import SlotCounts, Verdict, judge_slot

    # QC345-003, 2026-08-21, open+300m: one decision (enter AMAT DELL INTC LRCX MRVL), no order rows.
    qc345 = SlotCounts(decisions=1, intents=0, landed=0, failures=0)
    v = judge_slot(qc345, may_submit=True)
    assert v is not None and v.verdict is Verdict.DECIDED_NEVER_ATTEMPTED

    # And TECHIVOL-005 the same day: 8 intents, 0 landed, 8 failures.
    techivol = judge_slot(SlotCounts(decisions=1, intents=8, landed=0, failures=8), may_submit=True)
    assert techivol is not None and techivol.verdict is Verdict.ATTEMPTED_DID_NOT_LAND


def test_a_healthy_slot_stays_silent() -> None:
    """MOMENTUM-002 the same day — 1 decision, 6 orders, all landed. An alarm that fires on the
    working lane is one an operator switches off before the day it matters."""
    from api.slot_outcome import SlotCounts, judge_slot

    assert judge_slot(SlotCounts(decisions=1, intents=6, landed=6, failures=0), may_submit=True) is None


def test_the_scan_HAS_a_production_caller() -> None:
    """The whole point. A predicate nothing drives is a predicate that has never run."""
    from api import alerts

    src = inspect.getsource(alerts)
    assert "scan_slots" in src, (
        "nothing drives scan_slots — the detector that would have caught QC345 a week ago is still "
        "waiting to be called"
    )


def test_the_alerts_LOOP_reaches_it_and_not_just_the_module() -> None:
    """Importing it is not calling it. The chain from `run()` must actually arrive."""
    from api import alerts

    run_src = inspect.getsource(alerts.AlertsService.run)
    body = inspect.getsource(alerts.AlertsService)
    called = next((n for n in ("_announce_slots",) if n in run_src), None)
    assert called, f"AlertsService.run() does not call the slot scan: {run_src}"
    assert "scan_slots" in inspect.getsource(getattr(alerts.AlertsService, called))
    assert "alert_key" in body, "the verdict is not routed through the Notifier's dedupe key"


def test_it_is_GATED_like_every_other_alert() -> None:
    """`_enabled()` is checked per iteration precisely so that "default off" means off — a scan that
    ran regardless would query Postgres every 30s for a feature nobody switched on."""
    from api import alerts

    run_src = inspect.getsource(alerts.AlertsService.run)
    gate = run_src.index("if self._enabled():")
    assert run_src.index("_announce_slots") > gate, "the slot scan runs outside the enabled gate"


def test_a_FAILING_scan_does_not_cost_the_alerts_that_already_work() -> None:
    """`_announce_slots` runs between health and the digests. An unhandled raise there skips the
    digests for that poll — a NEW check taking down two WORKING ones, which is how a useful addition
    becomes a net loss. The loop's outer `except` catches it, but only after the damage."""
    import asyncio
    from types import SimpleNamespace

    from api import alerts

    svc = alerts.AlertsService.__new__(alerts.AlertsService)
    svc._n = SimpleNamespace(send=None)
    # `__init__` always sets this; the double must carry it too. Loosening `_check_failed` to tolerate
    # its absence would be adding a branch production can never take — the dead-guard trap.
    svc._check_failures = {}

    async def _boom():
        raise RuntimeError("postgres is down")

    svc._scan_slots_impl = _boom
    asyncio.run(svc._announce_slots())      # must not raise


def test_the_verdicts_are_REACHABLE_without_waiting_for_a_telegram(monkeypatch) -> None:
    """An operator at 09:31 needs to see this on a screen, and I need it to verify Monday. A check
    whose only output is a push nobody has enabled is the unwired problem wearing a different hat."""
    from api import app as app_module

    routes = {getattr(r, "path", None) for r in app_module.app.routes}
    assert "/slots" in routes, "the slot verdicts are not served — only a Telegram can ever show them"


def test_SHADOW_is_not_read_as_live() -> None:
    """SHADOW decides and forms no orders BY DESIGN. Reading it as live makes every dry-run session
    fire `DECIDED, NEVER ATTEMPTED` — an alarm on the one path an operator uses to try a strategy
    safely, which teaches them to ignore it before it catches anything real.

    Driven against the REAL `State` enum, not a stand-in: the whole question is what the lifecycle's
    own values translate to, and a fake enum could not be wrong about it.
    """
    from kumo_strategies.runtime.executor.lifecycle import State

    from api.alerts import may_submit_from_rows

    got = may_submit_from_rows(
        [("QC345-003", State.TRADING.value), ("TECHIVOL-005", State.SHADOW.value),
         ("BCTROT-004", "not-a-state")], State)
    assert got["QC345-003"] is True
    assert got["TECHIVOL-005"] is False, "a SHADOW lane would be alarmed on every session it ran"
    assert got["BCTROT-004"] is True, "an unknown state must not silence a live lane"
    # And the fixture must actually contain both answers, or the assertions above cannot discriminate.
    assert set(got.values()) == {True, False}


def test_the_map_USES_the_pure_translation() -> None:
    """Extracting it changes nothing unless the caller calls it."""
    import inspect

    from api import alerts

    assert "may_submit_from_rows" in inspect.getsource(alerts.AlertsService._may_submit_map)
