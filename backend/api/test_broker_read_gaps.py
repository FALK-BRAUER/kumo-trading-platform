"""What Nautilus's typed reports CANNOT tell us, asserted so it is not rediscovered.

WHY THIS FILE EXISTS
--------------------
Both broker paths now read Nautilus's own forms — `generate_order_status_reports` AND
`generate_position_status_reports` — which every adapter produces, including the shipped Interactive
Brokers one. That is what makes a second broker possible without porting the protection and exit paths
(#430, completed for positions by #641).

`PositionStatusReport` still carries `(account_id, instrument_id, position_side, quantity, avg_px_open,
venue_position_id)` and nothing else. Two facts this engine consumes are simply not in Nautilus's model,
and #641 resolved each by DERIVING rather than by keeping a broker-specific read:

  qty_available   Alpaca's statement of a venue-side reservation. `grep -rn "qty_available\\|
                  reserved_qty"` over the whole installed package returns ZERO hits — the domain model
                  has no concept of one, on any adapter. `_venue_shares_available` now derives the same
                  number venue-neutrally: |net position| − Σ resting reducing-order quantity — and a
                  venue that never reserves (IB, api/venues/facts.py) skips the availability wait
                  entirely rather than timing out on a constraint it does not have.

  market_value    consumed by `plan_protection` via the position rows. On the typed path it is derived
                  as signed quantity x last price (`protection.position_rows_from_reports`) from the
                  SAME price source that feeds the planner. That makes it the one derivation on that
                  path, not a second one beside the broker's statement; where both paths exist they are
                  pinned row-identical (`test_the_typed_and_rest_position_sources_produce_the_SAME_rows`).

What stays broker-specific, deliberately: the REST fallbacks for a node with no execution client (the
synthetic engine), and the activities LEDGER (`_refresh_realized_periods`) — see the ledger test below.
These tests fail the day Nautilus's model gains either fact, which is the signal to revisit the
derivations rather than a reason to keep them unexamined.
"""

from __future__ import annotations

import inspect


def _report_fields(cls) -> set[str]:
    return {p for p in inspect.signature(cls.__init__).parameters if p != "self"}


def test_available_quantity_is_DERIVED_because_nautilus_still_cannot_report_it():
    """The reservation concept is still absent from the domain model — which is now the JUSTIFICATION
    for the derivation, not for a shim. If this fails, the venue can state the number itself and the
    derived form should be checked against it (two derivations, compared — not replaced blindly).
    """
    import ast
    import pathlib

    from nautilus_trader.execution.reports import PositionStatusReport

    fields = _report_fields(PositionStatusReport)
    assert "qty_available" not in fields and "available_qty" not in fields, (
        f"PositionStatusReport now reports available quantity {fields} — compare the venue's statement "
        f"against the derived |net| − reserved before trusting either alone"
    )

    # The shim is GONE: `_venue_shares_available` must not touch the REST client at all — its inputs
    # (`_venue_net_position`, `_venue_reducing_orders`) own their fallbacks, and a raw read creeping
    # back here is the regression #641 removed.
    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_venue_shares_available"
    )
    seg = ast.get_source_segment(src, fn) or ""
    assert "self._http" not in seg, (
        "_venue_shares_available reads the broker REST API again — availability is derived from the "
        "typed reads precisely so a second broker does not need this function ported"
    )


def test_market_value_is_DERIVED_because_nautilus_still_cannot_report_it():
    """`plan_protection` consumes it; the typed report cannot carry it; #641 derives it as signed
    quantity x last price from the SAME price source the planner reads. If the field ever appears,
    the broker states the number again and the derivation becomes the second of two derivations —
    compare them rather than deleting either silently.
    """
    import inspect as _inspect

    from nautilus_trader.execution.reports import PositionStatusReport

    fields = _report_fields(PositionStatusReport)
    assert "market_value" not in fields, (
        "PositionStatusReport now carries market_value — compare the broker's statement against "
        "position_rows_from_reports' qty x price derivation before trusting either alone"
    )

    # The derivation exists and the reconciler actually reads the typed source — declared-but-unwired
    # is the #574 shape.
    from api.engine_node import UiFeedStrategy
    from api.protection import position_rows_from_reports  # noqa: F401 — existence is the assertion

    inner = _inspect.getsource(UiFeedStrategy._reconcile_protection_inner)
    assert "_venue_position_reports" in inner, (
        "the protection reconciler no longer consults the typed position read — the position path has "
        "regressed to broker-specific"
    )


