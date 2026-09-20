"""TECHIVOL-005 registers ONLY through the safe path (issue 33, #63).

Same hazard as QC345, same shape. `QC27RotationStrategy` has a `session_runner` parameter defaulting
to None, and `_try_decide` branches on it:

    if self._runner is not None:  self._run_session(...)   # lifecycle, journal, idempotency, budget
    else:                         self._decide_for(...)    # submits live orders, right there

Both branches register cleanly, log that they are watching, and look healthy. They differ only on the
next rebalance — which for QC27 is EVERY SESSION, not monthly — when one of them places orders nobody
authorised.

WHAT IS AND IS NOT TESTED HERE. The gateway's own behaviour (lifecycle gating, journal-before-broker,
the last look, exits before entries, sizing off the allocation) is tested and mutation-bitten in
kumo-trading-strategies' `tests/runtime/executor/test_qc27_runner.py`, which is the repo that defines those
controls. Duplicating it here would be a second set of assertions about one contract. This file tests
the WIRING: that cockpit supplies what the runner refuses to guess, and that no path can construct
the adapter without it.

Run with: PYTHONPATH=~/projects/kumo-trading-strategies/src:.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from strategies import qc27

_BACKEND = Path(__file__).resolve().parents[1]


def _code_of(fn) -> str:
    """A function's executable body — docstrings and comments removed, via `ast.unparse`.

    Every check below asks what the code DOES. This module deliberately explains the unsafe
    alternatives in prose so the next reader is warned, which means a check over raw source would be
    SATISFIED by an explanation and BROKEN by rewording one — it would be measuring the comments.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _schema() -> dict:
    d = json.loads((_BACKEND / "config" / "settings" / "strategies.schema.json").read_text())
    return d.get("properties", d)


# -- the gate --------------------------------------------------------------------------------------
def test_qc27_is_OFF_unless_the_gate_is_explicitly_set(monkeypatch):
    monkeypatch.setattr("api.settings.resolve", lambda _d: {})
    assert qc27.build_qc27_strategy() is None


def test_the_gate_comes_from_SETTINGS_not_from_the_ENVIRONMENT():
    """Whether a strategy is registered is an OPERATOR decision; an env var makes it a deployment.
    No env fallback, deliberately — a flag readable from two places is two derivations, and the one
    that loses is always the one somebody edited."""
    code = _code_of(qc27._enabled)
    assert "settings" in code
    assert "environ" not in code and "getenv" not in code


def test_the_gate_DEFAULTS_OFF_in_the_schema():
    assert _schema()["QC27_ENABLED"]["default"] is False


# -- the universe ----------------------------------------------------------------------------------
def test_enabling_WITHOUT_a_universe_fails_LOUDLY(monkeypatch):
    """A strategy switched on and silently absent is the worst outcome available: the operator sees
    the flag set, the node boots clean, and nothing trades or says why. QC27 ranks cross-sectionally,
    so an empty pool is not a quiet no-op."""
    monkeypatch.setattr("api.settings.resolve", lambda _d: {"QC27_ENABLED": True, "QC27_UNIVERSE": []})
    with pytest.raises(RuntimeError, match="QC27_UNIVERSE"):
        qc27.build_qc27_strategy()


def test_the_universe_setting_EXISTS_in_the_schema_an_operator_edits():
    s = _schema()["QC27_UNIVERSE"]
    assert s["type"] == "array"
    assert len(s["default"]) > 50, "a token default is not a usable tech pool"


def test_the_universe_default_contains_the_cash_proxy():
    """QC27 sweeps its shortfall into the cash proxy. A pool without it silently disables that leg —
    and the strategy would hold less than its target with no error."""
    from kumo_strategies.strategies.qc27_tech_inverse_vol import live_config
    assert live_config().cash_proxy_symbol in _schema()["QC27_UNIVERSE"]["default"]


def test_the_universe_is_normalised_not_trusted_raw(monkeypatch):
    monkeypatch.setattr("api.settings.resolve",
                        lambda _d: {"QC27_UNIVERSE": [" aapl ", "MSFT", "", "aapl"]})
    assert qc27._universe_symbols() == ["AAPL", "MSFT"]


