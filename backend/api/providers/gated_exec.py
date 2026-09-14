"""Put this repo's submit-path rules in front of an exec client, whoever wrote it (#782, #1030).

TWO WRAPS, ONE FILE, so the property "every exec client carries both" has one place to be true.
`install_budget_gate` (#782) is the capital rule; `install_ownership_gate` (#1030) is the split rule —
does the lane named on the order hold what the order claims to reduce. The second was written inline
in the Alpaca client (#748) and never reached the IBKR one, which is how the venue that trades by hand
ran with no guard against the WHD shape while the venue that does not run the short lane refused it.

WHY NOT A SUBCLASS. The shipped `InteractiveBrokersLiveExecClientFactory.create` constructs the
concrete client itself, from cached singletons, with named kwargs. Subclassing would mean
reproducing that constructor here — vendor code copied into our tree, which breaks silently on the
next Nautilus bump. The client is a plain Python class whose instances carry a `__dict__`
(verified against the installed package), so the gate is installed by WRAPPING THE BOUND METHOD on
the instance the vendor's own factory returned. The vendor keeps owning construction.

WHY NOT FORK THE ADAPTER. CLAUDE.md, and LGPL: extend out of tree, never modify the core.

WHAT THIS IS NOT. It is not a second copy of the budget rule. The decision comes from
`api.budget_guard.budget_allows`, the same function the Alpaca client calls — two exec clients
running two copies of a capital rule would drift, and that drift is exactly how #782 happened.
"""

from __future__ import annotations


def install_budget_gate(client, *, log=None, price_of=None, journal=None):
    """Wrap `client._submit_order` so an order is checked against its strategy's sleeve first.

    RETURNS THE CLIENT, so a factory can `return install_budget_gate(...)` in one line.

    `journal` is the DURABLE trace per decision (#990: `api.budget_journal.journal_row`); a factory
    that installs the gate without one installs a gate whose allows leave no record. `price_of` is
    the resolver seam; None means the lanes' own sequence (`api.budget_guard.DEFAULT_PRICE_OF`).

    IDEMPOTENT. Installing twice would run the gate twice — harmless for the verdict, but it would
    double the refusal logs and make the count meaningless. A flag on the instance makes the second
    call a no-op rather than relying on nobody doing it.
    """
    if getattr(client, "_budget_gate_installed", False):
        return client
    # ORDER OF INSTALLATION IS ORDER OF EXECUTION, reversed: the last wrap installed runs first.
    # Ownership must run BEFORE budget (a budget verdict on an order that is secretly an entry
    # answers the wrong question), so ownership is installed AFTER budget. A factory that does it
    # the other way round would compose silently and wrongly; refuse at install, where it is cheap.
    if getattr(client, "_ownership_gate_installed", False):
        raise RuntimeError(
            "install_budget_gate after install_ownership_gate puts the budget rule OUTSIDE the "
            "ownership rule — budget would be asked about mis-stamped exits. Install budget first."
        )

    inner = client._submit_order
    _log = log if log is not None else client._log

    async def _gated_submit_order(command):
        # IMPORTED PER CALL, not captured at install. A module-level binding taken at install time
        # freezes whichever function object existed then — so a later replacement of the rule is
        # silently not used, and a test that patches it measures the old one while reporting green.
        from api.budget_guard import book_for, budget_allows

        order = getattr(command, "order", None)
        if order is None:
            # A shape this gate cannot read must not silently BLOCK execution: the gate is an
            # allocation policy, not an interlock. Say so, then delegate.
            _log.warning("budget gate: command carries no order — submitting unchecked")
            return await inner(command)
        allowed, why, facts = await budget_allows(
            order, cache=client._cache,
            book_loader=lambda: book_for(client, log=_log), log=_log,
            price_of=price_of, journal=journal, now_ns=int(client._clock.timestamp_ns()))
        if not allowed:
            # DENIED, not rejected: the order never reached the venue, and `generate_order_denied`
            # is Nautilus's own terminal event for exactly that. Rejecting would claim the venue
            # said something it was never asked.
            _log.warning(f"Budget refused {order.client_order_id}: {why}"
                         + (f" | {facts}" if facts else ""))
            client.generate_order_denied(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=why,
                ts_event=client._clock.timestamp_ns(),
            )
            return None
        return await inner(command)

    client._submit_order = _gated_submit_order
    client._budget_gate_installed = True
    # SAY SO. Without this line an installed gate and an absent one look identical from outside the
    # process, and #782 is precisely a control everyone believed was running. A capability that
    # leaves no trace cannot be verified on a live stack, only assumed.
    _log.info(f"budget gate INSTALLED on {type(client).__name__} — "
              f"per-strategy sleeves are enforced on this client")
    return client


