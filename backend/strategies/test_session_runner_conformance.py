"""The injected-runner seam: cockpit's `SessionGateway` must satisfy how kumo-strategies CALLS it.

WHY THIS FILE EXISTS
--------------------
`momentum_rotation` takes `session_runner: object | None = None` — typed `object`, no Protocol, no ABC,
no isinstance check. Cockpit injects `SessionGateway` there. Nothing anywhere asserted that the two
agree, so on 2026-08-16 kumo-strategies `076035c` added a `slot=` kwarg to the call, cockpit's signature
did not grow it, and MOMENTUM-002 raised `TypeError` at 09:35 ET on 2026-08-17 and again on 2026-08-18.
Two live sessions produced no decision. It was found by a human reading container logs on day three.

WHY BINDING `PgSessionRunner` WOULD NOT HAVE CAUGHT IT
-----------------------------------------------------
Upstream's own runner grew `slot: str = 'open+5m'` in the SAME commit as the call site, so a test
comparing caller to `PgSessionRunner` stayed green throughout. The only thing that catches this is
binding the call site against COCKPIT's implementation. (kumo-strategies made this point; it is the
reason this test binds arguments rather than comparing two signatures.)

WHY NOT BIND `_session_coro` DIRECTLY
-------------------------------------
It is upstream's private method and would break on rename. kumo-strategies is exporting a
`SessionRunner` Protocol (kumo-strategies#45); when it lands, import it here and conform to it
structurally, and delete the hand-maintained call shapes below.

Cockpit issue #346.
"""

from __future__ import annotations

import inspect

import pytest

from strategies.momentum import SessionGateway

# Every shape kumo-strategies calls an injected runner with, as of 2026-08-18. Grepped from their
# source, not recalled:
#   momentum_rotation.py:_session_coro  ->  run(panel, session, jobs=..., slot=...)
#   qc345_rotation.py:319               ->  run(panel, session)          <- NEITHER kwarg
# The second is why `jobs` and `slot` must both stay DEFAULTED. Making `slot` required would fix
# MOMENTUM and break QC345 — swapping which strategy dies rather than fixing the seam.
CALL_SHAPES = [
    pytest.param((("panel", "2026-08-18"), {"jobs": object(), "slot": "open+5m"}),
                 id="momentum_rotation:_session_coro"),
    pytest.param((("panel", "2026-08-18"), {}),
                 id="qc345_rotation:319"),
]


@pytest.mark.parametrize("call", CALL_SHAPES)
def test_the_gateway_accepts_every_shape_kumo_strategies_calls_it_with(call):
    """The seam, asserted as a BIND — the same operation Python performs at the call site.

    `Signature.bind` raises exactly where the live call raised: an unexpected keyword. Reintroduce
    `async def run(self, panel, session, jobs=None)` and the `slot` case goes red with the same
    TypeError that killed 2026-08-17 and 2026-08-18.
    """
    args, kwargs = call
    sig = inspect.signature(SessionGateway.run)
    # `self` is bound on the instance at the real call site; drop it the same way.
    sig = sig.replace(parameters=[p for n, p in sig.parameters.items() if n != "self"])
    sig.bind(*args, **kwargs)


def test_slot_reaches_the_journal_or_the_idempotency_key_is_a_lie():
    """`slot` must be THREADED, not merely accepted.

    A signature that swallows `slot` and drops it passes the bind test above while quietly writing
    every slot's decision under the same key. `uq_exec_one_decision_per_session` is
    `(strategy_id, session, slot)` WHERE kind='decision' — so a dropped slot makes BCTROT-004's two
    daily decisions (`open+150m`, `close-20m`) collide on one row, and the second silently loses to
    the first. Accepting the kwarg without forwarding it is the more dangerous half of this bug,
    because it looks fixed.

    NOTE ON THIS TEST'S OWN HISTORY: the first version asserted `"slot=slot" in src` and PASSED with
    the bug reintroduced, because the string appears twice and satisfying either occurrence was
    enough. An undiscriminating assertion is worth nothing; it has to name WHICH call site.
    """
    src = inspect.getsource(SessionGateway.run)
    runner_call = [ln for ln in src.splitlines() if ").run(" in ln]
    assert runner_call, "the runner call moved — this test can no longer see what it is asserting"
    assert all("slot=slot" in ln for ln in runner_call), (
        f"SessionGateway.run accepts `slot` but does not forward it to the runner: {runner_call!r} — "
        f"the runner would decide under the default slot regardless of which slot fired"
    )
    journal_call = [ln for ln in src.splitlines() if "session=session" in ln]
    assert journal_call and all("slot=slot" in ln for ln in journal_call), (
        f"the journal write drops `slot`: {journal_call!r} — the action log would attribute every "
        f"slot's row to the default, and (strategy_id, session, slot) stops being honest"
    )


def test_the_defaulted_kwargs_are_defaulted_for_a_reason():
    """Pins WHY `jobs` and `slot` carry defaults, so a later 'tidy-up' cannot quietly make them required.

    QC345's rotation calls `run(panel, session)` with neither. This is the disagreement check: two
    callers, one signature, and the signature must satisfy both.
    """
    params = inspect.signature(SessionGateway.run).parameters
    for name in ("jobs", "slot"):
        assert params[name].default is not inspect.Parameter.empty, (
            f"`{name}` must stay defaulted — qc345_rotation calls run(panel, session) without it"
        )
