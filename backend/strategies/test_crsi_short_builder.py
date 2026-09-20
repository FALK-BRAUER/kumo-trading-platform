"""CRSISHORT-006 on cockpit (#858) — registered in SHADOW, submitting nothing.

Every test here was seen RED before the module existed, and the behavioural ones were bitten after
(the bite is named in each docstring). The class being pinned is "a lane that is switched on and
silently absent", and its sibling "a lane that is switched on and silently ARMED":

  * the registry allocates the tag, so `Trader.add_strategy` cannot collide on `order_id_tag`;
  * the builder passes every kwarg the INSTALLED adapter REQUIRES (read from its signature, never
    typed by hand — `test_installed_strategies_accept_what_we_pass` is the same rule);
  * the gateway in SHADOW journals a decision and touches no broker method;
  * the gateway in TRADING REFUSES with a `risk` row while the installed order path cannot carry a
    resting short LIMIT (issue 131) — an operator flipping the row early gets a loud
    refusal, not a market order that #123 measured as losing above 100 bps;
  * split-adjusted bars are a construction-time requirement of the adapter, satisfied on IBKR
    (measured: NVDA 2024-06-07 = 120.89 from ibkr-paper's gateway) and NOT on the raw Alpaca path.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest


from strategies.test_installed_strategies_carry_crsishort import crsishort_installed

pytestmark = pytest.mark.skipif(
    not crsishort_installed(),
    reason="installed kumo-trading-strategies has no CRSISHORT adapter — see "
           "test_installed_strategies_carry_crsishort.py, which is RED for this")

_BACKEND = Path(__file__).resolve().parents[1]


# -- fixtures ---------------------------------------------------------------------------------------
class _NoRows:
    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None


class _Session:
    """No rows, and it RECORDS what the builder writes — the registration row is asserted on it."""

    added: list = []
    commits: int = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        return _NoRows()

    def add(self, row):
        _Session.added.append(row)

    async def commit(self):
        _Session.commits += 1


class _Feed:
    """What the builder reads off the feed strategy: the venue calendar and which provider the
    bars come from. `data_provider` is what decides whether ADJUSTED is true (see #858)."""

    def __init__(self, data_provider: str, shortable_provider=None, daily_bars_cover: str = "rth",
                 price_adjustments: frozenset[str] | None = "by-provider"):
        self._cfg = type("Cfg", (), {"data_provider": data_provider})()
        self._venue_calendar = object()
        self.shortable_provider = shortable_provider
        # The provider's OWN declaration of what its daily bars cover (#616). IBKR declares
        # "extended"; the lab measured on RTH — the builder refuses the mismatch by default.
        self._daily_bars_cover = daily_bars_cover
        # The provider's OWN declaration of which price series it serves (#1124), as the REAL specs
        # declare it (api/providers/ibkr.py / alpaca/data_client.py): IB `split` only, Alpaca both.
        # Pass an explicit set to test the guard against a declaration; None = a double built
        # without the spec (undeclared).
        if price_adjustments == "by-provider":
            price_adjustments = {"ibkr": frozenset({"split"}),
                                 "alpaca": frozenset({"raw", "split"})}.get(data_provider)
        self._price_adjustments = price_adjustments


def _adapter_asks_for(monkeypatch, adjustment: str | None):
    """What the installed adapter's daily request asks for (ks#262 `HISTORY_REQUEST_PARAMS`). The
    venv's kumo-trading-strategies may predate ks#262, so the tests SAY which pin they run as — `None` =
    the pre-#262 adapter that asks for nothing."""
    from strategies import crsi_short

    monkeypatch.setattr(crsi_short, "_requested_adjustment", lambda: adjustment)


def _stub_store_and_calendar(monkeypatch):
    _adapter_asks_for(monkeypatch, "split")          # as the ks#262 adapter; tests override below
    import kumo_strategies.runtime.calendar as _cal
    import kumo_strategies.runtime.executor.store as _store
    import kumo_strategies.runtime.nautilus.broker as _brk

    monkeypatch.setattr(_store, "make_engine", lambda *a, **k: object())

    async def _create_all(_e):
        return None

    monkeypatch.setattr(_store, "create_all", _create_all)
    monkeypatch.setattr(_store, "make_sessionmaker", lambda _e: (lambda: _Session()))
    _Session.added, _Session.commits = [], 0
    # The last name-only seam every builder passes (#622); the real one resolves against a cache
    # that does not exist at build in a test.
    import strategies.momentum as _mom

    monkeypatch.setattr(_mom, "_lane_symbols", lambda syms: list(syms))
    monkeypatch.setattr(_brk, "NautilusBroker",
                        lambda strategy=None, instrument_ids=None: type(
                            "B", (), {"strategy": strategy, "instrument_ids": instrument_ids,
                                      "feed": None})())
    monkeypatch.setattr(_cal, "build_calendar",
                        lambda require_exchange=False, calendar=None: type("C", (), {
                            "require_exchange": require_exchange,
                            "next_fire": staticmethod(lambda after, offset: (None, None))})())


def _settings(monkeypatch, **over):
    base = {"CRSI_ENABLED": True, "CRSI_UNIVERSE": ["IONQ", "RGTI", "SMCI", "MSTR"],
            # A VALUE THE DEFAULT COULD NOT PRODUCE: the schema default is 400.
            "CRSI_MIN_WARM_SYMBOLS": 123, "CRSISHORT-006": 20_000.0,
            # THE DEPLOYED LADDER, because the lane now TRADES by default (#131 landed).
            # kumo-trading-strategies refuses at construction when a trading lane's slots cannot reach the
            # opening cross, and the BUILT-IN `("open+5m",)` cannot — so a builder test running on
            # the built-in would be testing a lane that raises, not the one that deploys. Tests
            # that want the refusal pass `**{"CRSISHORT-006_SLOTS": [...]}` explicitly.
            "CRSISHORT-006_SLOTS": ["open-10m", "open+5m"]}
    base.update(over)
    monkeypatch.setattr("api.settings.resolve", lambda _d: base)
    return base


# -- 1. registry ------------------------------------------------------------------------------------
def test_the_registry_allocates_006_to_CRSISHORT_and_the_tag_is_no_longer_free():
    from api.strategy_registry import REGISTRY, next_free_tag

    entry = next((e for e in REGISTRY if e.name == "CRSISHORT"), None)
    assert entry is not None, "CRSISHORT is not declared — no sleeve, absent from /strategies"
    assert entry.strategy_id == "CRSISHORT-006"
    assert next_free_tag() != "006", "006 handed out twice; add_strategy would refuse the node"


def test_SHORT_PERMITTED_names_exactly_the_short_lane():
    """`ownership.SHORT_PERMITTED` was empty by contract; the one lane that shorts is the ONLY
    entry, so every other lane's short still fires `short_violations`."""
    from api.ownership import SHORT_PERMITTED

    assert SHORT_PERMITTED == frozenset({"CRSISHORT-006"})


def test_decision_slots_mirror_the_adapters_own_default():
    """A hand-typed map drifted once (TECHIVOL-005, a false CRITICAL every 12 h). The map must
    equal what the adapter declares, and the lane-declared branch must resolve it."""
    from kumo_strategies.runtime.nautilus.crsi_short import CrsiShortStrategy

    from strategies.decision_slots import BUILTIN_SLOTS, lane_declared_slots

    declared = inspect.signature(CrsiShortStrategy.__init__).parameters["decision_slots"].default
    assert BUILTIN_SLOTS["CRSISHORT-006"] == tuple(declared)
    assert lane_declared_slots("CRSISHORT-006") == tuple(declared)


# -- 2. the builder ---------------------------------------------------------------------------------
def test_the_gate_off_returns_None_and_touches_no_store(monkeypatch):
    import kumo_strategies.runtime.executor.store as _store

    from strategies import crsi_short

    _settings(monkeypatch, CRSI_ENABLED=False)

    def _boom(*a, **k):
        raise AssertionError("the store was opened with the gate off")

    monkeypatch.setattr(_store, "make_engine", _boom)
    assert crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr")) is None


def test_the_builder_CONSTRUCTS_a_real_strategy_end_to_end_on_IBKR(monkeypatch):
    """The adapter, the config and the gateway are REAL; only network, database and credentials
    are stubbed. Bitten by: dropping `min_warm_symbols=` from the builder (TypeError at build —
    the #377 shape), and by passing `price_adjustment="raw"` (the adapter refuses)."""
    from kumo_strategies.runtime.nautilus.crsi_short import CrsiShortStrategy
    from kumo_strategies.strategies.crsi_short import ADJUSTED

    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)

    strategy = crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))

    assert isinstance(strategy, CrsiShortStrategy)
    assert str(strategy.id) == "CRSISHORT-006", "the wire id is not what cockpit allocated"
    assert isinstance(strategy._runner, crsi_short.CrsiShortSessionGateway), (
        "the adapter has no cockpit gateway — it would decide and log with no journal, no "
        "lifecycle and no idempotency in the path")
    assert strategy._adjustment == ADJUSTED
    assert strategy._min_warm == 123, "min_warm_symbols did not travel from settings"
    assert strategy._cfg.max_borrow_fee_annual is None, "gate-off must reach the config"
    # The lane needs `warmup_sessions` bars per name; the history request must cover it in
    # CALENDAR days with slack, or the lane warms by live accumulation (one bar/name/day).
    assert strategy._history_days is not None and strategy._history_days >= 150


