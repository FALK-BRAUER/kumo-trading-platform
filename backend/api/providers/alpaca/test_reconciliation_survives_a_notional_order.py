"""One dollar-based external order must not abort the whole reconciliation batch (#643).

THE DEFECT
----------
`_parse_order_report` does `Quantity.from_str(raw.get("qty", "0"))`. A DOLLAR-BASED (notional) Alpaca
order carries `"qty": null` — the key is PRESENT, so the `"0"` default never applies and
`Quantity.from_str(None)` raises `TypeError`. `generate_order_status_reports` had no per-row guard, so
that one row killed the ENTIRE batch. That method backs Nautilus's startup mass-status query, whose
`generate_mass_status` wraps the whole gather in `except Exception` and returns None → "Cannot
reconcile execution state" → zero strategies start, empty book, node logs RUNNING. That is #613
exactly — 26 inert minutes during market hours, and per #635 four phantom EXTERNAL positions minted
on the way back. External orders are deliberately retained (`filter_unclaimed_external_orders=False`),
so a notional order placed anywhere on the account reaches this path.

THE FIX'S SHAPE (absence-is-not-permission)
-------------------------------------------
Per-row guard: skip the unrepresentable row, NAME it (structured log.error + a record published on
the health plane), continue the batch. Three states, never two: a batch that skipped nothing
publishes an EMPTY list (known-clean), a batch that skipped publishes the offenders (known-bad), and
a bus that never saw the topic is "never asked" — a skipped order is its own condition, never a
silent drop.

Doubles are built from what production emits: the notional row is the GET /v2/orders shape Alpaca
returns for a notional order (`"qty": null` — key present, value null), and the fixture-property test
below proves the double raises exactly where and what production raises, so the batch test cannot go
vacuously green.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId, VenueOrderId

from api.providers.alpaca.exec_client import _UNRECONCILED_TOPIC, AlpacaExecutionClient


class _Clk:
    def timestamp_ns(self):
        return 1_800_000_000_000_000_000


class _Log:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))


class _Cache:
    """A cache that has never seen these venue ids — the external-order case (#79)."""

    def client_order_id(self, venue_order_id):
        return None


class _Bus:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))


class _Http:
    def __init__(self, rows):
        self._rows = rows

    async def list_orders(self, status="all", paginate=False):
        assert status == "all" and paginate is True  # the read contract pinned elsewhere; keep it honest here
        return list(self._rows)


def _host(rows):
    host = SimpleNamespace()
    host.account_id = AccountId("ALPACA-TEST")
    host._clock = _Clk()
    host._log = _Log()
    host._cache = _Cache()
    host._msgbus = _Bus()
    host._http = _Http(rows)
    host._symbol_to_id = {
        "AEM": InstrumentId.from_str("AEM.XNYS"),
        "SPY": InstrumentId.from_str("SPY.ARCX"),
    }
    # bind the REAL methods, so the seam under test is production's own code path
    host._parse_order_report = AlpacaExecutionClient._parse_order_report.__get__(host)
    host._client_order_id_for = AlpacaExecutionClient._client_order_id_for.__get__(host)
    return host


def _share_row(venue_id, coid, *, symbol="AEM"):
    """A plain share-quantity order as GET /v2/orders returns it."""
    return {
        "id": venue_id, "client_order_id": coid, "symbol": symbol, "side": "sell",
        "type": "stop", "time_in_force": "gtc", "status": "held",
        "qty": "10", "filled_qty": "0", "notional": None,
        "filled_avg_price": None, "limit_price": None, "stop_price": "50",
    }


def _notional_row(venue_id="v-notional", coid="ext-dollar-buy"):
    """A DOLLAR-BASED order as GET /v2/orders returns it: `qty` is PRESENT and null.

    `raw.get("qty", "0")` therefore returns None — the default never applies. This is the exact shape
    that aborted the batch.
    """
    return {
        "id": venue_id, "client_order_id": coid, "symbol": "SPY", "side": "buy",
        "type": "market", "time_in_force": "day", "status": "filled",
        "qty": None, "notional": "500", "filled_qty": "1.234567",
        "filled_avg_price": "405.12", "limit_price": None, "stop_price": None,
    }


def _run(host):
    cmd = SimpleNamespace(instrument_id=None, start=None, end=None, open_only=False)
    return asyncio.run(AlpacaExecutionClient.generate_order_status_reports(host, cmd))


# --------------------------------------------------------------------------------------------------
# Fixture property FIRST: the double must raise where production raises, or the batch test below
# proves nothing. `Quantity.from_str(None)` raises TypeError ("'value' argument was `None`") — read
# off the installed package, not invented. This stays true AFTER the fix, because the guard belongs
# in the batch loop, not inside the parser: the row itself remains genuinely unrepresentable.
# --------------------------------------------------------------------------------------------------

def test_the_notional_row_is_genuinely_unrepresentable():
    host = _host([])
    with pytest.raises(TypeError, match="value"):
        host._parse_order_report(_notional_row())


def test_the_share_rows_actually_parse():
    """The survivors must be real, parseable rows — a batch of unparseable rows tests nothing."""
    host = _host([])
    report = host._parse_order_report(_share_row("v1", "a"))
    assert report is not None and report.venue_order_id == VenueOrderId("v1")
    assert report.client_order_id == ClientOrderId("a")


# --------------------------------------------------------------------------------------------------
# The seam: the real `generate_order_status_reports`, the entry point Nautilus's mass-status calls.
# --------------------------------------------------------------------------------------------------

