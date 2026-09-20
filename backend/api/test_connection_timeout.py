"""#954 — the node's ENGINE-CONNECT timeout is configured, forwarded, bounded, and larger than a
measured boot.

ibkr-paper, 2026-09-11 07:18:13Z (cockpit aeed851 / strategies 84d09d3): `TradingNode: STARTING`, then
sixty seconds later `Timed out (60.0s) waiting for engines to connect and initialize`, then RUNNING
with `DataEngine.check_connected() == False / ExecEngine.check_connected() == False`; both clients
connected six seconds AFTER that. Past the timeout Nautilus proceeds to RUNNING without connected
engines, reconciliation never runs and zero strategies start — the #613 inert shape, reached by a
timer. Cost: three live lanes unstarted for 8 minutes pre-open. The instrument provider qualifies
~250 contracts at boot (the CRSISHORT universe added 130, 8 of them unresolvable, each waiting out
IB's answer); the 06:35Z boot of the same composition connected in 50.5 s, the failed one in ~66 s.

Paper (Alpaca) measured over 11 boots 2026-09-09..11: STARTING→ExecClient Connected 2.2–2.6 s,
STARTING→DataClient Connected 0.7–3.1 s (one outlier 17.9 s), STARTING→RUNNING 1.6–19.4 s. Paper is
not at risk; the fix is for the CLASS, and the value is chosen from ibkr-paper's numbers.

THE TWO EXISTING KNOBS ARE DEAD, AND THAT WAS ALREADY ON RECORD. `timeout_reconciliation` and
`timeout_disconnection` are read from `KUMO_*_TIMEOUT_S` here, and neither variable reaches the
engine container: `printenv` inside kumo-paper-engine-1 on 2026-09-11 finds no `*_TIMEOUT_S` at all.
`test_compose_forwards_every_gate.py` has carried both names in `_BASELINE_UNFORWARDED` since
2026-08-30 — known debt, not a discovery — and its `test_the_BASELINE_only_SHRINKS` goes red the
moment either is forwarded, so the fix must remove them from that set in the same commit. A third
knob beside two dead ones would be a third dead knob, so this file pins the forwarding as a PROPERTY
over every timeout variable the node reads, into the `environment:` block (build args do not reach
the process), of EVERY compose file that runs the engine.

TWO BUDGETS THIS VALUE MUST FIT. (1) The inert watchdog (#613) alarms when nothing has been published
`_INERT_AFTER_SECS` after start, and its clock covers the connect: connect + reconciliation +
portfolio run serially (`kernel.py` run_async) BEFORE any strategy publishes. Their sum must sit
inside the watchdog, at code level AND at runtime (an operator can set the env to 900). (2) The stop
budget: `timeout_disconnection` is summed against compose's literal `stop_grace_period` by
`test_shutdown_budget.py` — a value an instance could raise through the environment would reintroduce
#381 (docker SIGKILL mid-shutdown) through a knob. So disconnection is NOT a knob: a constant, pinned
to the compose literal, and not read from the environment at all.

THE `api` SERVICE NEEDS NONE OF THIS. `api/node_factory.py` resolves "live" to a RedisConsumer and
"synthetic" to a NodeManager; no TradingNode is built in that process, so the timeouts are forwarded
to the ENGINE service only — not for symmetry into the api, as `KUMO_ORDERS_ARMED` is (#814).

THE INPUT THE VALUE WAS DERIVED FROM: ~250 qualified contracts on ibkr-paper (the CRSISHORT universe
added 130). A universe that doubles re-runs the race with no test here moving — the number to
re-measure then is STARTING→both clients Connected, off the engine log, not this file.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from api.test_shutdown_budget import _ENGINE, _REPO, _nautilus_default


def _build_node_tree() -> ast.Module:
    """`build_node` ONLY — `test_shutdown_budget._node_timeout` walks the whole 12k-line module, so
    a `timeout_portfolio=` on any object anywhere would read as configured (review finding 8)."""
    import inspect
    from api import engine_node
    return ast.parse(inspect.getsource(engine_node.build_node))


def _timeout_kwargs() -> dict[str, ast.keyword]:
    return {n.arg: n for n in ast.walk(_build_node_tree())
            if isinstance(n, ast.keyword) and (n.arg or "").startswith("timeout_")}


def _node_timeout(name: str) -> float:
    """The default build_node passes for a `timeout_*` kwarg: the LAST numeric-or-numeric-string
    constant under the keyword (`_timeout_s("KUMO_X", "180")` and `_timeout_s("KUMO_X", 180)` both
    read 180). Absent kwarg and unreadable expression are different failures and say so."""
    kw = _timeout_kwargs().get(name)
    assert kw is not None, f"{name} is not passed to TradingNodeConfig in build_node — this test is blind"
    # BIND TO THE SHAPE MEANT (l21, #977 review): `_timeout_s(var, default)` → the default is args[1].
    # "Last constant wins" over ast.walk misreads `… if flag else 60` and `… or 5`, and walk order is
    # not source order. The scan below is the fallback for a bare constant only, and says so.
    v = kw.value
    if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "_timeout_s":
        assert len(v.args) >= 2 and isinstance(v.args[1], ast.Constant), f"{name}: _timeout_s default is not a constant"
        return float(v.args[1].value)
    assert isinstance(v, ast.Constant), f"{name}: neither a _timeout_s(...) call nor a bare constant — refusing to guess: {ast.dump(v)}"
    values = []
    for const in ast.walk(kw.value):
        if isinstance(const, ast.Constant) and isinstance(const.value, (int, float)) and not isinstance(const.value, bool):
            values.append(float(const.value))
        elif isinstance(const, ast.Constant) and isinstance(const.value, str):
            try:
                values.append(float(const.value))
            except ValueError:
                continue
    assert values, f"{name} is passed but its default is not a readable constant: {ast.dump(kw.value)}"
    return values[-1]

#: The two boots that decide the value (ibkr-paper, 2026-09-11, ~250 contracts incl. 8 unresolvable).
_IBKR_PAPER_CLEAN_BOOT_S = 50.5
_IBKR_PAPER_FAILED_BOOT_S = 66.0

#: What build_node must read from the environment, and what it must NOT.
_ENV_KNOBS = {"timeout_connection": "KUMO_CONNECTION_TIMEOUT_S",
              "timeout_reconciliation": "KUMO_RECONCILIATION_TIMEOUT_S"}
_NOT_A_KNOB = {"timeout_disconnection"}

#: Nautilus `timeout_*` fields this repo DELIBERATELY leaves at Nautilus's default, each with the
#: reason. Every other `timeout_*` field of TradingNodeConfig must be passed explicitly — an
#: unconfigured timeout is not a decision, it is the 60 s that sat under ibkr-paper until 2026-09-11.
_ACCEPTED_NAUTILUS_DEFAULTS = {
    # Waits for the portfolio to initialize AFTER the clients connect — not in the connect race, and
    # counted into the boot budget below at its default. Paper: RUNNING ≤ 19.4 s after STARTING.
    "timeout_portfolio": "runs after connect; counted in the boot budget at Nautilus's default",
    # Both are part of the STOP budget, pinned against stop_grace_period by test_shutdown_budget.py.
    "timeout_post_stop": "stop budget; summed into stop_grace_period by test_shutdown_budget.py",
    "timeout_shutdown": "stop budget; summed into stop_grace_period by test_shutdown_budget.py",
}


# -- compose readers: the ENGINE service's `environment:` block, in every file that runs the engine -----

def _engine_compose_files() -> list[pathlib.Path]:
    """Every deploy/compose*.yml whose engine service runs `api.engine_node`."""
    out = []
    for p in sorted((_REPO / "deploy").glob("compose*.yml")):
        block = _engine_service_block(p.read_text())
        if block is not None and "api.engine_node" in block:
            out.append(p)
    return out


def _engine_service_block(text: str) -> str | None:
    if "\n  engine:" not in text:
        return None
    engine = text[text.index("\n  engine:"):]
    nxt = re.search(r"\n  [a-z][a-z0-9_-]*:\n", engine[1:])
    return engine[: nxt.start() + 1] if nxt else engine


def _engine_environment_block(text: str) -> str:
    """ONLY the `environment:` mapping of the engine service — `build.args` also interpolates
    `${VAR:-x}` and never reaches the process (the review's demonstration: `KUMO_GIT_SHA`)."""
    block = _engine_service_block(text)
    assert block is not None, "no engine service"
    m = re.search(r"\n    environment:\n", block)
    assert m, "engine service has no environment: mapping"
    env = block[m.end():]
    nxt = re.search(r"\n    [a-z_]+:", env)   # next 4-space key of the service ends the mapping
    return env[: nxt.start()] if nxt else env


def _forwarded(text: str, var: str) -> str | None:
    """The interpolation compose forwards for `var` into the engine's ENVIRONMENT, e.g. `${VAR:-180}`,
    or None when the environment mapping does not carry the key."""
    env = _engine_environment_block(text)
    m = (re.search(rf"^\s+{re.escape(var)}:\s*[\"']?(\$\{{[^}}]*\}})[\"']?\s*(#.*)?$", env, re.M)        # mapping form
         or re.search(rf"^\s+-\s*[\"']?{re.escape(var)}=(\$\{{[^}}]*\}})[\"']?\s*(#.*)?$", env, re.M))  # list form
    return m.group(1) if m else None


def _callees(node: ast.AST) -> set[str]:
    """Names of everything called under `node` — `f(...)` AND `mod.f(...)`; an `ast.Name`-only scan is
    the evasion CLAUDE.md records (three sites found, both production lanes missed)."""
    out = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            if isinstance(c.func, ast.Name):
                out.add(c.func.id)
            elif isinstance(c.func, ast.Attribute):
                out.add(c.func.attr)
    return out


def _node_timeout_env_reads() -> dict[str, str]:
    """{timeout_kwarg: KUMO env var} for every `timeout_*` kwarg passed to TradingNodeConfig that is
    read from the environment — off the AST, as `_node_timeout` reads the default."""
    out: dict[str, str] = {}
    for node in _timeout_kwargs().values():
        for const in ast.walk(node.value):
            if isinstance(const, ast.Constant) and isinstance(const.value, str) and const.value.startswith("KUMO_"):
                out[node.arg] = const.value
    return out


# -- FIXTURES: the defect is reachable and the readers see their subjects -------------------------------

def test_FIXTURE_nautilus_default_connect_timeout_is_SHORTER_than_ibkr_papers_failed_boot():
    """The property that makes #954 reachable: 60 < 66. If Nautilus ever raises its default past the
    measured boot, this file's premise is gone and the tests below pin a number for no reason."""
    assert _nautilus_default("timeout_connection") == 60.0
    assert _nautilus_default("timeout_connection") < _IBKR_PAPER_FAILED_BOOT_S


def test_FIXTURE_the_compose_readers_see_paper_AND_prod_and_do_not_mistake_build_args_for_environment():
    from api.test_not_shipped import require_shipped
    require_shipped("deploy/compose.prod.yml")
    files = [p.name for p in _engine_compose_files()]
    assert files == ["compose.paper.yml", "compose.prod.yml"], files
    paper = (_REPO / "deploy" / "compose.paper.yml").read_text()
    assert _forwarded(paper, "KUMO_GIT_SHA") is None, "a build arg must NOT read as forwarded"
    assert _forwarded(paper, "KUMO_ORDERS_ARMED") is not None, "a real environment key must"
    # both YAML spellings compose accepts read as forwarded; anything else does not
    for shape in ("      KUMO_X: ${KUMO_X:-180}\n", "      KUMO_X: '${KUMO_X:-180}'\n", "      - KUMO_X=${KUMO_X:-180}\n",
                  '      - "KUMO_X=${KUMO_X:-180}"\n'):
        fake = "\n  engine:\n    command: api.engine_node\n    environment:\n" + shape + "    ports: []\n"
        assert _forwarded(fake, "KUMO_X") == "${KUMO_X:-180}", shape
    fake = "\n  engine:\n    command: api.engine_node\n    build:\n      args:\n        KUMO_X: ${KUMO_X:-1}\n    environment:\n      A: b\n"
    assert _forwarded(fake, "KUMO_X") is None


def test_FIXTURE_the_node_reads_exactly_the_two_knobs_from_KUMO_env_vars():
    """Asserted here AND inside every test that iterates the reader, so an empty reader cannot turn
    the forwarding tests green (review finding 4)."""
    assert _node_timeout_env_reads() == _ENV_KNOBS


# -- the value, and why it is what it is -----------------------------------------------------------------

def test_the_node_passes_timeout_connection_and_it_covers_the_failed_boot_with_margin():
    """180 s: at least TWICE the ~66 s boot that missed the default by six seconds, and more than three
    times the 50.5 s clean boot of the same composition — contract qualification is ~250 IB round
    trips and the 8 unresolvables each wait out IB, so a busy IB day is slower than the failed boot,
    not faster. Not a round number picked for shape: a value trimmed to 90 "because 60 was too low"
    re-runs the race on the next universe growth. `_node_timeout` raises if the kwarg is absent."""
    ours = _node_timeout("timeout_connection")
    assert ours >= 2 * _IBKR_PAPER_FAILED_BOOT_S, f"{ours}s does not cover the ~66 s boot twice over"
    assert ours >= 3 * _IBKR_PAPER_CLEAN_BOOT_S
    assert ours > _nautilus_default("timeout_connection")


def test_every_nautilus_timeout_is_either_configured_or_an_ACCEPTED_default_with_a_reason():
    """Enumerate the siblings (#954 found timeout_connection by accident, in a log line). Every
    `timeout_*` field TradingNodeConfig declares is either passed by build_node or listed above with
    the reason the default is right — absence is not a decision."""
    from nautilus_trader.live.config import TradingNodeConfig

    declared = {k for k in TradingNodeConfig().dict() if k.startswith("timeout_")}
    assert {"timeout_connection", "timeout_reconciliation", "timeout_disconnection", "timeout_portfolio"} <= declared
    passed = set(_timeout_kwargs())
    unaccounted = declared - passed - set(_ACCEPTED_NAUTILUS_DEFAULTS)
    assert not unaccounted, f"unconfigured and unexplained: {sorted(unaccounted)}"
    assert not (passed & set(_ACCEPTED_NAUTILUS_DEFAULTS)), "a field cannot be both passed and 'accepted default'"


# -- budget 1: the boot fits inside the inert watchdog, at code level and at runtime ---------------------

def _boot_budget_s() -> float:
    portfolio = _nautilus_default("timeout_portfolio")   # accepted default, see the table above
    return _node_timeout("timeout_connection") + _node_timeout("timeout_reconciliation") + portfolio


def test_the_boot_budget_fits_inside_the_inert_watchdog_at_code_level():
    """connect + reconciliation + portfolio run SERIALLY before any strategy can publish; the #613
    watchdog alarms `_INERT_AFTER_SECS` after start. 180 + 120 + 10 = 310 against a watchdog of 180
    would announce ENGINE INERT on every healthy slow boot and then "engine recovered" — the false
    alarm its own docstring says must not happen. Two derivations of one budget, pinned together."""
    from api.engine_node import _INERT_AFTER_SECS

    assert _boot_budget_s() < _INERT_AFTER_SECS, (
        f"boot budget {_boot_budget_s()}s >= inert watchdog {_INERT_AFTER_SECS}s — raise the watchdog with the reason")
    assert _INERT_AFTER_SECS - _boot_budget_s() >= 15.0, "one watchdog check period of margin, at least"
    # CEILING: the refusal message invites raising the watchdog, and nothing else bounds it. The
    # measured remediation on #954 was 8 minutes from the alarm; a watchdog slower than 10 minutes
    # is slower than the human it exists to wake, and the alarm's whole value is the head start.
    assert _INERT_AFTER_SECS <= 600.0, f"inert watchdog {_INERT_AFTER_SECS}s — slower than the remediation it exists to trigger"


def test_the_boot_budget_is_REFUSED_at_runtime_when_the_environment_exceeds_the_watchdog(monkeypatch):
    """The code-level pin cannot see `KUMO_CONNECTION_TIMEOUT_S=900` set by an instance. build_node
    must refuse a budget the watchdog cannot hold, naming the inputs — a node that boots with an
    inert alarm armed to fire on a healthy boot is a fallback that reads as health."""
    from api import engine_node as en

    en._boot_budget_or_refuse(connection=180.0, reconciliation=120.0, portfolio=10.0)   # the shipped budget holds
    with pytest.raises(ValueError, match="KUMO_CONNECTION_TIMEOUT_S"):
        en._boot_budget_or_refuse(connection=900.0, reconciliation=120.0, portfolio=10.0)
    # FLOOR (l21, #977 review): `KUMO_CONNECTION_TIMEOUT_S=0.5` passes `_timeout_s` (finite, positive) and
    # the ceiling, and reproduces #954 THROUGH THE KNOB ADDED TO FIX IT — a connect timeout below the
    # real connect time is the incident. Below Nautilus's own 60 s default is refused by name.
    for too_small in (0.5, 1e-9, 59.0):
        with pytest.raises(ValueError, match="KUMO_CONNECTION_TIMEOUT_S.*below"):
            en._boot_budget_or_refuse(connection=too_small, reconciliation=120.0, portfolio=10.0)
    en._boot_budget_or_refuse(connection=60.0, reconciliation=120.0, portfolio=10.0)   # the default itself is the floor
    # and build_node CALLS it (a helper nobody calls is the #955 shape)
    assert "_boot_budget_or_refuse" in _callees(_build_node_tree())


# -- budget 2: disconnection is NOT a knob ---------------------------------------------------------------

def test_the_disconnection_timeout_is_a_CONSTANT_not_an_environment_knob():
    """`stop_grace_period` is a compose LITERAL; `timeout_disconnection` is summed against it by
    test_shutdown_budget.py. A value an instance can raise through the environment reintroduces #381
    (docker SIGKILL mid-shutdown, positions in an unknown state) through a knob nobody would find. So
    the value is a constant in the code, the two files are pinned to each other, and the environment
    is not consulted. The dead `KUMO_DISCONNECTION_TIMEOUT_S` read is deleted, not forwarded."""
    reads = _node_timeout_env_reads()
    assert reads == _ENV_KNOBS
    assert "timeout_disconnection" not in reads
    assert _node_timeout("timeout_disconnection") == 30.0     # test_shutdown_budget still reads it off the AST
    src = _ENGINE.read_text()
    assert "KUMO_DISCONNECTION_TIMEOUT_S" not in src, "not a knob: neither read nor documented as one"


# -- the knobs reach the container, and the two defaults agree ------------------------------------------

def test_every_timeout_env_var_the_node_reads_is_FORWARDED_into_EVERY_engine_environment():
    """Measured 2026-09-11: `docker exec kumo-paper-engine-1 printenv | grep -c TIMEOUT_S` → 0; the
    reconciliation knob has never reached a container. compose has no `env_file` (#581), so a knob
    exists only if it is listed — in the `environment:` mapping, in every file that runs the engine."""
    reads = _node_timeout_env_reads()
    assert reads == _ENV_KNOBS
    missing = {(p.name, var) for p in _engine_compose_files() for var in reads.values()
               if _forwarded(p.read_text(), var) is None}
    assert not missing, f"read by build_node, never forwarded into the engine environment: {sorted(missing)}"


def test_the_compose_default_and_the_code_default_are_the_SAME_number_in_every_file():
    """Two derivations of one value in two files — they will drift (#574/#581). compose supplies its
    default when the instance leaves the variable unset (`${VAR:-N}`), the code supplies its own when
    the process runs outside compose; an operator reading either file must see the number that runs."""
    reads = _node_timeout_env_reads()
    assert reads == _ENV_KNOBS
    for p in _engine_compose_files():
        for kwarg, var in reads.items():
            fwd = _forwarded(p.read_text(), var)
            assert fwd is not None, (p.name, var)
            m = re.fullmatch(r"\$\{" + re.escape(var) + r":-([0-9.]+)\}", fwd)
            assert m, f"{p.name}: {var} is forwarded as {fwd} — it must carry a `:-<default>` equal to the code's"
            assert float(m.group(1)) == _node_timeout(kwarg), f"{p.name} {var}: compose {m.group(1)} vs code {_node_timeout(kwarg)}"


def test_the_forwarding_baseline_no_longer_lists_the_timeout_knobs():
    """`_BASELINE_UNFORWARDED` in test_compose_forwards_every_gate.py has carried both timeout names as
    known debt since 2026-08-30 and its `only_SHRINKS` test goes red the moment one is forwarded. A
    green run of THIS file must not be able to mean "still baselined"."""
    from api.test_compose_forwards_every_gate import _BASELINE_UNFORWARDED

    assert "KUMO_RECONCILIATION_TIMEOUT_S" not in _BASELINE_UNFORWARDED
    assert "KUMO_DISCONNECTION_TIMEOUT_S" not in _BASELINE_UNFORWARDED, "no longer read at all — not debt, gone"
    assert "KUMO_CONNECTION_TIMEOUT_S" not in _BASELINE_UNFORWARDED


# -- blank is unset, garbage refuses by name ---------------------------------------------------------------

def test_a_blank_env_value_is_the_default_and_a_malformed_one_REFUSES_naming_the_variable(monkeypatch):
    """A shell can hand the process `KUMO_CONNECTION_TIMEOUT_S=` (blank), which `float("")` turns into
    a nameless ValueError at boot. Blank is "never told us" (#581) → the default; `abc` refuses and
    names the variable, never guesses. One helper for both knobs, so they cannot drift."""
    from api.engine_node import _timeout_s

    monkeypatch.setenv("KUMO_CONNECTION_TIMEOUT_S", "")
    assert _timeout_s("KUMO_CONNECTION_TIMEOUT_S", "180") == 180.0
    monkeypatch.delenv("KUMO_CONNECTION_TIMEOUT_S")
    assert _timeout_s("KUMO_CONNECTION_TIMEOUT_S", "180") == 180.0
    monkeypatch.setenv("KUMO_CONNECTION_TIMEOUT_S", "240")
    assert _timeout_s("KUMO_CONNECTION_TIMEOUT_S", "180") == 240.0
    # NOT a number of seconds, every spelling: refuse by name. `nan` is the one that matters —
    # `nan <= 0` is False, so a sign check alone lets it through BOTH gates (helper and boot budget:
    # `nan >= 330` is also False) and Nautilus's fields are plain `float` and accept it (impl review).
    # That is the `nan <= 0` disarms-a-halt class in CLAUDE.md, at the config layer.
    for bad in ("abc", "0", "0.0", "-5", "nan", "NaN", "inf", "-inf", "1e400"):
        monkeypatch.setenv("KUMO_CONNECTION_TIMEOUT_S", bad)
        with pytest.raises(ValueError, match="KUMO_CONNECTION_TIMEOUT_S"):
            _timeout_s("KUMO_CONNECTION_TIMEOUT_S", "180")
    # every env-read node timeout goes through it — the AST shows the helper under each kwarg
    seen = set()
    for name, kw in _timeout_kwargs().items():
        if name in _ENV_KNOBS:
            assert "_timeout_s" in _callees(kw.value), f"{name} does not read through _timeout_s"
            seen.add(name)
    assert seen == set(_ENV_KNOBS), "coverage: every env knob resolved through the helper"
