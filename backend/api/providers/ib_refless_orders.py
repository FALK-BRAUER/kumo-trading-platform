"""An IB order the adapter cannot NAME must not kill the whole order-status batch (#785).

THE DEFECT. `nautilus_trader==1.229.0`,
`adapters/interactive_brokers/execution.py:517` builds an `OrderStatusReport` with an unguarded
`ClientOrderId(ib_order.orderRef)`. Fifty-eight lines above, the same function writes

    :459  # Use venue_order_id as key since orderRef may be empty for external orders.
          client_order_id = ClientOrderId(ib_order.orderRef) if ib_order.orderRef else None

The adapter knows the field can be empty for an order it did not place; one line forgot.
`generate_order_status_reports` loops the venue's open orders with NO per-order try, so the FIRST
hand-placed order discards every report in the batch — including the ones already parsed.

MEASURED on staging2 over 22h (2026-09-02T16:22Z to 2026-09-03T14:53Z): IB account DUPTEST02 held
18 orders with `orderRef=''`, and the engine logged 12,117 `Failed to generate order status
reports`, 11,844 `Error in check_order_consistency`, and 1,217 `protection: venue orders unreadable
— not placing anything`. Reconciliation and protection were blind for the whole session while 21
protective orders sat ACCEPTED at the venue.

THIS IS #643 ON THE IBKR SIDE. `providers/alpaca/exec_client.py` already refuses to let one
unrepresentable row abort a batch: it skips the row, names it, and publishes it on
`UNRECONCILED_TOPIC` every batch — empty list included. IBKR runs the shipped adapter, so none of
that was in its path. The same rule, published on the same topic, in the same row shape: two
derivations of one rule drift, and that drift is how #782 happened.

WHY WRAP `get_open_orders` AND NOT THE PARSER. The raise is inside the vendor's loop. Catching it
per order means reproducing `generate_order_status_reports` in this tree — the thing
`gated_exec.py`'s docstring exists to forbid, and it breaks silently on the next Nautilus bump.
Filtering the INPUT covers both vendor call sites (`:419` singular, `:540` batch) with no vendor code
copied.

WHY NOT A NATIVE KNOB. `InteractiveBrokersExecClientConfig` has exactly one field that changes which
orders are fetched: `fetch_all_open_orders`, which selects `reqAllOpenOrders` over `reqOpenOrders`
(`client/order.py:138`). It defaults to False, this repo does not set it, and staging2 saw all 18
refless orders on that narrow read anyway — so the knob is not the fix, and there is nothing
narrower. (It is also not statically pinned: `client/error.py:117` flips it to True at runtime after
IB error 326.) `LiveExecEngineConfig.filter_unclaimed_external_orders` runs on reports that already
EXIST, so it cannot help when report construction is what raises.

WHAT IS DELIBERATELY NOT FIXED HERE. The same vendor loop also dies on an unmapped
`order_state.status`, `orderType`, `tif` or `action` (KeyError through `MAP_ORDER_STATUS` and
friends), and on `instrument is None`. Those are the same CLASS and are NOT filtered here on
purpose: unlike an empty `orderRef`, which is a permanent structural property of the row, a failed
instrument lookup can be transient — dropping on it would convert a loud outage into a silent short
read, which is the worse failure. Isolating those needs a per-order try inside the vendor loop,
which is an upstream fix. Recorded on #785.

WHAT THIS MUST NEVER DO: turn a broken SHAPE into a short book. An order with no `orderRef`
ATTRIBUTE AT ALL, or a `get_open_orders` that returns something other than a list, is not an order
with an absent reference — it is the adapter having changed under us, and it must raise the way the
unpatched vendor would. `getattr(order, "orderRef", None)` would have made every such break look
like a routine skip; that was codex's first finding on this file and it is the reason attribute
access below is direct.
"""

from __future__ import annotations

# AT MODULE LEVEL, DELIBERATELY. `get_venue_order_id` is a vendor internal and this is the same
# derivation `execution.py:503` uses for the report's `venue_order_id`, so a skipped row and a
# reported one name the same order the same way. Imported here rather than inside the hot path so
# that if it MOVES on a Nautilus bump this module fails to import — loudly, in CI, where
# `test_EVERY_IB_exec_client_this_repo_hands_NAUTILUS_filters_refless_orders` imports every provider
# module. A per-row `except ImportError` would instead degrade the id format silently, and
# `alerts.py:114` keys on `venue_order_id`, so the alert identity would change with no failure.
from nautilus_trader.adapters.interactive_brokers.client.common import get_venue_order_id
from nautilus_trader.model.identifiers import ClientOrderId

