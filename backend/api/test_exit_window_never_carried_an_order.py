"""A release window that expires having never carried an exit is a SILENT naked position (#546 part 2).

Review finding, 2026-08-29. `NautilusBroker.exit` releases protection and then calls `submit()`,
which has FOUR local refusal paths that return ok=False BEFORE Nautilus creates an order:

    "{symbol} is not a subscribed instrument"      broker.py:74
    "no instrument definition cached for {symbol}" broker.py:77
    "{type(e).__name__}: {e}"                      broker.py:104
    "order not in the cache after submit"          broker.py:115

In every one: protection cancelled, sell never at the venue, and NO OrderRejected/OrderDenied is
emitted — so #546's rejection hook cannot fire. The suppression stands for its full TTL, the
reconciler is told not to re-arm, and nothing says the position is bare. The first two are not
hypothetical: qc27_runner's own comment records reading `.reason` instead of `.detail` and
discarding exactly those strings.

The observable that does not depend on an event: a window that CLOSED having never seen an exit
order for its instrument.
"""

from __future__ import annotations

from types import SimpleNamespace

import api.engine_node as mod


def _host(windows, now_ns):
    host = SimpleNamespace(
        _exit_windows=dict(windows),
        _naked_positions=[],
        clock=SimpleNamespace(timestamp_ns=lambda: now_ns),
        log=SimpleNamespace(error=lambda *a, **k: None, warning=lambda *a, **k: None,
                            info=lambda *a, **k: None, debug=lambda *a, **k: None),
    )
    return host


def test_the_fixture_holds_a_window_that_actually_expired():
    """FIXTURE PROPERTY: the window's deadline is in the past and it never saw an exit — without
    both, the sweep below has nothing to find and the test cannot fail."""
    host = _host({"AAPL.XNAS": {"until": 500, "saw_exit": False, "qty": 10}}, now_ns=1_000)
    w = host._exit_windows["AAPL.XNAS"]
    assert w["until"] < host.clock.timestamp_ns() and not w["saw_exit"]


def test_a_window_that_expired_WITHOUT_an_exit_is_reported():
    host = _host({"AAPL.XNAS": {"until": 500, "saw_exit": False, "qty": 10}}, now_ns=1_000)
    mod.UiFeedStrategy._sweep_exit_windows(host)
    assert host._naked_positions and host._naked_positions[0]["instrument_id"] == "AAPL.XNAS", (
        "a release window closed with no exit order ever sent and nothing reported it — the "
        "position was bare for the whole TTL in silence (#546 part 2)"
    )
    assert "AAPL.XNAS" not in host._exit_windows, "the expired window was not pruned"


def test_a_window_that_DID_carry_an_exit_is_silent():
    """The quiet direction: the exit went out, so the window closing is the normal path."""
    host = _host({"AAPL.XNAS": {"until": 500, "saw_exit": True, "qty": 10}}, now_ns=1_000)
    mod.UiFeedStrategy._sweep_exit_windows(host)
    assert host._naked_positions == []


def test_a_window_still_OPEN_is_left_alone():
    """An exit in flight is not a finding — reporting it would page on every normal exit."""
    host = _host({"AAPL.XNAS": {"until": 2_000, "saw_exit": False, "qty": 10}}, now_ns=1_000)
    mod.UiFeedStrategy._sweep_exit_windows(host)
    assert host._naked_positions == [] and "AAPL.XNAS" in host._exit_windows


def test_the_RECONCILER_actually_runs_the_sweep():
    """THE WIRING. The sweep must run on a timer, not on an event — its whole subject is the case
    where NO event arrives. The protection reconciler is that timer."""
    import ast
    import inspect
    import textwrap

    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(mod.UiFeedStrategy._reconcile_protection_inner))))
    assert "_sweep_exit_windows()" in src, (
        "nothing calls the exit-window sweep — the silent-naked case stays silent"
    )


def test_saw_exit_is_NOT_set_by_another_order_on_the_same_instrument():
    """REVIEW ROUND 2. `saw_exit` fired on ANY non-PROT- order event for the instrument: another
    lane's entry BUY, a MANUAL click, the reconciling snapshot re-publish of historical FILLED
    orders (which produced six false receipts on 2026-08-24), or a bracket protective leg whose
    client order id ALPACA generated and which therefore does not start with PROT-.

    The live paper book makes each of those reachable: DELL is held by QC345 and TECHIVOL, and
    AEM/GMAB/ARKK/RGEN/SSRM/VCTR by BCTROT and MOMENTUM — and rotation fires many orders at once,
    which is exactly when release windows are open. A false mark makes the sweep silent, so the
    detector goes blind precisely when the instrument is busy.

    One predicate for "is this event about our exit", shared with the rejection hook — the same
    file already had two, and they disagreed.
    """
    from types import SimpleNamespace as NS

    host = _host({"AAPL.XNAS": {"until": 5_000, "saw_exit": False, "qty": 10}}, now_ns=1_000)
    host._deny_reasons = {}
    host._naked_positions = []
    host.cache = NS(order=lambda coid: {
        "BUY-1": NS(side=NS(name="BUY")),        # another lane's entry
        "SELL-1": NS(side=NS(name="SELL")),      # a real exit
    }.get(str(coid)))
    host._order_frame = lambda o: {}
    host._maybe_transfer_budget = lambda e: None
    host._maybe_receipt = lambda o: None
    host._publish = lambda *a, **k: None
    host._publish_trades = lambda *a, **k: None
    host._naked_after_reject = lambda ev: None
    host._event_is_our_exit = (
        lambda ev, window=None: mod.UiFeedStrategy._event_is_our_exit(host, ev, window=window))

    buy = NS(client_order_id="BUY-1", instrument_id="AAPL.XNAS", strategy_id="MOMENTUM-002")
    mod.UiFeedStrategy._handle_order_event(host, buy)
    assert host._exit_windows["AAPL.XNAS"]["saw_exit"] is False, (
        "another lane's BUY marked our exit window as carried — the sweep goes blind"
    )

    sell = NS(client_order_id="SELL-1", instrument_id="AAPL.XNAS", strategy_id="MOMENTUM-002")
    mod.UiFeedStrategy._handle_order_event(host, sell)
    assert host._exit_windows["AAPL.XNAS"]["saw_exit"] is True, (
        "a real exit did NOT mark the window — the sweep would page on a successful exit"
    )


