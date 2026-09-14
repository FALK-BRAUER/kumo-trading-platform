"""The boot gate must RUN, not merely exist (#440, codex review 2026-08-23).

`run_boot_gate` was built, tested, mutation-bitten — and never called. `grep run_boot_gate` across the
package found only its own definition and a mention in its own docstring. So "preflight runs at boot"
was never true, and I said it was, repeatedly.

That is the fourth instance of one shape in a single session — an unwired detector (#439), an untested
caller argument (#462), an acceptor no caller drove (#467), and now a gate nothing invoked — and the
most embarrassing, because it is the PR that was about this class of defect.

WHERE IT HAS TO GO, and why not at attach: preflight needs equity, and equity does not exist at
registration. `_publish_account` is the first point it does, which is "shortly after boot" and hours
before any lane decides — QC345 decides at 09:35 and the snapshot lands at connection.

THIS FILE IS BLIND TO CONTROL FLOW, AND THAT COST A TENANT (#515). READ THIS BEFORE TRUSTING IT.
------------------------------------------------------------------------------------------------
Every assertion here walks the AST. An AST test CANNOT SEE A `return`. `_publish_account` opens with

    broker = self._broker_account
    if broker is not None:
        self._publish("account", {...})
        return

and the gate's original call site sat ~120 lines below that. On an Alpaca node the exec client
publishes `broker.account`, so that branch is taken on every tick — the gate was wired into the
FALLBACK branch, and test-alpaca never ran preflight at boot for its entire life. staging-ibkr ran it
only because nothing on an IBKR node publishes that topic.

This file was GREEN throughout, and its name says `IS_WIRED`. Statically reachable is not reached.

So it still earns its place — it catches "no production caller AT ALL", which is the defect codex
found on 2026-08-23 — but it is NOT evidence the gate runs. That question is behavioural and belongs
to `test_boot_gate_runs_on_the_alpaca_path.py`, which drives the publisher on BOTH branches and asks
whether the gate actually fired. Any new static reachability test in this package inherits the same
blindness; pair it with one that drives the entry point.
"""

from __future__ import annotations

import ast
import pathlib


def _src() -> str:
    return (pathlib.Path(__file__).parent / "engine_node.py").read_text()


def test_the_fixture_can_see_the_publisher():
    """Every assertion below passes over a file that does not parse or a function that moved."""
    tree = ast.parse(_src())
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_publish_account" in names, "_publish_account moved — this test is blind"


def test_run_boot_gate_HAS_A_PRODUCTION_CALLER():
    """THE DEFECT CODEX FOUND. Not 'is it imported' — is it CALLED, outside its own definition and
    outside tests."""
    tree = ast.parse(_src())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "run_boot_gate"
    ]
    assert calls, (
        "run_boot_gate is never called in engine_node — the boot gate exists, is tested, and has never "
        "run. 'preflight runs at boot' is not true while this is empty"
    )


def test_the_CHAIN_reaches_the_account_publisher_where_equity_first_exists():
    """Not at attach. Preflight needs equity and prices; neither exists at registration, so a gate wired
    there fails every cold start — which fires on the normal path, and an alarm that fires on the normal
    path gets switched off.

    Asserts the CHAIN rather than the literal call site: the gate is invoked from a small helper so the
    never-raises guard has somewhere to live, and that helper must be reached from `_publish_account`.
    An earlier version of this test demanded the call be lexically inside the publisher and failed on a
    correct implementation — a test that is wrong about working code gets deleted rather than heeded.
    """
    tree = ast.parse(_src())
    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[c] = n

    def enclosing(node):
        x = node
        while x is not None:
            if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return x.name
            x = parent.get(x)
        return None

    holder = {enclosing(n) for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "run_boot_gate"}
    assert holder, "no caller — covered by the test above"

    callers_of_holder = {
        enclosing(n) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) in holder
    }
    assert "_publish_account" in (holder | callers_of_holder), (
        f"the boot gate is reached from {sorted(holder | callers_of_holder)}, not from the account "
        f"publisher — if that point has no equity yet, every cold start reports every lane degraded"
    )


def test_the_gate_is_guarded_by_should_run_so_it_cannot_fire_every_tick():
    """`_publish_account` runs on every account update. Without the once-guard this would re-probe and
    re-alarm all day, and burn a broker call per update."""
    src = _src()
    body = src[src.index("def _publish_account"):]
    body = body[: body.index("\n    def ", 10)]
    assert "should_run" in body, (
        "the boot gate is called without `should_run` — it will fire on every account update"
    )