#: How many venue ids to name in one log line. The whole point is that an operator can act on the
#: line; 18 ids is already long, and an unbounded list turns a diagnostic into a wall.
_MAX_NAMED = 20

#: Where the per-transport state lives. Set on the `InteractiveBrokersClient`, which is the object
#: whose method is wrapped and which Nautilus caches per `(host, port, client_id)`.
_REPORTERS = "_refless_order_reporters"


def install_refless_order_filter(client, *, log=None):
    """Drop open orders the IBKR adapter cannot turn into a report, and SAY which.

    RETURNS THE CLIENT, so a factory can `return install_refless_order_filter(...)` in one line.

    WRAPS `client._client.get_open_orders` — the bound method on the `InteractiveBrokersClient` the
    vendor's own factory constructed, not on the execution client. That is a different object from
    the one `install_budget_gate` wraps (`_submit_order` on the exec client itself), and it is the
    right one here because `get_open_orders` is what both failing vendor paths read.

    THE TRANSPORT CAN BE SHARED, SO REPORTING IS KEYED BY ACCOUNT. Nautilus caches that client per
    `(host, port, client_id)` (`factories.py:120`), and more than one execution client can be built
    over one transport. A closure that captured the FIRST exec client's `_msgbus` would publish the
    second account's skipped rows onto the first account's health plane — visible nowhere, wrong
    everywhere. So the wrapper is installed once per transport and each exec client REGISTERS itself
    under its own account id; `get_open_orders(account_id)` then reports through the client that
    account belongs to. Registering a second client is not a double-install.
    """
    ib_client = client._client
    reporters = getattr(ib_client, _REPORTERS, None)
    _log = log if log is not None else client._log

    if reporters is None:
        reporters = {}
        setattr(ib_client, _REPORTERS, reporters)
        _install_wrapper(ib_client, reporters)
        # SAY SO. An installed filter and an absent one are indistinguishable from outside the
        # process otherwise, and #782 is precisely a control everyone believed was running.
        _log.info(
            f"IBKR: unnameable-open-order filter INSTALLED on {type(ib_client).__name__} — one "
            f"hand-placed order can no longer kill the order status batch (#785)"
        )

    reporters[client.account_id.get_id()] = (client, _log, {})
    return client


def _install_wrapper(ib_client, reporters):
    inner = ib_client.get_open_orders

    async def _get_open_orders_without_unnameable(account_id):
        orders = await inner(account_id)
        if orders is None:
            # A DISCONNECT IS NOT A CLEAN BOOK. `get_open_orders` returns None when the request
            # failed, and the vendor turns that into a ConnectionError so reconciliation refuses
            # rather than concluding "no open orders". Returning [] here — or publishing [], which
            # states KNOWN-CLEAN — would empty `venue_reported_ids` and recreate #785 by a new route.
            return None
        if not isinstance(orders, list):
            # NOT a shape this wrapper may reinterpret. The vendor annotates this return
            # `list[IBOrder] | None`; anything else means the adapter changed, and quietly iterating
            # it would turn that into a short book. Raise, as the unpatched parser would.
            raise TypeError(
                f"IBKR get_open_orders returned {type(orders).__name__}, not a list — refusing to "
                f"filter a shape this wrapper cannot understand (#785)"
            )

        kept = []
        skipped: list[dict] = []
        for order in orders:
            try:
                # THE PREDICATE IS THE CONSTRUCTOR ITSELF, not a blank test of our own.
                # `ClientOrderId` rejects '', ' ' and '\t' (`Condition.valid_string`) and raises
                # TypeError rather than ValueError on None. A hand-written `if not ref` agrees with
                # it on '' and disagrees on ' ', and two derivations of one rule drift.
                #
                # DIRECT ATTRIBUTE ACCESS: an order with no `orderRef` attribute is a shape break,
                # not an absent reference, and the AttributeError must escape.
                ClientOrderId(order.orderRef)
            except (TypeError, ValueError) as exc:
                skipped.append(_describe(order, exc))
                continue
            kept.append(order)

        _report(reporters, account_id, skipped)
        return kept

    ib_client.get_open_orders = _get_open_orders_without_unnameable


