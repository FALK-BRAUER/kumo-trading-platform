"""A lane's CADENCE is a fact about the lane and must be readable where liveness is read (#888).

QC345-003 is a MONTHLY rotation: `qc345_rotation._try_decide` -> `_is_rebalance()` -> `rebalance_dates()`,
the first session of each month. Every other session is exit-only and writes no journal row
(issue 157). So on 2026-09-11 `/strategies` read `last_decision 2026-09-01,
sessions_since_decision 7` for a lane that was doing exactly what it should — and the coordinator read
it as a lane dead for seven sessions, inside two minutes, because nothing on the row said "monthly".

The liveness fields (#349) are an alarm calibrated for DAILY lanes. Without the cadence beside them, a
correctly idle monthly lane and a dead daily lane render the same, and the alarm trains its reader to
ignore it — the failure mode #349 exists to prevent, one level up.

Cockpit's half: the registry DECLARES each lane's cadence (a required field — a default is what makes a
missing declaration invisible), `/strategies` carries `cadence`, `cadence_source`, `next_rebalance` and
`next_rebalance_state`, and the book tile shows "monthly · next <date>" beside the lane. The adapter's
half (issue 157) writes a `state` row per exit-only session and will expose its own date; the
`cadence_source` field exists so that when it does, a DISAGREEMENT between the registry and the adapter
is representable rather than a silent overwrite. Two derivations of the rebalance date — cockpit's
weekday calendar here, the adapter's `rebalance_dates()` over its own panel — agree on a plain month and
disagree on a month whose first weekday is a holiday, which is the day this screen would otherwise lie.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date
from typing import ClassVar

import pandas as pd
import pytest

from api.strategy_registry import REGISTRY, StrategyEntry, by_id


def _upstream_rebalances(start: str, end: str) -> list[str]:
    """The adapter's OWN rule over a weekday calendar — the second derivation."""
    from kumo_strategies.strategies.qc345_rotation.engine import rebalance_dates

    return [d.date().isoformat() for d in rebalance_dates(pd.bdate_range(start, end))]


def test_upstream_qc345_is_monthly_and_the_fixture_can_tell_monthly_from_daily():
    """FIXTURE PROPERTY FIRST. If the adapter's rule returned every session, `monthly` would be a label
    cockpit invented and nothing below could contradict it. Four months of weekdays -> four dates, one
    per month, each the first weekday: 2026-11-01 is a SUNDAY, so November's is the 2nd — the case a
    naive "first of the month" gets wrong."""
    got = _upstream_rebalances("2026-09-01", "2026-12-31")
    assert got == ["2026-09-01", "2026-10-01", "2026-11-02", "2026-12-01"], got
    assert len(pd.bdate_range("2026-09-01", "2026-12-31")) > 80, "a daily rule would have returned ~87"


# --- the declaration -------------------------------------------------------------------------------


def test_cadence_is_a_REQUIRED_field_so_a_new_lane_cannot_omit_it():
    """THE CLASS GUARD, aimed at the class. Iterating today's REGISTRY proves nothing about the lane
    added tomorrow: with `cadence: str = "daily"` the omission is legal and renders daily, which IS the
    #888 misreading. So pin the dataclass property itself, and the TypeError an omission raises."""
    field = {f.name: f for f in dataclasses.fields(StrategyEntry)}["cadence"]
    assert field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING, (
        "`cadence` has a default — a lane declared without one silently renders as that default"
    )
    with pytest.raises(TypeError):
        StrategyEntry(name="NEW", tag="099", settings_domain="", external_id="NEW", title="no cadence",  # type: ignore[call-arg]
                      protection="trail")


def test_every_registered_strategy_declares_a_cadence_from_the_closed_set():
    from api.strategy_registry import CADENCES

    for entry in REGISTRY:
        assert entry.cadence in CADENCES, (
            f"{entry.strategy_id} declares cadence {entry.cadence!r}, not one of {sorted(CADENCES)}"
        )


def test_qc345_is_declared_monthly_and_every_other_automated_lane_daily():
    """The declaration must AGREE with the adapter (proved monthly above), and the daily lanes must not
    be swept up by a lazy default. MANUAL has no schedule at all, which is its own state."""
    assert by_id("QC345-003").cadence == "monthly"
    for sid in ("MOMENTUM-002", "BCTROT-004", "TECHIVOL-005", "CRSISHORT-006"):
        assert by_id(sid).cadence == "daily", sid
    assert by_id("MANUAL-001").cadence == "manual"


