"""`/health` must not say `ok` while the stack cannot do the thing it exists to do.

MEASURED, on kumo-paper 2026-08-24 03:13, minutes after a recreate:

    /health          status ok, automated 2/2, every subsystem up
    reality          KUMO_ORDERS_ARMED=false            — no order could leave the process
                     MOMENTUM-002, BCTROT-004 absent    — never registered
                     exec_strategy_state               — all five rows still say TRADING

Three surfaces agreed the stack was healthy: `status`, the subsystem list, and a lane COUNT that had
quietly changed from 4 to 2. What caught it was reading the registered lane NAMES out of an engine log
by hand.

The cause was mine — kumo-trading-platform issue 498 parameterised two gates that the legacy `up-paper` path did not
supply, so both fell to their new `false` default. But the DEFECT this file guards is not that: it is
that an operator asking "is my stack healthy" got "yes" from a stack that would place nothing all day.

THE INVARIANT: a strategy the operator has moved to TRADING is a declaration of intent. If it cannot
trade — because nothing can submit, or because it never registered — the stack is NOT ok, whatever the
subsystems say. Both halves are contradictions between what was DECLARED and what is RUNNING, which is
the only thing a health surface can honestly check.
"""
from __future__ import annotations

import pytest

from api.inert import inert_contradictions


def test_the_fixture_can_express_a_healthy_stack():
    """Fixture property first: with everything consistent there must be NO contradiction, or every
    assertion below passes for the wrong reason."""
    assert inert_contradictions(
        orders_armed=True,
        declared_trading=["MOMENTUM-002", "BCTROT-004"],
        registered=["MANUAL-001", "MOMENTUM-002", "BCTROT-004"],
    ) == []


def test_a_disarmed_stack_with_TRADING_lanes_is_a_contradiction():
    """Half one of the live incident: the gate was false while five rows said TRADING."""
    out = inert_contradictions(
        orders_armed=False,
        declared_trading=["MOMENTUM-002", "QC345-003"],
        registered=["MOMENTUM-002", "QC345-003"],
    )
    assert out, "a disarmed stack with lanes in TRADING reported no contradiction"
    assert any("ORDERS_ARMED" in c for c in out), out
    assert any("MOMENTUM-002" in c and "QC345-003" in c for c in out), (
        f"the message must name WHICH lanes are affected, not just that something is wrong: {out}")


def test_a_TRADING_lane_that_never_registered_is_a_contradiction():
    """Half two, and the one a lane COUNT cannot show: 2/2 is a perfectly healthy-looking ratio when
    the other two never arrived."""
    out = inert_contradictions(
        orders_armed=True,
        declared_trading=["MOMENTUM-002", "BCTROT-004", "QC345-003"],
        registered=["MANUAL-001", "QC345-003"],
    )
    assert any("BCTROT-004" in c and "MOMENTUM-002" in c for c in out), out
    assert any("never registered" in c or "not registered" in c for c in out), out


def test_a_disarmed_stack_with_NO_trading_lanes_is_not_a_contradiction():
    """THE DISCRIMINATING HALF. Disarmed is a legitimate state — staging ran that way all evening on
    purpose. Flagging it unconditionally would make this warning noise, and a warning that fires on a
    correct stack is one an operator learns to scroll past."""
    assert inert_contradictions(
        orders_armed=False, declared_trading=[], registered=["MANUAL-001"]) == []


def test_a_registered_lane_that_is_NOT_declared_trading_is_not_a_contradiction():
    """A lane can be registered without being declared TRADING, and that is not a contradiction.

    THE ORIGINAL REASON GIVEN HERE WAS WRONG IN BOTH HALVES. [absent-row: historical] It said "an absent lifecycle row is
    DISABLED by design" — an absent row reads as TRADING (2026-08-19) — and it cited
    MOMENTUM-002 on ibkr-paper-retired as an example of running that way, when MOMENTUM-002 has an explicit
    `DISABLED` row there. So it named the wrong rule and the wrong evidence for it.

    The assertion was right anyway: a lane the operator has explicitly moved out of TRADING is
    registered and not trading, deliberately, and that must not read as an inert stack.
    """
    assert inert_contradictions(
        orders_armed=True, declared_trading=["BCTROT-004"],
        registered=["MANUAL-001", "MOMENTUM-002", "BCTROT-004"]) == []


@pytest.mark.parametrize("declared,registered", [(None, ["A"]), (["A"], None), (None, None)])
def test_unknown_inputs_report_NOTHING_rather_than_guessing(declared, registered):
    """`None` is "could not ask", which is not "wrong" — the same three-state rule as the provenance
    stamps. Reporting a contradiction from an unreadable input would make the health surface cry wolf
    exactly when the engine is still starting."""
    assert inert_contradictions(
        orders_armed=True, declared_trading=declared, registered=registered) == []


def test_the_feed_strategy_itself_is_never_reported_missing():
    """MANUAL-001 IS the feed (`UiFeedStrategy`), not a sibling, so it never appears in `armed_lanes`.

    Reporting it as "declared TRADING but never registered" would fire on every correct stack — and a
    warning that is wrong on a healthy system is one nobody reads by the second week. The caller passes
    it as registered because the engine answering at all IS its registration.
    """
    assert inert_contradictions(
        orders_armed=True,
        declared_trading=["MANUAL-001", "BCTROT-004"],
        registered=["MANUAL-001", "BCTROT-004"],
    ) == []


