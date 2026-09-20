"""A runner-produced HALT must survive the session that raised it (#459 part 2).

`pgrunner` never persists — `self.lifecycle.halt(...)` mutates an in-memory dataclass and returns it
in `SessionResult`. Writing it is cockpit's job, and the write had a hole: `UPDATE ... WHERE
state = expected` returning `rowcount > 0` treats AN ABSENT ROW exactly like AN OPERATOR OVERRIDE.

Both return 0. One means "a human moved it, theirs wins" and the other means "nobody has ever written
one, ours is the first" — and the second collides with `absent row == TRADING` to give a lane that
halts on risk and trades again next session.

THE FAKE BELOW MUST REJECT WHAT POSTGRES REJECTS, or it proves nothing:
`test_the_fake_rejects_a_duplicate_primary_key` pins that, because four defects in one week shipped
green through doubles that accepted what production refuses. It models the three things this function
actually depends on — UPDATE rowcount under a WHERE, SELECT presence, and INSERT with a unique
strategy_id — and nothing else.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace

import pytest
from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State
from sqlalchemy.exc import IntegrityError

from . import lifecycle_state


def _run(coro):
    # `asyncio.run`, not `get_event_loop().run_until_complete`. The latter passed this file in
    # isolation and failed all six tests under the full suite, because another test had already
    # closed the shared loop -- a green that depended on running order.
    return asyncio.run(coro)


class _FakeSession:
    """Models `exec_strategy_state` well enough for compare-and-set, and no further."""

    def __init__(self, rows):
        self._rows = rows
        self.statements: list = []

    async def execute(self, stmt):
        from sqlalchemy import Insert, Select, Update

        self.statements.append(type(stmt).__name__)
        sid, want_state = _where_of(stmt)

        if isinstance(stmt, Update):
            vals = stmt._values or {}
            got = {k.name if hasattr(k, "name") else str(k): _lit(v) for k, v in vals.items()}
            hit = (sid in self._rows
                   and (want_state is None or self._rows[sid]["state"] == want_state))
            if hit:
                self._rows[sid].update(got)
            return SimpleNamespace(rowcount=1 if hit else 0)

        if isinstance(stmt, Select):
            row = self._rows.get(sid)
            return _Scalars([row["state"]] if row else [])

        if isinstance(stmt, Insert):
            vals = {k.name if hasattr(k, "name") else str(k): _lit(v)
                    for k, v in (stmt._values or {}).items()}
            key = vals.get("strategy_id")
            if _lit(vals.get("state")) == "TRADING":
                # trg_no_birth_in_trading (migration 0016, #443). Postgres RAISES here. A fake that
                # accepted a TRADING birth would let this helper "pass" a write the database refuses.
                raise IntegrityError(
                    "strategy cannot be CREATED in TRADING", None, Exception())
            if key in self._rows:
                # POSTGRES REJECTS THIS. `strategy_id` is the primary key of exec_strategy_state, so
                # a double that quietly overwrote here would hide a lost operator state.
                raise IntegrityError("duplicate key value violates unique constraint", None, Exception())
            self._rows[key] = vals
            return SimpleNamespace(rowcount=1)

        raise AssertionError(f"the fake was given a statement it cannot model: {type(stmt).__name__}")

    def begin(self):
        # NOT `async def`. `AsyncSession.begin()` RETURNS an async context manager rather than being
        # a coroutine, so `async with sm() as s, s.begin():` works. A coroutine here raises
        # "'coroutine' object does not support the asynchronous context manager protocol" -- the same
        # sync/async mismatch that let a missing `await` on `broker.exit()` pass 60 tests in #459.
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Scalars:
    def __init__(self, vals):
        self._v = vals

    def scalars(self):
        return self

    def first(self):
        return self._v[0] if self._v else None


def _lit(v):
    return getattr(v, "value", v)


def _where_of(stmt):
    """Pull strategy_id and the expected state out of the statement's WHERE clause."""
    sid = want = None
    clause = getattr(stmt, "whereclause", None)
    if clause is None:
        return sid, want
    for c in getattr(clause, "clauses", [clause]):
        col = getattr(getattr(c, "left", None), "name", None)
        val = getattr(getattr(c, "right", None), "value", None)
        if col == "strategy_id":
            sid = val
        elif col == "state":
            want = val
    return sid, want


