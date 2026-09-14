"""A venue order read that FAILED must not be read as a venue that holds no orders (#791).

THE DEFECT, measured on test-alpaca 2026-09-04. The machine lost the network to Alpaca for about
twenty seconds. `nautilus_trader/live/execution_engine.py:1574` catches a failed report generation,
LOGS it, and `continue`s:

    for reports_or_exception in order_reports_all:
        if isinstance(reports_or_exception, BaseException):
            self._log.error(f"Failed to generate order status reports: {reports_or_exception}")
            continue
        ...
    venue_reported_ids = {r.client_order_id for r in all_order_reports if ...}
    return all_order_reports, venue_reported_ids

The return value cannot express "we could not ask". A read that FAILED and a venue that genuinely
holds no orders produce the identical empty set, and `_handle_missing_orders_at_venue` then treats
every cached open order as gone. At `open_check_missing_retries=5` and our
`open_check_interval_secs=5.0`, twenty-five seconds of an unreachable venue is enough to reject
every resting order. 33 live orders were rejected `ORDER_NOT_FOUND_AT_VENUE` at 08:46:03Z; they were
resting at Alpaca the whole time. What followed: 117,689 `InvalidStateTrigger: REJECTED ->
ACCEPTED`, 23 instruments holding a protective order the engine could not see, and two phantom
position pairs minted when one of the rejected stops actually filled (#791, #635).

THE VENDOR ALREADY KNOWS THE RIGHT ANSWER. The POSITION path took the identical outage in the same
second and skipped every symbol untouched — `Skipping position reconciliation for PATH.XNYS: failed
to query venue position status`. Unreadable means touch nothing. Orders and positions derive one
rule two ways and only one is right; this makes the order path agree with the position path.

WHY A WRAP AND NOT A FORK. Same reason as `providers/gated_exec.py` and
`providers/ib_refless_orders.py`: the fix belongs at the seam, on the instance the vendor built, so
Nautilus keeps owning its own code and the next bump cannot break silently on a copied loop. Two
bound methods are wrapped — the client's read, to learn the outcome, and the engine's missing-order
handler, to act on it.

WHAT THIS IS NOT. It is not a retry, a backoff, or a reconnect: the transport already handles those.
It only refuses to draw a conclusion from a question that was never answered.
"""

from __future__ import annotations

#: Outcome of a client's most recent order-status read, stored ON THE CLIENT.
#:
#: NOT in a dict captured at install time. The guard used to snapshot `exec_engine._clients` and key
#: a closure dict by client id, which was wrong three ways at once (codex): a client REGISTERED AFTER
#: the install was never watched, a client DEREGISTERED afterwards left a `failed` entry that pinned
#: the refusal on forever, and the same client object installed against a second engine wrote to the
#: first engine's dict. Hanging the outcome on the client makes it travel with the thing it describes
#: and die with it.
_OUTCOME = "_venue_read_outcome"
_WATCHED = "_venue_read_watched"

_OK = "ok"
_FAILED = "failed"
#: A client with no `_OUTCOME` attribute has never been read. That is the third state, and it is
#: absence rather than a value on purpose: `never asked` is not `asked and empty`.