def test_the_ORDER_report_still_carries_everything_the_order_path_needs():
    """The other half: the migration that DID happen rests on these fields existing.

    Asserted positively so a future Nautilus version that drops one fails here, rather than in the
    protection reconciler at 09:35.
    """
    from nautilus_trader.execution.reports import OrderStatusReport

    fields = _report_fields(OrderStatusReport)
    needed = {
        "instrument_id", "venue_order_id", "client_order_id", "order_side", "order_type",
        "order_status", "quantity", "filled_qty", "trigger_price",
    }
    missing = needed - fields
    assert not missing, (
        f"OrderStatusReport no longer carries {sorted(missing)} — the typed venue read depends on every "
        f"one of these, and the protection and exit paths are built on it"
    )


def test_every_remaining_raw_broker_read_is_a_sanctioned_fallback():
    """Aimed at the class: a NEW raw broker read appearing in the engine is a regression.

    After #641 no primary path reads the REST client: what remains is FALLBACKS for a node with no
    execution client (the synthetic engine), plus the activities ledger, which has no Nautilus
    equivalent on any adapter. Anything else calling the REST client directly is a path a second
    broker cannot take, and it should be caught here rather than discovered when the next venue is
    switched on. Notably ABSENT from the list: `_venue_shares_available`, whose raw `qty_available`
    read is deleted — availability is derived from the typed reads.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    tree = ast.parse(src)
    sanctioned = {
        "_reconcile_protection_inner",      # REST position fallback when there is no execution client
        "_venue_net_position",              # REST position fallback when there is no execution client
        "_venue_reducing_orders",           # REST order fallback when there is no execution client
        # The flip's confirm read (#898): ONE order's terminal status, typed single report first
        # (`generate_order_status_report`, which every real adapter implements), REST `get_order` by
        # venue id only when there is no execution client — the same shape as the two above.
        "_venue_order_status",
        "_refresh_realized_periods",        # the activities LEDGER — no Nautilus equivalent, see below
        "_refresh_today_ranges",            # market data, not execution
        "_refresh_rotation",                # market data, not execution
        "_cancel_reducing_leg",             # venue cancel — see #430 for the native replacement
        # The REST cancel MOVED here in #872 (extracted so the protection reconciler's wrong-mode
        # cancels take one route rather than a second copy). Same sanction, same reason: it is the
        # fallback for a node with no execution client, and it says so in a warning rather than
        # degrading quietly. The native `CancelOrder` path above it is what every real adapter takes.
        "_cancel_at_venue",
        "on_start", "on_stop",              # client lifecycle
    }
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in sanctioned:
            continue
        seg = ast.get_source_segment(src, fn) or ""
        if "self._http." in seg:
            offenders.append(fn.name)
    assert not offenders, (
        f"these functions read the broker's REST API directly: {offenders}. Every such call is a path a "
        f"second broker cannot take — use `_venue_order_reports()` unless the fact genuinely has no "
        f"Nautilus equivalent, and if it does not, add it to `sanctioned` with the reason"
    )


def test_a_dropped_row_shortens_the_typed_read_WITHOUT_signalling_it():
    """A KNOWN HAZARD, recorded rather than claimed closed. Found by adversarial review of #430.

    `_parse_order_report` returns None for two inputs and the caller filters them out:

      * a status absent from `_ALPACA_TO_ORDER_STATUS` — deliberately fail-closed for reconciliation
      * a symbol absent from `_symbol_to_id` — the provider cannot name the instrument

    Either way the typed list comes back SHORTER, not None. And short does not look like an error: it
    looks like "nothing else is resting". For the exit path that is the dangerous direction — it reads
    zero shares reserved and submits against shares a protective stop is holding.

    The exposure is narrow today: the exit path only asks about instruments this node holds and
    subscribes, so their symbols are in the map, and every Alpaca status the account produces is mapped.
    It is not zero: an externally-acquired position on an unsubscribed symbol would be invisible.

    This test asserts the CURRENT behaviour so the hazard is written down and cannot be rediscovered as
    a surprise. Closing it belongs with the broker shim in #430 Phase 2 — the provider has to report
    what it dropped, and the accessor has to treat any drop as "unreadable" rather than "empty".
    """
    from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    class _Clk:
        def timestamp_ns(self):
            return 1_800_000_000_000_000_000

    class _Lg:
        def __init__(self):
            self.warned = 0

        def warning(self, *a, **k):
            self.warned += 1

    log = _Lg()

    class _Host:
        account_id = AccountId("ALPACA-TEST")
        _clock = _Clk()
        _log = log
        _symbol_to_id = {"AEM": InstrumentId.from_str("AEM.XNYS")}  # WPM deliberately absent

        def _client_order_id_for(self, raw):
            return ClientOrderId(raw["client_order_id"])

    rows = [
        {"id": "v1", "client_order_id": "a", "symbol": "AEM", "side": "sell", "status": "held",
         "type": "stop", "qty": "10", "filled_qty": "0", "stop_price": "50"},
        # a symbol the provider cannot name — silently dropped
        {"id": "v2", "client_order_id": "b", "symbol": "WPM", "side": "sell", "status": "held",
         "type": "stop", "qty": "20", "filled_qty": "0", "stop_price": "60"},
        # a status the provider cannot map — dropped, and it warns
        {"id": "v3", "client_order_id": "c", "symbol": "AEM", "side": "sell", "status": "no_such_status",
         "type": "stop", "qty": "30", "filled_qty": "0", "stop_price": "70"},
    ]
    reports = [AlpacaExecutionClient._parse_order_report(_Host(), r) for r in rows]

    assert reports[1] is None, "the unmappable SYMBOL was not dropped — this test is measuring nothing"
    assert reports[2] is None, "the unmappable STATUS was not dropped"
    # WAS 1, NOW 2 — AND THAT CHANGE IS THE POINT, not bookkeeping. This asserted that the unmapped
    # STATUS warns while the unknown SYMBOL drops with NO TRACE AT ALL, and called the silent half "the
    # one worth fixing first". #451 fixed it: both drops now warn. So the SIGNAL half of this hazard is
    # closed and this line is how we found out — an exact count, not `>= 1`, is what noticed.
    #
    # THE STRUCTURAL HALF IS STILL OPEN. A warning in a log is not a caller-visible signal: both drops
    # still SHORTEN the returned list rather than failing it, and the accessor still cannot tell
    # "dropped" from "empty". A reconciler receiving a short list reads a position as naked and arms a
    # duplicate; that is unchanged. Closing it needs the read to return UNKNOWN rather than a quietly
    # truncated answer — #430 Phase 2, and the same routing gap as #455.
    assert log.warned == 2, (
        "both unmappable rows must warn — #451 closed the silent SYMBOL drop, so a count of 1 means "
        "one of the two drops has gone quiet again and is losing a resting order with no trace"
    )
    kept = [r for r in reports if r is not None]
    assert len(kept) == 1 and kept != reports, (
        "three orders went in and the caller receives one, with no signal that two were lost"
    )


def test_realized_pnl_needs_a_LEDGER_which_nautilus_does_not_model():
    """The third gap, and the one where migrating HALF the read would be worse than not migrating.

    `_refresh_realized_periods` makes ONE call — `list_activities(activity_type=None)` — and partitions
    the result: rows where `activity_type == 'FILL'` drive `realized_by_period`, and every OTHER row
    drives `cash_adjustments`.

    Part two is load-bearing, and there is a measured incident behind it. Realized-from-fills reported
    +$3,574.04 while the broker's own arithmetic said +$2,574.95. The whole $999.09 gap was cash that
    moved with NO fill behind it: 'PTP Withholding' -990.46 on a partnership holding, plus 38
    regulatory fees totalling -8.63.

    `generate_fill_reports` returns FILLS ONLY, and Nautilus's domain model has no concept of
    withholding, regulatory fees, dividends or journal entries — on any adapter.

    SO THE HALF-MIGRATION IS THE WORST OPTION. Taking fills from Nautilus and activities from REST turns
    one read into two, from two source systems, with two window semantics, feeding one P&L number. That
    is precisely the two-derivations-of-one-fact shape this whole migration exists to remove — a payload
    parsed twice, a predicate defined twice, a setting read by two callers keeping different halves.

    This read is not "fills plus extras". It is one accounting ledger partitioned by effect, and it stays
    whole and broker-specific until a second broker forces the port's shape. Building that port now, with
    one implementation and no second consumer, would be designing an abstraction before measuring — the
    mistake #430's own plan was revised twice to avoid.
    """
    import inspect

    from nautilus_trader.live.execution_client import LiveExecutionClient

    surface = {m for m in dir(LiveExecutionClient) if not m.startswith("_")}
    assert not {"generate_activity_reports", "generate_ledger_reports"} & surface, (
        "Nautilus now reports account activity beyond fills — the realized-P&L read can stop being "
        "broker-specific, and #430's ledger gap can close"
    )

    from api.realized_broker import cash_adjustments

    src = inspect.getsource(cash_adjustments)
    assert "activity_type" in src, (
        "`cash_adjustments` no longer partitions on activity_type — if the non-fill half has moved "
        "somewhere else, this gap needs restating rather than deleting"
    )
