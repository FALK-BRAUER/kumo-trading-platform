"""Startup reconciliation must be given long enough to finish, or the node runs with an empty book.

WHY THIS FILE EXISTS
--------------------
2026-08-19, twice in one evening. The node booted, spent Nautilus's default 30 seconds in
`Generating ExecutionMassStatus`, and gave up:

    [INFO]  TradingNode:      Awaiting execution state reconciliation (30.0s timeout)...
    [ERROR] ExecClient-ALPACA: Cannot reconcile execution state
    [ERROR] TradingNode:       Execution state could not be reconciled
    [INFO]  TradingNode:       RUNNING

`/positions` then returned 0 while the broker held 35, and no strategy ran. Nothing was broken and
nothing needed repairing — the venue had not finished answering. The mass status costs one Alpaca round
trip per order in the lookback, and an account's order count grows through a session, so a window that
is comfortable at the open is not comfortable at 16:00.

The failure is ASYMMETRIC, which is the whole argument for the number being generous: reconciling slowly
costs a slower boot; failing to reconcile costs every position and every strategy until a human notices,
and the node reports RUNNING the entire time.
"""

from __future__ import annotations

import ast
import pathlib

_ENGINE = pathlib.Path(__file__).parent / "engine_node.py"


def _node_config_kwargs() -> ast.Dict | None:
    tree = ast.parse(_ENGINE.read_text())
    for node in ast.walk(tree):
        # AnnAssign as well as Assign: production writes `node_config_kwargs: dict = dict(...)`, and a
        # finder that only knew Assign reported "no longer exists" against code that was right there.
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(getattr(t, "id", None) == "node_config_kwargs" for t in targets):
            return node.value
    return None


def test_the_reconciliation_timeout_is_set_at_all():
    """Nautilus's default is 30s and it is not enough here — leaving it unset is the defect."""
    call = _node_config_kwargs()
    assert call is not None, "node_config_kwargs no longer exists — this test is blind"
    assert isinstance(call, ast.Call), "node_config_kwargs is no longer built by a call"
    names = {kw.arg for kw in call.keywords}
    assert "timeout_reconciliation" in names, (
        "startup reconciliation is left on Nautilus's 30s default. When it expires the node runs with "
        "an EMPTY BOOK — no positions, no strategies — while logging RUNNING"
    )


def test_the_timeout_is_generous_and_overridable():
    """Pins the REASONING, not just a number.

    Generous because the failure is asymmetric. Overridable because the alternative to a knob is a
    rebuild, and this value can only be tuned against a real account's order count.
    """

    from api import engine_node  # noqa: F401  (import proves the module still parses)

    src = _ENGINE.read_text()
    call = _node_config_kwargs()
    kw = next(k for k in call.keywords if k.arg == "timeout_reconciliation")
    seg = ast.get_source_segment(src, kw.value) or ""
    # Read from the environment THROUGH the one helper every env-read node timeout shares (#954:
    # `_timeout_s`, blank → default, garbage → refuses naming the variable). `environ` in the segment
    # was the pre-#954 spelling; either proves the knob exists, and test_connection_timeout.py pins
    # that it reaches the container, which this file never asked.
    assert '_timeout_s("KUMO_RECONCILIATION_TIMEOUT_S"' in seg or "environ" in seg, (
        "the timeout is hardcoded — an operator hitting it again would need a rebuild to change it, "
        "which is the same trap the decision slots were in"
    )
    # The default must actually be bigger than the one that failed.
    default = next(
        (a.value for a in ast.walk(kw.value)
         if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.replace(".", "").isdigit()),
        None,
    )
    assert default is not None and float(default) > 30.0, (
        f"the default is {default!r}; 30s is the value that FAILED, twice, on 2026-08-19"
    )
