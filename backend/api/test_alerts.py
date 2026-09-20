"""Alert decisions (#199). Sync tests driving asyncio.run, per repo convention."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from api.alerts import _DRIFT_POLLS_BEFORE_ALERT, AlertsService, account_digest, health_conditions
from api.notify import Alert, Notifier
from api.notify.telegram import SGT

OK_HEALTH = {"subsystems": [{"name": "redis", "ok": True, "detail": ""},
                            {"name": "engine", "ok": True, "detail": ""}],
             "reconcile_drift": []}


# -- health conditions ---------------------------------------------------------------------------
def test_a_healthy_system_says_nothing():
    assert health_conditions(OK_HEALTH) == {}


def test_a_down_subsystem_is_critical():
    """Critical is the only thing that ignores quiet hours, and an engine that is not running is not
    trading — every hour down is a session missed. That is the 6 Aug failure exactly."""
    h = {**OK_HEALTH, "subsystems": [{"name": "engine", "ok": False, "detail": "no frames"}]}
    got = health_conditions(h)
    assert list(got) == ["subsystem_down:engine"]
    assert got["subsystem_down:engine"].critical is True
    assert "no frames" in got["subsystem_down:engine"].body


def test_each_down_subsystem_is_its_own_condition():
    h = {**OK_HEALTH, "subsystems": [{"name": "engine", "ok": False, "detail": ""},
                                     {"name": "redis", "ok": False, "detail": ""}]}
    assert set(health_conditions(h)) == {"subsystem_down:engine", "subsystem_down:redis"}


def test_drift_is_keyed_per_symbol():
    """A single `drift` key would announce the first symbol and stay silent through every one after."""
    h = {**OK_HEALTH, "reconcile_drift": [{"symbol": "HSBC", "broker_qty": 0, "platform_qty": -93},
                                          {"symbol": "MET", "broker_qty": 105, "platform_qty": 0}]}
    assert set(health_conditions(h)) == {"drift:HSBC", "drift:MET"}


def test_drift_wording_names_the_direction():
    """The 6 Aug phantom: cockpit showed 93 short the broker did not have. Saying 'positions held but
    not shown' there is the banner bug (#196) repeated on the phone."""
    h = {**OK_HEALTH, "reconcile_drift": [{"symbol": "HSBC", "broker_qty": 0, "platform_qty": -93}]}
    body = health_conditions(h)["drift:HSBC"].body
    assert "does not have" in body
    assert "not showing" not in body

    h2 = {**OK_HEALTH, "reconcile_drift": [{"symbol": "AAPL", "broker_qty": 100, "platform_qty": 0}]}
    assert "not showing" in health_conditions(h2)["drift:AAPL"].body


def test_drift_is_not_critical():
    """Real, but it does not justify waking someone — it is not actionable until the market opens."""
    h = {**OK_HEALTH, "reconcile_drift": [{"symbol": "X", "broker_qty": 0, "platform_qty": 1}]}
    assert health_conditions(h)["drift:X"].critical is False


# -- digest --------------------------------------------------------------------------------------
def test_the_digest_reports_the_account_not_the_strategy():
    a = account_digest("close", {"equity": 96593.9, "cash": 71077.8, "buying_power": 355756.28},
                       [{"instrument_id": "MET.XNYS", "side": "LONG", "quantity": 105,
                         "strategy_id": "MOMENTUM-002"},
                        {"instrument_id": "OLD.XNYS", "side": "FLAT", "quantity": 0,
                         "strategy_id": "X"}], [])
    assert "$96,593.90" in a.body
    assert "MET LONG 105" in a.body
    assert "OLD" not in a.body, "flat positions are not holdings"
    assert "Positions: 1" in a.body


def test_the_open_digest_carries_the_session_line():
    """'exited 6 · entered 0' is the line that would have caught 6 Aug the same evening."""
    a = account_digest("open", {"equity": 1}, [], [], session={"entered": 0, "exited": 6})
    assert "entered 0" in a.body and "exited 6" in a.body


def test_outstanding_drift_is_carried_into_the_digest():
    a = account_digest("close", {"equity": 1}, [], [{"symbol": "HSBC"}])
    assert "HSBC" in a.body and "Drift outstanding" in a.body


def test_a_digest_is_never_critical():
    assert account_digest("close", {"equity": 1}, [], []).critical is False


# -- service loop --------------------------------------------------------------------------------
class FakeNode:
    def __init__(self, drift=None):
        self._drift = drift or []

    def health(self):
        return {"reconcile_drift": self._drift}

    def positions(self):
        return []

    def account(self):
        return {"equity": 1.0}


class FakeTx:
    def __init__(self):
        self.sent = []

    async def send(self, alert: Alert, *, silent: bool = False) -> bool:
        self.sent.append(alert)
        return True


class FakeClock:
    def __init__(self, *states):
        self._states, self._i = list(states), 0

    async def get_clock(self):
        s = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return {"is_open": s}


ON = {"enabled": True}


def _svc(node, tx, http=None):
    return AlertsService(node, notifier=Notifier(transport=tx, dedupe_backend="memory", settings=ON), http=http)


def test_a_resolved_condition_is_cleared_so_it_can_alert_again(monkeypatch):
    """A symbol that drifts, reconciles, then drifts again must alert twice. Without the clear it is
    announced once and never again for the life of the process."""
    async def probe(_observed):
        return []

    monkeypatch.setattr("api.app._probe_subsystems", probe)
    tx = FakeTx()
    node = FakeNode(drift=[{"symbol": "HSBC", "broker_qty": 0, "platform_qty": -93}])
    svc = _svc(node, tx)

    # TWO polls to announce, since #519: a drift must persist for _DRIFT_POLLS_BEFORE_ALERT before it
    # is news, because a fill is transiently a drift while the projection folds it. The property under
    # test here is the CLEAR, which is unchanged.
    asyncio.run(svc._announce_health())
    asyncio.run(svc._announce_health())          # settled -> announced
    asyncio.run(svc._announce_health())          # still drifting — must not repeat
    assert len(tx.sent) == 1

    node._drift = []
    for _ in range(2):                           # must stay clean for _DOWN_POLLS_BEFORE_CLEAR
        asyncio.run(svc._announce_health())
    node._drift = [{"symbol": "HSBC", "broker_qty": 0, "platform_qty": -93}]
    for _ in range(_DRIFT_POLLS_BEFORE_ALERT):   # and it must settle again before re-alerting
        asyncio.run(svc._announce_health())
    assert len(tx.sent) == 2


