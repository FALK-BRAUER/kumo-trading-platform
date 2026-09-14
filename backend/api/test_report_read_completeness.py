"""The typed venue read must see every order, not the first page of them.

WHY THIS FILE EXISTS
--------------------
Found by an adversarial review of the typed-read migration (#430), and it is not a migration bug — it
predates it and affects Nautilus's own startup reconciliation.

`AlpacaExecutionClient.generate_order_status_reports` called `list_orders(status="all")` with no
`paginate`. The HTTP client's own docstring says what that means:

    `paginate=True` follows the cursor to completion. WITHOUT IT THIS SILENTLY TRUNCATES (#387 ...)

Alpaca caps `/v2/orders` at 500 rows per page, newest first. So every consumer of this method — the
protection reconciler, the exit path, and `generate_mass_status` during startup reconciliation — has
been reading at most the newest 500 orders and treating that as the whole account. The 2026-08-20 probe
counted 269 orders on this account; the margin is one busy week.

WHY TRUNCATION IS THE DANGEROUS DIRECTION
-----------------------------------------
A short list does not look like an error. It looks like "nothing else is resting". An older protective
stop that falls off page one becomes invisible: the reconciler reads the position as naked and arms a
duplicate, and the exit path reads zero shares reserved and submits an order the venue refuses on
`available: 0`. That is #387 and #245 restated, from a missing keyword argument.
"""

from __future__ import annotations

import ast
import inspect
import textwrap


def test_the_report_read_follows_the_cursor():
    """`paginate=True`, asserted on the call rather than on a comment.

    Behavioural coverage would need a live cursor; this pins the one argument that decides whether the
    read is complete, in the one place every typed consumer goes through.
    """
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    tree = ast.parse(textwrap.dedent(inspect.getsource(
        AlpacaExecutionClient.generate_order_status_reports)))
    call = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "list_orders"),
        None,
    )
    assert call is not None, "generate_order_status_reports no longer reads orders — this test is blind"
    kw = {k.arg: k for k in call.keywords}
    assert "paginate" in kw and getattr(kw["paginate"].value, "value", None) is True, (
        "the typed venue read does not paginate, so it sees only Alpaca's newest 500 orders and reports "
        "the rest as absent — an older protective stop goes invisible, the reconciler arms a duplicate "
        "and the exit path submits against reserved shares"
    )


def test_it_asks_for_all_statuses_not_just_open():
    """`status="all"`, for the same reason `open_only=False` exists on the accessor.

    Alpaca's `open` filter does not return HELD orders, and a HELD bracket leg reserves its shares —
    the 2026-08-20 probe found two protective stops invisible to the narrow read (#387).
    """
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    tree = ast.parse(textwrap.dedent(inspect.getsource(
        AlpacaExecutionClient.generate_order_status_reports)))
    call = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "list_orders")
    kw = {k.arg: getattr(k.value, "value", None) for k in call.keywords}
    assert kw.get("status") == "all", (
        f"the typed read asks for status={kw.get('status')!r} — HELD orders reserve shares and are "
        f"absent from Alpaca's open filter"
    )


def test_the_accessor_requests_closed_orders_too():
    """`open_only=False` on the command, which is the same guarantee one layer up.

    Flipping it to True keeps every test that feeds prebuilt reports green while production stops
    asking for HELD orders — the review found exactly that mutation.
    """
    from api.engine_node import UiFeedStrategy

    tree = ast.parse(textwrap.dedent(inspect.getsource(UiFeedStrategy._venue_order_reports)))
    call = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "GenerateOrderStatusReports"),
        None,
    )
    assert call is not None, "the accessor no longer builds the command — this test is blind"
    kw = {k.arg: getattr(k.value, "value", None) for k in call.keywords}
    assert kw.get("open_only") is False, (
        f"open_only={kw.get('open_only')!r} — a HELD bracket leg reserves shares and would not be "
        f"returned, so the exit path would read zero reserved and submit against them"
    )