# -- the runner is ALWAYS attached ------------------------------------------------------------------
def test_NO_construction_of_the_adapter_anywhere_omits_the_session_runner():
    """Aimed at the CLASS of mistake, not one instance. Any cockpit source constructing
    `QC27RotationStrategy` must pass `session_runner`, or it decides and submits with no controls."""
    offenders = []
    # OUR SOURCE ONLY. `_BACKEND.rglob` also walks `.venv/`, where kumo-trading-strategies' own
    # `backtesting/runner_qc27_nautilus.py` constructs the adapter WITHOUT a session_runner — correctly,
    # because a backtest harness has no live runner to give it. That is not a cockpit wiring defect and
    # this guarantee was never about it. It passed wherever the venv sits outside the tree and failed
    # the moment it sat inside, which makes the result depend on someone's environment layout rather
    # than on the code under test.
    #
    # Same shape as the strict-xfail hazard: a scan flipped by code that has nothing to do with the gap.
    for path in _BACKEND.rglob("*.py"):
        if any(part in {".venv", "site-packages", "__pycache__", "node_modules"} for part in path.parts):
            continue
        src = path.read_text()
        if "QC27RotationStrategy(" not in src:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "QC27RotationStrategy":
                if not any(k.arg == "session_runner" for k in node.keywords):
                    offenders.append(f"{path.relative_to(_BACKEND)}:{node.lineno}")
    assert not offenders, f"adapter constructed without a session_runner at {offenders}"


def test_the_builder_passes_the_COCKPIT_runner_not_some_other_one():
    """The adapter must hold COCKPIT's runner. Widened 2026-08-24 from a literal
    `QC27SessionRunner(` to the subclass relationship, because the builder now constructs
    `_AllocationAwareRunner` — a SUBCLASS of it, which re-reads the allocation before delegating.

    Asserting the base class rather than a name keeps the original question answerable: it is still
    "cockpit's runner, with every control in it", and a genuinely foreign runner still fails.
    """

    code = _code_of(qc27.build_qc27_strategy)
    assert "session_runner=runner" in code
    assert "QC27SessionRunner" in code, "the builder no longer references cockpit's runner at all"
    tree = ast.parse(textwrap.dedent(inspect.getsource(qc27.build_qc27_strategy)))
    subclasses = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                  and any(getattr(b, "id", None) == "QC27SessionRunner" for b in n.bases)]
    assert subclasses, "the runner the adapter gets does not derive from QC27SessionRunner"
    assert any(f"{c.name}(" in code for c in subclasses), (
        "a subclass is declared and something else is constructed")


def test_the_node_registers_qc27_ONLY_through_the_builder():
    """Registration may not CONSTRUCT the adapter directly — that path silently skips every control.

    Asserted over the AST, not the text. `engine_node.py` deliberately NAMES `QC27RotationStrategy`
    in a comment explaining why constructing it there would be wrong, so a substring check is
    satisfied by deleting the warning and broken by writing one. The first version of this test did
    exactly that and failed on its own comment."""
    src = (_BACKEND / "api" / "engine_node.py").read_text()
    assert "build_qc27_strategy" in src, "the node never registers TECHIVOL-005"
    calls = [n.lineno for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "QC27RotationStrategy"]
    assert not calls, f"engine_node constructs the adapter directly at lines {calls}"


# -- what the runner refuses to guess, and cockpit must supply -------------------------------------
def test_the_lifecycle_is_read_FRESH_every_session():
    """The runner holds a `Lifecycle` captured at CONSTRUCTION — once, at node boot. Without
    `read_state` the operator's state freezes for the process lifetime, so a HALT written on Tuesday
    would not be seen on Wednesday."""
    assert "read_state=_read_state" in _code_of(qc27.build_qc27_strategy)


def test_the_constructed_fallback_lifecycle_is_the_SAFE_one():
    """If `read_state` were ever dropped, the fallback must not read as "trades by default"."""
    from kumo_strategies.runtime.executor.lifecycle import State
    assert qc27._disabled_lifecycle().state is State.DISABLED


