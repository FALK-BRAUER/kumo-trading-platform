"""The typed venue read must answer identically to the raw one, order for order.

WHY THIS FILE EXISTS
--------------------
`engine_node` asks the broker "what is resting against this leg" by calling Alpaca's REST API and
parsing the JSON by hand. The SAME payload is already parsed, one layer down, into a typed
`OrderStatusReport` — `providers/alpaca/exec_client.py:1042` — which is the form Nautilus defines and
every adapter produces, including the shipped Interactive Brokers one.

So the engine is not bypassing Nautilus. It is parsing the same bytes a second time, in Alpaca's
vocabulary, and that second parse is where all the broker coupling lives: 32 raw `.get("...")` reads in
`engine_node.py`, 31 of which exist as typed fields on a report we already generate.

Moving to the typed source is what makes IBKR possible without a port. But this is the exit path — the
one that decides whether shares are free to sell — so the migration is only safe if the two answers are
provably identical. This file is that proof, run against the REAL parser rather than hand-built reports.

THE CASE THAT MATTERS MOST
--------------------------
A `held` order. #387 was exactly this: the check could not see a HELD stop, so the sweep did not cancel
the order reserving the shares and the exit came back `insufficient qty available (available: 0)`. The
typed parse maps `held -> OrderStatus.ACCEPTED`, which is in `_OPEN_STATUSES` — but that is a fact to
assert, not to assume, because `_parse_order_report` SKIPS any status it cannot map and a silently
shorter list reads as "nothing is resting".
"""

from __future__ import annotations


def _rows() -> list[dict]:
    """Alpaca order JSON in the shapes this path actually meets."""
    return [
        # the #387 case: a HELD bracket leg reserving shares
        {"id": "v1", "client_order_id": "PROT-SELL-AEM-XNYS-a1", "symbol": "AEM", "side": "sell",
         "status": "held", "type": "trailing_stop", "qty": "54", "filled_qty": "0", "trail_percent": "7.5"},
        # an ordinary resting stop
        {"id": "v2", "client_order_id": "PROT-SELL-AEM-XNYS-b2", "symbol": "AEM", "side": "sell",
         "status": "new", "type": "stop", "qty": "10", "filled_qty": "0", "stop_price": "100.5"},
        # partially filled — only the remainder reserves
        {"id": "v3", "client_order_id": "c3", "symbol": "AEM", "side": "sell",
         "status": "partially_filled", "type": "limit", "qty": "20", "filled_qty": "8", "limit_price": "120"},
        # closed — reserves nothing
        {"id": "v4", "client_order_id": "c4", "symbol": "AEM", "side": "sell",
         "status": "canceled", "type": "stop", "qty": "5", "filled_qty": "0", "stop_price": "99"},
        # wrong side
        {"id": "v5", "client_order_id": "c5", "symbol": "AEM", "side": "buy",
         "status": "new", "type": "limit", "qty": "7", "filled_qty": "0", "limit_price": "90"},
        # wrong instrument
        {"id": "v6", "client_order_id": "c6", "symbol": "WPM", "side": "sell",
         "status": "new", "type": "stop", "qty": "9", "filled_qty": "0", "stop_price": "50"},
    ]


def _reports(rows: list[dict]):
    """Parse with the REAL provider parser, bound to a minimal host.

    Not hand-built `OrderStatusReport`s: this file's entire claim is that the typed path answers like the
    raw one, and a double that constructs reports the way the test WISHES they were built would be
    asserting against itself. A host supplying exactly what the parser reads means a parser reaching for
    anything else raises here instead of silently passing.
    """
    from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    class _Clock:
        def timestamp_ns(self):
            return 1_800_000_000_000_000_000

    class _Log:
        def warning(self, *a, **k):
            self.seen = True

    class _Host:
        account_id = AccountId("ALPACA-TEST")
        _clock = _Clock()
        _log = _Log()
        _symbol_to_id = {"AEM": InstrumentId.from_str("AEM.XNYS"),
                         "WPM": InstrumentId.from_str("WPM.XNYS")}

        def _client_order_id_for(self, raw):
            return ClientOrderId(raw["client_order_id"])

    host = _Host()
    return [AlpacaExecutionClient._parse_order_report(host, r) for r in rows]


def test_the_typed_read_answers_identically_to_the_raw_one():
    """Verification by disagreement, which is this repo's rule for exactly this kind of migration.

    Same underlying orders, two parsers, one answer. If they differ, the migration is unsafe and this
    says so before it reaches the exit path.
    """
    from api.protection import reserving_orders, reserving_orders_typed

    rows = _rows()
    raw = reserving_orders(rows, "AEM.XNYS", "sell")
    typed = reserving_orders_typed([r for r in _reports(rows) if r is not None], "AEM.XNYS", "sell")

    assert [o["id"] for o in raw] == [o["id"] for o in typed], (
        f"the two reads disagree on WHICH orders reserve shares: raw={[o['id'] for o in raw]} "
        f"typed={[o['id'] for o in typed]}"
    )
    assert [o["_remaining"] for o in raw] == [o["_remaining"] for o in typed], (
        f"the two reads disagree on HOW MUCH is reserved: raw={[o['_remaining'] for o in raw]} "
        f"typed={[o['_remaining'] for o in typed]}"
    )


def test_a_HELD_order_is_seen_by_the_typed_read():
    """#387, restated as the migration's precondition.

    `held` maps to `OrderStatus.ACCEPTED`, which is in `_OPEN_STATUSES`. If a future mapping change drops
    it, the typed list silently shortens and the exit path reads "nothing is resting" for an order that
    is holding every share — which is the failure that cost two sessions.
    """
    from api.protection import reserving_orders_typed

    rows = [r for r in _rows() if r["status"] == "held"]
    reports = [r for r in _reports(rows) if r is not None]
    assert reports, "the HELD order did not survive the typed parse at all"
    out = reserving_orders_typed(reports, "AEM.XNYS", "sell")
    assert [o["id"] for o in out] == ["v1"], f"a HELD order reserving 54 shares was invisible: {out}"
    assert out[0]["_remaining"] == 54.0


def test_the_fixture_contains_orders_that_must_be_EXCLUDED():
    """Assert the fixture can distinguish, or the equivalence above is satisfied by returning everything."""
    from api.protection import reserving_orders_typed

    out = reserving_orders_typed([r for r in _reports(_rows()) if r is not None], "AEM.XNYS", "sell")
    ids = {o["id"] for o in out}
    assert "v4" not in ids, "a canceled order was counted as reserving"
    assert "v5" not in ids, "a BUY was counted on the sell leg"
    assert "v6" not in ids, "another instrument's order was counted"
    assert ids == {"v1", "v2", "v3"}


def test_a_partially_filled_order_reserves_only_the_remainder():
    from api.protection import reserving_orders_typed

    out = {o["id"]: o["_remaining"] for o in
           reserving_orders_typed([r for r in _reports(_rows()) if r is not None], "AEM.XNYS", "sell")}
    assert out["v3"] == 12.0, f"20 ordered minus 8 filled should reserve 12, got {out.get('v3')}"