def test_one_notional_order_does_not_abort_the_batch():
    """N=3 with one dollar-based row → N-1 reports and the offender NAMED. Seen red pre-fix: the
    whole call raised TypeError and reconciliation got NOTHING — the #613 inert shape."""
    host = _host([_share_row("v1", "a"), _notional_row(), _share_row("v3", "c")])
    reports = _run(host)
    assert [r.venue_order_id.value for r in reports] == ["v1", "v3"], (
        "one unrepresentable row must cost exactly that row, never the batch"
    )
    # NAMED, loudly: log.error carrying the venue id — not DEBUG, not a silent drop.
    named = [m for m in host._log.errors if "v-notional" in m]
    assert named, f"the skipped order was not named in an error log: errors={host._log.errors!r}"
    assert "SPY" in named[0] and "TypeError" in named[0], (
        "the error must say WHICH order and WHY, or the operator is left grepping"
    )


def test_the_skipped_order_reaches_the_health_plane():
    """The record rides the bus (like drift, #26) so the engine folds it into the health frame and
    the alert plane announces it — a log line alone dies unread (#454)."""
    host = _host([_share_row("v1", "a"), _notional_row()])
    _run(host)
    frames = [p for t, p in host._msgbus.published if t == _UNRECONCILED_TOPIC]
    assert frames, "nothing published on the unreconciled-orders topic — the skip is invisible upstream"
    rows = frames[-1]["orders"]
    assert len(rows) == 1
    row = rows[0]
    assert row["venue_order_id"] == "v-notional"
    assert row["symbol"] == "SPY"
    assert row["client_order_id"] == "ext-dollar-buy"
    assert "TypeError" in row["reason"]


def test_a_clean_batch_publishes_an_empty_list_so_the_condition_can_clear():
    """Three states, never two: [] is KNOWN-CLEAN, stated — not inferred from silence. Without this
    a fixed offender would leave the last bad frame standing forever."""
    host = _host([_share_row("v1", "a"), _share_row("v3", "c")])
    reports = _run(host)
    assert len(reports) == 2
    frames = [p for t, p in host._msgbus.published if t == _UNRECONCILED_TOPIC]
    assert frames and frames[-1]["orders"] == [], (
        "a clean batch must STATE clean — absence of a frame is a timestamp, not a property"
    )


def test_a_broken_publish_does_not_become_its_own_abort_path():
    """The guard exists to stop one failure from killing the batch; its own reporting must obey the
    same rule."""
    host = _host([_share_row("v1", "a"), _notional_row()])

    class _DeadBus:
        def publish(self, topic, payload):
            raise RuntimeError("bus down")

    host._msgbus = _DeadBus()
    reports = _run(host)
    assert [r.venue_order_id.value for r in reports] == ["v1"]
    assert any("publish failed" in w for w in host._log.warnings)


# --------------------------------------------------------------------------------------------------
# Wiring above the provider (venue-neutral dicts only). Declared-but-unwired is the #574 shape, so
# each hop is asserted: bus → engine handler → health frame → alert plane.
# --------------------------------------------------------------------------------------------------

def test_the_engine_subscribes_and_folds_the_skips_into_the_health_frame():
    import ast
    import inspect
    import pathlib
    import textwrap

    from api import engine_node
    from api.engine_node import UiFeedStrategy

    # the subscription exists and targets the handler
    src = pathlib.Path(inspect.getfile(engine_node)).read_text()
    assert "_UNRECONCILED_TOPIC, self._on_broker_unreconciled" in src, (
        "the engine never subscribes to the unreconciled-orders topic — the provider publishes into a void"
    )
    # the handler reads the payload key the provider writes (two derivations of one contract)
    handler = textwrap.dedent(inspect.getsource(UiFeedStrategy._on_broker_unreconciled))
    assert 'snapshot.get("orders"' in handler
    # the health frame carries it — grep the publish dict, not a comment
    tree = ast.parse(src)
    keys = [
        getattr(k, "value", None)
        for n in ast.walk(tree) if isinstance(n, ast.Dict)
        for k in n.keys if isinstance(k, ast.Constant)
    ]
    assert "unreconciled_orders" in keys, (
        "the health frame does not carry unreconciled_orders — the skip never reaches a surface a human reads"
    )


def test_the_engine_handler_stores_what_the_provider_publishes():
    from api.engine_node import UiFeedStrategy

    host = SimpleNamespace(_unreconciled_orders=[])
    payload = {"orders": [{"venue_order_id": "v-notional", "symbol": "SPY", "reason": "TypeError(...)"}],
               "ts": 1}
    UiFeedStrategy._on_broker_unreconciled(host, payload)
    assert host._unreconciled_orders == payload["orders"]
    # and the clean frame clears it
    UiFeedStrategy._on_broker_unreconciled(host, {"orders": [], "ts": 2})
    assert host._unreconciled_orders == []


def test_health_conditions_announce_a_skipped_order_per_venue_id():
    from api.alerts import health_conditions

    health = {
        "subsystems": [],
        "reconcile_drift": [],
        "unreconciled_orders": [
            {"venue_order_id": "v-notional", "symbol": "SPY", "client_order_id": "ext-dollar-buy",
             "reason": "TypeError(\"'value' argument was `None`\")"},
        ],
    }
    out = health_conditions(health)
    assert "unreconciled_order:v-notional" in out, f"no alert for the skipped order: {sorted(out)}"
    alert = out["unreconciled_order:v-notional"]
    assert "SPY" in alert.title
    assert "v-notional" in alert.body
    # and the condition CLEARS when the provider states clean
    assert not any(k.startswith("unreconciled_order:") for k in health_conditions(
        {"subsystems": [], "reconcile_drift": [], "unreconciled_orders": []}))
