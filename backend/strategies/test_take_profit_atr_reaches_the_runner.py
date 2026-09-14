"""`take_profit_atr` is an operator override that reaches the ExitConfig the live runner is built with (#1055).

ks#221 step 2: sell-in-strength at 4.5 ATR. The kumo-strategies runtime already honours it —
`LIVE_SUPPORTED_EXITS` lists `take_profit_atr`, `pgrunner` computes the ATR when `needs_atr`, and
`exits.py` fires "took profit at N ATR (target 4.5)". What was missing is on THIS side: the builder
passed `ExitConfig(give_back_frac=...)` and nothing else, `_live_overrides` read no
`<PREFIX>_TAKE_PROFIT_ATR`, and the schema declared no such key — three places, each of which alone
makes a written 4.5 inert. The seam is asserted end to end through the REAL values file, the REAL
`resolve` and the REAL `live_config`, never a monkeypatched domain (the double that hid #1054).

ABSENT = OFF, EXACTLY TODAY'S BEHAVIOUR. No schema default: `ExitConfig.take_profit_atr` stays None
and no ATR is computed for it (`needs_atr` false) — the lane trades as it does now until an operator
writes the key. 0 is refused rather than obeyed (exclusiveMinimum): a zero target would fire on the
first tick above entry.

Every test here was seen red on 6080604 for the reason its docstring names.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from api.settings import store
from strategies.momentum import _RESEARCHED, _live_overrides, bctrot_config, live_config

# A SEAM FILE: every test reads kumo-strategies. Under the merge gate's pin run the import must
# resolve to the pin (`MERGE_GATE_EXPECT_KS_TREE`); a skip there would be a green that measured
# nothing (ks#146). conftest refuses the session on a mismatch, so a plain import is enough — but
# say it, so nobody wraps these in an ImportError skip later.
pytest.importorskip("kumo_strategies.strategies.momentum_rotation.config") if not os.environ.get("MERGE_GATE_EXPECT_KS_TREE") else None

#: A target no default, no researched constant and no fixture produce. 4.5 is ks#221's number.
_PROBE = 4.5


@pytest.fixture
def values_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    return tmp_path


def _write(values_dir: Path, values: dict) -> None:
    (values_dir / "strategies.json").write_text(json.dumps(values))


# ------------------------------------------------------------------ fixture properties ---------

def test_FIXTURE_the_probe_is_not_a_researched_value_and_the_runtime_supports_the_rule():
    from kumo_strategies.strategies.momentum_rotation.config import LIVE_SUPPORTED_EXITS, ExitConfig

    assert "take_profit_atr" not in _RESEARCHED
    assert "take_profit_atr" in LIVE_SUPPORTED_EXITS, "the live runner no longer implements the rule — this ticket's premise is gone"
    assert ExitConfig().take_profit_atr is None, "the upstream default moved off None; 'absent = off' needs re-deriving"


def test_FIXTURE_with_NO_key_the_built_config_carries_NO_take_profit(values_dir: Path):
    """Today's shape, pinned: absent means off, and nothing else about the exits moves."""
    _write(values_dir, {})
    cfg = live_config("MOMENTUM")
    assert cfg.exits.take_profit_atr is None
    assert cfg.exits.give_back_frac == _RESEARCHED["give_back_frac"]


# ------------------------------------------------------------------ the wire -------------------

@pytest.mark.parametrize("prefix,build", [("MOMENTUM", lambda: live_config("MOMENTUM")), ("BCTROT", bctrot_config)])
def test_a_written_take_profit_REACHES_the_ExitConfig_the_runner_is_built_with(values_dir: Path, prefix, build):
    """THE SEAM. Real file → real resolve → `_live_overrides` → the builder → `ExitConfig`. On 6080604
    every hop drops it: the schema strips the key, the function does not read it, the builder does not
    pass it — so the assertion is on the LAST hop, and any dropped hop fails here."""
    _write(values_dir, {f"{prefix}_TAKE_PROFIT_ATR": _PROBE})

    cfg = build()

    assert cfg.exits.take_profit_atr == _PROBE, (
        f"{prefix}_TAKE_PROFIT_ATR written as {_PROBE} reached the runner's ExitConfig as "
        f"{cfg.exits.take_profit_atr!r} — the lane cannot sell in strength (#1055)")
    assert cfg.exits.give_back_frac == _RESEARCHED["give_back_frac"], "setting take-profit moved the give-back"


