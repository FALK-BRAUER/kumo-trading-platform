"""A rejected exit after a successful release leaves the position NAKED, silently (#546).

The exit sequence deliberately releases protection first — a resting stop reserves the shares, so
the sell cannot go out while it stands (#245's oversell is the alternative). `release_for_exit`
marks the instrument in `_exit_suppressed` so the protection reconciler will NOT re-arm during the
window, and the mark is TTL-bounded because every entry in it is a position deliberately left bare.

The gap: when the venue REJECTS that sell, nothing collapses the window. The suppression stands
for its full TTL, the reconciler stays told-not-to-re-arm, and no alert fires — the position is
naked for up to the TTL plus a reconciler tick and the only trace is an order frame nobody watches.
the operator's rule: degrade LOUDLY, and report the degraded state as its own condition.
"""

from __future__ import annotations

from types import SimpleNamespace

import api.engine_node as mod


class _Rejected:
    """Shaped like Nautilus's OrderRejected: the reason rides the EVENT, not the order."""

    def __init__(self, instrument_id="AAPL.XNAS", side="SELL"):
        self.client_order_id = "COID-1"
        self.instrument_id = instrument_id
        self.strategy_id = "MOMENTUM-002"
        self.reason = "insufficient buying power"
        self.order_side = SimpleNamespace(name=side)


def _cached_order(coid: str):
    """What `cache.order(coid)` returns for the ids these doubles use: a SELL exit for COID-1, a
    SELL protective stop for the PROT- id, a BUY entry for COID-BUY."""
    if coid == "COID-1":
        return SimpleNamespace(side=SimpleNamespace(name="SELL"))
    if coid.startswith("PROT-"):
        return SimpleNamespace(side=SimpleNamespace(name="SELL"))
    if coid == "COID-BUY":
        return SimpleNamespace(side=SimpleNamespace(name="BUY"))
    return None


def _host(*, suppressed, published):
    kicked = []
    host = SimpleNamespace(
        _exit_suppressed=dict(suppressed),
        _deny_reasons={},
        # The CACHE is what production consults for the rejected order's side — the event itself
        # carries none (verified against nautilus_trader.model.events.order: Rejected/Denied/
        # CancelRejected/ModifyRejected expose instrument_id, reason, client_order_id and no side).
        cache=SimpleNamespace(order=lambda coid: _cached_order(str(coid))),
        clock=SimpleNamespace(timestamp_ns=lambda: 1_000 * 10**9),
        log=SimpleNamespace(error=lambda *a, **k: published.append(("log", a[0] if a else "")),
                            warning=lambda *a, **k: None,
                            exception=lambda *a, **k: None,
                            info=lambda *a, **k: None,
                            debug=lambda *a, **k: None),
        _publish=lambda kind, frame: published.append((kind, frame)),
        # THE REAL ESCALATION, bound to the host — a stub here would only prove the seam calls
        # something, not that the something does the right thing on each of the three states.
        # The rest of the seam's collaborators, stubbed to no-ops: this test is about the escalation
        # branch, and the event carries no cached order so the frame path returns early anyway.
        _maybe_transfer_budget=lambda _e: None,
        _maybe_receipt=lambda _o: None,
        _publish_trades=lambda *a, **k: None,
        _order_frame=lambda o: {},
        _naked_positions=[],
        _kicked=kicked,
    )
    # The REAL shared predicate too — the escalation now asks it, and stubbing it would test a
    # different discriminator from the one production uses (#546, review round 2).
    host._event_is_our_exit = (
        lambda ev, window=None: mod.UiFeedStrategy._event_is_our_exit(host, ev, window=window))
    host._exit_windows = {}
    host._naked_after_reject = lambda ev: mod.UiFeedStrategy._naked_after_reject(host, ev)
    return host


def test_the_fixture_has_a_LIVE_suppression_to_collapse():
    """FIXTURE PROPERTY: the instrument really is inside its no-re-arm window when the rejection
    lands — otherwise the assertions below pass against a case that carries no risk."""
    host = _host(suppressed={"AAPL.XNAS": 2_000 * 10**9}, published=[])
    assert host._exit_suppressed["AAPL.XNAS"] > host.clock.timestamp_ns()


