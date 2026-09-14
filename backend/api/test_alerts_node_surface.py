"""#843 — `_announce_stranded_claims` has never run in production.

Measured on paper 2026-09-10 09:05 SGT, cockpit a1aab47, every poll:

    stranded_claims check failed (other alerts unaffected): AttributeError('the node exposes neither
    `account_positions` nor `claims_by_strategy` — the claims planes cannot be read, ...')

The node `app.py:233` hands to `AlertsService` is a `RedisConsumer`. It has `positions()`. The only
object in this repo carrying `account_positions` is the venue double (`api/venues/doubles.py:96`), and
every test in `test_alerts.py` sets both attributes on its `FakeNode` by hand — so the suite has been
green against a shape production does not have. The absent-on-both guard inside the check did its job
(a warning, not a green tick); the wiring above it was never connected.

These tests drive the check through the PRODUCTION class. The first one is the fixture property: it
proves that class has neither attribute, so a double with them pre-set cannot satisfy anything below.
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import pathlib
from contextlib import asynccontextmanager

from api.alerts import AlertsService, Notifier
from api.consumer import RedisConsumer
from api.models import PositionDTO

#: The 2026-08-31 freeze in the detector's own docstring: 23 real ARKK shares, both lanes claim all 23,
#: `own_ceiling` floors both to zero — sellable 0, stranded 23.
ARKK_BOOK = [
    PositionDTO(instrument_id="ARKK.BATS", side="LONG", quantity=23, avg_px_open=50.0,
                realized_pnl="0.00 USD", strategy_id="BCTROT-004"),
    PositionDTO(instrument_id="ARKK.BATS", side="FLAT", quantity=0, avg_px_open=0.0,
                realized_pnl="0.00 USD", strategy_id="MOMENTUM-002"),
]
ARKK_CLAIMS = [("BCTROT-004", "ARKK", 23.0), ("MOMENTUM-002", "ARKK", 23.0)]


class _Tx:
    def __init__(self):
        self.sent = []

    async def send(self, alert, *, silent: bool = False) -> bool:
        self.sent.append(alert)
        return True


class _Row:
    def __init__(self, sid, sym, qty):
        self._mapping = {"strategy_id": sid, "symbol": sym, "qty": qty}


def _production_node(positions, *, drift: list | None = None) -> RedisConsumer:
    """A REAL `RedisConsumer` — constructed, not `__new__`-ed — fed the same two frames the engine
    publishes over Redis (`consumer.py:_apply`): a `positions` frame and a `health` frame. Codex
    (#843 step 3) flagged that a `__new__` shell diverges the moment the check reads `health()`,
    which the fixed check does for `reconcile_drift`."""
    import json
    from api.feed_config import FeedConfig
    cfg = FeedConfig(data_provider="alpaca", provider_config={}, backfill_days=0, engine_kind="live",
                     trader_id="COCKPIT-001", venue="ALPACA", symbols=(), chart_defaults={},
                     chart_lookback_days={}, exec_provider="none", exec_config={}, ui_bridge={})
    node = RedisConsumer(cfg=cfg)
    node._apply({"type": "positions",
                 "payload": json.dumps({"positions": [p.model_dump() for p in positions]})})
    # `ts` MUST be present: the consumer refreshes bridge-liveness only when the engine's own ts
    # advances (`consumer.py:_apply`, "else a dead engine looks alive"). Without it `bridge_ok` is
    # False and `health()` gates `reconcile_drift` to [] — the exclusion below would silently not exist.
    node._apply({"type": "health",
                 "payload": json.dumps({"ts": 1, "engine_ok": True, "last_tick_ts": 1,
                                        "reconcile_drift": list(drift or [])})})
    return node


class _Reads:
    """How many times the ledger was read. A SILENCE assertion cannot tell a clean book from a check
    that never looked (the mutation bite left two silence tests green with the fix reverted); the
    read count can."""
    def __init__(self):
        self.count = 0


def _claims_in_postgres(monkeypatch, rows) -> _Reads:
    """`alerts.py` reaches the ledger through `api.db.engine.session_factory`; this is that, with rows."""
    reads = _Reads()

    class _Result:
        def all(self):
            return [_Row(*r) for r in rows]

    class _Session:
        async def execute(self, *_a, **_k):
            reads.count += 1
            return _Result()

    @asynccontextmanager
    async def _factory():
        yield _Session()

    import api.db.engine as engine
    monkeypatch.setattr(engine, "session_factory", _factory)
    return reads


def _svc(node, tx):
    return AlertsService(node, notifier=Notifier(transport=tx, dedupe_backend="memory",
                                                 settings={"enabled": True}))


# -- fixture property ------------------------------------------------------------------------------
def test_the_production_node_has_NEITHER_attribute_the_check_reads_and_DOES_have_positions():
    """If this ever passes because someone added `account_positions` to `RedisConsumer`, fine — but
    then the second assertion still has to hold, and the tests below still have to read a real book."""
    assert not hasattr(RedisConsumer, "account_positions")
    assert not hasattr(RedisConsumer, "claims_by_strategy")
    node = _production_node(ARKK_BOOK)
    assert type(node) is RedisConsumer and node.health()["bridge_ok"] is True
    assert [p.instrument_id for p in node.positions()] == ["ARKK.BATS", "ARKK.BATS"]
    # The seeded book must be able to EXPRESS the freeze through the assembly `/claims` uses —
    # otherwise a check that reads it correctly would have nothing to page on and the seam test
    # below would be green for the wrong reason.
    from api.claims_endpoint import account_from_positions
    from api.claims_invariant import exit_ceilings
    account = account_from_positions(node.positions())
    claims = {"BCTROT-004": {"ARKK": 23.0}, "MOMENTUM-002": {"ARKK": 23.0}}
    assert account == {"ARKK": 23.0}
    assert exit_ceilings(account, claims)["ARKK"]["stranded"] == 23


# -- the seam ---------------------------------------------------------------------------------------
def test_the_freeze_PAGES_when_the_check_reads_the_PRODUCTION_node(monkeypatch):
    """Red on a1aab47: `_check_failures["stranded_claims"] == (1, "AttributeError(...)")` and nothing
    sent. The check must read the book the way `/claims` does and page the ARKK freeze, naming both
    claimants."""
    _claims_in_postgres(monkeypatch, ARKK_CLAIMS)
    tx = _Tx()
    svc = _svc(_production_node(ARKK_BOOK), tx)

    asyncio.run(svc._announce_stranded_claims())

    assert "stranded_claims" not in svc._check_failures, svc._check_failures.get("stranded_claims")
    assert tx.sent, "23 shares no lane can sell, read through the production node, and nothing paged"
    text = tx.sent[0].title + tx.sent[0].body
    assert "ARKK" in text and "BCTROT-004" in text and "MOMENTUM-002" in text


def test_a_consistent_book_read_through_the_PRODUCTION_node_is_SILENT_and_NOT_failing(monkeypatch):
    """The other half of "it runs": a clean book must be a clean poll, not a broken one."""
    reads = _claims_in_postgres(monkeypatch, [("BCTROT-004", "ARKK", 23.0)])
    tx = _Tx()
    svc = _svc(_production_node(ARKK_BOOK), tx)
    asyncio.run(svc._announce_stranded_claims())
    assert "stranded_claims" not in svc._check_failures, svc._check_failures.get("stranded_claims")
    assert tx.sent == []
    assert reads.count == 1, "silent because it never looked — that is the defect, not a clean book"


# -- the class: nothing in the alert plane may read an attribute the node it is given lacks ----------
def _node_reads(tree: ast.AST) -> set[str]:
    """Every attribute `alerts.py` reads off `self._node`: direct `self._node.x`, and the literal in
    `getattr`/`hasattr(self._node, "x", ...)`."""
    read: set[str] = set()

    def _is_node(expr) -> bool:
        return (isinstance(expr, ast.Attribute) and expr.attr == "_node"
                and isinstance(expr.value, ast.Name) and expr.value.id == "self")

    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and _is_node(n.value):
            read.add(n.attr)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ("getattr", "hasattr") and n.args and _is_node(n.args[0])):
            assert len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and isinstance(
                n.args[1].value, str), f"line {n.lineno}: a non-literal attribute name cannot be checked"
            read.add(n.args[1].value)
    return read


def _alerts_tree() -> ast.AST:
    from api import alerts
    return ast.parse(pathlib.Path(inspect.getfile(alerts)).read_text())


def test_the_node_is_only_ever_read_DIRECTLY_so_the_scan_below_is_complete():
    """`node = self._node; node.x` or passing `self._node` into a helper would read attributes the scan
    cannot see (codex, #843 step 3). Forbid the shapes rather than enumerate them."""
    def _is_node(expr) -> bool:
        return (isinstance(expr, ast.Attribute) and expr.attr == "_node"
                and isinstance(expr.value, ast.Name) and expr.value.id == "self")

    offenders = []
    for n in ast.walk(_alerts_tree()):
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and _is_node(getattr(n, "value", None)):
            # `self._node = node` in __init__ is the one binding that IS the node; anything else aliases it.
            targets = [n.target] if not isinstance(n, ast.Assign) else n.targets
            if not all(_is_node(t) for t in targets):
                offenders.append(f"line {n.lineno}: aliased")
        if isinstance(n, ast.Call):
            callee = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if callee not in ("getattr", "hasattr") and any(_is_node(a) for a in n.args):
                offenders.append(f"line {n.lineno}: passed to {callee}()")
    assert offenders == [], offenders


def test_every_node_attribute_the_alert_plane_reads_EXISTS_on_every_node_the_app_can_pass():
    """`create_node()` returns a `RedisConsumer` (live) or a `NodeManager` (synthetic), typed as the
    `Node` protocol — the alert plane runs against all three. Two attributes were only ever satisfied
    by a test double; this pins the class, not the instance."""
    from api.node import NodeManager
    from api.node_factory import Node

    read = _node_reads(_alerts_tree())
    # Coverage guard: the scan must have found the reads we know are there, or it is scanning nothing.
    assert {"positions", "health", "account"} <= read, read
    missing = {cls.__name__: sorted(a for a in read if not hasattr(cls, a))
               for cls in (RedisConsumer, NodeManager, Node)}
    assert all(v == [] for v in missing.values()), (
        f"alerts.py reads attributes these nodes do not have: {missing} — every check that reaches "
        f"for them is inert in production and green only against a double"
    )


# -- one derivation of the claims book, one seam to Postgres --------------------------------------------
def test_the_alert_plane_has_NO_private_copy_of_the_claims_query():
    """`claims_endpoint.CLAIMS_SQL` is the one definition and `/claims` the one assembly. A second
    copy already lives in `_announce_split_divergence` (alerts.py ~661) and a third would be the fix
    for #843 done wrong: two derivations of one fact drift, and the drift is invisible while they agree."""
    private = [n.lineno for n in ast.walk(_alerts_tree())
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "exec_position_state" in n.value]
    assert private == [], f"alerts.py carries its own claims SQL at lines {private}; use claims_endpoint.CLAIMS_SQL"


def test_postgres_is_reached_only_through_api_db_engine_session_factory_at_call_time():
    """The seam `_claims_in_postgres` patches. A module-level alias or `from api.db import ...` would
    read the live database under this test and bypass the patch silently (codex, #843 step 3)."""
    bad = []
    for n in ast.walk(_alerts_tree()):
        if isinstance(n, ast.ImportFrom) and any(a.name == "session_factory" for a in n.names):
            if n.module != "api.db.engine" or n.col_offset == 0:
                bad.append(f"line {n.lineno}: from {n.module} (col {n.col_offset})")
    assert bad == [], bad


# -- the third state: symbols the broker and cache disagree on are NOT computed --------------------
def test_a_symbol_in_reconcile_drift_is_EXCLUDED_and_named_never_computed_on_the_cache(monkeypatch):
    """Broker net is the only hard anchor (CLAUDE.md). ARKK drifted: the cache says 23, the broker 0.
    A freeze computed on 23 would be computed on a number the broker contradicts — so ARKK is left
    out and NAMED, and a book that is clean apart from ARKK is silent, not failing, not clean-by-
    omission."""
    _claims_in_postgres(monkeypatch, ARKK_CLAIMS)
    tx = _Tx()
    node = _production_node(ARKK_BOOK, drift=[{"symbol": "ARKK", "broker_qty": 0, "cockpit_qty": 23}])
    svc = _svc(node, tx)
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
    assert not any("FROZEN" in a.title for a in tx.sent), "paged a freeze on a symbol the broker disagrees about"
    assert svc._stranded_excluded == {"ARKK"}
    assert "stranded_claims" not in svc._check_failures


# -- run-level failure semantics: a failed read must SURVIVE the `_checked` wrapper -----------------
def test_a_failed_read_is_still_recorded_AFTER_the_poll_loop_wrapper_runs(monkeypatch):
    """`run()` calls the check through `_checked`, which calls `_check_ok` on a normal return. The old
    check caught its own failure, recorded it and RETURNED — so the wrapper erased the record on the
    same poll and BROKEN_CHECK_POLLS never reached two (codex, #843 scope review). Drive the wrapper."""
    class _Broken:
        def sessionmaker(self):
            raise RuntimeError("postgres away")

    import api.db.engine as engine

    @__import__("contextlib").asynccontextmanager
    async def _factory():
        raise RuntimeError("postgres away")
        yield  # pragma: no cover

    monkeypatch.setattr(engine, "session_factory", _factory)
    tx = _Tx()
    svc = _svc(_production_node(ARKK_BOOK), tx)
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
    n, why = svc._check_failures.get("stranded_claims", (0, ""))
    assert n == 1 and "postgres away" in why, svc._check_failures
    assert tx.sent == []


# -- the one fold and the dotted ticker --------------------------------------------------------------
def test_the_fold_SUMS_a_duplicate_key_rather_than_overwriting_it():
    """`build_breaches` overwrote and `build_split` summed — two derivations, one already wrong."""
    from api.claims_invariant import claims_by_strategy
    rows = [{"strategy_id": "A", "symbol": "X", "qty": 5}, {"strategy_id": "A", "symbol": "X", "qty": 7},
            {"strategy_id": "B", "symbol": "X", "qty": 1}]
    assert claims_by_strategy(rows) == {"A": {"X": 12.0}, "B": {"X": 1.0}}


def test_a_dotted_ticker_keeps_its_dot_in_BOTH_folds():
    """`BRK.B.XNYS` is `BRK.B`; `split(".")[0]` made it `BRK` and the account book lost the position."""
    from api.claims_endpoint import account_from_positions
    from api.split_divergence import split_divergence
    book = [PositionDTO(instrument_id="BRK.B.XNYS", side="LONG", quantity=3, avg_px_open=1.0,
                        realized_pnl="0.00 USD", strategy_id="MANUAL-001")]
    assert account_from_positions(book) == {"BRK.B": 3.0}
    cache_rows = [{"strategy_id": "MANUAL-001", "instrument_id": "BRK.B.XNYS", "quantity": 3, "side": "LONG"}]
    assert split_divergence(cache_rows, {"MANUAL-001": {"BRK.B": 3.0}}) == {}


def test_NOTHING_under_api_carries_a_private_claims_query_except_claims_invariant():
    """The `alerts.py` guard above, widened to the package: every consumer runs the one query."""
    import api
    root = pathlib.Path(inspect.getfile(api)).parent
    offenders = []
    # `session_state.py` reads a LANE'S TRAIL (entry/peak/sessions per row) for the session view — a
    # different question from the claims book, and it never folds rows by strategy. The one allowed
    # sibling, named here with its reason so the next reader can tell an exemption from an oversight.
    allowed = {"claims_invariant.py", "session_state.py"}
    for f in sorted(root.rglob("*.py")):
        if f.name.startswith("test_") or f.name in allowed:
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and "FROM exec_position_state" in n.value:
                offenders.append(f"{f.relative_to(root)}:{n.lineno}")
    assert offenders == [], offenders


# -- codex, implementation review: three more states that must not read as clean --------------------
def test_a_STALE_bridge_is_an_UNKNOWN_book_not_a_clean_one(monkeypatch):
    """`positions()` serves the last frame forever; `health()` gates drift to [] once the bridge is
    stale. Read together a dead engine looks like a clean, drift-free book. The check must raise so
    `_checked` counts it, and must not clear a freeze alarmed while the engine was alive."""
    _claims_in_postgres(monkeypatch, ARKK_CLAIMS)
    tx = _Tx()
    node = _production_node(ARKK_BOOK)
    svc = _svc(node, tx)
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
    assert len(tx.sent) == 1 and "ARKK" in tx.sent[0].title            # alive: the freeze pages

    node._health_at = None                                              # the bridge goes stale
    assert node.health()["bridge_ok"] is False
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
    n, why = svc._check_failures.get("stranded_claims", (0, ""))
    assert n == 1 and "bridge stale" in why, svc._check_failures
    assert "ARKK" in svc._stranded_alarmed, "a stale bridge cleared a freeze it could not see"


def test_a_DOTTED_drift_symbol_excludes_the_dotted_account_key(monkeypatch):
    """Drift carries a bare `symbol` (`BRK.B`), exactly the key `account_from_positions` builds.
    Running it through `symbol_of` made it `BRK`, so a dotted drift symbol would NOT be excluded."""
    book = [PositionDTO(instrument_id="BRK.B.XNYS", side="LONG", quantity=3, avg_px_open=1.0,
                        realized_pnl="0.00 USD", strategy_id="BCTROT-004")]
    _claims_in_postgres(monkeypatch, [("BCTROT-004", "BRK.B", 3.0), ("MOMENTUM-002", "BRK.B", 3.0)])
    tx = _Tx()
    svc = _svc(_production_node(book, drift=[{"symbol": "BRK.B", "broker_qty": 0, "cockpit_qty": 3}]), tx)
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
    assert svc._stranded_excluded == {"BRK.B"}
    assert not any("FROZEN" in a.title for a in tx.sent), "computed a freeze on a drifted symbol"


def test_an_exclusion_is_SAID_once_per_change_and_forgotten_when_it_clears(monkeypatch):
    """Not a log line: an operator-visible alert when the excluded set changes to non-empty; nothing
    on the next identical poll; news again if it returns after clearing."""
    _claims_in_postgres(monkeypatch, ARKK_CLAIMS)
    tx = _Tx()
    drift = [{"symbol": "ARKK", "broker_qty": 0, "cockpit_qty": 23}]
    node = _production_node(ARKK_BOOK, drift=drift)
    svc = _svc(node, tx)
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
    said = [a for a in tx.sent if "NOT computing" in a.title]
    assert len(said) == 1 and "ARKK" in said[0].body and said[0].critical is False

    import json
    node._apply({"type": "health", "payload": json.dumps({"ts": 2, "engine_ok": True, "reconcile_drift": []})})
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))     # cleared: freeze pages
    assert svc._stranded_excluded == set() and any("FROZEN" in a.title for a in tx.sent)
    node._apply({"type": "health", "payload": json.dumps({"ts": 3, "engine_ok": True, "reconcile_drift": drift})})
    asyncio.run(svc._checked("stranded_claims", svc._announce_stranded_claims))
    assert len([a for a in tx.sent if "NOT computing" in a.title]) == 2
