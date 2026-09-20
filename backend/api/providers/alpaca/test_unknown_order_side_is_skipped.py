"""An unknown/absent order side must be SKIPPED, not reconciled as SELL (#652 item 8a).

`_parse_order_report` mapped side with a bare ternary — `BUY if raw.get("side") == "buy" else SELL`
— so an order whose side Alpaca reports as anything else (or not at all) reconciled as a SELL. An
unmapped STATUS in the same parser already fails closed with a warning; side got the opposite
posture: absence read as permission, and as the more dangerous of the two sides at that (a phantom
SELL on reconciliation is how protection gets stripped).

Same doubles discipline as test_reconciliation_survives_a_notional_order.py: the host binds the
REAL `_parse_order_report`, and rows are the GET /v2/orders shape.
"""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import AccountId, InstrumentId

from api.providers.alpaca.exec_client import AlpacaExecutionClient


class _Clk:
    def timestamp_ns(self):
        return 1_800_000_000_000_000_000


class _Log:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def error(self, msg, *a, **k):
        self.warnings.append(str(msg))


class _Cache:
    def client_order_id(self, venue_order_id):
        return None


def _host():
    host = SimpleNamespace()
    host.account_id = AccountId("ALPACA-TEST")
    host._clock = _Clk()
    host._log = _Log()
    host._cache = _Cache()
    host._symbol_to_id = {"AEM": InstrumentId.from_str("AEM.XNYS")}
    host._parse_order_report = AlpacaExecutionClient._parse_order_report.__get__(host)
    host._client_order_id_for = AlpacaExecutionClient._client_order_id_for.__get__(host)
    return host


def _row(**overrides):
    row = {
        "id": "v-side-1", "client_order_id": "c-side-1", "symbol": "AEM", "side": "sell",
        "type": "stop", "time_in_force": "gtc", "status": "held",
        "qty": "10", "filled_qty": "0", "notional": None,
        "filled_avg_price": None, "limit_price": None, "stop_price": "50",
    }
    row.update(overrides)
    return row


def test_fixture_property_known_sides_still_parse_to_the_right_enum():
    """Both real sides must travel, or the skip test below could pass by skipping EVERYTHING."""
    host = _host()
    assert host._parse_order_report(_row(side="sell")).order_side == OrderSide.SELL
    assert host._parse_order_report(_row(side="buy")).order_side == OrderSide.BUY


def test_an_unknown_side_is_skipped_and_named_not_reconciled_as_SELL():
    """Seen red pre-fix: returned a report with order_side == SELL."""
    host = _host()
    assert host._parse_order_report(_row(side="short_exempt")) is None, (
        "an unmapped side must fail closed like an unmapped status — inventing SELL hands "
        "reconciliation a phantom reducing order"
    )
    assert any("short_exempt" in w for w in host._log.warnings), (
        "the skipped order must be NAMED with the side that failed to map"
    )


def test_an_ABSENT_side_is_skipped_too():
    host = _host()
    row = _row()
    del row["side"]
    assert host._parse_order_report(row) is None
