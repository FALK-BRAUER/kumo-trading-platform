"""Cockpit's gateways must accept every kwarg the INSTALLED strategies call them with.

THE DIRECTION THIS COVERS, AND WHY IT WAS UNCOVERED. `test_installed_strategies_accept_what_we_pass`
asserts the outbound half: the installed classes accept what cockpit's builders SEND. The deploy's
"installed strategies accept what cockpit sends" check is that same one. Nothing asserted the
INBOUND half — that cockpit's own `SessionGateway`, which kumo-trading-strategies calls as a runner, accepts
the shape it is CALLED with.

That gap is not hypothetical. issue 118 (the gap-filter fix) adds

    momentum_rotation.py:631   await self._runner.run(panel, ..., slot=slot, opens=opens)

and cockpit's `SessionGateway.run` had no `opens`. Deploying that revision would have raised
TypeError at the decision row and killed BCTROT-004 AND MOMENTUM-002 — MOMENTUM too, because the
keyword is passed whether or not that lane's gap filter is configured. It is the August `slot=`
incident line for line: "`_j` ... forwarded `**kw`, so the one caller that passed `slot=` explicitly
raised TypeError — killing MOMENTUM and BCTROT at the decision row, before any order."

Neither repo could catch it. kumo-trading-strategies' SessionRunner protocol test binds the runners in THAT
repo; cockpit's gateway is not one of them. Cockpit's contract test only looks outbound. This is the
concrete cost of cockpit holding runner-shaped code (#793), and until that boundary moves, this test
is the seam.
"""

from __future__ import annotations

import ast
import inspect


def _kwargs_passed_to_runner_run(module) -> set[str]:
    """Every keyword the installed module passes to a `.run(...)` on its runner.

    AST over the INSTALLED source, not over a checkout: the question is what the revision this venv
    resolves will actually call, which is the same reason `test_installed_strategies_accept_what_we_pass`
    inspects the installed class rather than trusting a pin.
    """
    tree = ast.parse(inspect.getsource(module))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "run":
            continue
        target = node.func.value
        # `self._runner.run(...)` / `self._gateway.run(...)` — the runner seam. A bare `x.run()` on
        # something unrelated must not widen this.
        if not (isinstance(target, ast.Attribute) and "runner" in target.attr or
                isinstance(target, ast.Attribute) and "gateway" in target.attr):
            continue
        found |= {kw.arg for kw in node.keywords if kw.arg}
    return found


def test_the_gateway_accepts_every_kwarg_the_INSTALLED_rotation_calls_it_with():
    """The property. Killed by removing any parameter from `SessionGateway.run`."""
    from conftest import real_installed_module as real_module
    from strategies.momentum import SessionGateway

    momentum_rotation = real_module("kumo_strategies.runtime.nautilus.momentum_rotation")
    passed = _kwargs_passed_to_runner_run(momentum_rotation)
    # FIXTURE PROPERTY: if the scan found nothing, every assertion below is vacuous — that is the
    # "non-empty is not complete" shape, and a renamed attribute would silently empty it.
    assert passed, "found no runner .run(...) kwargs in the installed momentum_rotation — scan is blind"
    assert "slot" in passed, f"expected the known `slot` kwarg among {passed} — scan is matching the wrong call"

    accepted = set(inspect.signature(SessionGateway.run).parameters)
    missing = passed - accepted
    assert not missing, (
        f"the installed momentum_rotation calls the runner with {sorted(missing)}, which "
        f"cockpit's SessionGateway.run does not accept. Deploying that revision raises TypeError at "
        f"the decision row and kills every lane this gateway serves (BCTROT-004, MOMENTUM-002) "
        f"before any order is placed."
    )


class _Gate:
    """Just enough of `SessionGateway` to drive `_opens_kwarg`, which is a pure function of its
    argument and the INSTALLED runner's signature."""

    _strategy_id = "BCTROT-004"

    from strategies.momentum import SessionGateway as _SG

    _opens_kwarg = _SG._opens_kwarg


def test_the_gateway_FORWARDS_opens_rather_than_swallowing_it():
    """Accepting and dropping would be worse than crashing.

    A gateway that takes `opens` and does not pass it on leaves the gap filter reading nothing, on a
    lane whose journal still says "gap filter declined N entries" — declared and inert.

    BEHAVIOURAL, because the first version of this test grepped `SessionGateway.run`'s source for the
    word "opens" and its own DOCSTRING satisfied it: deleting the forward left the test green. A
    source grep that the documentation passes is not a test.
    """
    from kumo_strategies.runtime.executor.pgrunner import PgSessionRunner

    if "opens" not in inspect.signature(PgSessionRunner.run).parameters:
        import pytest

        pytest.skip("installed runner predates #118; the conditional path is covered below")

    kw = _Gate()._opens_kwarg({"AAPL": 1.0})
    assert kw.get("opens") == {"AAPL": 1.0}, f"opens was not forwarded to the runner: {kw}"


def test_forwarding_is_CONDITIONAL_on_the_installed_runner_accepting_it():
    """Cockpit must be deployable BEFORE issue 118, not in lockstep.

    Driven against a runner whose `run` has NO `opens`, which is what is deployed today. The first
    version grepped `SessionGateway.run` for "signature" — and the check lives in `_runner_kwargs`,
    a different function, so the test was reading a place the code was never in.
    """
    from kumo_strategies.runtime.executor import pgrunner

    real = pgrunner.PgSessionRunner.run

    async def run_without_opens(self, panel, session, jobs=None, slot="open+5m"):
        return None

    pgrunner.PgSessionRunner.run = run_without_opens
    try:
        kw = _Gate()._opens_kwarg({"AAPL": 1.0})
    finally:
        pgrunner.PgSessionRunner.run = real

    assert kw == {}, (
        f"cockpit forwarded `opens` to a runner that cannot accept it ({kw}) — that is a TypeError "
        f"at the decision row and forces a lockstep deploy with kumo-trading-strategies"
    )


def test_an_installed_runner_that_CANNOT_take_opens_is_reported_not_silently_dropped():
    """Three states, never two.

    `_live_reread_kwargs` drops unaccepted kwargs silently and the docstring of
    `test_installed_strategies_accept_what_we_pass` records what that cost: two lanes kept a slot
    captured at build, the whole suite ran against a revision production does not run, and the
    wiring test was green throughout. A dropped `opens` means the gap filter is INERT, which is a
    condition an operator must be able to see. Killed by dropping without logging.
    """
    from strategies.momentum import SessionGateway

    # THE WHOLE CLASS, and the message must name `opens` AND its consequence. The first version of
    # this test read only `SessionGateway.run` and accepted any `_log.warning` in it — there is an
    # unrelated one about a failed session observer, so it passed before the fix existed. A guard
    # satisfied by a neighbouring log line is the "detector aimed one level away" shape.
    src = inspect.getsource(SessionGateway)
    logged = [ln for ln in src.splitlines() if "INERT" in ln.upper()]
    assert logged, "nothing reports an `opens` the installed runner cannot accept"
    joined = " ".join(logged) + " " + src
    assert "opens" in joined and "_log.error" in src, (
        "the drop is reported, but not as an error naming `opens` — an operator cannot tell the gap "
        "filter went inert from it"
    )