def test_validate_refuses_a_cadence_outside_the_set_and_the_module_validates_itself_at_import():
    """`validate()` had NO production caller — every check in it was reachable from tests only, so a
    registry that would not boot was refused nowhere a boot happens. The docstring promises "at import
    time"; make it true, and make cadence one of the things it refuses."""
    import ast
    import pathlib

    from api.strategy_registry import RegistryError, validate

    bad = (StrategyEntry(name="X", tag="098", settings_domain="", external_id="X", title="x", cadence="weekly",
                         protection="trail"),)
    with pytest.raises(RegistryError, match="cadence"):
        validate(bad)

    src = pathlib.Path(__file__).parent.joinpath("strategy_registry.py").read_text()
    module_level_calls = [
        node.value.func.id
        for node in ast.parse(src).body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "validate" in module_level_calls, "the registry does not validate itself at import"


# --- the date -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("last", "today", "expected"),
    [
        # The measured case: decided on the September rebalance, read mid-September -> first October
        # session. This is the value that would have made the coordinator's reading impossible.
        ("2026-09-01", date(2026, 9, 11), "2026-10-01"),
        # ON the rebalance day, before the slot has fired: today IS the next rebalance.
        ("2026-09-01", date(2026, 10, 1), "2026-10-01"),
        # ON the rebalance day, after it decided: the next one, and November's first weekday is the
        # 2nd (the 1st is a Sunday) — a bare `.replace(day=1)` would report a day the market is shut.
        ("2026-10-01", date(2026, 10, 1), "2026-11-02"),
        # A weekend before the month's first weekday: the coming Monday, not yesterday's Sunday.
        ("2026-10-01", date(2026, 11, 1), "2026-11-02"),
        # Never decided, read mid-month: this month's rebalance is PAST, so next month's.
        (None, date(2026, 9, 11), "2026-10-01"),
        # Never decided, read on the 1st: today.
        (None, date(2026, 12, 1), "2026-12-01"),
        # YEAR ROLLOVER — a `month + 1` without carrying the year is the classic December defect.
        ("2026-12-01", date(2026, 12, 15), "2027-01-01"),
        # A mid-month decision (a forced rebalance, kumo-trading-strategies' `force_rebalance` path) is still a
        # decision for THIS month: the next scheduled one is next month's.
        ("2026-09-15", date(2026, 9, 20), "2026-10-01"),
        # The ledger AHEAD of our clock (skew, or a session written in the venue's tomorrow): the
        # month it decided in is spent, whichever clock is right.
        ("2026-10-01", date(2026, 9, 30), "2026-11-02"),
        # FAR ahead — two months, then nine. The first version stepped from today's month under a
        # `range(3)` bound and RAISED past it, and the endpoint did not catch that, so one ledger row
        # ~52 days ahead took every lane off the screen (implementation review). Bounded by
        # construction now: start from the day after the decision.
        ("2026-11-02", date(2026, 9, 11), "2026-12-01"),
        ("2027-06-15", date(2026, 9, 11), "2027-07-01"),
    ],
)
def test_next_rebalance_is_the_first_weekday_of_the_next_month_not_yet_decided(last, today, expected):
    from api.app import _next_rebalance

    assert _next_rebalance("monthly", last, today) == expected


def test_non_rotation_cadences_have_no_date_and_an_unknown_cadence_is_refused_not_None():
    """`None` is legitimate ONLY for a cadence that has no rebalance calendar — and it is readable only
    because `cadence` sits beside it. An UNKNOWN cadence returning `None` would be the two-state trap
    again (looks like daily); it raises, and the registry guard above keeps it from ever reaching here."""
    from api.app import _next_rebalance

    assert _next_rebalance("daily", "2026-09-10", date(2026, 9, 11)) is None
    assert _next_rebalance("manual", None, date(2026, 9, 11)) is None
    with pytest.raises(ValueError, match="cadence"):
        _next_rebalance("weekly", None, date(2026, 9, 11))


def test_every_declarable_cadence_has_a_date_arm():
    """CLOSED SET, BOTH ENDS. `CADENCES` gates what a lane may declare; `_next_rebalance` has an arm
    per value and raises for anything else. Add `quarterly` to the set alone and `validate()` accepts
    it, the registry test accepts it, and `/strategies` 500s the day a lane declares it (delta review).
    The set's docstring said so in prose; this is the test."""
    from api.app import _next_rebalance
    from api.strategy_registry import CADENCES

    for cadence in sorted(CADENCES):
        _next_rebalance(cadence, "2026-09-01", date(2026, 9, 11))  # must not raise


