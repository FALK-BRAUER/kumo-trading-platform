"""The two repos must agree about `release_for_exit`, and nothing checked that they did.

WHY THIS FILE EXISTS
--------------------
#358 is a fix that spans two repositories. Cockpit owns `UiFeedStrategy.release_for_exit`;
kumo-trading-strategies owns `NautilusBroker.exit()`, which calls it. Between them sits a seam with no type,
no Protocol, and — until this file — nothing that checked the call could bind.

Three separate defects lived in that gap in one evening, every one of them with BOTH suites green:

  1. `exit()` called `self.strategy.release_for_exit(...)`. `broker.strategy` is the ROTATION strategy,
     a different object; the method is on the FEED strategy. AttributeError, uncaught in
     `pgrunner._submit`, aborting the whole session — exits AND entries — after the first symbol's
     intent row was journalled. Cockpit's test bound the real function to a host; kumo-trading-strategies'
     patched it onto a double. Neither used the object the broker holds.
  2. The call passed `strategy_id="MOMENTUM-002"`. Protective stops carry MANUAL-001, so the argument
     silently excluded the very order being cancelled and the cancel-confirm returned True having
     confirmed nothing. That parameter has since been REMOVED from cockpit's signature, and a stale
     caller still passing it would raise TypeError in production.
  3. A commit landed on the kumo-trading-strategies branch reasoning at length that the strategy_id argument
     was "load-bearing" — written against a signature that no longer exists.

WHY THE VENV CANNOT BE TRUSTED TO ANSWER THIS
----------------------------------------------
`pyproject.toml` pins kumo-trading-strategies by git SHA, but `deploy/Dockerfile.backend:38-39` installs it from
the LOCAL CHECKOUT passed as a build context — so the pin is decorative and the checkout is what runs.
This venv was sitting on the pinned SHA while the container ran something else entirely, which is how a
`RiskLimits` field could exist in production and be absent in tests at the same time.

So this file asserts against WHATEVER IS INSTALLED, and says so when that is stale. An installed package
without `exit()` is not a skip — it is the drift itself, reported.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _broker():
    try:
        from kumo_strategies.runtime.nautilus.broker import NautilusBroker
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"kumo_strategies is not importable ({exc}) — the seam cannot be checked at all")
    return NautilusBroker


def test_the_installed_broker_actually_has_the_exit_path():
    """The premise. Without this the rest of the file passes vacuously against an old install."""
    broker = _broker()
    assert hasattr(broker, "exit"), (
        "the INSTALLED kumo_strategies has no `NautilusBroker.exit` — the exit path is not wired in the "
        "code this venv (and, if built now, the image) would actually run. Reinstall from the checkout: "
        "`uv pip install --python .venv/bin/python --no-deps --reinstall ../../kumo-trading-strategies`"
    )
    assert "feed" in broker.__dataclass_fields__, (
        "the installed NautilusBroker has no `feed` field, so cockpit's `broker.feed = feed` sets an "
        "attribute nothing reads and every exit reports the release as unwired"
    )


def _release_call() -> ast.Call:
    """The `release_for_exit(...)` call inside the installed `exit()`, as an AST node."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(_broker().exit)))
    call = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "release_for_exit"),
        None,
    )
    assert call is not None, "`NautilusBroker.exit` never calls release_for_exit — the seam is dead"
    return call


def test_the_call_binds_against_cockpits_real_signature():
    """THE CHECK THAT WOULD HAVE CAUGHT ALL THREE DEFECTS.

    Not a string match on the source — a real `Signature.bind`. A resurrected `strategy_id=`, a renamed
    parameter, or a dropped argument all fail here for the same reason they would fail in production:
    the arguments do not bind.
    """
    from api.engine_node import UiFeedStrategy

    call = _release_call()
    sig = inspect.signature(UiFeedStrategy.release_for_exit)
    # `self` is supplied by the bound attribute access at the call site.
    positional = [object()] + [object() for _ in call.args]
    kwargs = {kw.arg: object() for kw in call.keywords if kw.arg}
    try:
        sig.bind(*positional, **kwargs)
    except TypeError as exc:
        pytest.fail(
            f"kumo-trading-strategies calls release_for_exit with {len(call.args)} positional arg(s) and "
            f"{sorted(kwargs)} — which does NOT bind against cockpit's {sig}: {exc}. "
            f"In production this is a TypeError on every exit."
        )