def test_a_single_poll_blip_does_not_count_as_recovery(monkeypatch):
    """Clearing on ONE clean poll turned a flapping dependency into an alert per down-transition:
    down, up, down, up each read as a fresh incident. That is how a channel gets muted."""
    async def probe(_observed):
        return []

    monkeypatch.setattr("api.app._probe_subsystems", probe)
    tx = FakeTx()
    drift = [{"symbol": "HSBC", "broker_qty": 0, "platform_qty": -93}]
    node = FakeNode(drift=list(drift))
    svc = _svc(node, tx)

    for _ in range(_DRIFT_POLLS_BEFORE_ALERT):    # settle, then announce (#519)
        asyncio.run(svc._announce_health())
    assert len(tx.sent) == 1, "the fixture never announced, so the flap below proves nothing"
    for _ in range(3):                            # flap across the poll boundary
        node._drift = []
        asyncio.run(svc._announce_health())
        node._drift = list(drift)
        asyncio.run(svc._announce_health())
    assert len(tx.sent) == 1, f"a flap re-alerted {len(tx.sent)} times"


def test_the_first_clock_observation_does_not_fire_a_digest():
    """Otherwise every API restart during market hours sends an 'open' digest."""
    tx = FakeTx()
    svc = _svc(FakeNode(), tx, http=FakeClock(True, True))
    asyncio.run(svc._digests(datetime(2026, 8, 10, 22, 0, tzinfo=SGT)))
    assert tx.sent == []


def test_a_digest_fires_on_the_open_transition():
    tx = FakeTx()
    svc = _svc(FakeNode(), tx, http=FakeClock(False, True))
    now = datetime(2026, 8, 10, 21, 30, tzinfo=SGT)
    asyncio.run(svc._digests(now))               # first observation: closed
    asyncio.run(svc._digests(now))               # transition to open
    assert [a.title for a in tx.sent] == ["Account · open"]


def test_a_digest_fires_on_the_close_transition():
    tx = FakeTx()
    svc = _svc(FakeNode(), tx, http=FakeClock(True, False))
    now = datetime(2026, 8, 11, 4, 0, tzinfo=SGT)
    asyncio.run(svc._digests(now))
    asyncio.run(svc._digests(now))
    assert [a.title for a in tx.sent] == ["Account · close"]


def test_no_transition_means_no_digest():
    """A holiday never opens, so it never transitions, so it never reports a session that did not
    happen. That falls out of using the venue's clock rather than a weekday rule."""
    tx = FakeTx()
    svc = _svc(FakeNode(), tx, http=FakeClock(False, False, False))
    now = datetime(2026, 8, 10, 22, 0, tzinfo=SGT)
    for _ in range(3):
        asyncio.run(svc._digests(now))
    assert tx.sent == []


def test_a_hanging_clock_cannot_stall_the_alert_loop():
    """`run()` awaits the digests serially after the health alerts, and the Alpaca session has no
    per-call deadline — so a hung clock request would stall every future engine-down alert behind it.
    A notifier that goes quiet because an unrelated endpoint is slow is the failure this exists to
    prevent. A missed digest costs a summary; a stalled loop costs the alert that matters."""
    class Hanging:
        async def get_clock(self):
            await asyncio.sleep(3600)

    svc = _svc(FakeNode(), FakeTx(), http=Hanging())
    import api.alerts as alerts_mod
    original, alerts_mod._CLOCK_TIMEOUT = alerts_mod._CLOCK_TIMEOUT, 0.05
    try:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(svc._digests(datetime(2026, 8, 10, 22, 0, tzinfo=SGT)))
    finally:
        alerts_mod._CLOCK_TIMEOUT = original


def test_nothing_is_probed_or_fetched_while_disabled():
    """'Default off' must mean off, not 'does the work then declines to send'. Probing Redis and
    Postgres and calling the venue clock every 30s for a feature nobody enabled is load and surprise
    for no benefit."""
    calls = []

    class CountingClock:
        async def get_clock(self):
            calls.append(1)
            return {"is_open": True}

    svc = AlertsService(FakeNode(), notifier=Notifier(transport=FakeTx(), dedupe_backend="memory",
                                                      settings={"enabled": False}),
                        http=CountingClock())
    assert svc._enabled() is False
    assert calls == []


def test_a_drift_row_with_no_symbol_is_skipped_not_collapsed():
    """Every symbol-less row keyed to the same string, so the last silently overwrote the rest and
    only one was ever announced."""
    h = {**OK_HEALTH, "reconcile_drift": [{"broker_qty": 0, "platform_qty": -1},
                                          {"symbol": "MET", "broker_qty": 1, "platform_qty": 0}]}
    assert set(health_conditions(h)) == {"drift:MET"}


def test_without_a_clock_client_digests_are_skipped_not_crashed():
    svc = _svc(FakeNode(), FakeTx(), http=None)
    asyncio.run(svc._digests(datetime(2026, 8, 10, 22, 0, tzinfo=SGT)))


class RealDtoNode:
    """Returns what the real node returns — PYDANTIC models, not dicts.

    The first version of `_digests` called `.get()` on the AccountDTO. Pydantic has no `.get()`, so
    it raised AttributeError, the run loop swallowed it, and the digest silently never sent. Every
    test passed, because the fake node returned plain dicts. This node exists so that cannot recur.
    """

    def health(self):
        return {"reconcile_drift": []}

    def positions(self):
        from api.models import PositionDTO
        return [PositionDTO(instrument_id="MET.XNYS", side="LONG", quantity=105.0,
                            avg_px_open=94.85, realized_pnl="0.00 USD",
                            strategy_id="MOMENTUM-002")]

    def account(self):
        from api.models import AccountDTO
        return AccountDTO(equity=96593.9, cash=71077.8, buying_power=355756.28,
                          multiplier=4.0, ts=1786264285495842468)


def test_the_digest_works_against_the_real_dto_types():
    tx = FakeTx()
    svc = _svc(RealDtoNode(), tx, http=FakeClock(True, False))
    now = datetime(2026, 8, 11, 4, 0, tzinfo=SGT)
    asyncio.run(svc._digests(now))
    asyncio.run(svc._digests(now))
    assert len(tx.sent) == 1, "the digest must actually send against real DTOs"
    body = tx.sent[0].body
    assert "$96,593.90" in body, f"equity missing from digest: {body}"
    assert "MET LONG 105" in body, f"position missing from digest: {body}"


