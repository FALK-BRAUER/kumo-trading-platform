"""Every account-wide order read must paginate (#387, again).

Alpaca caps `/v2/orders` at 500 rows, NEWEST FIRST. Without `paginate=True` a read returns at most the
newest page and looks exactly like a complete answer — a short list does not read as an error, it reads
as "nothing else is resting". The oldest rows are the first to vanish, and a long-lived GTC protective
stop is precisely the oldest thing on the account.

`generate_order_status_reports` is what NAUTILUS'S OWN STARTUP RECONCILIATION calls through
`generate_mass_status`, so every boot reconciled against a possibly-truncated list. The paper account
was at 269 orders on 2026-08-20 and climbs every session — the cap is on ROWS, not on time.

The consequence is #387 and #245 restated: a dropped stop makes reconciliation skip adoption, the
protection check read the position as naked and arm a duplicate, and the exit path read zero shares
reserved and submit against them.

AIMED AT THE CLASS. `engine_node.py` already passes `paginate=True` at both its call sites, so this was
two derivations of one fact disagreeing inside one repo. The AST rule below covers every current and
future caller rather than the three that happened to be wrong, because this is the second time #387 has
been fixed.
"""

from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).parent.parent.parent  # api/


def _unpaginated_calls():
    """Every `list_orders(...)` call in api/ that does not pass `paginate=True`."""
    out = []
    for path in sorted(_ROOT.rglob("*.py")):
        if path.name.startswith("test_") or path.name == "http.py":
            continue  # the definition itself, and the tests that drive it directly
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "list_orders":
                continue
            kw = {k.arg: k for k in node.keywords}
            ok = "paginate" in kw and isinstance(kw["paginate"].value, ast.Constant) \
                and kw["paginate"].value.value is True
            if not ok:
                out.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    return out


#: The functions that read the order book today, NAMED. A floor (`found >= 4`) is not enough and the
#: kumo-cockpit-ibkr session hit exactly why: their call-site test collected `{builder: was_fed}` and
#: asserted none were unfed, so DELETING a call — what a refactor does — removed the key instead of
#: setting it False, the set went empty, and the assertion passed. An invariant over an empty set
#: passes for the wrong reason, and the empty set can arrive through a door the floor does not guard.
#:
#: Naming them means a call site that DISAPPEARS fails here and says which one, rather than quietly
#: lowering a count that still clears the bar.
EXPECTED_READERS = frozenset({
    "_reconcile_protection_inner",       # engine_node: the protection coverage oracle
    "_venue_reducing_orders",            # engine_node: shares already reserved by resting sells
    "generate_order_status_reports",     # exec_client: Nautilus startup reconciliation
    # NOT `generate_order_status_report` (singular) since #354: it asks Alpaca for ONE order by id
    # (`GET /v2/orders/{id}` / `orders:by_client_order_id`), which cannot truncate — nothing to paginate.
    "_cancel_all_orders",          # exec_client: scoped cancel
})


def _reader_names() -> set[str]:
    """Every function containing a `list_orders` call, across api/."""
    out = set()
    for path in sorted(_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text())
        parent = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parent[c] = n
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "list_orders":
                x = n
                while x is not None:
                    if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.add(x.name)
                        break
                    x = parent.get(x)
    return out


def test_the_scan_sees_exactly_the_readers_it_expects():
    """Named, not counted. A reader that vanishes fails here by NAME; a new one fails here and has to
    be added deliberately — which is the moment someone asks whether it paginates."""
    found = _reader_names()
    assert found == EXPECTED_READERS, (
        f"order-book readers changed: gone={sorted(EXPECTED_READERS - found)} "
        f"new={sorted(found - EXPECTED_READERS)}. A new one must be added here AND paginate"
    )


def test_no_account_wide_order_read_truncates_silently():
    assert _unpaginated_calls() == [], (
        f"{_unpaginated_calls()} call list_orders without paginate=True. Alpaca caps the response at "
        f"500 rows newest-first, so this silently drops the OLDEST orders — which is where long-lived "
        f"GTC protective stops live (#387)"
    )


# ==================================================================================================
# THE SEAM. The AST rule above pins the ARGUMENT; this drives the method and proves a second-page
# order actually reaches the reconciliation report.
# ==================================================================================================
class _RefusingHttp:
    """A double that REJECTS what Alpaca effectively rejects.

    Alpaca does not error on an unpaginated read — it returns page one and looks fine, which is exactly
    why this shipped. A permissive double reproduces that and teaches nothing, so this one raises: the
    test then fails loudly on the call SHAPE instead of passing on a truncated result.
    """

    def __init__(self, pages):
        self._pages = pages

    async def list_orders(self, status="all", limit=500, paginate=False):
        if not paginate:
            raise AssertionError(
                "list_orders called without paginate=True — Alpaca would have returned only the "
                "newest 500 rows and this would have looked like a complete answer"
            )
        return [o for page in self._pages for o in page]


