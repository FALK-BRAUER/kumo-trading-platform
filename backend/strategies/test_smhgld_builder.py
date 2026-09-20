"""SMHGLD-007 registers in the node behind `SMHGLD_ENABLED` (#965, cockpit half of issue 177).

#953 delivered the executing gateway and left the lane "registered nowhere … the correct state until"
this: a builder that constructs the upstream `SmhGldSleeveStrategy` (kumo-trading-strategies ≥ 06f3055)
against the cockpit gateway, a settings gate that defaults OFF (no env gate, CRSISHORT's policy), a
registry tag, and the registration in `build_node` through `build_optional_strategy` — so a network
failure at boot cannot take the node down with it (#377), while a misconfiguration RAISES by name.

Every test here was seen red before the builder existed. The double set is the CRSISHORT builder's
(`_stub_store_and_calendar`, `_settings`, `_Feed`): only network, database and credentials are stubbed;
the adapter, the config, the gateway and the capability guard are real.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from strategies.test_crsi_short_builder import _Feed, _settings, _stub_store_and_calendar

_BACKEND = Path(__file__).resolve().parents[1]
SID = "SMHGLD-007"


def smhgld_installed() -> bool:
    """Whether the installed kumo-trading-strategies carries the SMHGLD sleeve adapter (#177, 06f3055)."""
    try:
        import kumo_strategies.runtime.nautilus.smhgld_sleeve  # noqa: F401
    except ImportError:
        return False
    return True


def _upstream_or_skip():
    try:
        from kumo_strategies.runtime.nautilus.smhgld_sleeve import SmhGldSleeveStrategy
    except ImportError:
        pytest.skip("installed kumo-trading-strategies predates the SMHGLD sleeve adapter (issue 177, 06f3055)")
    return SmhGldSleeveStrategy


# -- declaration: registry, settings schema, registration site -------------------------------------------

def test_the_registry_allocates_007_to_SMHGLD_and_the_tag_is_no_longer_free():
    from api.strategy_registry import REGISTRY, next_free_tag
    entry = next((e for e in REGISTRY if e.name == "SMHGLD"), None)
    assert entry is not None, "SMHGLD is not declared — no sleeve, absent from /strategies"
    assert entry.strategy_id == SID
    assert next_free_tag() != "007", "007 handed out twice; add_strategy would refuse the node"


def test_the_settings_schema_declares_the_gate_OFF_by_default_and_the_two_legs():
    schema = json.loads((_BACKEND / "config" / "settings" / "strategies.schema.json").read_text())
    props = schema["properties"]
    assert props["SMHGLD_ENABLED"]["type"] == "boolean" and props["SMHGLD_ENABLED"]["default"] is False, \
        "the gate must exist and default OFF — a lane nobody switched on must not register"
    assert "SMHGLD-007" in props["SMHGLD_ENABLED"]["description"]
    assert props["SMHGLD_SYMBOLS"]["default"] == ["SMH", "GLD"], "the two legs, risk then defensive"


def test_build_node_registers_SMHGLD_through_build_optional_strategy_only():
    """The same seam as QC345/TECHIVOL/CRSISHORT: transport failure at boot logs and skips the lane,
    misconfiguration raises. A direct `SmhGldSleeveStrategy(...)` in build_node would be the #377 shape."""
    import ast
    from api import engine_node
    tree = ast.parse(inspect.getsource(engine_node.build_node))
    # ON THE AST, not a substring: a commented-out registration satisfied the first draft of this test
    # (bitten) — the same evasion the repo already records for the #511 scan.
    builds = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "build_optional_strategy" and c.args
              and isinstance(c.args[0], ast.Constant) and c.args[0].value == SID]
    assert builds, "SMHGLD-007 is not registered in build_node through build_optional_strategy"
    lam = builds[0].args[1]
    assert isinstance(lam, ast.Lambda) and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                                                and c.func.id == "build_smhgld_strategy" for c in ast.walk(lam))
    # and the RESULT is registered on the feed, guarded by `is not None` (transport failure skips it)
    bound = next((n.targets[0].id for n in ast.walk(tree) if isinstance(n, ast.Assign) and n.value is builds[0]
                  and isinstance(n.targets[0], ast.Name)), None)
    assert bound, "the built strategy is not bound to a name"
    regs = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "register_strategy" and c.args and isinstance(c.args[1], ast.Name) and c.args[1].id == bound]
    assert regs, f"{bound} is built but never passed to feed.register_strategy"
    assert not any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "SmhGldSleeveStrategy"
                   for c in ast.walk(tree)), "a direct construction in build_node is the #377 shape"