def test_every_alert_prefix_this_module_emits_is_gated():
    """Binds the emitter to the gate table, which had silently drifted apart.

    `_GATES` was keyed by the settings-flag names (`reconcile_drift`, `engine_down`) while this module
    emits `drift:SYM` and `subsystem_down:NAME`. Nothing matched, so every per-alert switch was inert:
    turning `notify_reconcile_drift` off changed nothing. The unit test passed because it used a key
    production never produces.

    Anything emitted here must be a deliberate entry in `_GATES` — a new alert kind should not become
    ungateable by accident.

    NON-EMPTY WAS NOT COMPLETE (#723). This test matched only `out[f"x:` and `key = f"x:`, and so
    saw THREE of the eight prefixes emitted in its own file: every alert built inline at the send
    site — `send(f"split_divergence:{sym}:{sid}", ...)`, `send(f"external_position:...")` — was
    invisible to it. `external_position` had in fact been ungated since #639 and this guard, written
    for exactly that, passed. Its own vacuity check (`assert emitted`) was satisfied by the three it
    could see.

    So the scan now covers the inline send form, and the modules that BUILD this service's alerts
    rather than `alerts.py` alone — the pool sweep keys its alerts in `pool_sweep.py`, one import
    away, and a guard that stops at a file boundary stops at the first refactor.
    """
    import re
    from pathlib import Path

    from api.notify import _GATES

    here = Path(__file__).parent
    emitters = ("alerts.py", "pool_sweep.py")
    src = "\n".join((here / name).read_text() for name in emitters)
    emitted = (set(re.findall(r'out\[f"([a-z_]+):', src))
               | set(re.findall(r'key = f"([a-z_]+):', src))
               | set(re.findall(r'send\(\s*f"([a-z_]+):', src)))
    # ASSERT COVERAGE, NOT MERELY NON-EMPTY: a scan that finds "something" is not one that finds
    # everything, and the three-of-eight miss above passed a bare non-empty check for a year.
    for name in emitters:
        assert re.search(r'f"[a-z_]+:', (here / name).read_text()), (
            f"{name} yielded no alert key at all — the scan has drifted off its subject"
        )
    assert emitted, "no alert keys found — did the emitter change shape?"
    missing = emitted - set(_GATES)
    assert not missing, (
        f"alert prefixes with no settings gate: {sorted(missing)}. Add each to _GATES and give it a "
        f"flag in config/settings/notifications.schema.json, or the switch will not switch."
    )


def test_the_gated_flags_all_exist_in_the_schema():
    """The reverse drift: a gate naming a flag the schema does not define reads as False-by-absence
    for anyone who inspects settings, while `cfg.get(flag, True)` quietly defaults it on."""
    import json
    from pathlib import Path

    from api.notify import _GATES

    schema = json.loads(
        (Path(__file__).parents[1] / "config" / "settings" / "notifications.schema.json").read_text())
    declared = set(schema["properties"])
    assert set(_GATES.values()) <= declared, f"undeclared: {set(_GATES.values()) - declared}"


def test_turning_a_switch_off_actually_silences_that_alert():
    """End to end on a REAL key, which is what the earlier version of this test failed to use."""
    tx = FakeTx()
    n = Notifier(transport=tx, dedupe_backend="memory", settings={**ON, "notify_reconcile_drift": False})
    asyncio.run(n.send("drift:HSBC", Alert("drift", "x"), now=datetime(2026, 8, 10, 12, 0, tzinfo=SGT)))
    assert tx.sent == [], "notify_reconcile_drift=False must silence drift alerts"

    n2 = Notifier(transport=tx, dedupe_backend="memory", settings={**ON, "notify_engine_down": False})
    asyncio.run(n2.send("subsystem_down:engine", Alert("down", "x", critical=True),
                        now=datetime(2026, 8, 10, 12, 0, tzinfo=SGT)))
    assert tx.sent == [], "notify_engine_down=False must silence subsystem alerts"


def test_a_node_with_no_account_still_produces_a_digest():
    """A synthetic node returns None for account. That must degrade, not crash."""
    class NoAccount(RealDtoNode):
        def account(self):
            return None

    tx = FakeTx()
    svc = _svc(NoAccount(), tx, http=FakeClock(True, False))
    now = datetime(2026, 8, 11, 4, 0, tzinfo=SGT)
    asyncio.run(svc._digests(now))
    asyncio.run(svc._digests(now))
    assert len(tx.sent) == 1


# -- trade receipts (#199 E) -----------------------------------------------------------------------
class FakeOrder:
    def __init__(self, sid, sym, side, qty, avg, status="FILLED", coid="c1"):
        self.strategy_id, self.instrument_id = sid, sym
        self.side = type("S", (), {"name": side})()
        self.status = type("St", (), {"name": status})()
        self.filled_qty, self.avg_px = qty, avg
        self.client_order_id = coid


def _receipt_probe():
    """Drive `_maybe_receipt` without a Nautilus node: capture what it would schedule."""
    from api.engine_node import UiFeedStrategy

    scheduled = []

    class Probe:
        _loop = object()                      # non-None so the guard passes
        _notifier = None

        def __init__(self):
            self._receipted = set()           # per-probe, so one test cannot leak into another
        _maybe_receipt = UiFeedStrategy._maybe_receipt

        async def _send_receipt(self, sym, side, coid, body):
            scheduled.append((sym, side, coid, body))

    return Probe(), scheduled


def test_a_partially_filled_order_gets_no_receipt(monkeypatch):
    """Per completed ORDER, not per fill. On 6 Aug Alpaca returned 12 fill events for 7 symbols and
    AFL alone filled in five pieces for one intended sell — a message per fill is a shredder."""
    import asyncio as aio

    p, scheduled = _receipt_probe()
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))
    p._maybe_receipt(FakeOrder("MOMENTUM-002", "AFL.XNYS", "SELL", 45, 126.3, status="PARTIALLY_FILLED"))
    assert scheduled == []


def test_a_manual_order_gets_no_receipt(monkeypatch):
    """MANUAL orders are the operator's own clicks; a receipt back is noise."""
    import asyncio as aio

    p, scheduled = _receipt_probe()
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))
    p._maybe_receipt(FakeOrder("MANUAL-001", "FIG.XNYS", "BUY", 233, 23.14))
    assert scheduled == []


def test_a_completed_automated_order_is_reported_once(monkeypatch):
    """FILLED is terminal, but the reconciling snapshot re-publish can revisit a finished order."""
    import asyncio as aio

    p, scheduled = _receipt_probe()
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))
    o = FakeOrder("MOMENTUM-002", "AFL.XNYS", "SELL", 80, 126.34, coid="abc")
    p._maybe_receipt(o)
    p._maybe_receipt(o)
    assert len(scheduled) == 1, scheduled
    sym, side, coid, body = scheduled[0]
    assert sym == "AFL" and side == "SELL" and coid == "abc"
    assert "AFL" in body and "80" in body and "126.34" in body
    assert "MOMENTUM-002" in body


# -- the SEAM: does anything actually CALL _maybe_receipt --------------------------------------------
#
# THE THREE TESTS ABOVE ALL PASSED WHILE THE FEATURE WAS DEAD IN PRODUCTION FOR TWO WEEKS. They drive
# `_maybe_receipt` directly, so they prove the function is correct and say nothing about whether
# anything reaches it. The single call site was `engine_node.py:4518`:
#
#     async def _apply_budget_transfer(self, seller, proceeds, fill_id, recipient) -> None:
#         try:    ...                      # the transfer commits here
#         except Exception as exc: ...
#         self._maybe_receipt(order)       # NameError: 'order' is not defined
#
# `order` was never a parameter and never bound — it appeared exactly once in that function, at the
# call. Introduced 2026-08-10 (88b896b, #204). Scheduled fire-and-forget with
# `run_coroutine_threadsafe`, so the NameError was an unretrieved task exception: nothing logged,
# nothing raised, no receipt, for fourteen sessions.
#
# It was ALSO on the wrong path. `_apply_budget_transfer` is reached only from
# `_maybe_transfer_budget`, which returns early unless `event.order_side == SELL` — so even with
# `order` bound, a completed BUY could never have produced a receipt.
#
# Drive the real entry point.