class TestItIsACTUALLYWiredIntoHealth:
    """A correct rule with no caller is the defect this whole weekend has been about.

    Five mechanisms were found unwired in two days — the boot gate, `distribute_unallocated`,
    `mark_to_market`, `ensure_sleeves`, and `conforms`. Each was written, tested, exported and never
    invoked. `api/inert.py` would be the sixth if this class did not exist.
    """

    def test_health_calls_the_check(self):
        import ast
        import inspect

        import api.app as app_mod

        src = inspect.getsource(app_mod.health)
        names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                 for n in ast.walk(ast.parse(src.strip())) if isinstance(n, ast.Call)}
        assert "_inert_contradictions" in names, (
            "health() no longer calls the inert check — the rule is live and nothing reads it")

    def test_the_response_model_carries_it(self):
        from api.models import HealthResponse

        assert "inert" in HealthResponse.model_fields, (
            "HealthResponse dropped `inert` — the endpoint would compute it and the DTO would eat it, "
            "which is the exact shape of #233, #322 and #336")

    def test_the_helper_reports_the_live_incident_when_driven(self, monkeypatch):
        """Drive the REAL helper with the state kumo-paper was actually in at 03:13."""
        import asyncio
        from contextlib import asynccontextmanager

        import api.app as app_mod

        monkeypatch.setenv("KUMO_ORDERS_ARMED", "false")

        class _Result:
            """SQLAlchemy returns a Result with `.all()`, not a list.

            The first version of this double WAS a list, so `.all()` raised AttributeError, the
            helper's never-raises guard swallowed it, and the test saw an empty answer — a double that
            could not represent production, hiding the very wiring it was written to prove.
            """

            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        class _Session:
            async def execute(self, *a, **k):
                return _Result([("MOMENTUM-002",), ("BCTROT-004",), ("QC345-003",)])

        @asynccontextmanager
        async def _factory():
            yield _Session()

        monkeypatch.setattr("api.db.engine.session_factory", lambda: _factory())
        observed = {"armed_lanes": {"QC345-003": True, "TECHIVOL-005": True}}
        out = asyncio.run(app_mod._inert_contradictions(observed))
        assert any("ORDERS_ARMED" in c for c in out), out
        assert any("MOMENTUM-002" in c and "BCTROT-004" in c for c in out), (
            f"the two lanes that vanished must be NAMED: {out}")

    def test_the_helper_never_raises_when_the_database_is_unreachable(self, monkeypatch):
        """A health endpoint that fails because one check failed tells an operator nothing about the
        others — and this one runs on every poll."""
        import asyncio

        import api.app as app_mod

        def _boom():
            raise RuntimeError("postgres is down")

        monkeypatch.setattr("api.db.engine.session_factory", _boom)
        assert asyncio.run(app_mod._inert_contradictions({"armed_lanes": {}})) == []


# --------------------------------------------------------------------------------------------------
# A STALE BRIDGE IS "UNKNOWN", NOT "NOTHING IS REGISTERED"
# --------------------------------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_a_STALE_BRIDGE_reports_NOTHING_rather_than_every_lane_MISSING(monkeypatch):
    """MEASURED, ON THE DEPLOY THAT SHIPPED THIS FEATURE. Minutes after `make up`:

        /strategies  ->  5 registered: MANUAL-001 MOMENTUM-002 BCTROT-004 TECHIVOL-005 QC345-003
        /health      ->  "4 strategies are set to TRADING but never registered in the engine"

    Two readings of one fact, disagreeing, and the NEW detector was the one lying. It read
    `armed_lanes`, which `consumer.py` drops to `{}` on a stale frame — deliberately, because "a lane
    that WAS armed when the engine died is not armed now". Its own comment says "an empty list here is
    not a claim of health", and this check turned it into exactly that.

    `{}` is not `None`, so the unknown-guard never fired. Absence of evidence read as evidence of
    absence, in the detector built to catch absence of evidence.

    It fires on the NORMAL PATH — every deploy, until the first health frame lands — and an alarm that
    fires on the normal path gets switched off. It also blocked `make up`, which is how it was found.
    """
    from api import app as app_mod

    captured: dict = {}

    def _spy(**kw):
        captured.update(kw)
        return []

    monkeypatch.setattr("api.inert.inert_contradictions", _spy)

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return _Rows()

    class _Rows:
        def all(self):
            return [("MOMENTUM-002",), ("BCTROT-004",)]

    monkeypatch.setattr("api.db.engine.session_factory", lambda: _S())

    _run(app_mod._inert_contradictions(
        {"bridge_ok": False, "armed_lanes": {}}))

    assert captured, "the helper was never called — this test proves nothing"
    assert captured["registered"] is None, (
        f"registered={captured['registered']!r} on a STALE bridge — an empty `armed_lanes` from a "
        f"dead engine was read as 'these lanes do not exist', so every TRADING lane is reported "
        f"missing on every deploy until the first health frame arrives")


def test_a_LIVE_bridge_still_reports_the_lanes_it_can_see(monkeypatch):
    """The other direction. A guard that returns None whenever it is unsure is not a detector."""
    from api import app as app_mod

    captured: dict = {}

    def _spy(**kw):
        captured.update(kw)
        return []

    monkeypatch.setattr("api.inert.inert_contradictions", _spy)

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return _Rows()

    class _Rows:
        def all(self):
            return [("MOMENTUM-002",)]

    monkeypatch.setattr("api.db.engine.session_factory", lambda: _S())

    _run(app_mod._inert_contradictions(
        {"bridge_ok": True, "armed_lanes": {"MOMENTUM-002": True, "BCTROT-004": False}}))

    assert captured["registered"] is not None, "a live bridge reported 'unknown'"
    assert "MOMENTUM-002" in captured["registered"] and "BCTROT-004" in captured["registered"], (
        f"registered={captured['registered']} — a lane that is registered but NOT ARMED must still "
        f"count as registered; armed and registered are different facts")