def test_every_kwarg_the_installed_adapter_REQUIRES_is_passed(monkeypatch):
    """Required = no default in the installed signature. Read, not typed, so a new required kwarg
    upstream goes red here before it goes red at node boot."""
    from kumo_strategies.runtime.nautilus.crsi_short import CrsiShortStrategy

    from strategies import crsi_short

    required = {n for n, p in inspect.signature(CrsiShortStrategy.__init__).parameters.items()
                if p.default is inspect._empty and n not in ("self", "cfg", "instrument_ids")}
    passed: dict = {}

    class _Recorder(CrsiShortStrategy):
        def __init__(self, cfg, *a, **kw):
            passed.update(kw)
            super().__init__(cfg, *a, **kw)

    # The builder asks the class's SIGNATURE which optional kwargs it may pass
    # (`_live_reread_kwargs`); a `*a, **kw` recorder hides every optional name and the builder
    # drops them — the double must carry the real signature or it measures a narrower builder.
    _Recorder.__init__.__signature__ = inspect.signature(CrsiShortStrategy.__init__)

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    import kumo_strategies.runtime.nautilus.crsi_short as _up

    monkeypatch.setattr(_up, "CrsiShortStrategy", _Recorder)
    crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))
    assert required <= set(passed), f"builder omits required kwargs: {required - set(passed)}"