def test_an_unreadable_last_decision_raises_rather_than_guessing():
    """BOTH readings raise. `_sessions_since` used to return 0 for an unparseable session — "decided
    today", the healthiest value there is, for a row that cannot be read — and would have sat beside
    `unreadable_ledger` on the same row (implementation review). The endpoint names the state instead
    (tested at the seam below)."""
    from api.app import UnreadableSession, _next_rebalance, _sessions_since

    for bad in ("", "n/a", "2026-13-40"):
        with pytest.raises(UnreadableSession):
            _next_rebalance("monthly", bad, date(2026, 9, 11))
        with pytest.raises(UnreadableSession):
            _sessions_since(bad, date(2026, 9, 11))


def test_the_row_clock_is_the_LEDGERS_calendar_eastern_time_not_the_containers_utc_date():
    """Session dates are ET; the container runs UTC. At 01:30 UTC on the 11th it is 21:30 ET on the
    10th, and a `date.today()` clock would count the 11th as a missed session all evening and could name
    a rebalance a day early. The instant is chosen so the two calendars DISAGREE — agreement at noon
    would prove nothing."""
    from datetime import datetime

    from api import app as app_mod

    class _Frozen:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 11, 1, 30, tzinfo=UTC).astimezone(tz)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_mod, "datetime", _Frozen)
        assert app_mod._today() == date(2026, 9, 10)


def test_the_registry_is_validated_when_EITHER_process_boots_not_on_the_first_request():
    """`strategy_registry` self-checks on import — but every importer was lazy, inside a function, and
    the engine's sits under a try/except that logs and continues. So the first refusal would have
    surfaced on the first `/strategies` request, or been swallowed on the engine (implementation
    review). Both process entry modules import it at module level."""
    import ast
    import pathlib

    here = pathlib.Path(__file__).parent
    for entry in ("app.py", "engine_node.py"):
        tree = ast.parse(here.joinpath(entry).read_text())
        eager = [
            n for n in tree.body
            if (isinstance(n, ast.Import) and any(a.name == "api.strategy_registry" for a in n.names))
            or (isinstance(n, ast.ImportFrom) and n.module == "api.strategy_registry")
        ]
        assert eager, f"{entry} does not import api.strategy_registry at module level — a bad registry boots"


def test_cockpits_calendar_agrees_with_the_adapters_rule_on_a_plain_month():
    """TWO DERIVATIONS OF ONE FACT. Cockpit's weekday calendar and the adapter's `rebalance_dates()`
    must agree on October 2026 (the 1st is a Thursday) and November (the 1st is a Sunday). Both share
    the weekday assumption; only the adapter's month rule is independent here. Where they would
    DISAGREE — a month whose first weekday is a market holiday — is precisely the day the tile would
    otherwise name a session that never happens; that disagreement is the detector, not a bug."""
    from api.app import _next_rebalance

    upstream = _upstream_rebalances("2026-09-01", "2026-11-30")
    assert upstream[1] == _next_rebalance("monthly", upstream[0], date(2026, 9, 11))
    assert upstream[2] == _next_rebalance("monthly", upstream[1], date(2026, 10, 1))


# --- the seam -------------------------------------------------------------------------------------


def _drive_strategies(monkeypatch, *, today: date, decided: dict[str, dict],
                      health: dict | None = None) -> dict[str, dict]:
    """Drive the REAL endpoint through the doubles it reads: the sleeve book, the decision ledger, the
    engine frame and the clock. `load_book`/`last_decisions`/`session_factory` are imported INSIDE
    `get_strategies`, so patching the source modules binds at call time."""
    import asyncio
    from contextlib import asynccontextmanager

    from api import app as app_mod
    from api import budget_store
    from api.budget import Sleeve
    from api.db import engine as db_engine

    class _Book:
        sleeves: ClassVar[dict] = {"QC345-003": Sleeve("QC345-003", 20_000.0, 20_000.0)}

    @asynccontextmanager
    async def _session():
        yield object()

    async def _load_book(_session):
        return _Book()

    async def _last_decisions(_session):
        return decided

    frame = {"armed_lanes": {"QC345-003": True}, "next_fire_ns": {}} if health is None else health

    class _Node:
        def health(self):
            return frame

    monkeypatch.setattr(db_engine, "session_factory", _session)
    monkeypatch.setattr(budget_store, "load_book", _load_book)
    monkeypatch.setattr(budget_store, "last_decisions", _last_decisions)
    monkeypatch.setattr(app_mod, "_today", lambda: today)
    monkeypatch.setattr(app_mod.app.state, "node", _Node(), raising=False)

    body = asyncio.run(app_mod.get_strategies())
    return {r["strategy_id"]: r for r in body["strategies"]}


