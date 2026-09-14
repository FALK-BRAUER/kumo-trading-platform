"""An instance's refreshers must run, and the runner must be observable when they do NOTHING.

THE ASYMMETRY IS THE POINT, and kumo-strategies named it:

    a refresher that RUNS AND FAILS     keeps the previous set and says so — degrades honestly
    a refresher that NEVER RUNS         leaves a health row ageing quietly until it crosses the stale
                                        threshold, and then EVERY LANE STOPS DECIDING AT ONCE

So the tests weight the second case. A runner that is silent when idle is indistinguishable from one
that is dead, and that only becomes visible at the moment it is most expensive.

kumo-cockpit#486 step 5.
"""

from __future__ import annotations

import asyncio
import json
import stat

import pytest

from api.source_runner import RunnerState, discover, parse_output, run_once


def _script(d, name: str, body: str):
    p = d / f"{name}.sh"
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def test_no_sources_directory_is_NORMAL_not_an_error(tmp_path):
    """An instance may legitimately supply no refreshers. That must not look like a fault."""
    assert discover(str(tmp_path / "absent")) == []


def test_a_tick_that_does_NOTHING_still_records_an_attempt(tmp_path):
    """THE FAILURE MODE THIS MODULE EXISTS FOR.

    A runner that only records on success cannot tell you it has stopped. `last_attempt_ts` and
    `attempts` move on EVERY tick — including the tick where there was nothing to do — because
    "idle" and "dead" produce identical silence otherwise.
    """
    st = RunnerState()
    assert st.attempts == 0 and st.last_attempt_ts == 0.0
    asyncio.run(run_once(str(tmp_path), st))
    assert st.attempts == 1, "a tick with no refreshers did not record an attempt"
    assert st.last_attempt_ts > 0, "an empty tick left last_attempt_ts at zero — indistinguishable from dead"


def test_only_EXECUTABLE_scripts_are_discovered(tmp_path):
    """A non-executable file is a script someone is still writing, not one to run."""
    (tmp_path / "draft.sh").write_text("#!/bin/sh\necho '[]'")
    _script(tmp_path, "ready", "echo '[]'")
    assert [p.stem for p in discover(str(tmp_path))] == ["ready"]


def test_a_FAILING_refresher_does_not_stop_the_others(tmp_path, monkeypatch):
    """One bad script must not take the pool down with it."""
    calls = []

    async def _fake_refresh(name, symbols, **kw):
        calls.append(name)
        return len(symbols)

    from api import pool

    monkeypatch.setattr(pool, "refresh_source", _fake_refresh, raising=True)
    _script(tmp_path, "aaa_broken", "exit 3")
    _script(tmp_path, "zzz_good", "echo '[\"AAPL\"]'")

    st = RunnerState()
    out = asyncio.run(run_once(str(tmp_path), st))
    assert out["aaa_broken"].startswith("failed:")
    assert out["zzz_good"] == "ok:1", out
    assert calls == ["zzz_good"], "a failing refresher still wrote to the pool"
    assert st.failures == 1


def test_a_failing_refresher_does_NOT_write_an_empty_set(tmp_path, monkeypatch):
    """The dangerous direction. A broken script must leave the previous set alone.

    Replacing a source with whatever a broken script emitted is how a truncated feed liquidates a
    book — so the runner must not call the writer at all when the script failed.
    """
    from api import pool

    called = []
    monkeypatch.setattr(pool, "refresh_source",
                        lambda *a, **k: called.append(a) or 0, raising=True)
    # PRINTS VALID JSON *AND* EXITS NON-ZERO. With `exit 1` alone the output is empty, `parse_output`
    # raises regardless, and the returncode check is never the thing that refuses — the fixture could
    # not reach the branch it was written for. A mutation deleting that check passed.
    _script(tmp_path, "broken", "echo '[\"AAPL\"]'\nexit 1")
    asyncio.run(run_once(str(tmp_path), RunnerState()))
    assert called == [], "a failed refresher reached the writer"


@pytest.mark.parametrize("bad", ["42", '"AAPL"', "[1,2]", "not json", '{"AAPL": 3}'])
def test_unparseable_output_is_REFUSED_not_coerced(bad):
    """Guessing produces a plausible symbol set nobody chose.

    Refusing keeps the previous set, which is strictly better: the pool goes stale and says so,
    instead of silently becoming something else.
    """
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_output(bad)


def test_both_accepted_shapes_parse():
    """The fixture must be able to succeed, or the refusals above prove only that it always raises."""
    assert parse_output('["AAPL","MSFT"]') == ["AAPL", "MSFT"]
    assert parse_output('{"AAPL":{"days_held":3}}') == {"AAPL": {"days_held": 3}}