def test_FIXTURE_the_feed_double_declares_what_the_real_specs_declare():
    """The double must not be looser than production: the sets it hands the builder are the ones the
    two real specs declare (pinned by source, the same way `test_adjustment_rides_the_request` pins
    them), so a guard that passes here passes on a real node for the same reason."""
    import inspect

    from api.providers import ibkr
    from api.providers.alpaca import http as alpaca_http

    assert _Feed("ibkr")._price_adjustments == frozenset({"split"})
    assert 'price_adjustments=frozenset({"split"})' in inspect.getsource(ibkr.build_data)
    assert _Feed("alpaca")._price_adjustments == alpaca_http.PRICE_ADJUSTMENTS == frozenset({"raw", "split"})


def test_the_builder_reads_the_DECLARATION_not_the_provider_name(monkeypatch):
    """#1124 (2026-09-18). ADJUSTED is a fact about the bars the feed can serve, declared by the
    component that pulls them (`DataClientSpec.price_adjustments`). Alpaca serves `split` per request
    — measured: NVDA 2024-06-07 = 120.89, == IB — so an Alpaca feed that DECLARES it is accepted;
    the old `provider != "ibkr"` check refused it for the provider's name, which is how
    `CRSI_ENABLED: true` on alpaca-paper would have crash-looped paper (#1102, #1123)."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    assert crsi_short.build_crsi_short_strategy(feed=_Feed("alpaca")) is not None
    assert crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr")) is not None


def test_the_builder_REFUSES_a_feed_whose_declaration_LACKS_split(monkeypatch):
    """A venue serving `raw` only cannot carry this lane — refused by the DECLARATION, naming
    #1124 — never a strategy constructed on prices that book a reverse split as a -1000% short."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    with pytest.raises(RuntimeError, match="1124"):
        crsi_short.build_crsi_short_strategy(
            feed=_Feed("alpaca", price_adjustments=frozenset({"raw"})))