def test_the_strategies_endpoint_serves_cadence_and_next_rebalance_per_lane(monkeypatch):
    """A registry field and a helper that nothing wires into the row is the "computed, contracted,
    unreachable" state test_armed_is_served exists for."""
    rows = _drive_strategies(
        monkeypatch,
        today=date(2026, 9, 11),
        decided={
            "QC345-003": {"session": "2026-09-01", "slot": "open+5m"},
            "MOMENTUM-002": {"session": "2026-09-10", "slot": "open+5m"},
        },
    )
    qc = rows["QC345-003"]
    assert qc["cadence"] == "monthly"
    assert qc["cadence_source"] == "registry"
    assert qc["next_rebalance"] == "2026-10-01"
    assert qc["next_rebalance_state"] == "scheduled"

    mo = rows["MOMENTUM-002"]
    assert mo["cadence"] == "daily"
    assert mo["next_rebalance"] is None
    assert mo["next_rebalance_state"] == "not_a_rotation"

    # EVERY row carries all four keys — a reader must never infer "daily" from an absent field.
    for sid, row in rows.items():
        for key in ("cadence", "cadence_source", "next_rebalance", "next_rebalance_state"):
            assert key in row, f"{sid} lacks {key}"


def test_the_liveness_count_and_the_rebalance_date_read_ONE_clock(monkeypatch):
    """Pinned with a `today` FAR from the wall clock. With today = 2026-09-11 (the day this was written)
    `sessions_since_decision == 8` is what a leftover `date.today()` returns anyway, so that case would
    pass today and fail tomorrow — a time bomb, not a test. March 2026: 03-03..03-10 is six weekdays and
    the next rebalance is April 1st; a row built on two clocks cannot produce both."""
    rows = _drive_strategies(
        monkeypatch,
        today=date(2026, 3, 10),
        decided={"QC345-003": {"session": "2026-03-02", "slot": "open+5m"}},
    )
    qc = rows["QC345-003"]
    assert qc["sessions_since_decision"] == 6
    assert qc["next_rebalance"] == "2026-04-01"


def test_a_corrupt_ledger_row_is_its_own_state_on_the_row_not_a_500_and_not_a_date(monkeypatch):
    """Three states. The screen answers other questions (sleeves, armed) and must not 500 because one
    lane's ledger row is unreadable — but that lane's date must not be guessed either. Named."""
    rows = _drive_strategies(
        monkeypatch,
        today=date(2026, 9, 11),
        decided={"QC345-003": {"session": "n/a", "slot": "open+5m"}},
    )
    qc = rows["QC345-003"]
    assert qc["cadence"] == "monthly"
    assert qc["next_rebalance"] is None
    assert qc["next_rebalance_state"] == "unreadable_ledger"
    # NOT "0 sessions since decision" beside it — the same unreadable date, the same unknown.
    assert qc["sessions_since_decision"] is None
    assert qc["last_decision"] == "n/a", "the raw value stays visible so the operator can see what is wrong"


def test_the_row_reads_cadence_from_the_REGISTRY_ENTRY_not_from_a_table_that_agrees_with_it(monkeypatch):
    """SURVIVED THE FIRST MUTATION ROUND. A row builder reading `{"QC345-003": "monthly"}.get(id,
    "daily")` passed every test above, because a private table that AGREES with the registry is
    indistinguishable from reading the registry — agreement is not connection. So the endpoint is
    driven against a registry the table cannot know: one synthetic monthly lane, a value the default
    could not produce."""
    from api import strategy_registry

    synthetic = (
        StrategyEntry(name="ROTATEST", tag="097", settings_domain="", external_id="ROTATEST",
                      title="synthetic monthly lane for #888", cadence="monthly", protection="trail"),
    )
    monkeypatch.setattr(strategy_registry, "REGISTRY", synthetic)
    rows = _drive_strategies(monkeypatch, today=date(2026, 9, 11), decided={})
    assert set(rows) == {"ROTATEST-097"}, "the endpoint did not iterate the patched registry — this test is blind"
    assert rows["ROTATEST-097"]["cadence"] == "monthly"
    assert rows["ROTATEST-097"]["next_rebalance"] == "2026-10-01"