def test_an_ABSENT_lifecycle_row_is_TRADING():
    """2026-08-19, and deliberately NOT re-derived here — it mirrors
    `QC345SessionGateway._lifecycle`. Pinned because it means `QC27_ENABLED` is the LAST gate, not
    the first of two: a brand-new strategy trades on its first session."""
    code = _code_of(qc27.build_qc27_strategy)
    assert "State.TRADING" in code and "no lifecycle row" in code


def test_sizing_uses_THIS_STRATEGYS_allocation_not_the_account():
    """A 20k strategy on a 100k account that sizes off the account builds two oversized positions
    where the research measured ten — the budget gate then refuses, so it reads as "buys the same and
    gets rejected".

    REWRITTEN 2026-08-24. This asserted `qc27.ALLOCATED_EQUITY == 20_000.0` and that the builder
    passed that constant. Both were true, both stayed true, and the lane's settings knob did nothing
    for its entire life — because a CONSTANT cannot be what an operator configured, it can only agree
    with it, and this one did agree. The test measured the wrong fact and its passing meant nothing.

    So it now asks the question the old one skipped: does the number come FROM SETTINGS. Not the
    account, AND not a literal.
    """
    code = _code_of(qc27.build_qc27_strategy)
    assert "_allocated_equity()" in code, "the builder does not resolve the allocation at all"
    assert "RiskLimits(allocated_equity=ALLOCATED_EQUITY)" not in code, (
        "the hardcoded constant is back in the builder — a dead settings knob, again")
    assert "resolve" in _code_of(qc27._allocated_equity), (
        "the allocation is not read from the settings store an operator edits")


def test_the_budget_gate_is_COCKPITS_and_not_a_second_rule():
    """Two derivations of one limit disagree, and the disagreement looks like a strategy quietly
    holding more than it was granted."""
    code = _code_of(qc27._budget_allows) if hasattr(qc27, "_budget_allows") \
        else _code_of(qc27.build_qc27_strategy)
    assert "may_submit" in code
    assert "budget_gate=" in _code_of(qc27.build_qc27_strategy)


def test_the_budget_precheck_failing_does_not_stop_trading():
    """The exec client enforces the same predicate at submission. A pre-check that cannot run must
    not become a reason to stop trading."""
    assert "deferring to the exec client" in inspect.getsource(qc27.build_qc27_strategy)


# -- identity and calendar --------------------------------------------------------------------------
def test_qc27_carries_COCKPITS_identity_and_tag():
    from api.strategy_ids import TECHIVOL, TECHIVOL_TAG
    assert qc27.STRATEGY_ID == str(TECHIVOL) == "TECHIVOL-005"
    assert qc27.ORDER_ID_TAG == TECHIVOL_TAG == "005"


def test_the_tag_is_not_one_already_allocated():
    """A duplicate does not degrade — Nautilus raises at `Trader.add_strategy` and the node does not
    boot, taking MANUAL, MOMENTUM, QC345 and BCTROT down with it."""
    assert qc27.ORDER_ID_TAG not in {"001", "002", "003", "004"}


def test_the_tag_is_passed_EXPLICITLY_and_not_left_to_a_default():
    """The adapter has NO default tag and raises without one, precisely so cockpit allocates it."""
    assert "order_id_tag=ORDER_ID_TAG" in _code_of(qc27.build_qc27_strategy)


def test_qc27_claims_NO_instruments():
    """`external_order_claims` are EXCLUSIVE node-wide and raise InvalidConfiguration at
    `Trader.add_strategy`. QC27's tech pool overlaps MOMENTUM's and BCTROT's by construction, so any
    claim stops the node booting."""
    assert "external_order_claims=None" in _code_of(qc27.build_qc27_strategy)


