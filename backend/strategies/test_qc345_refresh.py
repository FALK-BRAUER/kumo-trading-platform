"""The scheduled universe refresh, and the write that would have wiped the budgets (#324).

Run with: PYTHONPATH=~/projects/kumo-strategies/src:.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from strategies import qc345_refresh as R


def _code_of(fn) -> str:
    """Executable body only — docstrings and comments stripped via `ast.unparse`.

    This module explains the unsafe alternatives in prose so the next reader is warned, which means a
    check over raw source is satisfied by an explanation and broken by rewording one.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


# --------------------------------------------------------------------------------------------------
# The write. This is the one that would have cost real money to discover.
# --------------------------------------------------------------------------------------------------


def test_persisting_the_universe_does_NOT_wipe_the_BUDGETS(tmp_path, monkeypatch):
    """`settings.save` writes the WHOLE domain: keys absent from `incoming` are dropped from the
    values file and fall back to schema defaults on the next resolve. The `strategies` domain also
    holds every budget target and TRANSFER_TO.

    So `save("strategies", {"QC345_UNIVERSE": [...]})` would not fail and would not warn — it would
    reset MOMENTUM's and QC345's allocations to their defaults. A monthly job quietly undoing an
    operator's capital decisions, found whenever somebody next looked at the numbers.
    """
    from api.settings import store

    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    store.save("strategies", {"MOMENTUM-002": 20000.0, "QC345-003": 40000.0,
                              "TRANSFER_TO": "BCTROT-004"})

    R.persist_universe(["AAPL", "MSFT"])

    after = store.resolve("strategies")
    assert after["QC345_UNIVERSE"] == ["AAPL", "MSFT"]
    assert after["MOMENTUM-002"] == 20000.0, "the refresh reset MOMENTUM's budget"
    assert after["QC345-003"] == 40000.0, "the refresh reset QC345's budget"
    assert after["TRANSFER_TO"] == "BCTROT-004", "the refresh cleared the transfer target"


def test_the_write_READS_BEFORE_it_writes():
    """The mechanism, not just the outcome. A future edit that keeps the test passing by luck —
    because the fixture happened to set every key — would still reintroduce the clobber."""
    body = _code_of(R.persist_universe)
    assert "settings.resolve('strategies')" in body, "the current values are never read"
    assert "settings.save('strategies', current)" in body, (
        "the whole merged domain is not written back — a partial save drops the other keys"
    )


def test_the_universe_is_written_SORTED():
    """A set-ordered universe would produce a spurious settings diff on every refresh, and a diff
    that is always noise is a diff nobody reads."""
    assert "sorted(symbols)" in _code_of(R.persist_universe)


# --------------------------------------------------------------------------------------------------
# The gate and the refusals
# --------------------------------------------------------------------------------------------------


def test_the_refresh_gate_comes_from_SETTINGS_not_the_ENVIRONMENT():
    """Same rule as the registration gate: an operator decision must not require a redeploy."""
    body = _code_of(R._enabled)
    assert "settings.resolve('strategies')" in body
    assert "environ" not in body


def test_the_refresh_is_OFF_unless_explicitly_enabled(tmp_path, monkeypatch):
    from api.settings import store

    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    assert R._enabled() is False, "an unset gate read as ON"
    store.save("strategies", {"QC345_UNIVERSE_REFRESH": True})
    assert R._enabled() is True, "the setting cannot turn it on — then the gate is unreachable"


def test_a_disabled_gate_schedules_NOTHING(monkeypatch):
    monkeypatch.setattr(R, "_enabled", lambda: False)

    class _Clock:
        def __init__(self):
            self.armed = []

        def set_timer(self, **kw):
            self.armed.append(kw)

    class _Strategy:
        clock = _Clock()
        _loop = object()

    s = _Strategy()
    R.schedule_refresh(s)
    assert s.clock.armed == [], "a timer was armed with the gate off"


def test_an_EMPTY_derivation_REFUSES_to_persist(monkeypatch):
    """A refresh that quietly wrote `[]` would make the next node start refuse to boot with a
    message about an operator-supplied setting the operator never touched."""
    from strategies import qc345_universe as U

    monkeypatch.setattr(R, "_live_config", lambda: None, raising=False)
    monkeypatch.setattr(U, "fetch_assets", lambda: _tiny_assets())
    monkeypatch.setattr(U, "substrate", lambda a, c: ["AAA"])
    monkeypatch.setattr(U, "fetch_daily_bars", lambda names, **kw: _tiny_bars())
    monkeypatch.setattr(U, "derive_universe", lambda b, a, c, **kw: ([], {}))
    monkeypatch.setattr("strategies.qc345._live_config", lambda: None)

    wrote = []
    monkeypatch.setattr(R, "persist_universe", lambda s: wrote.append(s))

    with pytest.raises(RuntimeError, match="refusing to persist an empty universe"):
        R.refresh_universe()
    assert wrote == [], "an empty universe was persisted before the refusal"


