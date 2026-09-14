"""The `session` state frame (#212). Offline: a fake session factory returns rows, so these assert
the FRAME CONTRACT rather than Postgres.

Two consumers depend on this shape — the homescreen and the session-narrative evidence bundle — so
the tests are about what the frame must never lose, not about SQL.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from api.session_state import build

TS = datetime(2026, 8, 10, 13, 35, tzinfo=UTC)


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Result:
    """Stands in for a SQLAlchemy Result: iterable, plus .first() and .scalar()."""

    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        if not self._rows:
            return None
        row = self._rows[0]
        return row.value if hasattr(row, "value") else getattr(row, "session", None)


class FakeDb:
    def __init__(self, plan):
        self._plan = plan

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        for key, rows in self._plan.items():
            if key in sql:
                return Result(rows)
        return Result([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def factory(plan):
    return lambda: FakeDb(plan)


FULL = {
    "exec_strategy_state": [Row(state="TRADING", reason="operator: start paper trading",
                                updated_at=TS)],
    "kind = 'decision'": [Row(session="2026-08-10", summary="TRADING: hold 8 · enter 8 · exit 2",
                              ts=TS, detail={
        "reasons": {"MET": "left the ranking",
                    "PRU": "gave back all of a 0.7% peak and is 1.5% below entry"},
        "target_book": ["AEM", "CGAU"], "equity": 96634.77,
        "ranking": [{"symbol": "CGAU", "score": 1.82}] * 40,
    })],
    "kind IN ('order'": [
        Row(kind="order", symbol="CGAU", summary="BUY 459 CGAU @~21.01: submitted via nautilus",
            ts=TS),
        Row(kind="order", symbol="MET", summary="SELL 105 MET: submitted via nautilus", ts=TS),
        Row(kind="risk", symbol=None, summary="skipped CHEF: position cap", ts=TS),
        Row(kind="error", symbol="AEM", summary="submit failed: insufficient buying power", ts=TS),
    ],
    "COUNT(*)": [Row(value=6)],
    "America/New_York": [Row(value="2026-08-10")],
    "kind = 'error'": [Row(summary="submit failed: insufficient buying power",
                           session="2026-08-10", ts=TS)],
    "exec_position_state": [Row(symbol="MET", entry=94.85, peak=99.95, qty=105, sessions_held=1,
                                sessions_since_high=1, quality="reconstructed", updated_at=TS)],
}


_UNSET = object()


def frame(plan=_UNSET):
    """`plan={}` means an EMPTY database and must not fall back to the full fixture — `plan or FULL`
    treated it as 'unspecified', which silently turned the empty-database test into a duplicate of
    the happy path."""
    return asyncio.run(build(factory(FULL if plan is _UNSET else plan), "MOMENTUM-002"))


def test_the_frame_carries_lifecycle_decision_journal_and_trail():
    f = frame()
    assert f["strategy_id"] == "MOMENTUM-002"
    assert f["session"] == "2026-08-10"
    assert f["lifecycle"]["state"] == "TRADING"
    assert f["decision"]["summary"].startswith("TRADING:")
    assert len(f["journal"]) == 4
    assert f["trail"][0]["symbol"] == "MET"


def test_intent_and_outcome_are_both_present_and_distinguishable():
    """`decision.summary` says 'enter 8'; two of those were cap-blocked and never submitted. Both
    numbers matter, so the frame carries the decision AND a count derived from the order rows — a
    consumer must never have to guess which one it is holding."""
    f = frame()
    assert "enter 8" in f["decision"]["summary"], "intent must survive"
    assert f["submitted_count"] == 6, "outcome must be counted from the order rows"
    assert f["decision_is_today"] is True


def test_exit_reasons_survive_verbatim():
    """These sentences are the system's own explanation of itself — the narrative feature quotes
    them rather than composing its own, so losing or reformatting them here breaks that."""
    assert frame()["decision"]["reasons"]["PRU"] == \
        "gave back all of a 0.7% peak and is 1.5% below entry"


def test_trail_provenance_is_carried():
    """`quality='adopted'` means the peak was never observed, so peak-relative exits are INERT for
    that position (#197 B1). A UI showing a trail without it implies protection that is not running."""
    assert frame()["trail"][0]["quality"] == "reconstructed"


def test_errors_are_surfaced_separately_not_buried_in_the_journal():
    assert frame()["errors"][0]["summary"] == "submit failed: insufficient buying power"


def test_the_ranking_is_bounded():
    """40 ranked names would be published every tick to render a top-few list."""
    assert len(frame()["decision"]["ranking"]) == 15


def test_numeric_columns_come_back_as_numbers():
    """Postgres NUMERIC arrives as Decimal, which is not JSON-serialisable — the bridge would drop
    the whole frame."""
    t = frame()["trail"][0]
    assert isinstance(t["entry"], float) and isinstance(t["qty"], float)
    import json
    json.dumps(frame())  # must not raise


def test_timestamps_are_serialisable():
    assert frame()["lifecycle"]["since"] == TS.isoformat()


# -- degenerate states ------------------------------------------------------------------------------
def test_a_strategy_that_has_never_run_yields_a_frame_not_an_error():
    """First boot. The UI must be able to say 'no session yet' rather than render nothing."""
    f = frame({"exec_strategy_state": [Row(state="PAUSED", reason="never started", updated_at=TS)]})
    assert f["session"] is None
    assert f["decision"] is None
    assert f["journal"] == [] and f["trail"] == []
    assert f["lifecycle"]["state"] == "PAUSED"


def test_a_session_blocked_before_deciding_carries_no_decision():
    """Blocked by a data gate: journal rows exist, no decision row. Reporting a rotation here would
    be inventing one."""
    plan = {**FULL, "kind = 'decision'": []}
    f = frame(plan)
    assert f["decision"] is None and f["session"] is None
    assert f["errors"], "an error must surface even when nothing decided"


def test_an_empty_database_does_not_crash():
    f = frame({})
    assert f["lifecycle"] is None and f["session"] is None


@pytest.mark.parametrize("bad", [None, "not-a-dict"])
def test_a_decision_with_unusable_detail_still_yields_a_frame(bad):
    plan = {**FULL, "kind = 'decision'": [Row(session="2026-08-10", summary="x", ts=TS,
                                              detail=bad)]}
    f = frame(plan)
    assert f["decision"]["reasons"] == {} and f["decision"]["ranking"] == []


def test_the_session_comes_from_the_latest_decision_not_the_max_session_string():
    """Found against the LIVE database: the journal holds rows labelled `2026-08-11` that were
    written on 08-03 — future-dated leftovers from a partially-completed cleanup. `MAX(session)`
    returned those, so the frame reported a session eight days ahead with no decision while the real
    last session had a full one. A homescreen on that says 'no decision today' on a day that traded.
    """
    plan = {**FULL, "kind IN ('order'": [
        Row(kind="risk", symbol=None, summary="pool sources stale or failed", ts=TS)]}
    assert frame(plan)["session"] == "2026-08-10", "took a stale future-dated session label"


def test_a_decision_from_a_previous_session_is_not_reported_as_today():
    """`session` is the last session that DECIDED. If today blocked at a gate, paused, or is a
    holiday, that decision is still the latest — and a homescreen showing it as current would display
    a stale rotation as live. Carrying today's exchange date makes the difference checkable."""
    plan = {**FULL, "America/New_York": [Row(value="2026-08-11")]}
    f = frame(plan)
    assert f["session"] == "2026-08-10"
    assert f["today"] == "2026-08-11"
    assert f["decision_is_today"] is False