def test_the_calendar_REFUSES_the_holiday_unaware_fallback():
    """`build_calendar()` defaults to `require_exchange=False`, which silently returns a weekday
    calendar when the exchange calendar is unavailable — and a weekday calendar trades on holidays.
    Any path that can place orders must refuse it."""
    code = _code_of(qc27.build_qc27_strategy)
    # The PROPERTY, not the literal one-line call — #628 added `calendar=` and reformatted it. What
    # must stay true is that the holiday-unaware fallback is still REFUSED on a path that places
    # orders; an injected venue calendar satisfies that requirement rather than relaxing it.
    assert "require_exchange=True" in code, (
        "qc27 no longer refuses the holiday-unaware calendar — it would schedule sessions on market "
        "holidays and the orders would bounce"
    )
    assert "require_exchange=False" not in code


# -- the config is the researched one ----------------------------------------------------------------
#: Config fields whose values were MEASURED and whose reasoning lives in kumo-trading-strategies'
#: `live_config()`. A literal for any of them in cockpit is a second source for a researched number.
_MEASURED_FIELDS = {"momentum_price_field", "rebalance_period", "portfolio_size",
                    "lookback_sessions", "realized_vol_window", "stop_loss_portfolio_frac"}


def test_the_live_config_is_IMPORTED_not_respelled_in_cockpit():
    """Duplicating the measured departures here is how a runtime value drifts from the verified
    backtest — the change gets made in one repo and the other keeps trading the old number.

    Scanned over the WHOLE MODULE's AST, not one function. The first version checked only
    `_live_config`, and a mutation that respelled `portfolio_size=10` inside `build_qc27_strategy`
    survived it — the guard was real but aimed at one of several places the mistake fits.

    Keyword arguments only, so `_live_config`'s legitimate mention of these names as OVERRIDE KEYS
    (strings in a tuple) is untouched; what is banned is binding one to a literal value.
    """
    tree = ast.parse(Path(qc27.__file__).read_text())
    offenders = [f"{kw.arg}={kw.value.value!r} (line {kw.value.lineno})"
                 for node in ast.walk(tree) if isinstance(node, ast.Call)
                 for kw in node.keywords
                 if kw.arg in _MEASURED_FIELDS and isinstance(kw.value, ast.Constant)]
    assert not offenders, f"cockpit respells measured config instead of importing it: {offenders}"
    assert "live_config" in _code_of(qc27._live_config), "the promoted config is never imported"


def test_the_decision_slot_is_DERIVED_from_the_strategys_own_offset():
    """A literal here is a second derivation of a fact kumo-trading-strategies owns — and the two drifting is
    exactly the bug that had the slot reading open+5m while the strategy filled at 12:00."""
    from kumo_strategies.strategies.qc27_tech_inverse_vol import OPEN_OFFSET_MINUTES
    assert qc27._decision_slot() == f"open+{OPEN_OFFSET_MINUTES}m"
    assert "open+150m" not in _code_of(qc27._decision_slot)


# -- nothing else moved --------------------------------------------------------------------------
def test_existing_strategy_registration_is_UNCHANGED():
    src = (_BACKEND / "api" / "engine_node.py").read_text()
    for name in ("build_qc345_strategy", "MOMENTUM", "BCTROT"):
        assert name in src, f"{name} registration disturbed"


def test_the_other_tags_are_untouched():
    from api.strategy_ids import MANUAL_TAG, MOMENTUM_TAG
    assert (MANUAL_TAG, MOMENTUM_TAG) == ("001", "002")