def _order_event_probe(order):
    """`UiFeedStrategy._handle_order_event` with only the node replaced. Everything the handler calls
    is real except the cache, the bus and the notifier — which is the seam under test."""
    from api.engine_node import UiFeedStrategy

    scheduled = []

    class Probe:
        _loop = object()
        _notifier = None

        def __init__(self):
            self._receipted = set()
            self._deny_reasons = {}
            self.published = []
            self.cache = type("C", (), {"order": staticmethod(lambda coid: order)})()

        _handle_order_event = UiFeedStrategy._handle_order_event
        _maybe_receipt = UiFeedStrategy._maybe_receipt

        def _publish(self, topic, frame): self.published.append((topic, frame))
        def _order_frame(self, o): return {"coid": str(o.client_order_id)}
        def _maybe_transfer_budget(self, event): pass
        def _publish_trades(self): pass

        async def _send_receipt(self, sym, side, coid, body):
            scheduled.append((sym, side, coid, body))

    return Probe(), scheduled


class FakeEvent:
    def __init__(self, coid="abc", reason=None):
        self.client_order_id = coid
        self.reason = reason


def test_a_completed_automated_order_produces_a_receipt_THROUGH_THE_EVENT_HANDLER(monkeypatch):
    """THE SEAM. Not `_maybe_receipt(order)` — `_handle_order_event(event)`, which is what Nautilus
    actually calls. A green test on the helper is what let this ship dead.
    """
    import asyncio as aio

    order = FakeOrder("MOMENTUM-002", "AFL.XNYS", "SELL", 80, 126.34, coid="abc")
    p, scheduled = _order_event_probe(order)
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))

    p._handle_order_event(FakeEvent(coid="abc"))

    assert scheduled, (
        "a FILLED automated order reached the event handler and no receipt was scheduled — the "
        "operator is not told that a machine traded on their behalf")
    sym, side, coid, _body = scheduled[0]
    assert (sym, side, coid) == ("AFL", "SELL", "abc")


def test_a_completed_BUY_also_produces_a_receipt(monkeypatch):
    """The old call site sat on the SELL-only budget path (`_maybe_transfer_budget` returns unless
    `order_side == SELL`), so a filled BUY could not have been reported even with `order` bound.
    Pins the direction, not just the wiring."""
    import asyncio as aio

    order = FakeOrder("BCTROT-004", "RGEN.XNAS", "BUY", 11, 181.13, coid="buy1")
    p, scheduled = _order_event_probe(order)
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))

    p._handle_order_event(FakeEvent(coid="buy1"))

    assert scheduled and scheduled[0][1] == "BUY", (
        "a filled BUY produced no receipt — entries are as much 'a machine traded for you' as exits")


def test_an_order_the_cache_cannot_resolve_does_not_raise(monkeypatch):
    """`cache.order()` returns None for an event whose order is not cached — a reconciling restart
    does exactly this. The handler must publish nothing and survive, not raise into Nautilus."""
    import asyncio as aio

    p, scheduled = _order_event_probe(None)
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))

    p._handle_order_event(FakeEvent(coid="gone"))
    assert scheduled == []


def test_NO_method_in_the_engine_references_a_name_it_never_binds():
    """AIMED AT THE CLASS. `self._maybe_receipt(order)` was a plain NameError sitting on a live path
    for two weeks, and every test in this repo passed. pyflakes finds it in under a second.

    This is the same defect family as the `NameError` in the trade-cycle projection that killed the
    whole feed while the UI showed an empty book against 8 held positions. One check, both caught.
    """
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache",
         "--output-format=concise", "--select", "F821", str(backend / "api"),
         str(backend / "strategies"), str(backend / "scripts")],
        capture_output=True, text=True)
    findings = [ln for ln in out.stdout.splitlines() if ": F821" in ln]
    assert not findings, (
        "undefined names on live code paths:\n  " + "\n  ".join(findings))


# -- drift must SETTLE before it is news (#519) ------------------------------------------------------
#
# WHAT HAPPENED, 2026-08-24 22:24 SGT. TECHIVOL-005 filled HPE 36 and the operator received, seconds
# later:
#
#   Broker sync drift · HPE — The broker holds HPE 36.0 that the cockpit is not showing.
#                             An empty book must never be mistaken for a flat account.
#
# By the time it was checked, `/health` reported `reconcile_drift: []` and the position was there. The
# broker reports a fill before the cockpit's projection folds it, so a drift row is TRANSIENTLY TRUE
# on the way through — on every entry, for every lane.
#
# The wording is deliberately alarming and should stay that way: it exists for #363, where a corrupted
# cached fill left the engine inert with an empty book while reporting RUNNING. An alert that ALSO
# fires routinely on every fill trains the operator to ignore exactly the message that must never be
# ignored. Three lanes entered tonight; this would have fired on each.
#
# ASYMMETRY IS DELIBERATE AND ONLY DRIFT CHANGES. A subsystem that is down should still be announced
# on the first poll — waiting would delay the alert this service exists to send. Drift is the one
# condition whose transient truth is a normal part of trading.


def _drift_row(sym="HPE"):
    return {"symbol": sym, "broker_qty": 36, "platform_qty": 0}


def test_a_drift_seen_on_ONE_poll_is_not_announced(monkeypatch):
    """The fold race. A fill is in flight; the projection catches up within a poll."""
    async def probe(_observed):
        return []

    monkeypatch.setattr("api.app._probe_subsystems", probe)
    tx = FakeTx()
    node = FakeNode(drift=[_drift_row()])
    svc = _svc(node, tx)

    asyncio.run(svc._announce_health())
    assert tx.sent == [], "a single-poll drift was announced — this fires on every fill"

    node._drift = []
    asyncio.run(svc._announce_health())
    assert tx.sent == [], "a drift that resolved within one poll was announced anyway"


def test_a_drift_that_PERSISTS_is_still_announced(monkeypatch):
    """The real case must survive the settle window, or the fix has removed the detector."""
    async def probe(_observed):
        return []

    monkeypatch.setattr("api.app._probe_subsystems", probe)
    tx = FakeTx()
    node = FakeNode(drift=[_drift_row()])
    svc = _svc(node, tx)

    for _ in range(_DRIFT_POLLS_BEFORE_ALERT):
        asyncio.run(svc._announce_health())
    assert len(tx.sent) == 1, f"a persistent drift was not announced: {tx.sent}"


def test_a_SUBSYSTEM_outage_is_still_announced_on_the_FIRST_poll(monkeypatch):
    """Only drift settles. Delaying a redis/postgres/engine alert would blunt the thing this service
    is for — and on 2026-08-24 the whole stack was down for 43 minutes with nobody told."""
    async def probe(_observed):
        return [SimpleNamespace(name="redis", ok=False, detail="connection refused")]

    monkeypatch.setattr("api.app._probe_subsystems", probe)
    tx = FakeTx()
    node = FakeNode(drift=[])
    svc = _svc(node, tx)

    asyncio.run(svc._announce_health())
    assert len(tx.sent) == 1, "a subsystem outage waited for a settle window it must not have"