def test_the_key_steers_ONLY_its_own_prefix(values_dir: Path):
    """#794's rule, for the new key: MOMENTUM's target must not reach BCTROT, and vice versa."""
    _write(values_dir, {"MOMENTUM_TAKE_PROFIT_ATR": _PROBE})
    assert live_config("MOMENTUM").exits.take_profit_atr == _PROBE
    assert bctrot_config().exits.take_profit_atr is None
    _write(values_dir, {"BCTROT_TAKE_PROFIT_ATR": 3.25})
    assert bctrot_config().exits.take_profit_atr == 3.25
    assert live_config("MOMENTUM").exits.take_profit_atr is None


def test_the_built_config_asks_the_runtime_to_COMPUTE_the_ATR_only_when_the_key_is_set(values_dir: Path):
    """The consequence the runner acts on: `needs_atr` is what makes pgrunner compute `trailing_atr`
    before `evaluate_exits`. Off → no ATR work (today); set → ATR computed, so the rule can fire
    rather than raise "an ATR-scaled exit rule is configured but no atr was passed"."""
    from kumo_strategies.strategies.momentum_rotation.exits import needs_atr

    _write(values_dir, {})
    assert needs_atr(live_config("MOMENTUM").exits) is False
    _write(values_dir, {"MOMENTUM_TAKE_PROFIT_ATR": _PROBE})
    assert needs_atr(live_config("MOMENTUM").exits) is True


def test_the_floor_is_ONE_number_shared_by_the_schema_and_the_builder():
    """Two derivations of one bound: the schema's `minimum` (what the file accepts) and
    `TAKE_PROFIT_ATR_FLOOR` (what the builder refuses below, for a hand-built domain). Asserted
    equal, so neither can be "tuned" without the other — the reviewer's finding was a comment
    claiming an agreement the code did not have."""
    from strategies.momentum import TAKE_PROFIT_ATR_FLOOR

    props = store.load_schema("strategies")["properties"]
    for key in ("MOMENTUM_TAKE_PROFIT_ATR", "BCTROT_TAKE_PROFIT_ATR"):
        assert props[key]["minimum"] == TAKE_PROFIT_ATR_FLOOR, key
    # and the builder's own guard, with the schema bypassed (a hand-built resolve):
    from api import settings as real
    original = real.resolve
    real.resolve = lambda domain: {"MOMENTUM_TAKE_PROFIT_ATR": TAKE_PROFIT_ATR_FLOOR - 0.01}
    try:
        assert _live_overrides("MOMENTUM").take_profit_atr is None
        real.resolve = lambda domain: {"MOMENTUM_TAKE_PROFIT_ATR": TAKE_PROFIT_ATR_FLOOR}
        assert _live_overrides("MOMENTUM").take_profit_atr == TAKE_PROFIT_ATR_FLOOR
    finally:
        real.resolve = original


def test_targets_OUTSIDE_the_labs_grid_are_refused_and_read_as_OFF(values_dir: Path):
    """The schema bounds the target to [3.0, 10.0]: the lab's grid never went below 3.25 and the
    README measures < 3 ATR at about -9pp, so 0.5 would be legal and destructive; 0 fires on the
    first tick above entry. A refused value leaves the rule OFF rather than obeying a number the
    lane cannot mean — and `_live_overrides` refuses <= 0 one hop later on its own."""
    for bad in (0, -1.5, 0.5, 2.99, 10.01, "four"):
        _write(values_dir, {"MOMENTUM_TAKE_PROFIT_ATR": bad})
        assert live_config("MOMENTUM").exits.take_profit_atr is None, f"{bad!r} reached the runner"
    for ok in (3.0, 4.5, 10.0):
        _write(values_dir, {"MOMENTUM_TAKE_PROFIT_ATR": ok})
        assert live_config("MOMENTUM").exits.take_profit_atr == ok