def _sm(rows):
    sess = _FakeSession(rows)

    class _Ctx:
        def __call__(self):
            return self

        async def __aenter__(self):
            return sess

        async def __aexit__(self, *a):
            return False

    ctx = _Ctx()
    ctx.session = sess
    return ctx


# --------------------------------------------------------------------------------------------------
# The fake's own conformance — asserted BEFORE anything relies on it
# --------------------------------------------------------------------------------------------------


def test_the_fake_rejects_a_duplicate_primary_key():
    """If the fake accepted a second INSERT for one strategy_id, every test below would pass over a
    function that silently clobbers an operator's row."""
    rows = {"X": {"strategy_id": "X", "state": "TRADING"}}
    sess = _FakeSession(rows)
    from kumo_strategies.runtime.executor.store import StrategyState
    from sqlalchemy import insert

    with pytest.raises(IntegrityError):
        _run(sess.execute(insert(StrategyState).values(strategy_id="X", state="HALTED")))


def test_the_fake_agrees_with_AsyncSession_about_which_calls_are_coroutines():
    """Bound to the real class, not restated. `execute` is a coroutine, `begin` is not — get either
    backwards and the fake diverges from production in a way no behavioural test can see."""
    import inspect

    from sqlalchemy.ext.asyncio import AsyncSession

    for name in ("execute", "begin"):
        real = inspect.iscoroutinefunction(getattr(AsyncSession, name))
        mine = inspect.iscoroutinefunction(getattr(_FakeSession, name))
        assert real == mine, (
            f"_FakeSession.{name} is {'async' if mine else 'sync'} but AsyncSession.{name} is "
            f"{'async' if real else 'sync'}")


def test_the_fake_reports_rowcount_0_when_the_state_does_not_match():
    """The whole defect lives in rowcount. A fake that always returned 1 could not see it."""
    rows = {"X": {"strategy_id": "X", "state": "SHADOW"}}
    sess = _FakeSession(rows)
    from kumo_strategies.runtime.executor.store import StrategyState
    from sqlalchemy import update

    res = _run(sess.execute(
        update(StrategyState)
        .where(StrategyState.strategy_id == "X", StrategyState.state == "TRADING")
        .values(state="HALTED")))
    assert res.rowcount == 0


# --------------------------------------------------------------------------------------------------
# The behaviour
# --------------------------------------------------------------------------------------------------


def test_a_HALT_with_NO_ROW_is_WRITTEN_not_dropped():
    """THE DEFECT. `UPDATE ... WHERE` matches nothing, rowcount is 0, and the old code returned False
    — dropping the halt and telling the caller a human had intervened.

    A lane with no row is not exotic: ibkr-paper-retired registers five lanes and `exec_strategy_state`
    holds two, so three lanes there take this path on their FIRST halt.
    """
    rows: dict = {}
    sm = _sm(rows)

    ok = _run(lifecycle_state.save_if_unchanged(
        sm, "NEW-009", Lifecycle(State.HALTED, "daily loss -3.1%"), State.TRADING))

    assert ok == lifecycle_state.SAVED, (
        f"got {ok!r} — the halt was reported as an operator override, but there is no row to override")
    assert rows["NEW-009"]["state"] == "HALTED", (
        f"the halt did not reach the table: {rows}. An absent row reads as TRADING, so this lane "
        f"trades again next session with no record of why it stopped")
    assert rows["NEW-009"]["reason"] == "daily loss -3.1%"


def test_an_OPERATOR_who_moved_the_row_still_WINS():
    """The property the original was built to protect, and the one an absent-row insert could break.
    A blind upsert here put TRADING back over a human's HALT and the next session traded."""
    rows = {"M-002": {"strategy_id": "M-002", "state": "SHADOW", "reason": "operator: on-call"}}
    sm = _sm(rows)

    ok = _run(lifecycle_state.save_if_unchanged(
        sm, "M-002", Lifecycle(State.TRADING, "runner: nothing to do"), State.TRADING))

    assert ok == lifecycle_state.OPERATOR_WON, (
        f"got {ok!r} — the runner overwrote a state a human set during the session")
    assert rows["M-002"]["state"] == "SHADOW"
    assert rows["M-002"]["reason"] == "operator: on-call"