def install_ownership_gate(client, *, log=None):
    """Wrap `client._submit_order` so an order must REDUCE the lane it is stamped with, or be an entry
    on that lane's declared side — never open the other side on a lane holding nothing (#748, #1030).

    RETURNS THE CLIENT, idempotent, says INSTALLED — the same contract as `install_budget_gate`,
    for the same reasons. The rule is `api.exit_ownership.classify_exit`, which reads the lane's
    declared side from `api.ownership` itself; this wrap only fetches the lane's holding and acts on
    the verdict.

    WHAT IS NOT ASKED, and why each:
      * a command with no order — the gate is not an interlock; delegate and say so;
      * a CLOSED order — the Alpaca client returned before the guard on `is_closed`; the wrap now
        runs outside that check on both venues and must not deny (an invalid transition) what the
        client is about to drop anyway;
      * A PROTECTIVE STOP, BY IDENTITY (`PROTECTION_COID_PREFIX`), NOT BY `is_reduce_only`. #748's
        guard skipped every reduce-only order on the reasoning that Nautilus refuses one that would
        open a netting position. Measured on 1.229.0 (review of #1030): the risk engine's pre-check
        runs only when the command carries a `position_id` (`risk/engine.pyx:424`), which a
        strategy's `submit_order(order)` never does; the execution engine's check is POST-FILL
        (`execution/engine.pyx:1701` — refuses to BOOK, the venue already executed); and on IB the
        flag is dropped at the adapter. kumo-strategies submits EVERY lane exit and cover
        `reduce_only=is_exit` (`runtime/nautilus/broker.py:200`), so a flag-based skip made the guard
        blind to exactly the orders it was written for. What the skip was protecting is narrower —
        cockpit's own stops rest before the entry's position appears in the cache — and that is a
        matter of WHICH order, not of a flag any order can carry.

    FAILS OPEN on an unreadable book (the read raising, or the cache answering None), at WARNING,
    like the budget gate: a guard that halts a healthy book gets switched off, after which it guards
    nothing. A refusal is `generate_order_denied` — Nautilus's own event for "refused before the
    venue" — not `generate_order_rejected`, which would claim the venue said something it was never
    asked (kumo-strategies#103 journals Rejected as "rejected by the venue"). The consumer treats
    the reject/deny family alike (`engine_node._naked_after_reject`). If the denial itself raises
    (a state transition), it propagates: the order can neither be refused nor honestly sent.
    """
    if getattr(client, "_ownership_gate_installed", False):
        return client

    inner = client._submit_order
    _log = log if log is not None else client._log

    async def _owned_submit_order(command):
        # IMPORTED PER CALL, for the reason the budget gate gives: a binding taken at install time
        # freezes the rule that existed then.
        from api.exit_ownership import classify_exit
        from api.protection import PROTECTION_COID_PREFIX

        order = getattr(command, "order", None)
        if order is None:
            _log.warning("ownership guard: command carries no order — submitting unchecked")
            return await inner(command)
        if getattr(order, "is_closed", False):
            return await inner(command)
        coid = str(getattr(order, "client_order_id", ""))
        if coid.startswith(PROTECTION_COID_PREFIX):
            return await inner(command)

        held = None
        try:
            positions = client._cache.positions_open(
                strategy_id=order.strategy_id, instrument_id=order.instrument_id,
            )
            if positions is not None:
                held = sum(float(p.signed_qty) for p in positions)
        except Exception as exc:  # noqa: BLE001 — a guard must never be the thing that halts a book
            _log.warning(f"ownership guard: holding unreadable for {coid}: {exc} — submitting")
            return await inner(command)

        verdict = classify_exit(
            side=order.side.name, quantity=float(order.quantity), lane=str(order.strategy_id),
            instrument_id=str(order.instrument_id), lane_signed_qty=held,
        )
        if verdict.ok:
            # `unreadable` is reachable only through `held is None`, i.e. a cache that answered
            # None — the real one never does, and a raise took the branch above. Kept as the
            # rule's own contract (`classify_exit` says so), logged in case a cache ever does.
            if verdict.reason == "unreadable":
                _log.warning(f"ownership guard: {verdict.detail} — submitting")
            return await inner(command)

        _log.error(
            f"OWNERSHIP REFUSED {coid}: {verdict.reason} — {verdict.detail}. This order would open "
            f"a position on a lane that does not hold the instrument, which corrupts the per-lane "
            f"split silently (the account net still matches the venue)."
        )
        client.generate_order_denied(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            reason=f"{verdict.reason}: {verdict.detail}",
            ts_event=client._clock.timestamp_ns(),
        )
        return None

    client._submit_order = _owned_submit_order
    client._ownership_gate_installed = True
    _log.info(f"ownership guard INSTALLED on {type(client).__name__} — an exit must reduce the lane "
              f"it is stamped with, on that lane's declared side")
    return client