# -- the builder ---------------------------------------------------------------------------------------------

def test_the_gate_off_returns_None_and_touches_no_store(monkeypatch):
    import kumo_strategies.runtime.executor.store as _store
    from strategies import smhgld
    _settings(monkeypatch, SMHGLD_ENABLED=False)

    def _boom(*a, **k):
        raise AssertionError("the store was opened with the gate off")
    monkeypatch.setattr(_store, "make_engine", _boom)
    assert smhgld.build_smhgld_strategy(feed=_Feed("alpaca")) is None


def test_the_builder_CONSTRUCTS_a_real_strategy_end_to_end(monkeypatch):
    """Adapter, config, gateway and capability guard are REAL. The guard is the point: the upstream
    adapter refuses at construction a runner that does not offer EXECUTES_DELTAS by identity, so a
    strategy that comes back constructed is one whose runner can execute its deltas."""
    SmhGldSleeveStrategy = _upstream_or_skip()
    from kumo_strategies.runtime.nautilus.capabilities import EXECUTES_DELTAS, offers
    from strategies import smhgld
    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, SMHGLD_ENABLED=True)
    strategy = smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))
    assert isinstance(strategy, SmhGldSleeveStrategy)
    assert str(strategy.id) == SID, "the wire id is not what cockpit allocated"
    assert isinstance(strategy._runner, smhgld.SmhgldSessionGateway), "no cockpit gateway — the lane would decide with nothing executing"
    assert offers(strategy._runner, EXECUTES_DELTAS)
    assert tuple(strategy._runner.symbols) == ("SMH", "GLD")


def test_every_kwarg_the_installed_adapter_REQUIRES_is_passed(monkeypatch):
    """Required = no default in the installed signature. Read, not typed, so a new required kwarg
    upstream goes red here before it goes red at node boot."""
    SmhGldSleeveStrategy = _upstream_or_skip()
    from strategies import smhgld
    required = {n for n, p in inspect.signature(SmhGldSleeveStrategy.__init__).parameters.items()
                if p.default is inspect._empty and n not in ("self", "cfg", "instrument_ids")}
    assert required, "fixture: the adapter declares at least one required kwarg (order_id_tag)"
    passed: dict = {}

    class _Recorder(SmhGldSleeveStrategy):
        def __init__(self, cfg, *a, **kw):
            passed.update(kw)
            super().__init__(cfg, *a, **kw)
    _Recorder.__init__.__signature__ = inspect.signature(SmhGldSleeveStrategy.__init__)
    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, SMHGLD_ENABLED=True)
    import kumo_strategies.runtime.nautilus.smhgld_sleeve as _up
    monkeypatch.setattr(_up, "SmhGldSleeveStrategy", _Recorder)
    smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))
    assert required <= set(passed), f"builder omits required kwargs: {required - set(passed)}"
    assert passed.get("session_runner") is not None, "the runner is the safety model — never omitted"


def test_the_builder_REFUSES_by_name_when_the_installed_strategies_lack_the_sleeve(monkeypatch):
    """A gate that is ON with nothing to register must RAISE naming the gate, never return None —
    a lane switched on and silently absent is the worst outcome available."""
    import builtins
    from strategies import smhgld
    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, SMHGLD_ENABLED=True)
    real_import = builtins.__import__

    def _no_upstream(name, *a, **k):
        if name.startswith("kumo_strategies.runtime.nautilus.smhgld_sleeve"):
            raise ImportError("no module named smhgld_sleeve (pin predates issue 177)")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _no_upstream)
    with pytest.raises(RuntimeError, match="SMHGLD_ENABLED"):
        smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))