def test_give_back_and_take_profit_written_TOGETHER_land_in_their_own_fields(values_dir: Path):
    """Kills the wrong-slot mutant (`take_profit_atr=give_back`): two distinct probes in one file,
    each asserted in its own field, and the tuple read by NAME."""
    _write(values_dir, {"MOMENTUM_GIVE_BACK_FRAC": 0.35, "MOMENTUM_TAKE_PROFIT_ATR": _PROBE})
    o = _live_overrides("MOMENTUM")
    assert (o.give_back_frac, o.take_profit_atr) == (0.35, _PROBE)
    cfg = live_config("MOMENTUM")
    assert (cfg.exits.give_back_frac, cfg.exits.take_profit_atr) == (0.35, _PROBE)


# ------------------------------------------------------------------ the runner seam -----------

def test_the_RUNNER_built_from_the_written_key_takes_profit_at_the_target(values_dir: Path):
    """THE SEAM PAST THE CONFIG (b4enxqbg's MUST). `needs_atr` True is the consequence; the runner is
    what acts: `pgrunner` computes the ATR and `_trail_exits` hands it to `evaluate_exits`, whose
    take-profit arm fires "took profit at N ATR (target 4.5)". Driven through the REAL
    `PgSessionRunner._trail_exits` on the config the cockpit builder produced from the file — a
    builder that set the field on a config the runner copies without it would pass every test
    above and never sell in strength. The ATR is HANDED IN here; the hop that computes it
    (`pgrunner.py:1085`, `trailing_atr(px) if needs_atr(...)`) is pinned upstream (ks#226's
    `needs_atr` AST pin and pgrunner's own tests), not re-proved in this repo."""
    import asyncio

    from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
    from kumo_strategies.runtime.executor.pgrunner import PgSessionRunner
    from kumo_strategies.strategies.momentum_rotation.exits import LIVE, TrailState

    _write(values_dir, {"MOMENTUM_TAKE_PROFIT_ATR": _PROBE})
    cfg = live_config("MOMENTUM")
    assert cfg.exits.take_profit_atr == _PROBE

    class Runner(PgSessionRunner):
        async def _load_state(self, symbols):
            return {"AAA": TrailState(entry_px=100.0, peak_px=112.0, quality=LIVE)}

        async def _save_state(self, sym, st, qty=None):
            return None

    class Broker:
        def positions(self):
            return {"AAA": 10}

        def equity(self):
            return 100_000.0

        def last_price(self, sym, **_):
            return 110.0

    class Jrn:
        strategy_id = "MOMENTUM-002"

        async def write(self, *a, **k):
            return 1

    runner = Runner(strategy_id="MOMENTUM-002", pool=None, journal=Jrn(),
                    lifecycle=Lifecycle(State.TRADING, "armed"), cfg=cfg, broker=Broker())
    # ATR 2.0: the position is (110 - 100) / 2.0 = 5.0 ATR in profit, above the 4.5 target.
    exits = asyncio.run(runner._trail_exits({"AAA": 10}, {"AAA": 110.0}, atr={"AAA": 2.0}))

    assert "AAA" in exits, f"the runner built from the written key did not take profit: {exits}"
    assert "took profit" in exits["AAA"] and "(target 4.5)" in exits["AAA"], exits["AAA"]
    # And OFF stays off: the same book on the absent-key config fires nothing.
    _write(values_dir, {})
    runner_off = Runner(strategy_id="MOMENTUM-002", pool=None, journal=Jrn(),
                        lifecycle=Lifecycle(State.TRADING, "armed"), cfg=live_config("MOMENTUM"), broker=Broker())
    assert asyncio.run(runner_off._trail_exits({"AAA": 10}, {"AAA": 110.0}, atr={"AAA": 2.0})) == {}