def test_a_rejected_exit_inside_the_release_window_is_ESCALATED():
    published = []
    host = _host(suppressed={"AAPL.XNAS": 2_000 * 10**9}, published=published)
    mod.UiFeedStrategy._handle_order_event(host, _Rejected())
    assert "AAPL.XNAS" not in host._exit_suppressed, (
        "the no-re-arm window still stands after the exit was rejected — the reconciler stays told "
        "not to re-arm and the position is naked for the rest of the TTL (#546)"
    )
    assert host._naked_positions and host._naked_positions[0]["instrument_id"] == "AAPL.XNAS", (
        "the naked position was not reported as its own condition — degrade LOUDLY (#546)"
    )


def test_a_rejection_OUTSIDE_any_release_window_is_ordinary():
    """The quiet direction: a rejected entry, or a rejected sell on a position whose protection was
    never released, must not page. An alarm on the normal path gets switched off."""
    published = []
    host = _host(suppressed={}, published=published)
    mod.UiFeedStrategy._handle_order_event(host, _Rejected())
    assert not host._naked_positions, (
        "an ordinary rejection pages — an alarm on the normal path is one that gets switched off"
    )


def test_an_EXPIRED_suppression_is_not_a_naked_window():
    """The TTL already lapsed, so the reconciler is free to re-arm — nothing to escalate."""
    published = []
    host = _host(suppressed={"AAPL.XNAS": 500 * 10**9}, published=published)
    mod.UiFeedStrategy._handle_order_event(host, _Rejected())
    assert not host._naked_positions


class _CancelRejected:
    """A CANCEL rejection — what the release sequence generates ON ITS OWN NORMAL PATH.

    `release_for_exit` cancels the resting protective stop while its own suppression window is
    live, and a cancel of an order the venue has already filled or cancelled is a routine 422 that
    the Alpaca client turns into generate_order_cancel_rejected. Nautilus sets
    `reason = reason or str(None)`, so EVERY reject family carries a truthy reason and reaches the
    escalation gate — including this one.
    """

    def __init__(self, instrument_id="AAPL.XNAS"):
        self.client_order_id = "PROT-SELL-AAPL-XNAS-1"
        self.instrument_id = instrument_id
        self.strategy_id = "MANUAL-001"
        self.reason = "order already filled"
        self.order_side = SimpleNamespace(name="SELL")


def test_a_CANCEL_rejection_does_NOT_collapse_the_window():
    """REVIEW FINDING (2026-08-29). Collapsing here is the failure `_extend_standoff` exists to
    prevent: the reconciler is handed the instrument back MID-RELEASE and can re-arm a stop that
    re-reserves the very shares the exit is still waiting for, so the exit never goes out and the
    rotation silently does not rotate — plus a critical page claiming the position is unprotected
    when protection was never lost."""
    published = []
    host = _host(suppressed={"AAPL.XNAS": 2_000 * 10**9}, published=published)
    mod.UiFeedStrategy._handle_order_event(host, _CancelRejected())
    assert "AAPL.XNAS" in host._exit_suppressed, (
        "a rejected CANCEL collapsed the release window — the reconciler can now re-arm under a "
        "live exit and re-reserve its shares (the #245 oversell's precondition)"
    )
    assert not host._naked_positions, (
        "a routine cancel-rejection pages as UNPROTECTED — an alarm on the normal path"
    )


def test_the_ALARM_IS_ON_THE_ENGINE_FRAME():
    """The PUBLISHER half only, and it says so — the first version of this test claimed in its
    docstring that /health served the field, which was FALSE: HealthResponse had no such field and
    pydantic dropped it (review round 2, the fourth instance of #233/#322/#336). The CONSUMER half
    is pinned in test_naked_positions_survive_the_dto.py, against the model and the endpoint."""
    import ast
    import inspect
    import textwrap

    src = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(mod.UiFeedStrategy._on_snapshot))))
    assert "naked_after_reject" in src, (
        "the naked-position escalation is not on the health frame — it reaches no operator"
    )