def test_the_production_plan_wraps_the_upstream_decide_and_order_plan():
    """The gateway's `plan` callable is what turns a feature panel into orders. In production it must
    be the upstream `decide` + `order_plan`, not a re-derivation: driven on a two-leg panel where the
    lane holds nothing and the equity is 100k, the plan opens BOTH legs (the all-buy first session)."""
    _upstream_or_skip()
    import pandas as pd
    from kumo_strategies.strategies.smhgld_sleeve.config import SmhGldSleeveConfig
    from kumo_strategies.strategies.smhgld_sleeve.engine import Order, build_feature_panel
    from strategies import smhgld
    cfg = SmhGldSleeveConfig()
    days = pd.bdate_range("2026-01-02", periods=320)
    rows = []
    for sym, px in (("SMH", 250.0), ("GLD", 300.0)):
        for i, d in enumerate(days):
            p = px * (1 + 0.0004 * i)
            rows.append({"ticker": sym, "date": d, "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1_000_000})
    panel = build_feature_panel(pd.DataFrame(rows), cfg)
    day = panel[panel["date"] == panel["date"].max()]
    assert set(day["ticker"]) == {"SMH", "GLD"} and bool(day["eligible"].all()), "fixture: both legs eligible on the last day"
    plan = smhgld.production_plan(cfg)
    view, orders = plan(day, "2026-09-11", held={}, prices={"SMH": day[day.ticker == "SMH"].close.iloc[0], "GLD": day[day.ticker == "GLD"].close.iloc[0]}, equity=100_000.0, lot=1)
    assert set(view) == {"regime", "weights"} and view["regime"] == "opening"
    assert all(isinstance(o, Order) for o in orders) and {o.symbol for o in orders} == {"SMH", "GLD"}
    assert all(o.delta > 0 and o.held_qty == 0 for o in orders), "the all-buy opening session"


def test_the_builder_REFUSES_a_leg_count_other_than_two_naming_the_setting(monkeypatch):
    _upstream_or_skip()
    from strategies import smhgld
    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, SMHGLD_ENABLED=True, SMHGLD_SYMBOLS=["SMH", "GLD", "SLV"])
    with pytest.raises(RuntimeError, match="SMHGLD_SYMBOLS"):
        smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))
    _settings(monkeypatch, SMHGLD_ENABLED=True, SMHGLD_SYMBOLS=["GLD", "SMH"])   # right count, wrong order
    with pytest.raises(RuntimeError, match="SMHGLD_SYMBOLS"):
        smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))