def test_the_builder_REFUSES_an_adapter_that_asks_for_NOTHING_the_pre_262_pin(monkeypatch):
    """On a pin without ks#262 the adapter's request carries no adjustment, so on Alpaca it would
    build on RAW bars whatever the door can serve — refused, naming the pin, before the
    declaration is even read (ks review of #1128)."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    _adapter_asks_for(monkeypatch, None)
    with pytest.raises(RuntimeError, match="ks#262"):
        crsi_short.build_crsi_short_strategy(feed=_Feed("alpaca"))
    with pytest.raises(RuntimeError, match="ks#262"):
        crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))


def test_the_guard_reads_the_ADAPTERS_request_not_a_literal(monkeypatch):
    """A double that asks for a series the venue lacks must be refused with BOTH names in the
    message — proves the comparison is requested-vs-declared, not `"split" in declared`."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    _adapter_asks_for(monkeypatch, "raw")
    with pytest.raises(RuntimeError, match="'raw'") as caught:
        crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))      # IB serves split only
    assert "['split']" in str(caught.value)
    assert crsi_short.build_crsi_short_strategy(feed=_Feed("alpaca")) is not None  # Alpaca serves raw


def test_the_builder_REFUSES_an_UNDECLARED_feed_as_undeclared_never_as_raw(monkeypatch):
    """Three states: serves split, does not, never said. A double built without the spec is the
    third, and it is named as such — absence must not read as either real answer."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    with pytest.raises(RuntimeError, match="undeclared"):
        crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr", price_adjustments=None))


def test_the_builder_REFUSES_without_borrow_data_unless_the_gate_is_explicitly_off(monkeypatch):
    """Three states: a provider (#857), an explicit `CRSI_BORROW_GATE_OFF`, or NEITHER — and neither
    is a refusal naming #857, not a config that reads as gated and is inert (#26)."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch)
    with pytest.raises(RuntimeError, match="857"):
        crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))


def test_a_shortable_provider_on_the_feed_keeps_the_fee_gate_ON(monkeypatch):
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch)
    provider = lambda symbols: {s: None for s in symbols}  # noqa: E731
    strategy = crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr", shortable_provider=provider))
    assert strategy._cfg.max_borrow_fee_annual is not None
    assert strategy._borrow_rates is provider


# -- 3. the gateway ---------------------------------------------------------------------------------
class _Journal:
    def __init__(self):
        self.rows: list[tuple[str, str, dict]] = []

    async def write(self, kind, summary, *, session, detail=None, symbol=None,
                    correlation=None, slot=None):
        self.rows.append((kind, summary, {"session": session, "slot": slot,
                                          "detail": detail or {}}))
        return len(self.rows)

    async def decided_this_session(self, session, slot=None):
        return any(k == "decision" and r["session"] == session and r["slot"] == slot
                   for k, _s, r in self.rows)


class _Broker:
    """A broker that REJECTS every order-shaped call. Anything reaching it is the defect."""

    def strategy_positions(self):
        return {"IONQ": -100}

    def __getattr__(self, name):
        raise AssertionError(f"the gateway touched broker.{name} — nothing may be submitted")


def _intent():
    from kumo_strategies.runtime.nautilus.crsi_short import SessionIntent

    return SessionIntent(cover={"IONQ": "reversal"}, cover_kind={"IONQ": "reversal"},
                         cover_px={"IONQ": 41.2}, enter=("RGTI",), limits={"RGTI": 12.36},
                         refused={"QUBT": "no locate"})


def _gateway(state, journal, broker):
    from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State

    from strategies.crsi_short import CrsiShortSessionGateway

    async def _read():
        return Lifecycle(State(state), "test")

    return CrsiShortSessionGateway(journal=journal, broker=broker, read_state=_read,
                                   intent=lambda session, panel: _intent(),
                                   strategy_id="CRSISHORT-006")


def test_SHADOW_journals_the_full_intent_and_submits_NOTHING():
    """Fixture property first: SHADOW decides and may not submit. Then: one `decision` row carrying
    entries, limits, covers and refusals with `position_side: SHORT`, one `state` row, and the
    broker double never touched. Bitten by: calling `broker.submit` in SHADOW (AssertionError)."""
    from kumo_strategies.runtime.executor.lifecycle import State

    assert State.SHADOW.decides and not State.SHADOW.may_submit_entries
    journal, broker = _Journal(), _Broker()
    gw = _gateway("SHADOW", journal, broker)

    result = asyncio.run(gw.run(panel=None, session="2026-09-11", slot="open+5m"))

    assert result.decided is True and result.submitted == 0 and result.state == "SHADOW"
    kinds = [k for k, _s, _r in journal.rows]
    # ONE decision row. The `state` (outcome) row is the ADAPTER's, written through
    # `session_journal()` — which finds it as the PUBLIC attribute `journal` (kumo-trading-platform issue 587).
    assert kinds.count("decision") == 1 and "state" not in kinds, kinds
    assert gw.journal is journal, "the adapter's session_journal() reads `.journal`"
    decision = next(r for k, _s, r in journal.rows if k == "decision")
    assert decision["slot"] == "open+5m"
    d = decision["detail"]
    assert d["position_side"] == "SHORT"
    assert d["enter"] == ["RGTI"] and d["limits"] == {"RGTI": 12.36}
    assert d["cover"] == {"IONQ": "reversal"} and d["refused"] == {"QUBT": "no locate"}