def test_the_settle_counter_resets_when_the_drift_clears(monkeypatch):
    """Otherwise two unrelated one-poll blips hours apart add up to an alert nobody can explain."""
    async def probe(_observed):
        return []

    monkeypatch.setattr("api.app._probe_subsystems", probe)
    tx = FakeTx()
    node = FakeNode(drift=[_drift_row()])
    svc = _svc(node, tx)

    asyncio.run(svc._announce_health())      # blip 1
    node._drift = []
    asyncio.run(svc._announce_health())      # clean — must forget blip 1
    node._drift = [_drift_row()]
    asyncio.run(svc._announce_health())      # blip 2, and it is the FIRST of a new run
    assert tx.sent == [], "two separate blips accumulated into an alert"


# -- a restart must not replay old receipts (#520) ---------------------------------------------------
#
# `_receipted` is an in-memory set, so it is empty after every restart. The reconciling snapshot then
# re-publishes historical FILLED orders through `_handle_order_event`, and each one produces a receipt
# for a trade that happened days ago.
#
# This could not surface before 8316e00, because the single call site was a NameError and had never
# run. Observed immediately after: 6 alerts following a redeploy, including `Sell SSRM` and `Sell BETA`
# for orders that were not from that session. Small that night only because the notifier was
# unconfigured and the book was short; on a larger book a restart replays a burst.
#
# `_maybe_receipt`'s docstring already anticipates the WITHIN-process case ("FILLED is terminal, but a
# snapshot re-publish can revisit it") and the set handles that. It does not survive the process.


def _receipt_probe_at(started_ns: int):
    """The REAL `_maybe_receipt`, with a known process-start time."""
    from api.engine_node import UiFeedStrategy

    scheduled = []

    class Probe:
        _loop = object()
        _notifier = None
        _started_ns = started_ns

        def __init__(self):
            self._receipted = set()

        _maybe_receipt = UiFeedStrategy._maybe_receipt

        async def _send_receipt(self, sym, side, coid, body):
            scheduled.append((sym, side, coid, body))

    return Probe(), scheduled


def _order_at(ts_last: int, coid="old1"):
    o = FakeOrder("MOMENTUM-002", "SSRM.XNAS", "SELL", 52, 38.24, coid=coid)
    o.ts_last = ts_last
    return o


def test_an_order_that_filled_BEFORE_this_process_started_gets_no_receipt(monkeypatch):
    """The restart replay. A receipt for a days-old trade is not news, and a burst of them is noise
    arriving at the moment an operator most needs to trust the channel."""
    import asyncio as aio

    p, scheduled = _receipt_probe_at(1_000)
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))
    p._maybe_receipt(_order_at(999))
    assert scheduled == [], "a pre-boot fill was reported as if it had just happened"


def test_an_order_that_fills_AFTER_start_is_still_reported(monkeypatch):
    """The whole feature. If this stops working the fix has removed the receipts."""
    import asyncio as aio

    p, scheduled = _receipt_probe_at(1_000)
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))
    p._maybe_receipt(_order_at(1_001))
    assert len(scheduled) == 1, scheduled


def test_a_pre_boot_order_is_remembered_so_a_later_republish_stays_quiet(monkeypatch):
    """The snapshot re-publishes repeatedly. Suppressing without remembering would re-evaluate the
    same order every time, which is fine today and breaks the moment the rule gains a clock."""
    import asyncio as aio

    p, scheduled = _receipt_probe_at(1_000)
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))
    o = _order_at(999)
    p._maybe_receipt(o)
    assert str(o.client_order_id) in p._receipted


def test_an_order_with_NO_timestamp_is_still_reported(monkeypatch):
    """FAIL OPEN, deliberately, and opposite to the drift rule. A missing `ts_last` means we cannot
    tell old from new; staying silent would drop a real fill, and a spurious receipt costs a message
    while a missing one costs the operator the knowledge that a machine traded for them."""
    import asyncio as aio

    p, scheduled = _receipt_probe_at(1_000)
    monkeypatch.setattr(aio, "run_coroutine_threadsafe", lambda coro, loop: aio.run(coro))
    o = FakeOrder("MOMENTUM-002", "SSRM.XNAS", "SELL", 52, 38.24, coid="nots")
    p._maybe_receipt(o)                       # FakeOrder has no ts_last at all
    assert len(scheduled) == 1, "an order with no timestamp was silently dropped"


# -- the checks that matter must self-report (#651 item 2) -----------------------------------------
# `_check_failed` was wired only for slot_scan / never_ran / external_positions. A persistently
# raising health probe (or venue clock) propagated to `run()`'s catch instead — which skipped every
# other announce that poll and was never counted by BROKEN_CHECK_POLLS, the counter that exists so a
# broken check reports itself. The thing that would tell you is the thing that was broken.


def _run_polls(svc, seconds=0.25):
    """Drive the REAL entry point — `run()` — for a few polls, then cancel."""
    async def drive():
        task = asyncio.create_task(svc.run())
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())


def _quiet_guarded_checks(svc):
    """Silence the checks that need Postgres — each is already individually guarded and tested."""
    async def noop():
        return None

    svc._announce_slots = noop
    svc._announce_never_ran = noop
    svc._announce_external_positions = noop


def test_a_raising_health_probe_reports_itself_and_does_not_eat_the_digest(monkeypatch):
    """On main the raise propagated to run()'s catch: the digest for that poll was skipped and
    BROKEN_CHECK_POLLS never counted it — silence from the health check read as health."""
    async def broken_probe(_observed):
        raise RuntimeError("probe wedged")

    monkeypatch.setattr("api.app._probe_subsystems", broken_probe)
    tx = FakeTx()
    svc = AlertsService(FakeNode(), notifier=Notifier(transport=tx, dedupe_backend="memory",
                                                      settings=ON),
                        http=FakeClock(False, True, True, True), poll_seconds=0.01)
    _quiet_guarded_checks(svc)
    _run_polls(svc)

    titles = [a.title for a in tx.sent]
    assert "the health check is broken" in titles, (
        f"a health probe that raises every poll must eventually page as ITS OWN condition; "
        f"got only {titles}"
    )
    assert "Account · open" in titles, (
        f"the broken health check took the digest down with it — one check's failure must not "
        f"skip the others; got {titles}"
    )


def test_a_raising_venue_clock_reports_the_digest_check_broken(monkeypatch):
    """The mirror image: health fine, clock raising. On main the raise escaped run()'s per-poll
    catch uncounted, so the digest could be dead forever with no page."""
    async def probe(_observed):
        return []

    monkeypatch.setattr("api.app._probe_subsystems", probe)

    class BrokenClock:
        async def get_clock(self):
            raise RuntimeError("clock 500")

    tx = FakeTx()
    svc = AlertsService(FakeNode(), notifier=Notifier(transport=tx, dedupe_backend="memory",
                                                      settings=ON),
                        http=BrokenClock(), poll_seconds=0.01)
    _quiet_guarded_checks(svc)
    _run_polls(svc)

    assert "the digests check is broken" in [a.title for a in tx.sent], (
        "a venue clock that raises every poll must page as its own condition — a digest that dies "
        "silently is how one blip loses a day's summary forever"
    )