# -- 10. THE BUILDER ACTUALLY RUNS ------------------------------------------------------------------
def test_the_builder_CONSTRUCTS_a_real_strategy_end_to_end(monkeypatch, tmp_path):
    """EVERY OTHER TEST IN THIS FILE READS SOURCE. None of them execute the builder, so a signature
    mismatch — a keyword the runner or the adapter does not accept — would pass all of them and fail
    at node startup, in a deploy, with the market open.

    That is the "built, never executed" class this whole branch exists to avoid: QC345's
    `KeyError: 'eligible'` and MOMENTUM's 22 fills with zero terminal rows both passed review and had
    never run. A builder asserted only by `ast.unparse` has exactly that shape.

    Stubs are limited to the things that need a network, a database or credentials — the store, the
    exchange lookup, the broker and the calendar. The runner, the adapter and the config are REAL,
    because they are what this test exists to construct.

    THE CALENDAR STUB IS ITSELF A FINDING. Un-stubbed, this test failed with "no APCA credentials, so
    only the HOLIDAY-UNAWARE WeekdayCalendar is available — refusing it here because this path can
    place orders". So `require_exchange=True` is enforced rather than decorative — and it means
    `build_qc27_strategy` RAISES on a box without Alpaca credentials. `build_optional_strategy`
    catches only OSError, so that RuntimeError propagates and the node does not boot. That is the
    same exposure `build_qc345_strategy` already carries and is correct for a misconfiguration, but
    it is worth knowing before a deploy: no credentials means no node, not a missing strategy.
    """
    from nautilus_trader.model.identifiers import InstrumentId

    # A VALUE THE FALLBACK COULD NOT PRODUCE (2026-08-25). This seeded nothing for the lane's
    # allocation, so `_allocated_equity()` returned None, `_BUILD_TIME_FALLBACK_EQUITY` supplied
    # 20_000.0, and the assertion below read `== 20_000.0` and passed — through the DEAD path, while
    # looking like it tested the wiring. That is the exact shape qc27.py's own comment warns about
    # ("A test asserted `== 20_000.0` and passed"), still present in this smoke test.
    #
    # 34_567.0 cannot come from anywhere but the settings read.
    monkeypatch.setattr("api.settings.resolve", lambda _d: {
        "QC27_ENABLED": True, "QC27_UNIVERSE": ["AAPL", "MSFT", "NVDA", "GLD"],
        qc27.STRATEGY_ID: 34_567.0})

    # The store: a sessionmaker is never used during construction, only captured by the closures.
    import kumo_strategies.runtime.executor.store as _store
    monkeypatch.setattr(_store, "make_engine", lambda *a, **k: object())
    async def _create_all(_e): return None
    monkeypatch.setattr(_store, "create_all", _create_all)
    # A SESSIONMAKER THAT ACTUALLY OPENS. It used to be `lambda: None`, which was safe only while
    # nothing in the builder used it — the builder now reads its HELD CLAIMS so a position outside the
    # universe can still be SOLD (see `_held_claims`), and a double that cannot be entered as an async
    # context manager cannot represent production. The rows are empty, which is the honest answer for
    # a lane holding nothing; what matters is that the call SUCCEEDS the way it does live.
    class _NoRows:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *a, **k):
            return _NoRows()

    monkeypatch.setattr(_store, "make_sessionmaker", lambda _e: (lambda: _Session()))

    # `_lane_symbols` is the last point at which the pool is still just names — venues are
    # resolved in the strategy's `on_start` now, from the attached adapter (#622).
    import strategies.momentum as _mom
    monkeypatch.setattr(_mom, "_lane_symbols",
                        lambda syms: [InstrumentId.from_str(f"{s}.XNAS") for s in syms])

    # The broker opens no connection at construction, but it does want a live strategy later; the
    # builder assigns it afterwards, which is part of what is being checked.
    import kumo_strategies.runtime.nautilus.broker as _brk
    monkeypatch.setattr(_brk, "NautilusBroker",
                        lambda strategy=None, instrument_ids=None: type(
                            "B", (), {"strategy": strategy, "instrument_ids": instrument_ids})())

    # See the docstring: refuses to build without credentials, which is the guard working.
    import kumo_strategies.runtime.calendar as _cal
    real_build_calendar = _cal.build_calendar
    monkeypatch.setattr(_cal, "build_calendar",
                        # `calendar=` accepted since #628 — an injected venue calendar SATISFIES
                        # require_exchange rather than relaxing it. A double that rejects a kwarg
                        # production passes cannot represent production.
                        lambda require_exchange=False, calendar=None: type("C", (), {
                            "require_exchange": require_exchange,
                            "next_fire": staticmethod(lambda after, offset: (None, None))})())

    strategy = qc27.build_qc27_strategy()
    assert real_build_calendar is not _cal.build_calendar, "calendar stub did not take"

    assert strategy is not None
    assert str(strategy.id) == "TECHIVOL-005", "the wire id is not what cockpit allocated"
    # The one invariant: the adapter must be holding cockpit's runner, or it decides and submits
    # itself with no lifecycle, journal, idempotency, risk or budget in the path.
    from kumo_strategies.runtime.executor.qc27_runner import QC27SessionRunner
    assert isinstance(strategy._runner, QC27SessionRunner), "the adapter has no session runner"
    assert strategy._runner.limits.allocated_equity == 34_567.0, (
        "the allocation did not travel from settings into the runner's limits. If this reads "
        f"{qc27._BUILD_TIME_FALLBACK_EQUITY}, the settings read returned None and the build-time "
        "fallback answered — which is the dead knob the whole change was about")
    assert strategy._runner.limits.allocated_equity != qc27._BUILD_TIME_FALLBACK_EQUITY, (
        "seeded value collides with the fallback — pick one the fallback cannot produce")
    assert strategy._runner.read_state is not None, "lifecycle would be frozen at node boot"
    assert strategy._runner.budget_gate is not None
    assert strategy._runner.strategy_id == "TECHIVOL-005"
    # The researched config, not the dataclass defaults.
    assert strategy._cfg.rebalance_period == "D"
    assert strategy._cfg.momentum_price_field == "close"
    assert strategy._open_offset == 150, "not filling at midday"