def test_TRADING_is_REFUSED_with_a_risk_row_while_the_limit_path_is_absent():
    """THIS gateway has no submission path (the rotation lanes submit through a runner; this
    lane's runner is the gateway and it builds no order — the LOO/DAY two-order intent is a design
    on issue 131). TRADING therefore refuses LOUDLY — a `risk` row naming #858 and the
    absent path, `blocked` on the result — BEFORE any broker read, and never submits. The broker
    double here RAISES ON EVERY method including `strategy_positions` (codex, #858 review): the
    refusal must not depend on a venue call. Bitten by: falling through to the SHADOW path in
    TRADING (the risk row disappears); reading the book before refusing (AssertionError)."""
    journal = _Journal()

    class _Dead:
        def __getattr__(self, name):
            raise AssertionError(f"TRADING refusal touched broker.{name}")

    gw = _gateway("TRADING", journal, _Dead())

    result = asyncio.run(gw.run(panel=None, session="2026-09-11", slot="open+5m"))

    assert result.submitted == 0 and result.blocked and "858" in result.blocked
    # RESPELT, NOT LOOSENED (issue 131). "no submission path" became FALSE: the gateway
    # has one, and what it refuses on now is the UPSTREAM order path being incomplete — the entry
    # rests in the opening cross (108 of 231 trades, 77.6% of the return) and a day-limit-only lane
    # is a different strategy. The assertion names the REASON, which must survive a rewording.
    assert "ORDER_PATH_COMPLETE" in result.blocked, result.blocked
    risk = [s for k, s, _r in journal.rows if k == "risk"]
    assert risk and "858" in risk[0]
    assert not any(k == "decision" for k, _s, _r in journal.rows), "an acting state journals no SHADOW decision"


def test_LIQUIDATING_is_refused_the_same_way():
    journal = _Journal()

    class _Dead:
        def __getattr__(self, name):
            raise AssertionError(f"refusal touched broker.{name}")

    result = asyncio.run(_gateway("LIQUIDATING", journal, _Dead()).run(panel=None, session="2026-09-11"))
    assert result.submitted == 0 and "858" in (result.blocked or "")


def test_a_broker_read_that_FAILS_in_SHADOW_still_journals_the_intent():
    journal = _Journal()

    class _Flaky:
        def strategy_positions(self):
            raise RuntimeError("venue timeout")

    result = asyncio.run(_gateway("SHADOW", journal, _Flaky()).run(panel=None, session="2026-09-11"))
    kinds = [k for k, _s, _r in journal.rows]
    assert result.decided and "decision" in kinds and "error" in kinds
    decision = next(r for k, _s, r in journal.rows if k == "decision")
    assert decision["detail"]["held"] is None, "an unreadable book is None, never 'held nothing'"


def test_DISABLED_writes_no_decision_and_says_why():
    journal, broker = _Journal(), _Broker()
    gw = _gateway("DISABLED", journal, broker)
    result = asyncio.run(gw.run(panel=None, session="2026-09-11", slot="open+5m"))
    assert result.decided is False and "DISABLED" in (result.blocked or "")
    assert not any(k == "decision" for k, _s, _r in journal.rows)


def test_a_slot_already_decided_is_not_decided_twice():
    """The journal is the idempotency key (#189, #29): a restart mid-session must not journal the
    same slot again."""
    journal, broker = _Journal(), _Broker()
    gw = _gateway("SHADOW", journal, broker)
    asyncio.run(gw.run(panel=None, session="2026-09-11", slot="open+5m"))
    asyncio.run(gw.run(panel=None, session="2026-09-11", slot="open+5m"))
    assert [k for k, _s, _r in journal.rows].count("decision") == 1