# -- a digest must survive one blip (#651 item 3) --------------------------------------------------
# The open/close transition was consumed BEFORE the send: `Notifier.send` returning False lost that
# day's digest permanently, and the notifier's 15-minute retry TTL was unreachable from this call
# site because nothing ever called send for that key again.


class FlakyTx:
    """Telegram down for the first N attempts — what one 502 from api.telegram.org looks like."""

    def __init__(self, fail_first=1):
        self.sent = []
        self._fail = fail_first

    async def send(self, alert: Alert, *, silent: bool = False) -> bool:
        if self._fail:
            self._fail -= 1
            return False
        self.sent.append(alert)
        return True


def test_one_failed_send_does_not_lose_the_days_digest():
    """The transition happened; delivery failed; the NEXT poll must retry rather than shrug."""
    tx = FlakyTx(fail_first=1)
    svc = _svc(FakeNode(), tx, http=FakeClock(False, True, True, True))
    # In production the dedupe paces the retry at 15 minutes (redis TTL / in-memory failed-mark);
    # here it ages instantly so the retry lands on the very next poll.
    svc._n._RETRY_AFTER_FAILURE_S = 0.0
    now = datetime(2026, 8, 10, 21, 30, tzinfo=SGT)
    asyncio.run(svc._digests(now))               # first observation: closed
    asyncio.run(svc._digests(now))               # transition to open — send fails (blip)
    assert tx.sent == [], "fixture property: the first delivery attempt must actually fail"
    asyncio.run(svc._digests(now))               # next poll — must retry, not shrug
    assert [a.title for a in tx.sent] == ["Account · open"], (
        "the open/close transition was consumed before the send — one Telegram blip lost the "
        "day's digest permanently"
    )


def test_the_digest_retry_is_bounded_and_gives_up_loudly(caplog):
    """A digest that can never deliver must stop retrying after the window — and say so."""
    import logging

    tx = FlakyTx(fail_first=10_000)
    svc = _svc(FakeNode(), tx, http=FakeClock(False, True, True, True))
    now = datetime(2026, 8, 10, 21, 30, tzinfo=SGT)
    asyncio.run(svc._digests(now))
    asyncio.run(svc._digests(now))               # transition — fails
    from api.alerts import _DIGEST_RETRY_WINDOW_S

    later = now + timedelta(seconds=_DIGEST_RETRY_WINDOW_S + 60)
    with caplog.at_level(logging.WARNING):
        asyncio.run(svc._digests(later))         # past the window — abandon, loudly
    assert tx.sent == []
    assert any("digest" in r.message and "abandon" in r.message for r in caplog.records), (
        "giving up must be its own reported condition, never silence"
    )
    # And abandoned means abandoned: no further attempts.
    attempts_before = tx._fail
    asyncio.run(svc._digests(later))
    assert tx._fail == attempts_before, "still retrying after abandoning — the bound is not a bound"


def test_a_digest_gate_turned_off_does_not_become_a_retry_loop():
    """`notify_daily_digest: false` must mean off — not a pending retry hammering the book every poll."""
    tx = FlakyTx(fail_first=0)
    svc = AlertsService(FakeNode(),
                        notifier=Notifier(transport=tx, dedupe_backend="memory",
                                          settings={**ON, "notify_daily_digest": False}),
                        http=FakeClock(False, True, True, True))
    now = datetime(2026, 8, 10, 21, 30, tzinfo=SGT)
    for _ in range(3):
        asyncio.run(svc._digests(now))
    assert tx.sent == []
    assert getattr(svc, "_pending_digest", None) is None


class MirrorPairNode(RealDtoNode):
    """The live paper book, 2026-08-30: a LONG and an offsetting SHORT of ONE symbol in two SIBLING
    lanes, netting to exactly what the broker holds.

    This is the shape no detector on the live path sees. `_report_reconcile_drift` sums `signed_qty`
    per SYMBOL, so +28 - 28 == 0 against a broker holding 0 — no drift, and it is RIGHT: the account
    IS flat. The OWNERSHIP under it is wrong. WHD stood like this for eight days, banner green.

    `_announce_external_positions` (#639) does not cover it: neither leg is EXTERNAL. Four of the
    eight mirrored shorts on paper were booked to MOMENTUM-002, so half the damage was unalarmed.
    """

    def positions(self):
        from api.models import PositionDTO
        return [
            PositionDTO(instrument_id="WHD.XNYS", side="LONG", quantity=28.0, avg_px_open=70.57,
                        realized_pnl="0.00 USD", strategy_id="BCTROT-004"),
            PositionDTO(instrument_id="WHD.XNYS", side="SHORT", quantity=28.0, avg_px_open=70.57,
                        realized_pnl="0.00 USD", strategy_id="MOMENTUM-002"),
        ]


def test_a_sibling_lane_SHORT_is_alarmed_even_though_the_book_nets_flat():
    """`api.ownership.short_violations` (#437) already implements this check and is imported by ONE
    offline script. Nothing on the live alert path calls it, which is why eight mirrored shorts stood
    for nine days while /health said ok."""
    tx = FakeTx()
    svc = _svc(MirrorPairNode(), tx)

    # The detector that DOES run today is blind here — neither leg is EXTERNAL.
    asyncio.run(svc._announce_external_positions())
    assert not tx.sent, "precondition broken: a non-EXTERNAL mirror pair should not trip #639"

    asyncio.run(svc._announce_short_violations())

    assert tx.sent, "a SHORT in a long-only lane raised no alert"
    said = " ".join(f"{a.title} {a.body}" for a in tx.sent)
    assert "MOMENTUM-002" in said, "the alert does not name the lane carrying the short"
    assert "WHD" in said, "the alert does not name the symbol"
    assert any(a.critical for a in tx.sent), "a short in a long-only book is not a mere warning"
    # The detector's contract is "report ONLY the negative leg — an alarm that indicts the victim is
    # one an operator learns to skip" (ownership.py). Hold the WIRING to it, not just the unit.
    assert "BCTROT-004" not in said, "the alert indicts the lane whose long was stranded"