def test_a_HUNG_refresher_is_abandoned_rather_than_hanging_the_runner(tmp_path, monkeypatch):
    """The next tick is worth more than this one finishing.

    A hung script must not become a hung runner — that is the "never runs again" case arriving
    through the mechanism meant to prevent it.
    """
    import api.source_runner as mod

    monkeypatch.setattr(mod, "TIMEOUT_S", 0.3, raising=True)
    _script(tmp_path, "hangs", "sleep 30")
    st = RunnerState()
    out = asyncio.run(run_once(str(tmp_path), st))
    assert out["hangs"].startswith("failed:"), out
    assert st.attempts == 1


def test_the_runner_NEVER_writes_the_table_itself():
    """One writer. `PoolSource`'s contract is that a refresh REPLACES the whole set, so two writers
    with replace semantics do not race — the loser's rows vanish."""
    import ast
    import inspect

    from api import source_runner

    # CAPABILITY, NOT PROSE. The first version asserted `"exec_pool_source" not in src` and failed on
    # the module's own DOCSTRING, which says it does not write that table. Matching prose instead of
    # code is a defect class this repo has catalogued, and this test had it.
    tree = ast.parse(inspect.getsource(source_runner))
    imported = {
        (n.module or "") + "." + a.name
        for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names
    }
    forbidden = {i for i in imported if "PoolSource" in i or "pg_insert" in i or "sqlalchemy" in i}
    assert not forbidden, (
        f"the runner imports a write path directly ({sorted(forbidden)}) — one writer, or two "
        f"replace-semantics writers make the loser's rows vanish"
    )
    calls = {
        f"{getattr(n.func.value, 'id', '')}.{n.func.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "pool.refresh_source" in calls, (
        f"the runner does not CALL the single writer; found {sorted(c for c in calls if c)}"
    )


def test_the_wrapper_refuses_to_CREATE_a_source(monkeypatch):
    """A typo must not become a source that is fresh, real and feeds nothing.

    `refresh_source("typo_watchlist", …)` creating silently is the same defect class as a default
    identity: well-formed, plausible, and undetectable downstream, while the source it was meant to
    refresh ages quietly into a hard block on deciding.

    Upstream's default is `create=True` DELIBERATELY — the live refreshers run against a deployed
    stack and a strict default would start failing any source lacking a health row. A NEW surface has
    no such history, so it takes the strict rule from its first call.
    """
    import asyncio

    from api import pool

    seen = {}

    class _P:
        async def refresh_source(self, name, symbols, **kw):
            seen.update(kw)
            return 0

    monkeypatch.setattr(pool, "_pool", lambda: _P(), raising=True)
    asyncio.run(pool.refresh_source("whatever", ["AAPL"]))
    assert seen.get("create") is False, (
        f"the endpoint wrapper does not pass create=False (got {seen!r}) — a typo would create a "
        f"phantom source"
    )


def test_the_runner_IS_STARTED_by_the_app():
    """THE SEAM, and the defect both reviews of #486 predicted.

    A runner that exists, is correct, and is never called leaves `source_health()` reporting stale
    with nobody able to say why — and a stale source is a HARD BLOCK on deciding, so it does not
    degrade a lane, it stops it deciding at all.

    This repo's own `test_no_public_function_is_orphaned_without_a_written_reason` caught exactly that
    on the first run of this module: `run_once` and `discover` had no production caller. It was right.
    """
    import ast
    import pathlib

    text = pathlib.Path(__file__).resolve().parent.joinpath("app.py").read_text()
    tree = ast.parse(text)
    started = any(
        isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "create_task"
        and "run_forever" in ast.dump(n)
        for n in ast.walk(tree)
    )
    assert started, (
        "nothing in app.py starts the source runner — the refreshers would exist, be correct, and "
        "never run, which is the failure this module was written for"
    )


def test_the_loop_does_not_EXIT_on_a_failed_tick():
    """One bad tick must not end the runner.

    A loop that exits on error converts a recoverable failure into the unrecoverable one: the pool
    ages quietly until every lane stops deciding at once.
    """
    import inspect

    from api.source_runner import run_forever

    src = inspect.getsource(run_forever)
    assert "while True" in src, "the runner does not loop"
    assert "except Exception" in src, "a failing tick would end the runner"
    assert "CancelledError" in src, (
        "CancelledError is swallowed by the broad except — shutdown would hang rather than cancel"
    )