def test_run_accepts_jobs_and_slot_like_every_runner():
    """`jobs` AND `slot` are the SessionRunner protocol; a missing kwarg killed two lanes at the
    decision call (#831 shape)."""
    from strategies.crsi_short import CrsiShortSessionGateway

    params = inspect.signature(CrsiShortSessionGateway.run).parameters
    assert "jobs" in params and "slot" in params
    assert params["jobs"].default is None and params["slot"].default is None


# -- 4. boot ------------------------------------------------------------------------------------------
def test_engine_node_registers_the_lane_through_build_optional_strategy():
    """The four-line block, wrapped like QC345 and TECHIVOL so a transport failure costs this lane
    and nothing else, and `feed.register_strategy` so its positions project as managed cycles."""
    src = (_BACKEND / "api" / "engine_node.py").read_text()
    tree = ast.parse(src)
    wrapped = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "build_optional_strategy"
        and node.args and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "CRSISHORT-006"
    ]
    assert wrapped, "CRSISHORT-006 is not registered through build_optional_strategy"
    assert "build_crsi_short_strategy" in src
    assert "feed.register_strategy(crsi.id, crsi)" in src


def test_the_builder_REFUSES_extended_hours_daily_bars_unless_the_deviation_is_STATED(monkeypatch):
    """IBKR declares `daily_bars_cover="extended"` (#616); the lab's panel is RTH. Prior close,
    high/low and volume all move with pre/post-market folded in. Refused by default; a stated
    setting turns it into a logged deviation. Bitten by: gating on the provider name alone."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    with pytest.raises(RuntimeError, match="extended"):
        crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr", daily_bars_cover="extended"))
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True, CRSI_ACCEPT_EXTENDED_DAILY_BARS=True)
    assert crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr", daily_bars_cover="extended")) is not None


def test_the_builder_REFUSES_an_ACCEPTED_deviation_that_is_NOT_occurring(monkeypatch):
    """l21wvpmj, #951 review: once the node serves RTH daily bars (#875) the acceptance setting is
    never read — it changes nothing today, which is exactly why it survives, and the failure comes
    later: a revert or a deploy to an extended node applies the acceptance WITHOUT anyone re-deciding
    it, degraded from "a human stated this deviation on this date" to "a value nobody remembers
    setting". An acceptance of a deviation that is not occurring is a contradiction; refusing it
    means the setting cannot rot. Bitten by: leaving the `cover != "rth"` branch as the only reader."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True, CRSI_ACCEPT_EXTENDED_DAILY_BARS=True)
    with pytest.raises(RuntimeError, match="not occurring"):
        crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr", daily_bars_cover="rth"))
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    assert crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr", daily_bars_cover="rth")) is not None


def test_registration_WRITES_the_SHADOW_row_when_none_exists_and_keeps_an_explicit_one(monkeypatch):
    """"Registered in SHADOW" is a row, not a hope: an absent row reads TRADING (2026-08-19).
    Bitten by: dropping the write (no row added); by overwriting an operator's DISABLED."""
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))
    rows = [r for r in _Session.added if getattr(r, "strategy_id", None) == "CRSISHORT-006"]
    assert len(rows) == 1 and rows[0].state == "SHADOW" and _Session.commits == 1
    assert "858" in rows[0].reason

    # An explicit row wins: nothing is added when one exists.
    class _Row:
        state, reason = "DISABLED", "operator"

    class _HasRow(_NoRows):
        def first(self):
            return _Row()

    class _SessionWithRow(_Session):
        async def execute(self, *a, **k):
            return _HasRow()

    import kumo_strategies.runtime.executor.store as _store

    monkeypatch.setattr(_store, "make_sessionmaker", lambda _e: (lambda: _SessionWithRow()))
    _Session.added, _Session.commits = [], 0
    crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))
    assert _Session.added == [] and _Session.commits == 0


def test_an_UNWRITABLE_lifecycle_table_still_registers_the_lane_and_says_so(monkeypatch, caplog):
    import logging

    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)

    class _Broken(_Session):
        def add(self, row):
            raise RuntimeError("lifecycle table read-only")

    import kumo_strategies.runtime.executor.store as _store

    monkeypatch.setattr(_store, "make_sessionmaker", lambda _e: (lambda: _Broken()))
    with caplog.at_level(logging.ERROR):
        assert crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr")) is not None
    assert any("registration lifecycle row" in r.getMessage() for r in caplog.records)