def test_a_transport_blip_does_not_mute_the_short_alert_forever():
    """MARK ON DELIVERY, NEVER ON ATTEMPT — the rule `_announce_pool_unlisted` states at :846.

    Marking the local set before asking overrides the notifier's 15-minute retry-after-failure and
    run()'s per-poll re-read of the settings, so one Telegram blip — or one poll with the gate off —
    silences the alert for the life of the process. On the very alert added because eight shorts
    stood nine days unannounced, a silent mute is the failure being fixed.

    Asserted on the LOCAL set rather than on an immediate resend: after a failed send the notifier
    deliberately holds its own key for `_RETRY_AFTER_FAILURE_S`, so the next poll is expected to be
    quiet. What must NOT happen is this service forgetting there is anything left to say.
    """
    tx = FlakyTx(fail_first=1)
    svc = _svc(MirrorPairNode(), tx)

    asyncio.run(svc._announce_short_violations())   # blip: the transport refuses
    assert not tx.sent, "precondition broken: the flaky transport was supposed to fail this one"
    assert not svc._short_alarmed, (
        "a FAILED send was marked as announced — one blip mutes this alert permanently"
    )

    # The gate-off case, which the same mark kills: nothing delivered, nothing remembered.
    off = AlertsService(MirrorPairNode(),
                        notifier=Notifier(transport=FakeTx(), dedupe_backend="memory",
                                          settings={**ON, "notify_short_violation": False}))
    asyncio.run(off._announce_short_violations())
    assert not off._short_alarmed, "a gate-refused send was marked; turning the switch on is now dead"


def test_a_repaired_short_that_RECURS_is_announced_again():
    """The second occurrence must not be silent. This commit repairs no residue and does not close
    the root cause, so recurrence is the expected next event, not a hypothetical."""
    tx = FakeTx()
    node = MirrorPairNode()
    svc = _svc(node, tx)

    asyncio.run(svc._announce_short_violations())
    assert len(tx.sent) == 1

    flat = RealDtoNode()                            # the book is repaired
    svc._node = flat
    asyncio.run(svc._announce_short_violations())
    assert len(tx.sent) == 1, "a clean book must not re-page"

    svc._node = node                                # and it comes back
    asyncio.run(svc._announce_short_violations())

    assert len(tx.sent) == 2, "the recurrence after a repair was silent"


class EmptyBookNode(RealDtoNode):
    """A positions() that succeeds and returns NOTHING — a stale or seeding frame.

    `consumer.py` starts `_positions` at [] and replaces it wholesale per engine frame, so an engine
    bounce, a redis flush, or any window before the first frame lands reads as an empty book rather
    than as an error. It is not an error, so nothing raises and nothing is marked broken.
    """

    def positions(self):
        return []


def test_an_empty_book_does_not_forget_the_shorts_it_already_announced():
    """PRUNE ONLY AGAINST A READ THAT SAW THE BOOK — the clause `_announce_pool_unlisted` states at
    :886 ("pruning on [an unavailable verdict] would forget the whole standing list and re-page every
    name the moment the catalog came back").

    An empty positions frame yields no violations, which the prune would read as "every short was
    repaired" — wiping the local set AND the notifier's durable keys, so all eight standing criticals
    re-page as fresh the moment the real frame returns. A real repair still leaves ~17 legitimate
    longs on this account; an entirely empty book is stale, never good news.
    """
    tx = FakeTx()
    svc = _svc(MirrorPairNode(), tx)
    asyncio.run(svc._announce_short_violations())
    assert len(tx.sent) == 1
    assert svc._short_alarmed, "precondition broken: nothing was announced to forget"

    svc._node = EmptyBookNode()
    asyncio.run(svc._announce_short_violations())

    assert svc._short_alarmed, "a stale/empty frame was treated as a repair and forgot the standing short"

    svc._node = MirrorPairNode()                 # the real frame comes back
    asyncio.run(svc._announce_short_violations())

    assert len(tx.sent) == 1, "the standing short re-paged as fresh after an empty-frame blip"


def test_a_standing_short_is_not_re_ASKED_every_poll():
    """The local set's job beyond the first page: suppress the ATTEMPT, not merely the delivery.

    Asserted on calls into the notifier rather than on the transport, because the notifier's own
    dedupe would swallow a repeat and leave the transport count identical — a mutation replacing
    `self._short_alarmed &= standing` with `.clear()` stays green against `tx.sent`. Without this,
    a standing violation re-enters the notifier every poll and re-pages each repeat window.
    """
    tx = FakeTx()
    svc = _svc(MirrorPairNode(), tx)
    asked: list[str] = []
    real_send = svc._n.send

    async def counting_send(key, alert, **kw):
        asked.append(key)
        return await real_send(key, alert, **kw)

    svc._n.send = counting_send
    asyncio.run(svc._announce_short_violations())
    assert len(asked) == 1, "the first poll must ask exactly once"

    asyncio.run(svc._announce_short_violations())
    asyncio.run(svc._announce_short_violations())

    assert len(asked) == 1, "a standing short was re-asked; the local set is not suppressing attempts"


# ==================================================================================================
# EVERY DETECTOR MUST BE ON THE POLL LOOP — the defect this whole file exists to not repeat
# ==================================================================================================
def test_EVERY_announcer_is_actually_CALLED_from_the_poll_loop():
    """#437 EXISTED AND NOTHING CALLED IT. `ownership.short_violations` was written, tested,
    documented with the WHD incident it was built for — and imported by one offline script and by no
    live path. Eight mirrored shorts stood nine days with /health ok.

    THE FIX FOR THAT REPRODUCED IT ONE LAYER UP. Measured, not supposed: deleting
    `await self._announce_short_violations()` from `run()` left all 76 tests green, because every
    test drove the announcer DIRECTLY. So did deleting `_announce_external_positions`,
    `_announce_split_divergence` and `_announce_never_ran` — 50 green each. The whole family was
    unpinned, and a detector nothing calls is a detector that does not exist.

    Driving one announcer proves the announcer. Only this proves the WIRING, and it is written
    against the CLASS rather than the instance so the next detector added is covered by construction
    — the question is never "is #437 wired" but "what would catch #437 AND its siblings".

    Resolves real call nodes, not substrings: an announcer named in a comment or an import satisfies
    a text search, which is how the orphan guard fails at this same job.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent / "alerts.py").read_text())
    service = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == "AlertsService"), None)
    assert service is not None, "AlertsService is gone — this test is no longer looking at anything"

    declared = {n.name for n in service.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name.startswith("_announce_")}
    run = next((n for n in service.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run"), None)
    assert run is not None, "AlertsService.run is gone — this test is no longer looking at anything"

    # Reached either directly — `await self._announce_x()` — or handed to a wrapper as
    # `self._announce_x`, which is how `_checked(...)` takes them. Both are real references to the
    # bound method; a mention inside a string or a comment is not.
    reached = {
        node.attr for node in ast.walk(run)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "self" and node.attr.startswith("_announce_")
    }

    # FIXTURE PROPERTY FIRST: a parse that found no announcers at all would make the subset check
    # below pass vacuously, which is the exact shape of the bug under test.
    assert len(declared) >= 5, f"only found {declared} — the scan is no longer seeing the family"
    assert "_announce_short_violations" in declared, "the #437 announcer is not where this looks"

    orphaned = sorted(declared - reached)
    assert not orphaned, (
        f"these detectors are DECLARED and never reached from the poll loop, so they cannot fire: "
        f"{orphaned}. That is #437 exactly — a check that exists, is tested, and runs nowhere."
    )


# ==================================================================================================
# STRANDED CLAIMS: shares nobody can sell, and nothing said so (#748 / claims_invariant)
#
# Measured live on paper 2026-08-31, hours before an open:
#
#   ARKK  claimed 46  held 23  BCTROT-004=23 MOMENTUM-002=23  sellable 0  stranded 23
#   VCTR  claimed 32  held 16  BCTROT-004=16 MOMENTUM-002=16  sellable 0  stranded 16
#
# `own_ceiling` gives each lane `min(my_claim, acct - other_claims)` = `min(23, 0)` = 0. The
# arithmetic is CORRECT and exists for the WHD incident, where one lane sold shares another had
# bought — it is the safe direction. The defect is that the resulting state is a FREEZE: 39 real
# shares unreachable by either claimant on any path, including LIQUIDATING.
#
# AND NOTHING ALARMED. `exit_ceilings` computes `stranded` and only the `/claims` endpoint called it,
# so the freeze was visible to whoever thought to curl and to nobody else. Every sibling condition
# here — external positions, short violations, split divergence — pages. This one did not.
# ==================================================================================================
from api.test_alerts_node_surface import _claims_in_postgres, _production_node  # noqa: E402


def _book(*legs):
    """`(instrument_id, strategy_id, qty)` legs -> PositionDTOs, the shape the engine publishes."""
    from api.models import PositionDTO
    return [PositionDTO(instrument_id=iid, side="LONG" if q > 0 else "FLAT", quantity=abs(q),
                        avg_px_open=1.0, realized_pnl="0.00 USD", strategy_id=sid)
            for iid, sid, q in legs]


def _stranded(svc):
    """Through the poll loop's wrapper, the way `run()` reaches it — see the node-surface tests."""
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))


