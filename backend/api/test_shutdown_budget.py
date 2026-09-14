"""A restart must not be a kill (#381).

WHY THIS FILE EXISTS
--------------------
On 2026-08-20 a restart issued while the node was still in startup produced:

    [WARN] COCKPIT-001.TradingNode: Timed out (10.0s) waiting for node to stop
    Status
    ------
    DataEngine.check_disconnected() == False
    ExecEngine.check_disconnected() == False

Both engines were STILL CONNECTED when the budget expired, so the node stopped being asked to shut down
and was torn down mid-flight — holding three live positions. Startup here runs ~90 seconds (346 instrument
and bar requests across MOMENTUM's 92, BCTROT's 90 and QC345's 164), and every deploy passes through that
window.

TWO NUMBERS IN TWO FILES THAT MUST AGREE
----------------------------------------
The fix is not one value. Docker sends SIGTERM and SIGKILLs after `stop_grace_period`; Nautilus spends
that time on `timeout_disconnection`, then `timeout_post_stop`, then `timeout_shutdown`. Raise Nautilus's
budget alone and docker kills the process partway through the longer shutdown it was just granted —
strictly worse than before, because now it dies with MORE work in flight. Raise docker's alone and
Nautilus still gives up at 10s.

CLAUDE.md: "Two derivations of one fact will disagree. When a check exists in two places, pin that they
use the same predicate." These are one fact — how long the node may take to stop — expressed in
`engine_node.py` and `compose.paper.yml`. Nothing else makes them agree.
"""

from __future__ import annotations

import ast
import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parents[2]
_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"
_COMPOSE = _REPO / "deploy" / "compose.paper.yml"


def _node_timeout(name: str) -> float:
    """The default this repo passes for a `timeout_*` kwarg, read off the AST.

    Read from the source rather than by constructing the node: building a TradingNodeConfig needs
    clients, credentials and a running loop, and the value under test is the literal we pass.
    """
    tree = ast.parse(_ENGINE.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != name:
            continue
        # `_timeout_s("KUMO_...", "30")` → its second argument; a bare `30.0` (a constant is not a knob,
        # #954) → itself. Bound to the shape meant (l21, #977 review): a scan for "the last constant"
        # misreads `… if flag else 60` and `… or 5`, and ast.walk order is not source order.
        v = node.value
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "_timeout_s":
            assert len(v.args) >= 2 and isinstance(v.args[1], ast.Constant), f"{name}: _timeout_s default is not a constant"
            return float(v.args[1].value)
        found = []
        for const in ast.walk(node.value):
            if isinstance(const, ast.Constant) and isinstance(const.value, (int, float)) and not isinstance(const.value, bool):
                found.append(float(const.value))
            elif isinstance(const, ast.Constant) and isinstance(const.value, str):
                try:
                    found.append(float(const.value))
                except ValueError:
                    continue
        if found:
            return found[-1]
    raise AssertionError(f"{name} is not passed to TradingNodeConfig — this test is blind")


def _nautilus_default(name: str) -> float:
    from nautilus_trader.live.config import TradingNodeConfig

    return float(TradingNodeConfig().dict()[name])


def _stop_grace_seconds() -> float:
    """`stop_grace_period` on the ENGINE service, in seconds.

    Parsed with a regex rather than a YAML loader so this test carries no dependency the container may
    not have; the value is asserted to exist first, so a parse miss fails loudly instead of defaulting.
    """
    text = _COMPOSE.read_text()
    engine = text[text.index("\n  engine:") :]
    nxt = re.search(r"\n  [a-z][a-z0-9_-]*:\n", engine[1:])
    if nxt:
        engine = engine[: nxt.start() + 1]
    m = re.search(r"stop_grace_period:\s*(\d+)s", engine)
    assert m, "the engine service sets no stop_grace_period — docker's 10s default kills the node"
    return float(m.group(1))


def test_the_compose_file_is_actually_being_read():
    """The fixture's own property first — a wrong path would make everything below vacuous."""
    assert _COMPOSE.exists(), f"compose not found at {_COMPOSE}"
    assert "\n  engine:" in _COMPOSE.read_text()


def test_the_node_is_given_longer_than_nautilus_would_allow_by_default():
    """10s was the measured failure, and it is Nautilus's default — so inheriting it is the bug."""
    ours = _node_timeout("timeout_disconnection")
    theirs = _nautilus_default("timeout_disconnection")
    assert theirs == 10.0, (
        "Nautilus's default changed; the number this fix was measured against no longer holds and the "
        "reasoning needs rechecking rather than the assertion loosening"
    )
    assert ours > theirs, (
        f"timeout_disconnection is {ours}s against a default of {theirs}s — the node will give up with "
        f"its engines still connected, exactly as it did on 2026-08-20"
    )


def test_docker_grants_more_time_than_the_node_can_possibly_use():
    """THE POINT OF THIS FILE.

    Docker's grace must exceed everything Nautilus may spend after SIGTERM, or the process is SIGKILLed
    partway through a shutdown that was deliberately made longer — which leaves MORE in flight than the
    original bug did, not less.
    """
    grace = _stop_grace_seconds()
    spend = (
        _node_timeout("timeout_disconnection")
        + _nautilus_default("timeout_post_stop")
        + _nautilus_default("timeout_shutdown")
    )
    assert grace > spend, (
        f"stop_grace_period is {grace}s but the node may spend up to {spend}s stopping "
        f"(disconnection + post_stop + shutdown). Docker will SIGKILL it mid-shutdown."
    )


def test_the_disconnect_budget_is_not_confused_with_the_startup_time():
    """A guard against the obvious over-correction.

    Startup is ~90s, and the tempting reading of #381 is "make the timeout 90". It is not the same
    quantity: what has to fit is the DISCONNECT — engines closing their in-flight requests — not the
    requests completing. A 90s+ disconnect budget would make every ordinary deploy wait on a node that
    finished stopping in three seconds.
    """
    assert _node_timeout("timeout_disconnection") < 90.0, (
        "the disconnect budget has been set to the startup time; these are different quantities and the "
        "deploy path pays for the confusion on every restart"
    )