def test_the_builder_DECLARES_shadow_only_where_the_adapter_asks(monkeypatch):
    """`shadow_only` IS A KNOB NOW, NOT A CONSTANT — and it is tested with a value the default
    could not produce.

    It used to be hardcoded True, because `CrsiShortStrategy` raised at construction unless the
    order path was built. #131 landed that path, so the default is now False and this lane TRADES;
    a shadow deployment stays reachable through `CRSI_SHADOW_ONLY` WITHOUT a rebuild, which is
    platform issue 853's first phase and the safest way to validate a lane against a venue.

    The flag was also DEAD upstream until 2425465 — assigned at `crsi_short.py:376`, read nowhere,
    gating only a build-time raise (platform issue 1027). Asserting it is PASSED is therefore not enough
    on its own; what makes this test mean something is that both values travel.

    On a revision without the kwarg it is dropped, not passed."""
    from kumo_strategies.runtime.nautilus.crsi_short import CrsiShortStrategy

    from strategies import crsi_short

    passed: dict = {}

    class _Recorder(CrsiShortStrategy):
        def __init__(self, cfg, *a, **kw):
            passed.update(kw)
            super().__init__(cfg, *a, **kw)

    # The builder asks the class's SIGNATURE which optional kwargs it may pass
    # (`_live_reread_kwargs`); a `*a, **kw` recorder hides every optional name and the builder
    # drops them — the double must carry the real signature or it measures a narrower builder.
    _Recorder.__init__.__signature__ = inspect.signature(CrsiShortStrategy.__init__)

    import kumo_strategies.runtime.nautilus.crsi_short as _up

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    monkeypatch.setattr(_up, "CrsiShortStrategy", _Recorder)
    crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))
    accepts = "shadow_only" in inspect.signature(CrsiShortStrategy.__init__).parameters
    assert passed.get("shadow_only") is (False if accepts else None), (
        "the default deployment TRADES: shadow_only must be False where the adapter asks for it, "
        "and absent where it does not exist")

    if not accepts:
        return
    # THE OTHER VALUE MUST TRAVEL, or this proves only that a literal reached the constructor.
    passed.clear()
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True, CRSI_SHADOW_ONLY=True)
    crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))
    assert passed.get("shadow_only") is True, (
        "CRSI_SHADOW_ONLY did not reach the adapter — the knob is wired to nothing, which is the "
        "exact defect platform issue 1027 reported one level up")


def test_a_ks_CONSTRUCTOR_refusal_is_a_RuntimeError_at_the_builder_so_it_costs_the_lane_not_the_node(monkeypatch):
    """#1123 fold (ks worker's #1125 review). The installed adapter refuses at CONSTRUCTION with
    **ValueError** — `CrsiShortStrategy.__init__` :424 (adjustment), :432 (min_warm), :441 (borrow
    ceiling w/o provider), :453 (no slot reaches the auction with shadow_only=False) — and the
    builder called it unwrapped. Under `build_optional_strategy`'s rule (RuntimeError = refusal,
    everything else = bug) that ValueError would still crash-loop the node. The :453 case is what a
    mistyped slot list on paper hits: the built-in `("open+5m",)` is after the cross.

    Driven through the REAL builder and the REAL constructor (only store/calendar/settings stubbed),
    then through `build_optional_strategy` — the seam, not a double that raises what production
    does not."""
    from api import engine_node
    from strategies import crsi_short

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch, CRSI_BORROW_GATE_OFF=True, **{"CRSISHORT-006_SLOTS": ["open+5m"]})

    # FIXTURE PROPERTY: the constructor really does refuse this ladder, and with ValueError.
    with pytest.raises(RuntimeError, match="open\\+5m|auction|cross") as caught:
        crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr"))
    assert isinstance(caught.value.__cause__, ValueError), (
        "the builder must re-raise the adapter's ValueError as a RuntimeError FROM it — the cause "
        "is how the operator learns which line refused")

    engine_node.clear_skipped_builds()
    out = engine_node.build_optional_strategy(
        "CRSISHORT-006", lambda: crsi_short.build_crsi_short_strategy(feed=_Feed("ibkr")))
    assert out is None
    assert engine_node.skipped_builds()["CRSISHORT-006"].startswith("refused: RuntimeError: ")