def _describe(order, exc) -> dict:
    """One skipped row, in the shape `alpaca/exec_client.py:1300` publishes.

    RAW VALUES, not reprs: the Alpaca side publishes the broker's own `client_order_id`, and a
    `repr` here would put the strings `"''"` and `"None"` on a topic whose single consumer
    (`engine_node.py:8028`) and alert reader (`alerts.py:114`) are shared between both brokers.

    GUARDED AS A WHOLE, because it is the DIAGNOSTIC path: an order whose `contract` accessor raises
    must not kill the batch from the code that exists to describe why it was dropped. It degrades
    LOUDLY — a row with a null `venue_order_id` and a reason that names both failures is obviously
    anomalous, where an id in a different format would silently change alert identity.
    """
    try:
        return {
            "venue_order_id": str(get_venue_order_id(order.orderId, order.permId)),
            "symbol": _symbol_of(order),
            "client_order_id": order.orderRef,
            "reason": repr(exc),
        }
    except Exception as describe_exc:  # noqa: BLE001
        return {
            "venue_order_id": None,
            "symbol": None,
            "client_order_id": None,
            "reason": f"{exc!r}; and this order could not be described: {describe_exc!r}",
        }


def _symbol_of(order):
    contract = getattr(order, "contract", None)
    symbol = getattr(contract, "symbol", None)
    return str(symbol) if symbol else (str(contract) if contract is not None else None)


def _report(reporters, account_id, skipped):
    entry = reporters.get(account_id)
    if entry is None:
        # DEGRADE LOUDLY, AND STILL FILTER. Reporting is not allowed to decide whether the batch
        # survives, and an account nobody registered is itself worth a line — three states: reported,
        # nothing to report, and nobody to report to.
        for _client, _log, _ in reporters.values():
            _log.warning(
                f"IBKR: {len(skipped)} unnameable open order(s) on account {account_id}, which no "
                f"registered execution client owns — they are filtered but NOT on the health plane"
            )
            return
        return

    client, _log, state = entry
    # KEYED ON THE WHOLE ROW, not on the venue ids alone. Same ids with a changed reason or symbol
    # is a different fact, and `alerts.py:123` dedupes by venue id too — so if this line did not
    # fire, nothing would tell an operator the situation had changed.
    fingerprint = frozenset(tuple(sorted(row.items(), key=lambda kv: kv[0])) for row in skipped)
    changed = state.get("fingerprint") != fingerprint
    state["fingerprint"] = fingerprint

    if skipped and changed:
        # ERROR ONLY WHEN THE SET CHANGES. This runs on every reconciliation tick (~6s on staging2),
        # and 18 lines every six seconds is a second defect — it buries the log the way the thing it
        # replaced did. The health-plane publish below carries the CURRENT state on every batch
        # regardless; this line exists so the change has a timestamp.
        ids = sorted(str(row["venue_order_id"]) for row in skipped)
        named = ", ".join(ids[:_MAX_NAMED])
        more = "" if len(ids) <= _MAX_NAMED else f" (+{len(ids) - _MAX_NAMED} more)"
        _log.error(
            f"IBKR: {len(skipped)} open order(s) on {account_id} carry no usable orderRef and "
            f"CANNOT be turned into an OrderStatusReport — skipping them so the batch survives "
            f"(#785). They are INVISIBLE to reconciliation: shares they reserve and protection "
            f"they provide are not counted. {named}{more}"
        )
    elif not skipped and changed:
        _log.info(f"IBKR: every open order on {account_id} carries a usable orderRef again")

    # PUBLISHED EVERY BATCH, EMPTY INCLUDED — three states, never two. `[]` states KNOWN-CLEAN, so a
    # fixed offender clears instead of the last bad frame standing forever; a non-empty list names
    # the offenders; a topic never published is NEVER ASKED. Same topic and same row shape as the
    # Alpaca side (#643), so `engine_node.py`'s single consumer needs no broker case.
    #
    # ONE DIFFERENCE FROM ALPACA, STATED: Alpaca publishes only from `generate_order_status_reports`,
    # while this fires from `get_open_orders`, which the vendor's SINGULAR query (`execution.py:419`)
    # also calls. The cadence is therefore denser. The CONTENT cannot disagree — both callers read
    # the same whole open-orders book — and `test_the_SINGULAR_path_publishes_the_SAME_rows` pins
    # that, so a denser stream of the same truth is the only difference.
    try:
        from api.bus_topics import UNRECONCILED_TOPIC

        client._msgbus.publish(
            UNRECONCILED_TOPIC,
            {"orders": skipped, "ts": client._clock.timestamp_ns()},
        )
    except Exception as exc:  # noqa: BLE001
        # A GUARD WHOSE REPORTING CAN RAISE IS A NEW ABORT PATH WITH A REASSURING NAME. This is the
        # `_record_book_truth` defect, whose except branch called a clock the object did not have
        # and so aborted the protection pass it was watching.
        _log.warning(f"IBKR: unreconciled-orders publish failed: {exc!r}")