def test_the_ORDINARY_case_still_updates_in_place():
    rows = {"M-002": {"strategy_id": "M-002", "state": "TRADING", "reason": "operator: start"}}
    sm = _sm(rows)

    ok = _run(lifecycle_state.save_if_unchanged(
        sm, "M-002", Lifecycle(State.HALTED, "daily loss"), State.TRADING))

    assert ok == lifecycle_state.SAVED
    assert rows["M-002"]["state"] == "HALTED"
    assert sm.session.statements == ["Update"], (
        f"the ordinary path did extra queries: {sm.session.statements} — the SELECT and INSERT are "
        f"only for the rowcount-0 branch")


def test_a_MISSING_row_that_disagrees_with_what_the_session_READ_is_refused_not_papered_over():
    """`expected` says the session began at HALTED; the table has no row, which means TRADING. Both
    cannot be true. Writing either would bury the contradiction — and this is the pair that has
    already been inverted once in a runbook, so it is exactly the disagreement worth surfacing."""
    rows: dict = {}
    sm = _sm(rows)

    ok = _run(lifecycle_state.save_if_unchanged(
        sm, "X-001", Lifecycle(State.TRADING, "runner: resume"), State.HALTED))

    assert ok == lifecycle_state.CONTRADICTION, (
        f"got {ok!r} — a contradiction reported as OPERATOR_WON makes the caller journal "
        f"'the operator changed it', naming a human who did nothing. That false cause is the "
        f"defect this whole change exists to remove; collapsing it here reintroduces it.")
    assert rows == {}, f"a row was invented from a contradiction: {rows}"


def test_a_TRADING_BIRTH_is_refused_because_the_DATABASE_refuses_it():
    """`trg_no_birth_in_trading` (migration 0016, #443) raises on `INSERT ... state = 'TRADING'`.

    A row inserted already trading has no transition, so no promotion is ever journalled and the
    strategy is live with nothing on the record — that is how TECHIVOL-005 went live on 2026-08-21
    and formed 8 orders against other strategies' positions.

    The caller only reaches the insert when the state CHANGED, so this should be unreachable. It is
    refused explicitly anyway: unreachable-by-argument is how the last four defects got in, and a
    named rule beats a Postgres exception naming a trigger.
    """
    rows: dict = {}
    sm = _sm(rows)

    ok = _run(lifecycle_state.save_if_unchanged(
        sm, "NEW-009", Lifecycle(State.TRADING, "runner: fine"), State.TRADING))

    assert ok == lifecycle_state.CONTRADICTION, f"got {ok!r}"
    assert rows == {}, f"a strategy was born TRADING: {rows}"


def test_the_fake_refuses_a_TRADING_birth_like_the_TRIGGER_does():
    """Pins the fake against the database rule, so the test above cannot pass because the fake is
    lenient. Bound to the real trigger's condition, which is `NEW.state = 'TRADING'`."""
    from kumo_strategies.runtime.executor.store import StrategyState
    from sqlalchemy import insert

    sess = _FakeSession({})
    with pytest.raises(IntegrityError):
        _run(sess.execute(insert(StrategyState).values(strategy_id="Z", state="TRADING")))

    sess2 = _FakeSession({})
    _run(sess2.execute(insert(StrategyState).values(strategy_id="Z", state="DISABLED")))
    assert sess2._rows["Z"]["state"] == "DISABLED", "the fake refuses births it should allow"


def test_every_stored_state_this_file_uses_is_a_REAL_state():
    """`PAUSED` was used here as an operator-set state. There is no `PAUSED` in `State` — production
    can never store it, so the operator-wins test was asserting against a value that cannot occur.
    Codex caught it. Bound to the enum so the next invented state fails instead of reading fine."""
    import re

    valid = {s.value for s in State}
    src = pathlib.Path(__file__).read_text()
    used = set(re.findall(r'"state": "([A-Z_]+)"', src)) | set(re.findall(r'state="([A-Z_]+)"', src))
    assert used, "the scan found no stored states — it would pass by finding nothing"
    assert used <= valid, f"{used - valid} are not real States (valid: {sorted(valid)})"