# -- the allocation: three states, each one seen red on its own --------------------------------------
#
# WHY THIS SECTION EXISTS. `test_sizing_uses_THIS_STRATEGYS_allocation_not_the_account` asserted
# `ALLOCATED_EQUITY == 20_000.0` and that the builder passed it. Both halves were true and the lane
# was still wrong: a MODULE CONSTANT cannot be what an operator configured, it can only agree with it.
# It did agree — `strategies.TECHIVOL-005` is 20000 today — which is precisely why nothing found this.
# The test measured the wrong fact and its passing was evidence of nothing.
#
# Every other lane re-reads: `momentum.py` and `qc345.py:_allocated_equity` both call
# `resolve("strategies")`, at build AND per session, because an operator edits the target from the UI
# while the node keeps running. TECHIVOL-005 was the third lane to get this wrong.
#
# THREE STATES, NOT TWO, and the collapse between two of them is the whole defect:
#   present -> the number, sized off it
#   zero    -> 0.0 reaches the runner as 0.0 and the lane declines to enter
#   absent  -> None, and the runner falls back to the ACCOUNT (kumo-trading-strategies be244d9 confirms it
#              cannot tell "could not resolve" from "no allocation configured" — so cockpit must not
#              encode not-arrived as None expecting a refusal that will not come)
# `x or y` treats 0.0 as falsy, which fuses zero into absent. That is the bug at all three sites.


def _runner_for(monkeypatch, tmp_path, values):
    """The REAL builder, driven end to end, returning the runner the adapter actually holds.

    Not a hand-built runner: the question is whether the thing that reaches production re-reads, and
    a runner constructed by the test would answer a question nobody asked. Stubs are exactly those in
    `test_the_builder_CONSTRUCTS_a_real_strategy_end_to_end` — store, exchange lookup, broker,
    calendar — and nothing else.
    """
    from nautilus_trader.model.identifiers import InstrumentId

    settings = {"QC27_ENABLED": True, "QC27_UNIVERSE": ["AAPL", "MSFT", "NVDA", "GLD"]}
    settings.update(values)
    monkeypatch.setattr("api.settings.resolve", lambda _d: dict(settings))

    import kumo_strategies.runtime.executor.store as _store
    monkeypatch.setattr(_store, "make_engine", lambda *a, **k: object())
    async def _create_all(_e): return None
    monkeypatch.setattr(_store, "create_all", _create_all)
    monkeypatch.setattr(_store, "make_sessionmaker", lambda _e: (lambda: None))

    import strategies.momentum as _mom
    monkeypatch.setattr(_mom, "_lane_symbols",
                        lambda syms: [InstrumentId.from_str(f"{s}.XNAS") for s in syms])

    import kumo_strategies.runtime.nautilus.broker as _brk
    monkeypatch.setattr(_brk, "NautilusBroker",
                        lambda strategy=None, instrument_ids=None: type(
                            "B", (), {"strategy": strategy, "instrument_ids": instrument_ids})())

    import kumo_strategies.runtime.calendar as _cal
    monkeypatch.setattr(_cal, "build_calendar",
                        # `calendar=` accepted since #628 — an injected venue calendar SATISFIES
                        # require_exchange rather than relaxing it. A double that rejects a kwarg
                        # production passes cannot represent production.
                        lambda require_exchange=False, calendar=None: type("C", (), {
                            "require_exchange": require_exchange,
                            "next_fire": staticmethod(lambda after, offset: (None, None))})())

    return qc27.build_qc27_strategy()._runner, settings