_NEW_ORDER = {"id": "new-1", "symbol": "AEM", "status": "new", "qty": "9", "filled_qty": "0",
              "side": "buy", "order_type": "market", "time_in_force": "day",
              "client_order_id": "COID-NEW"}
#: The thing that goes missing: a GTC protective stop opened weeks ago, so it sorts LAST.
_OLD_STOP = {"id": "old-1", "symbol": "AEM", "status": "new", "qty": "9", "filled_qty": "0",
             "side": "sell", "order_type": "stop", "stop_price": "200.00", "time_in_force": "gtc",
             "client_order_id": "PROT-SELL-AEM"}


class _FakeSelf:
    """A duck-typed `self`, because the real class CANNOT be impersonated.

    `AlpacaExecutionClient` inherits from a Cython `Component` whose `_log` is READ-ONLY — assigning it
    on an instance raises. Rather than loosen production to make a double possible, the method is called
    UNBOUND with an object carrying exactly the four attributes it touches. That also states the
    dependency surface out loud: if the method starts using a fifth, this fails rather than passing
    against a mock that would have absorbed it.
    """

    def __init__(self, pages):
        from nautilus_trader.model.identifiers import InstrumentId

        self._http = _RefusingHttp(pages)
        self._symbol_to_id = {"AEM": InstrumentId.from_str("AEM.XNYS")}
        self._log = type("L", (), {"warning": lambda self, m: None,
                                   "error": lambda self, m: None})()
        self._clock = type("C", (), {"timestamp_ns": lambda self: 1_787_000_000_000_000_000})()
        # THE REAL PARSER, bound to this object. Stubbing it would test that a stub returns what the
        # stub was told to return; binding production's own method means the report must survive the
        # actual field mapping, and a change there breaks this test rather than sliding past it.
        from api.providers.alpaca.exec_client import AlpacaExecutionClient

        self._parse_order_report = AlpacaExecutionClient._parse_order_report.__get__(self)
        # Read by the parser when it builds the report. Named here rather than mocked away, so the
        # dependency surface of this method stays visible in one place.
        from nautilus_trader.model.identifiers import AccountId

        self.account_id = AccountId("ALPACA-001")
        # Also real, and bound for the same reason: id resolution is where #242's unclaimed-leg bug
        # lived, so a stub here would quietly test nothing. Its only dependency is a cache lookup that
        # MISSES, which is the ordinary path for an order we did not submit in this process.
        self._client_order_id_for = AlpacaExecutionClient._client_order_id_for.__get__(self)
        self._cache = type("Cache", (), {"client_order_id": lambda self, _v: None})()


def _reports(pages):
    import asyncio

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    fn = AlpacaExecutionClient.generate_order_status_reports
    return asyncio.run(fn(_FakeSelf(pages), None))


def test_the_fixture_puts_the_protective_stop_on_the_SECOND_page():
    """Fixture property first. If both orders sat on page one, a truncating implementation would pass
    this test — the fixture must be able to violate the property it asserts."""
    pages = [[_NEW_ORDER], [_OLD_STOP]]
    assert len(pages) > 1 and _OLD_STOP not in pages[0]


def test_an_old_protective_stop_on_page_two_REACHES_reconciliation():
    """The live consequence. Nautilus's startup reconciliation calls this through
    `generate_mass_status`; a stop missing here is not adopted, the protection check then reads the
    position as naked and arms a duplicate, and the exit path reads zero shares reserved (#387, #245)."""

    reports = _reports([[_NEW_ORDER], [_OLD_STOP]])
    coids = {str(r.client_order_id) for r in reports}
    assert "PROT-SELL-AEM" in coids, f"the page-two protective stop never reached reconciliation: {coids}"
    assert len(reports) == 2


def test_the_reconciliation_reads_ask_for_ALL_statuses_not_just_open():
    """The other half of #387, and it survived the first sweep.

    Narrowing `generate_order_status_reports` to `status="open"` passed every test: the fixture's
    orders are all open, so a truncation by STATUS was invisible where a truncation by PAGE was not.
    Reconciliation must see filled and cancelled orders too — that is how Nautilus learns an order it
    submitted has already terminated, and without them a filled order looks like one that never
    resolved.

    Pinned PER FUNCTION: the scoped-cancel path uses `status="open"` legitimately, and a blanket rule
    would flag it and get itself deleted.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "exec_client.py").read_text()
    tree = ast.parse(src)
    checked = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("generate_order_status_report"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "list_orders":
                status = {k.arg: k.value for k in node.keywords}.get("status")
                assert isinstance(status, ast.Constant) and status.value == "all", (
                    f"{fn.name} reconciles against status={getattr(status, 'value', '?')!r} — it must "
                    f"ask for 'all', or filled and cancelled orders are invisible to reconciliation"
                )
                checked += 1
    # ONE since #354: the singular lookup asks by id and no longer lists. The batch reader is the one
    # that must ask for 'all'; a count of 0 here means the scan is not seeing it.
    assert checked >= 1, f"only {checked} reconciliation reads found — this test is not seeing them"