def test_the_call_goes_through_feed_not_strategy():
    """Defect 1, pinned by name.

    `broker.strategy` is the rotation strategy and has no `release_for_exit`. Binding checks arguments,
    not the receiver, so this is a separate assertion rather than a consequence of the one above.
    """
    call = _release_call()
    receiver = getattr(getattr(call.func, "value", None), "attr", None)
    assert receiver == "feed", (
        f"release_for_exit is called on `self.{receiver}` — it must be `self.feed`. `strategy` is the "
        f"ROTATION strategy, a different object with no delegation; the call would raise AttributeError "
        f"and abort the entire session, exits and entries alike"
    )


def test_the_lane_the_caller_passes_NEVER_BECOMES_THE_OWNER_FILTER():
    """Defect 2, pinned by name, because it FAILED SILENTLY rather than loudly.

    Passing MOMENTUM-002 as the OWNER excluded the MANUAL-001 stop from `_reducing_orders_open`, so
    `_await_reducing_orders_clear` returned True on its first poll having waited for nothing — the
    step whose absence caused #245 and #252, a no-op on the path built to end #358, every test green.

    THIS TEST USED TO ASSERT THE CALLER PASSES NO `strategy_id` AT ALL, AND THAT WAS THE WRONG
    QUESTION. As of `cb5ee79` the deployed kumo-trading-strategies DOES pass it (`nautilus/broker.py:162`),
    and that is the design completing rather than a regression: `release_for_exit` carries TWO
    identities on purpose. `owner` is the FILTER and stays `self.id` (MANUAL-001), who actually holds
    the protective stops. The lane is the AUTHORISATION identity, passed on as `canceller`, and its
    arrival is exactly what retires the `proxy` concession — engine_node.py says so in its own
    comment, and predicted this: "It disappears the moment `strategy_id` arrives."

    So the old assertion made the seam look clean by demanding the OTHER repo never change, and would
    have had someone "fix" a completed design. The property that actually protects production is
    narrower: the lane must never be used as `owner`.

    Surfaced only after the backend venv was aligned to the DEPLOYED strategies pin — the local venv
    carried an older revision, so this seam had been asserted against code production does not run.
    """
    import ast
    import inspect
    import pathlib as _pathlib

    from api import engine_node

    src = _pathlib.Path(inspect.getfile(engine_node)).read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "release_for_exit"), None)
    assert fn is not None, "release_for_exit moved — this test is blind"

    accepts = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "strategy_id" in accepts, (
        "cockpit no longer accepts `strategy_id`, but the deployed kumo-trading-strategies passes it "
        "(nautilus/broker.py:162) — that is a TypeError on every exit in production"
    )

    # THE FIXTURE'S OWN PROPERTY FIRST: `owner` must exist and must be assigned from self.id, or the
    # assertion below is about a variable that is not the filter and proves nothing.
    owner_assigns = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", None) == "owner" for t in n.targets)]
    assert owner_assigns, "no `owner` binding in release_for_exit — re-read it, this test is blind"

    # #840 REVERSED HALF OF THIS PIN. Since #748 the stop covering a lane's shares carries the LANE's
    # id, so `owner` MUST include the lane — a MANUAL-only filter confirmed nothing on 60 exits in six
    # sessions. What still must never happen is the lane REPLACING MANUAL-001: aggregate stops carry
    # MANUAL-001 and a lane-only filter would miss them (the original #245/#252 vacuity).
    for a in owner_assigns:
        src_a = ast.get_source_segment(src, a.value) or ""
        assert "self.id" in src_a, (
            f"`owner` at line {a.lineno} no longer carries self.id (MANUAL-001): an aggregate "
            f"PROT-SELL stop would be missed and `_await_reducing_orders_clear` would confirm nothing "
            f"— #245, #252, with a green suite"
        )
        assert "strategy_id" in src_a, (
            f"`owner` at line {a.lineno} no longer carries the exiting lane: since #748 the stop "
            f"resting for a lane's shares is stamped with that lane, and a MANUAL-only filter watched "
            f"nothing on every lane exit (#840)"
        )

    # And the lane must still reach the authorisation path, or the `proxy` concession never retires.
    assert "canceller=strategy_id" in (ast.get_source_segment(src, fn) or ""), (
        "the lane no longer reaches `_cancel_reducing_leg` as `canceller`, so `proxy` stays on "
        "forever and every exit keeps presenting as the feed"
    )