def _drive_one_session(monkeypatch, runner):
    """Call the runner the way the ADAPTER does, and report what sizing would have read.

    THE SEAM, NOT THE UNIT. A green test on a resolution helper says nothing about whether anything
    calls it — which is the exact shape that broke this platform five times in one day. So this drives
    `runner.run(...)` for real and intercepts at `QC27SessionRunner.run`, the boundary cockpit's
    override delegates across. What that base method sees on `self.limits` IS what sizing reads:
    `qc27_runner.py` takes `self.limits.allocated_equity` inside `run`, not at construction.
    """
    from kumo_strategies.runtime.executor.qc27_runner import QC27SessionRunner

    seen = {}

    async def _record(self, *a, **kw):
        seen["allocated_equity"] = self.limits.allocated_equity

    monkeypatch.setattr(QC27SessionRunner, "run", _record, raising=True)
    # `asyncio.run`, NOT `@pytest.mark.asyncio`. THIS REPO HAS NO pytest-asyncio: an async test
    # function is collected, never awaited, and reported as PASSED on a coroutine nobody ran. Three
    # of the four tests below would have been assertions about nothing — the same shape as the
    # `needs_services` suite that errors, is deselected, and tells no one.
    asyncio.run(runner.run(None, "2026-08-24"))
    return seen


def test_the_allocation_is_RE_READ_PER_SESSION_not_frozen_at_the_boot_value(
        monkeypatch, tmp_path):
    """An operator edits TECHIVOL-005's target in the UI. The node keeps running for days.

    12345 rather than a round number ON PURPOSE: 20000 is both the old constant AND today's settings
    value, so a test written with 20000 passes whether the code reads settings or ignores them. That
    coincidence is what hid the defect for the lane's entire life. The number here must be one no
    constant could produce.
    """
    runner, settings = _runner_for(monkeypatch, tmp_path, {"TECHIVOL-005": 12345.0})
    seen = _drive_one_session(monkeypatch, runner)
    assert seen["allocated_equity"] == 12345.0, (
        "sizing read something other than the operator's configured allocation — a lane whose "
        "settings knob does nothing is a lane nobody can resize without a deploy")


def test_an_allocation_of_ZERO_reaches_sizing_as_ZERO_and_is_not_collapsed_into_absent(
        monkeypatch, tmp_path):
    """0 is BOTH the schema default and an operator saying "wind down", and it must survive the trip.

    THE FIXTURE'S OWN PROPERTY FIRST: 0.0 is falsy, which is what makes this reachable at all. If the
    value could not be confused with absent there would be nothing here to violate — and a test that
    cannot fail carries no information. Assert the trap exists, then assert the code steps over it.

    On kumo-staging every lane but BCTROT-004 is allocated 0 against a ~999k account, so a collapse
    here is not theoretical: it is ~1M of sizing for a lane an operator switched off.
    """
    assert not bool(0.0), "the fixture cannot bind — 0.0 is not falsy, so nothing here is at risk"
    runner, settings = _runner_for(monkeypatch, tmp_path, {"TECHIVOL-005": 0})
    seen = _drive_one_session(monkeypatch, runner)
    assert seen["allocated_equity"] == 0.0, (
        "a ZERO allocation was fused into ABSENT — the lane sizes off the whole account, which is "
        "the opposite of what setting it to zero asks for")
    assert seen["allocated_equity"] is not None