def test_the_built_lanes_ALWAYS_carry_a_runner_so_the_adapters_own_fallback_never_evaluates_exits():
    """The premise ks#222 rests on: cockpit builds MOMENTUM and BCTROT through `_build_rotation`,
    which passes `session_runner=` to the adapter, so the adapter's runner-less decide path
    (momentum_rotation.py:886 — evaluates exits with no ATR and would raise with take_profit set)
    is never taken here. Pinned by reading the shared builder's source: both public builders route
    through it, and it names the kwarg. The lanes need Postgres to construct, hence the AST."""
    import ast
    import inspect
    import textwrap

    import strategies.momentum as m

    for fn in (m.build_momentum_strategy, m.build_bctrot_strategy):
        src = textwrap.dedent(inspect.getsource(fn))
        calls = {n.func.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_build_rotation" in calls, f"{fn.__name__} no longer builds through _build_rotation"
    shared = textwrap.dedent(inspect.getsource(m._build_rotation))
    kws = {k.arg for n in ast.walk(ast.parse(shared)) if isinstance(n, ast.Call) for k in n.keywords}
    assert "session_runner" in kws, "_build_rotation constructs the lane without session_runner="


# ------------------------------------------------------------------ the class -----------------

def _exit_fields() -> set[str]:
    from kumo_strategies.strategies.momentum_rotation.config import ExitConfig

    return {f.name for f in dataclasses.fields(ExitConfig)}


def test_every_exit_rule_the_schema_lets_an_operator_set_is_one_the_LIVE_runner_implements():
    """A key for an unsupported exit cannot be declared. Derived: every `<PREFIX>_<RULE>` schema key
    whose lower-cased rule is an `ExitConfig` field must be in ks `LIVE_SUPPORTED_EXITS` — the set
    the live runner honours, not the backtest's. Declaring `MOMENTUM_STALL_DAYS` before the runner
    implements it would be a knob that reads applied and does nothing."""
    from kumo_strategies.strategies.momentum_rotation.config import LIVE_SUPPORTED_EXITS

    fields = _exit_fields()
    declared = store.load_schema("strategies")["properties"]
    exit_keys = {k for k in declared for p in ("MOMENTUM_", "BCTROT_")
                 if k.startswith(p) and k[len(p):].lower() in fields}
    assert exit_keys, "no exit-rule keys are declared at all — the fixture finds nothing"
    unsupported = sorted(k for k in exit_keys if k.split("_", 1)[1].lower() not in LIVE_SUPPORTED_EXITS)
    assert unsupported == [], f"schema declares operator keys for exit rules the live runner ignores: {unsupported}"
    assert {"MOMENTUM_TAKE_PROFIT_ATR", "BCTROT_TAKE_PROFIT_ATR"} <= exit_keys


def test_every_exit_rule_the_BUILDER_sets_is_one_the_LIVE_runner_implements(values_dir: Path):
    """The other direction, through ks's own predicate: with every declared exit key written, the
    built ExitConfig asks for no rule the live runner ignores (`unsupported_live_exits`)."""
    from kumo_strategies.strategies.momentum_rotation.config import unsupported_live_exits

    _write(values_dir, {"MOMENTUM_GIVE_BACK_FRAC": 0.35, "MOMENTUM_TAKE_PROFIT_ATR": _PROBE})
    assert unsupported_live_exits(live_config("MOMENTUM").exits) == []


def test_EVERY_key_live_overrides_reads_is_declared_by_the_schema_including_the_new_one():
    """#1054's class guard, re-run here so this file is red on 6080604 for the schema reason too:
    the recorder collects what `_live_overrides` asks for; each must be a declared property."""
    from api import settings as real

    asked: set[str] = set()

    class _Recording(dict):
        def get(self, key, default=None):
            asked.add(key)
            return super().get(key, default)

    original = real.resolve
    real.resolve = lambda domain: _Recording()
    try:
        _live_overrides("MOMENTUM")
    finally:
        real.resolve = original
    assert "MOMENTUM_TAKE_PROFIT_ATR" in asked, f"_live_overrides does not read the take-profit key; asked {sorted(asked)}"
    declared = set(store.load_schema("strategies")["properties"])
    assert not (asked - declared), f"read but undeclared (stripped before resolve): {sorted(asked - declared)}"
