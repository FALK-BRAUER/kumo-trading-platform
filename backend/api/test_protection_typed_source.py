"""The protection plan must be identical whether the venue is read raw or typed.

WHY THIS FILE EXISTS
--------------------
The protection reconciler reads the broker by calling Alpaca's REST API and parsing the JSON by hand,
while the same payload is already parsed one layer down into `OrderStatusReport` — Nautilus's own form,
and the one the shipped Interactive Brokers adapter produces. Moving to the typed source is what makes a
second broker possible without porting this file (#430).

This is the path that decides which positions get a protective stop, so "probably equivalent" is not
good enough. The assertion is the strongest available: the SAME `ProtectionPlan` from both sources.

THE TRAP THIS FILE EXISTS TO CATCH
----------------------------------
`protection.PROTECTIVE_TYPES` is already bilingual — it holds both `{"stop", "stop_limit",
"trailing_stop"}` and `{"STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP_MARKET"}` — so `protective_orders`
survives a typed source unchanged.

`engine_node._PROTECTIVE` is a SECOND COPY of that set and holds only the Alpaca spellings. Feed it
typed orders and it matches nothing: every protective stop becomes invisible, the reconciler reads the
book as naked, and it arms a duplicate stop on every position that already had one. Two derivations of
one fact, and this is the direction that hurts.
"""

from __future__ import annotations


def _rows() -> list[dict]:
    return [
        {"id": "v1", "client_order_id": "PROT-SELL-AEM-XNYS-a1", "symbol": "AEM", "side": "sell",
         "status": "held", "type": "trailing_stop", "qty": "54", "filled_qty": "0", "trail_percent": "7.5"},
        {"id": "v2", "client_order_id": "PROT-SELL-WPM-XNYS-b2", "symbol": "WPM", "side": "sell",
         "status": "new", "type": "stop", "qty": "10", "filled_qty": "0", "stop_price": "100.5"},
        {"id": "v3", "client_order_id": "c3", "symbol": "AEM", "side": "sell",
         "status": "canceled", "type": "stop", "qty": "5", "filled_qty": "0", "stop_price": "99"},
    ]


def _positions() -> list[dict]:
    return [
        {"instrument_id": "AEM.XNYS", "symbol": "AEM", "quantity": 54.0, "side": "long", "market_value": 11000.0},
        {"instrument_id": "WPM.XNYS", "symbol": "WPM", "quantity": 25.0, "side": "long", "market_value": 2000.0},
    ]


def _reports(rows):
    from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    class _Clock:
        def timestamp_ns(self):
            return 1_800_000_000_000_000_000

    class _Log:
        def warning(self, *a, **k):
            pass

    class _Host:
        account_id = AccountId("ALPACA-TEST")
        _clock = _Clock()
        _log = _Log()
        _symbol_to_id = {"AEM": InstrumentId.from_str("AEM.XNYS"),
                         "WPM": InstrumentId.from_str("WPM.XNYS")}

        def _client_order_id_for(self, raw):
            return ClientOrderId(raw["client_order_id"])

    return [r for r in (AlpacaExecutionClient._parse_order_report(_Host(), x) for x in rows) if r is not None]


def _plan(orders):
    from api.protection import plan_protection

    return plan_protection(
        positions=_positions(),
        orders=orders,
        atr_by_symbol={"AEM.XNYS": 5.0, "WPM.XNYS": 2.0},
        price_by_symbol={"AEM.XNYS": 200.0, "WPM.XNYS": 80.0},
    )


def test_the_plan_is_the_same_from_either_source():
    """Verification by disagreement — the repo's own rule for a migration on a safety path."""
    from api.protection import orders_from_reports

    raw = _plan(_rows())
    typed = _plan(orders_from_reports(_reports(_rows())))

    assert sorted(raw.covered_instrument_ids) == sorted(typed.covered_instrument_ids), (
        f"the two sources disagree about what is COVERED: raw={sorted(raw.covered_instrument_ids)} "
        f"typed={sorted(typed.covered_instrument_ids)}"
    )
    assert [i.instrument_id for i in raw.intents] == [i.instrument_id for i in typed.intents], (
        f"the two sources disagree about what needs ARMING: raw={[i.instrument_id for i in raw.intents]} "
        f"typed={[i.instrument_id for i in typed.intents]}"
    )
    assert [o.instrument_id for o in raw.oversize] == [o.instrument_id for o in typed.oversize]


def test_the_fixture_actually_exercises_both_outcomes():
    """Assert the fixture can distinguish, or equality above is satisfied by two empty plans.

    AEM has a HELD trailing stop covering all 54 — it must come back COVERED. WPM's stop covers 10 of
    25 — it must come back needing an intent. A fixture where everything landed in one bucket would pass
    the equivalence test against a completely broken typed source.
    """
    raw = _plan(_rows())
    assert raw.covered_instrument_ids, "nothing is covered — the fixture cannot detect a lost stop"
    assert raw.intents, "nothing needs arming — the fixture cannot detect a spurious stop"


def test_a_HELD_trailing_stop_counts_as_protection_through_the_typed_source():
    """#387 restated for this path: a HELD bracket leg IS protection, and losing it means the reconciler
    arms a duplicate over a position that already has one."""
    from api.protection import orders_from_reports

    typed = _plan(orders_from_reports(_reports(_rows())))
    assert "AEM.XNYS" in typed.covered_instrument_ids, (
        "the HELD trailing stop was not seen as protection through the typed source"
    )


def test_engine_node_does_not_keep_a_SECOND_copy_of_the_protective_type_set():
    """The trap. `PROTECTIVE_TYPES` is bilingual; a private Alpaca-only copy in the engine is not.

    Fed typed orders it matches nothing, every stop becomes invisible, and the reconciler arms a
    duplicate on every already-protected position. Two derivations of one fact, and this is the
    direction that costs money.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    tree = ast.parse(src)
    literals = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "_PROTECTIVE" for t in n.targets)
        and isinstance(n.value, (ast.Set, ast.Call))
    ]
    assert not literals, (
        "`engine_node` defines its own `_PROTECTIVE` set instead of using "
        "`protection.PROTECTIVE_TYPES`. The shared one accepts BOTH vocabularies; a private copy holds "
        "only Alpaca's, so a typed venue read matches nothing and every protective stop goes invisible"
    )