def test_a_position_NEITHER_LANE_CAN_SELL_pages(monkeypatch):
    """The live ARKK shape. Both lanes claim the whole 23, so both ceilings floor to zero."""
    tx = FakeTx()
    _claims_in_postgres(monkeypatch, [("BCTROT-004", "ARKK", 23.0), ("MOMENTUM-002", "ARKK", 23.0)])
    svc = _svc(_production_node(_book(("ARKK.BATS", "BCTROT-004", 23))), tx)

    _stranded(svc)

    assert tx.sent, "39 shares no lane can sell, and nothing was announced"
    a = tx.sent[0]
    assert "ARKK" in a.title
    assert a.critical is True, "a position that cannot be exited is not an FYI"
    body = a.body + a.title
    assert "BCTROT-004" in body and "MOMENTUM-002" in body, (
        "the alarm must name WHO holds the stale claims — 'ARKK is stranded' is not actionable"
    )


def test_a_CONSISTENT_ledger_is_silent(monkeypatch):
    """The common case must stay quiet, or the alarm becomes wallpaper. Claims summing to no more
    than the account leave every lane a ceiling at least its own claim."""
    tx = FakeTx()
    reads = _claims_in_postgres(monkeypatch, [("BCTROT-004", "AEM", 54.0), ("MANUAL-001", "AEM", 2.0)])
    svc = _svc(_production_node(_book(("AEM.XNYS", "BCTROT-004", 54), ("AEM.XNYS", "MANUAL-001", 2))), tx)
    _stranded(svc)
    assert tx.sent == [] and "stranded_claims" not in svc._check_failures
    assert reads.count == 1, "silent because it never read the ledger — not the same as a clean book"


def test_an_OVER_CLAIM_THAT_STRANDS_NOTHING_is_silent(monkeypatch):
    """RBRK's live shape: claimed 14 against 0 held. The ledger is inconsistent and `over_claimed`
    covers that separately — but there are no shares to strand, so this check must not fire. Alarming
    on it would page for every stale claim on a closed position, which is most of them."""
    tx = FakeTx()
    reads = _claims_in_postgres(monkeypatch, [("TECHIVOL-005", "RBRK", 14.0)])
    svc = _svc(_production_node(_book(("RBRK.XNYS", "TECHIVOL-005", 0))), tx)
    _stranded(svc)
    assert tx.sent == []
    assert reads.count == 1, "silent because it never read the ledger — not the same as a clean book"


def test_it_does_not_page_TWICE_for_the_same_frozen_symbol(monkeypatch):
    """The freeze persists until a lane reconciles, which can be hours. Re-paging every 30s is the
    wallpaper trap that gets an alert muted."""
    tx = FakeTx()
    _claims_in_postgres(monkeypatch, [("BCTROT-004", "ARKK", 23.0), ("MOMENTUM-002", "ARKK", 23.0)])
    svc = _svc(_production_node(_book(("ARKK.BATS", "BCTROT-004", 23))), tx)
    _stranded(svc)
    _stranded(svc)
    assert len(tx.sent) == 1


def test_a_freeze_that_CLEARS_and_RETURNS_is_news_again(monkeypatch):
    """These self-heal when the stale claimant next reconciles. A recurrence means the retirement did
    not hold, which is exactly what an operator needs to hear a second time."""
    tx = FakeTx()
    frozen = [("BCTROT-004", "ARKK", 23.0), ("MOMENTUM-002", "ARKK", 23.0)]
    _claims_in_postgres(monkeypatch, frozen)
    svc = _svc(_production_node(_book(("ARKK.BATS", "BCTROT-004", 23))), tx)
    _stranded(svc)

    _claims_in_postgres(monkeypatch, [("BCTROT-004", "ARKK", 23.0)])          # MOMENTUM retires
    _stranded(svc)
    _claims_in_postgres(monkeypatch, frozen)
    _stranded(svc)
    assert len(tx.sent) == 2


def test_a_BROKEN_READ_reports_ITSELF_rather_than_reading_as_clean(monkeypatch):
    """A check that fails silently is indistinguishable from a clean poll — the failure two detectors
    in this file already shipped with. It must record its own breakage, and the record must SURVIVE
    the poll loop's `_checked` wrapper (#843)."""
    tx = FakeTx()
    _claims_in_postgres(monkeypatch, [("X", "Y", 1.0)])
    node = _production_node(_book())

    def _angry():
        raise RuntimeError("positions unreadable")

    monkeypatch.setattr(node, "positions", _angry)
    svc = _svc(node, tx)
    _stranded(svc)
    n, why = svc._check_failures.get("stranded_claims", (0, ""))
    assert n >= 1, "a failed book read did not record itself — silence reads as clean"
    assert tx.sent == [], "a broken read must not also page a freeze it never established"


def test_the_stranded_claims_check_IS_IN_THE_POLL_LOOP():
    """A detector nothing calls is the class this repo keeps paying for. `exit_ceilings` computed
    `stranded` all along and only the `/claims` endpoint used it — which is why 39 frozen shares sat
    unannounced. Pinning the CALL, not just the method."""
    import ast
    import inspect
    import pathlib
    import textwrap

    from api import alerts

    tree = ast.parse(pathlib.Path(inspect.getfile(alerts)).read_text())
    run = next((n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"), None)
    assert run is not None, "AlertsService.run moved — this test is blind"
    body = textwrap.dedent(ast.unparse(run))
    assert "_announce_stranded_claims" in body, (
        "the poll loop does not call the stranded-claims check, so a frozen position pages nowhere"
    )