def test_the_REAL_gateway_run_drives_the_production_plan_to_two_opening_orders():
    """THE SEAM, not the unit: `SmhgldSessionGateway.run()` calls `plan(panel, session, held=<book dict>,
    prices=…, equity=…, lot=…)` — no `shares` kwarg. A plan whose signature differs raises inside the
    gateway's try, journals an error row and refuses the session: a lane that decides and executes
    nothing, with a green unit test on the plan. Drive the real gateway with the real plan on the
    executor tests' production-shaped doubles (broker refuses what the venue refuses)."""
    _upstream_or_skip()
    import asyncio
    import pandas as pd
    from kumo_strategies.strategies.smhgld_sleeve.config import SmhGldSleeveConfig
    from kumo_strategies.strategies.smhgld_sleeve.engine import build_feature_panel
    from strategies import smhgld
    from strategies.test_smhgld_delta_execution import _Broker, _Journal, _gateway
    cfg = SmhGldSleeveConfig()
    days = pd.bdate_range("2026-01-02", periods=320)
    rows = [{"ticker": sym, "date": d, "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1_000_000}
            for sym, px in (("SMH", 565.49), ("GLD", 398.5)) for i, d in enumerate(days) for p in (px * (1 + 0.0004 * i),)]
    panel = build_feature_panel(pd.DataFrame(rows), cfg)
    day = panel[panel["date"] == panel["date"].max()]
    last = {s: float(day[day.ticker == s].close.iloc[0]) for s in ("SMH", "GLD")}
    journal = _Journal()
    broker = _Broker(equity=100_000.0, held={}, prices=last, buys_need_sells_first=False)
    gw = _gateway("TRADING", journal, broker, smhgld.production_plan(cfg))
    asyncio.run(gw.run(panel=day, session="2026-09-11"))
    kinds = [r[0] for r in journal.rows] if hasattr(journal, "rows") else []
    assert "error" not in kinds, f"the gateway refused the session: {[r for r in journal.rows if r[0] == 'error'][:2]}"
    assert sorted(o.symbol for o in broker.submitted) == ["GLD", "SMH"], f"submitted {[(o.symbol, o.quantity) for o in broker.submitted]}"


def test_deployed_is_marked_to_the_CURRENT_price_not_the_entry(monkeypatch):
    """l21, #985 review: `deployed` at ENTRY reads a leg up 40% as under-deployed, and the budget lets
    the lane buy more of it. Every other budget reader marks to the current price. Drive the builder's
    real `_read_budget` with a claim of 10 SMH entered at 400 while the broker's last price is 560:
    deployed must be 5,600, not 4,000."""
    _upstream_or_skip()
    import asyncio
    from strategies import smhgld
    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, SMHGLD_ENABLED=True)

    class _Row:
        strategy_id, symbol, qty, entry = "SMHGLD-007", "SMH", 10, 400.0

    class _Rows:
        def scalars(self): return self
        def all(self): return [_Row()]
        def first(self): return _Row()

    import strategies.test_crsi_short_builder as _t
    orig = _t._Session.execute

    async def _execute(self, stmt):
        return _Rows() if "position_state" in str(stmt).lower() or "PositionState" in str(stmt) else await orig(self, stmt)
    monkeypatch.setattr(_t._Session, "execute", _execute)
    from api.budget import Book, Sleeve
    monkeypatch.setattr("api.budget_store.load_book", lambda s: _book(Book, Sleeve))
    strategy = smhgld.build_smhgld_strategy(feed=_Feed("alpaca"))
    gw = strategy._runner
    gw._broker.last_price = lambda sym: {"SMH": 560.0, "GLD": 398.5}[sym]   # the gateway's own price seam
    sleeve, deployed = asyncio.run(gw.read_budget())
    assert sleeve is not None and deployed == 5_600.0, f"deployed={deployed} (entry-priced would be 4,000)"


async def _book(Book, Sleeve):
    return Book({"SMHGLD-007": Sleeve("SMHGLD-007", 20_000.0, 20_000.0)})


def test_a_MISSING_price_is_refused_BY_NAME_not_as_a_plan_failure(monkeypatch):
    """l21, #985 review: the `price_missing` block sat AFTER the plan call, but `order_plan` does
    `prices[symbol]` with no guard — so a missing key raises KeyError and a None value raises
    TypeError, both INSIDE the broad `except` that codes them `plan_failed`. The block was
    unreachable and the operator got `plan failed: KeyError: 'GLD'` instead of the sentence written
    for exactly this case. The fix is a MOVE: `missing` needs only `prices` and `self.symbols`, both
    live before the plan is called."""
    import asyncio
    from strategies.test_smhgld_delta_execution import _Broker, _Journal, _gateway
    from strategies.smhgld import production_plan
    _upstream_or_skip()
    from kumo_strategies.strategies.smhgld_sleeve.config import SmhGldSleeveConfig

    for label, prices in (("key absent", {"SMH": 565.49}), ("value None", {"SMH": 565.49, "GLD": None})):
        journal = _Journal()
        broker = _Broker(equity=100_000.0, held={}, prices={"SMH": 565.49, "GLD": 398.5},
                         buys_need_sells_first=False, price_missing=("GLD",) if label == "value None" else ())
        if label == "key absent":
            broker.last_price = lambda sym: prices.get(sym)
        gw = _gateway("TRADING", journal, broker, production_plan(SmhGldSleeveConfig()))
        asyncio.run(gw.run(panel=None, session="2026-09-11", slot="close-20m"))
        codes = [(r[-1] if isinstance(r[-1], dict) else {}).get("detail", {}).get("code") for r in journal.rows]
        assert "price_missing" in codes, f"{label}: got {codes} — the refusal is still the plan's exception"
        assert "plan_failed" not in codes, f"{label}: coded as a plan failure, not a missing price"
        assert broker.submitted == [], label