def test_a_FAILED_release_closes_its_window_rather_than_reporting_later():
    """REVIEW ROUND 2. `release_for_exit`'s finally pops `_exit_suppressed` when the release did not
    land — the DESIGNED, RECOVERED outcome: nothing is sent, protection stands, the reconciler owns
    the instrument again, and the function already logged it. The window was NOT popped there, so it
    expired later and the sweep announced 'the position was unprotected for the whole window' about
    a position that never lost its protection. A false page on an already-handled condition — and
    the share-release timeout is the #245/#252 family, which fired live for FSM and VCTR."""
    import asyncio
    from types import SimpleNamespace as NS

    host = NS(
        _exit_suppressed={"AAPL.XNAS": 9_999},
        _exit_windows={"AAPL.XNAS": {"until": 9_999, "saw_exit": False, "qty": 10}},
        _naked_positions=[],
        clock=NS(timestamp_ns=lambda: 1_000),
        log=NS(error=lambda *a, **k: None, warning=lambda *a, **k: None,
               info=lambda *a, **k: None, debug=lambda *a, **k: None, exception=lambda *a, **k: None),
    )
    mod.UiFeedStrategy._close_exit_window(host, "AAPL.XNAS")
    assert "AAPL.XNAS" not in host._exit_windows, (
        "a released=False path left its window open — the sweep will page about a position whose "
        "protection was never lost"
    )
    # ...and the sweep then has nothing to say.
    mod.UiFeedStrategy._sweep_exit_windows(host)
    assert host._naked_positions == []


def test_the_RELEASE_PATH_closes_the_window_where_it_drops_the_suppression():
    """The wiring for the above: the same `if not released` branch must do both, or they drift."""
    import ast
    import inspect
    import textwrap

    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(mod.UiFeedStrategy.release_for_exit))))
    assert "_close_exit_window(" in src, (
        "release_for_exit never closes its observation window on the failure path"
    )


def test_saw_exit_ignores_ANOTHER_LANES_sell_and_a_REPLAYED_fill():
    """REVIEW ROUND 3. Sharing one predicate closed the entry-BUY case; four remained, all SELLs:

      * another LANE's exit on the same instrument — BCTROT and MOMENTUM rotate the SAME pool and
        exit the same names in the same minutes; DELL is held by QC345 and TECHIVOL
      * the reconciling snapshot RE-PUBLISH of historical FILLED orders (engine_node.py:771 — this
        path produced six false receipts on 2026-08-24)
      * a bracket PROTECTIVE leg whose client order id ALPACA generated, so it lacks the PROT-
        prefix — and the release sequence CANCELS those legs inside the window
      * a MANUAL-001 discretionary sell

    Each marks the window carried, so the sweep stays silent about a position that never got its
    exit. Time and lane are the practical discriminators: the exit's own coid is unknowable here
    because the caller submits AFTER release_for_exit returns.
    """
    from types import SimpleNamespace as NS

    win = {"until": 5_000, "saw_exit": False, "qty": 10, "opened_ns": 1_000, "lane": "MOMENTUM-002"}
    host = _host({"AAPL.XNAS": dict(win)}, now_ns=2_000)
    host.cache = NS(order=lambda coid: NS(side=NS(name="SELL")))

    stale = NS(client_order_id="OLD-1", instrument_id="AAPL.XNAS",
               strategy_id="MOMENTUM-002", ts_event=500)          # predates the window
    assert mod.UiFeedStrategy._event_is_our_exit(host, stale, window=host._exit_windows["AAPL.XNAS"]) is False, (
        "a replayed historical fill marked the window — the snapshot path does exactly this"
    )

    other = NS(client_order_id="NEW-1", instrument_id="AAPL.XNAS",
               strategy_id="BCTROT-004", ts_event=1_500)          # another lane, inside the window
    assert mod.UiFeedStrategy._event_is_our_exit(host, other, window=host._exit_windows["AAPL.XNAS"]) is False, (
        "another lane's exit on the same instrument marked our window — they rotate one pool"
    )

    ours = NS(client_order_id="NEW-2", instrument_id="AAPL.XNAS",
              strategy_id="MOMENTUM-002", ts_event=1_500)
    assert mod.UiFeedStrategy._event_is_our_exit(host, ours, window=host._exit_windows["AAPL.XNAS"]) is True, (
        "our own exit inside the window was rejected — the sweep would page on a successful exit"
    )