def test_an_ABSENT_allocation_is_STOPPED_HERE_and_never_becomes_the_whole_account(
        monkeypatch, tmp_path):
    """No key at all. Absent is not zero and it is ALSO not "help yourself to the account".

    I FIRST WROTE THIS ASSERTING `is None`, and it was wrong. The reasoning came from kumo-trading-strategies'
    own framing — keep absent distinguishable, pass None — and that framing was corrected at the
    source (0b5a591): downstream CANNOT tell "cockpit manages this lane but could not resolve it"
    from "no allocation configured". Both size off the whole account. So passing None to preserve a
    distinction only preserves it on this side of the call; on the far side it is the ~103k account,
    which is the exact sizing `allocated_equity` exists to prevent.

    Cockpit is the last place that knows the difference, so cockpit is where it stops. An absent
    allocation gets the conservative build-time fallback and a loud log — never the account, and
    never 0.0 either, which would silence a lane through the `notional <= 0` skip with nothing in the
    journal to say why.
    """
    runner, settings = _runner_for(monkeypatch, tmp_path, {})
    assert "TECHIVOL-005" not in settings, "the fixture supplied the very key it is testing for"
    seen = _drive_one_session(monkeypatch, runner)
    assert seen["allocated_equity"] is not None, (
        "an absent allocation reached sizing as None — downstream reads that as the ACCOUNT")
    assert seen["allocated_equity"] == qc27._BUILD_TIME_FALLBACK_EQUITY
    assert seen["allocated_equity"] != 0.0, "absent was rendered as a wind-down"
    # The account on alpaca-paper is ~103k against a 20k lane. Pin the GAP, not just the value: this
    # assertion is what would fail if somebody restored the account fallback.
    assert seen["allocated_equity"] < 50_000.0, (
        "sizing got a number large enough to be an account rather than a sleeve")


def test_NO_lane_cockpit_builds_collapses_an_ABSENT_allocation_into_a_ZERO():
    """AIMED AT THE CLASS. The question is not "does qc27 read settings now" — it is "what would have
    caught this AND its siblings", and there are three siblings.

    `momentum.py` reads `float(resolve("strategies").get(id) or 0.0)`: a MISSING key and a key that
    ARRIVED AS 0 become the same 0.0 before anything downstream sees either, so momentum cannot
    express a zero at all. kumo-trading-strategies had the same `or` at two sites (be244d9). This is the third
    place one number was made to mean two things.

    Reading `_code_of` and not raw source: the modules explain the unsafe alternative in prose, so a
    text search would be satisfied by an explanation and broken by rewording one.
    """
    from strategies import momentum, qc345

    #: Every place cockpit turns the `strategies` settings domain into an allocation. Named
    #: EXPLICITLY rather than discovered, because a scan that finds nothing passes, and a scan that
    #: silently stopped finding things is the failure mode this test is here to prevent.
    READERS = (
        ("qc27._allocated_equity", getattr(qc27, "_allocated_equity", None)),
        ("qc345._allocated_equity", getattr(qc345, "_allocated_equity", None)),
        ("momentum.SessionGateway._limits_for_session",
         getattr(momentum.SessionGateway, "_limits_for_session", None)),
    )
    missing = [name for name, fn in READERS if fn is None]
    assert not missing, (
        f"an allocation reader named here no longer exists: {missing}. Rename it in this list too — "
        f"a check that silently stops checking is worse than no check")

    offenders = []
    for name, fn in READERS:
        code = _code_of(fn)
        if "or 0" in code:
            offenders.append(f"{name}: defaults with `or`")
        elif "is None" not in code and "is not None" not in code:
            offenders.append(f"{name}: never distinguishes absent from zero explicitly")
    assert not offenders, (
        "these fuse ABSENT into ZERO before anything downstream can tell them apart — `x or y` "
        f"treats 0.0 as falsy, so one number is made to mean two things: {offenders}")