def install_venue_read_guard(exec_engine, *, log=None):
    """Refuse to disown resting orders on the strength of a venue read that did not answer.

    RETURNS THE ENGINE, so the wiring reads as one line.

    IDEMPOTENT. A second install would wrap the wrapper: the verdict is unchanged, but the refusal
    would be logged twice and the count an operator reads after an incident stops meaning anything.
    """
    if getattr(exec_engine, "_venue_read_guard_installed", False):
        return exec_engine

    _log = log if log is not None else exec_engine._log
    inner = exec_engine._handle_missing_orders_at_venue

    # WATCH WHAT IS REGISTERED NOW, as well as lazily below. Nautilus calls the clients' reads and
    # then `_handle_missing_orders_at_venue` within one `_check_orders_consistency` pass, so watching
    # ONLY at call time would leave the first pass's read unrecorded — read as never-asked, and
    # refused. Safe, but it would make the very first consistency check after every boot a no-op for
    # no reason.
    for client in dict(getattr(exec_engine, "_clients", {}) or {}).values():
        _watch_reads(client)

    async def _handle_missing_orders_at_venue(open_order_ids, venue_reported_ids):
        # READ THE REGISTRY LIVE, and watch anything new. Nautilus registers and deregisters
        # execution clients at runtime (`execution/engine.pyx:465`, `:507`, `:586`), and it builds
        # `venue_reported_ids` from `self._clients.values()` at the moment of the check
        # (`live/execution_engine.py:1554`) — so the set this guard must vouch for is whatever is
        # registered NOW, never whatever existed at install.
        clients = dict(getattr(exec_engine, "_clients", {}) or {})
        for client in clients.values():
            _watch_reads(client)

        failed = sorted(
            str(cid) for cid, c in clients.items() if getattr(c, _OUTCOME, None) == _FAILED
        )
        if failed:
            # ONE FAILING CLIENT REFUSES THE WHOLE PASS. `venue_reported_ids` is a UNION across
            # clients, so a healthy client's report cannot vouch for a failed client's orders —
            # those orders are simply absent from the union, which is exactly the state that
            # produces a wrongful rejection.
            _log.error(
                f"venue order read FAILED for {', '.join(failed)} — refusing to treat "
                f"{len(open_order_ids)} cached open order(s) as missing at the venue. A read that "
                f"could not be answered is not a venue holding nothing (#791); the position path "
                f"already skips on the same failure. No order will be rejected on this pass."
            )
            return

        unread = sorted(
            str(cid) for cid, c in clients.items() if getattr(c, _OUTCOME, None) is None
        )
        if unread:
            # NEVER ASKED. Before the first read the engine knows nothing about what the venue
            # holds, and concluding from silence is the same error one step earlier.
            _log.warning(
                f"venue order read has not run yet for {', '.join(unread)} — refusing to resolve "
                f"missing orders from a question never asked (#791)"
            )
            return

        return await inner(open_order_ids, venue_reported_ids)

    exec_engine._handle_missing_orders_at_venue = _handle_missing_orders_at_venue
    exec_engine._venue_read_guard_installed = True
    # SAY SO. An installed guard and an absent one are indistinguishable from outside the process
    # otherwise, and this one suppresses a safety mechanism when it fires.
    _log.info(
        f"venue-read guard INSTALLED on {type(exec_engine).__name__} — a failed order read can no "
        f"longer reject resting orders (#791)"
    )
    return exec_engine


def _watch_reads(client) -> None:
    """Record whether this client's last order-status read answered.

    RECORDS, NEVER ABSORBS. The exception is re-raised unchanged: Nautilus's own handler at
    `live/execution_engine.py:1574` still logs it and still drops the client's reports from the
    union. Swallowing it here would change what reconciliation sees, which is a second defect
    wearing this one's clothes.

    CATCHES `BaseException`, DELIBERATELY, which is the one place in this repo that is right.
    Nautilus gathers with `return_exceptions=True` and treats ANY `BaseException` as a failed read;
    on Python 3.13 `asyncio.CancelledError` is one. Recording only `Exception` would leave a
    cancelled read looking like a venue that answered "nothing" — the exact confusion this exists to
    end. It is re-raised immediately, so nothing is absorbed and a shutdown still shuts down.
    """
    if getattr(client, _WATCHED, False):
        return

    inner = client.generate_order_status_reports

    async def generate_order_status_reports(command):
        try:
            reports = await inner(command)
        except BaseException:
            setattr(client, _OUTCOME, _FAILED)
            raise
        setattr(client, _OUTCOME, _OK)
        return reports

    client.generate_order_status_reports = generate_order_status_reports
    setattr(client, _WATCHED, True)