def _tiny_assets():
    import pandas as pd

    return pd.DataFrame([{"symbol": "AAA", "name": "AAA Inc.", "exchange": "NASDAQ",
                          "status": "active", "tradable": True}])


def _tiny_bars():
    import pandas as pd

    return pd.DataFrame([{"ticker": "AAA", "date": pd.Timestamp("2026-08-14"), "open": 1.0,
                          "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}])


# --------------------------------------------------------------------------------------------------
# Scheduling: Nautilus owns the clock, and the fetch stays off the loop
# --------------------------------------------------------------------------------------------------


def test_the_schedule_uses_the_NAUTILUS_CLOCK():
    """Hand-rolled asyncio scheduling plus launchd is a documented past mistake in this repo, and
    `Clock.set_timer` works in backtest as well."""
    body = _code_of(R.schedule_refresh)
    assert "strategy.clock.set_timer" in body
    assert "sleep" not in body, "the schedule hand-rolls a sleep loop instead of using the clock"


def test_the_SIX_MINUTE_FETCH_never_runs_on_the_event_loop():
    """`refresh_universe` is blocking urllib for ~373.8s measured. Awaiting it on the node's loop
    would stall market data, order events and the UI feed for that entire window — the node would
    look hung, once a day, for six minutes."""
    body = _code_of(R.schedule_refresh)
    assert "asyncio.to_thread(refresh_universe)" in body, (
        "the blocking fetch is not handed to a worker thread"
    )


def test_the_refresh_runs_DAILY_even_though_the_rebalance_is_MONTHLY():
    """A monthly timer that misses its fire — restart, redeploy, an exception — leaves the universe
    stale for a whole cycle before anyone finds out. The refresh is idempotent, so the extra runs
    cost one background thread that nothing waits on."""
    assert "pd.Timedelta(days=1)" in _code_of(R.schedule_refresh)


def test_a_FAILED_refresh_keeps_the_last_known_universe_and_does_not_kill_the_node():
    body = _code_of(R.schedule_refresh)
    assert "except Exception" in body
    assert "keeping the last known universe" in inspect.getsource(R.schedule_refresh)


def test_a_missing_event_loop_is_reported_rather_than_silently_skipped(monkeypatch, caplog):
    """Enabled-and-not-scheduled is the worst state available: the flag is set, the node is clean,
    and the universe silently never updates."""
    import logging

    monkeypatch.setattr(R, "_enabled", lambda: True)

    class _Strategy:
        _loop = None

    with caplog.at_level(logging.WARNING, logger=R._log.name):
        R.schedule_refresh(_Strategy())
    assert any("no event loop" in r.message for r in caplog.records)


# --------------------------------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------------------------------


def test_a_never_written_universe_reports_None_not_zero(tmp_path, monkeypatch):
    """Zero would read as "refreshed today", which is the opposite of the truth."""
    from api.settings import store

    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    assert R.universe_age_days() is None


def test_age_is_measured_from_the_persisted_file(tmp_path, monkeypatch):
    """So a refresh that silently stopped firing is VISIBLE. It otherwise looks exactly like one
    with nothing new to say — the universe simply stops moving, and a monthly strategy would take
    months to make that obvious."""
    import pandas as pd

    from api.settings import store

    monkeypatch.setattr(store, "_VALUES_DIR", tmp_path)
    store.save("strategies", {"QC345_UNIVERSE": ["AAPL"]})

    assert R.universe_age_days() == 0
    later = pd.Timestamp.now("UTC") + pd.Timedelta(days=60)
    assert R.universe_age_days(now=later) >= R.STALE_AFTER_DAYS, (
        "a two-month-old universe is not reported as past the staleness threshold"
    )


def test_the_refresh_is_ARMED_BY_THE_NODE_not_left_as_dead_code():
    """The seam. `schedule_refresh` being correct says nothing about whether anything calls it, and
    an unarmed refresher is indistinguishable from one with nothing to do.

    It is armed from the feed actor rather than from `build_qc345_strategy` for a mechanical reason:
    the strategy captures its event loop in `on_start`, which has not run at build time.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "api/engine_node.py").read_text()
    assert "from strategies.qc345_refresh import schedule_refresh" in src, (
        "nothing in the engine ever arms the universe refresh"
    )
    assert "schedule_refresh(self)" in src
