"""The market-aware plane writes its journal rows through the LANE's own resolver, `session_journal()`,
not through a hand-rolled attribute chain (#955).

Measured on paper 2026-09-11 after the b930bd2 recreate: `/health.market_aware.journal_absent` lists ALL
FOUR lanes. `_emit_market_aware` resolved the journal as
`getattr(lane, "journal", None) or getattr(lane, "_journal", None)`; a production lane (a Nautilus
`Strategy` subclass from kumo-strategies) exposes NEITHER — its runner holds the journal, and the
contract ships `session_journal()` (`runtime/nautilus/contract.py:709`) written for exactly this
defect (kumo-cockpit#587: "QC345 returned None and wrote nothing for the lane's entire life").
So every market-aware event — including today's fault pages — PAGED BUT DID NOT JOURNAL, on every
lane, on both instances.

Three things this file pins, and the second is the one that gets skipped:
 1. the poller calls `session_journal()` — never a third attribute name (a third name IS the drift);
 2. `journal_absent` is evaluated for EVERY registered lane on EVERY poll, so absence from the list
    means "has a journal", never "was not asked" (it was populated only inside the emit path before);
 3. the fixture is a REAL lane object built by cockpit's own builder: a double exposing `journal` is a
    double that cannot represent production, and it is why this shipped green.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from api.engine_node import UiFeedStrategy
from api.market_aware import HOOKS
from api.observation import Observations


# -- 3. a REAL lane, from cockpit's own builder -------------------------------------------------------

def _real_lane(monkeypatch):
    """`CrsiShortStrategy` built by `build_crsi_short_strategy` through the harness the builder's own
    tests use. Skips BY NAME on a pin that lacks the adapter."""
    pytest.importorskip("kumo_strategies.runtime.nautilus.crsi_short")
    from strategies import crsi_short
    from strategies import test_crsi_short_builder as h
    h._stub_store_and_calendar(monkeypatch)
    h._settings(monkeypatch, CRSI_BORROW_GATE_OFF=True)
    lane = crsi_short.build_crsi_short_strategy(feed=h._Feed("ibkr"))
    assert lane is not None, "the builder returned None — the gate or a refusal fired; see its own tests"
    return lane


def test_FIXTURE_a_production_lane_exposes_NEITHER_name_the_poller_looked_for_and_DOES_answer_session_journal(monkeypatch):
    lane = _real_lane(monkeypatch)
    assert getattr(lane, "journal", None) is None and getattr(lane, "_journal", None) is None, \
        "the hand-rolled chain would have found something — the premise of #955 has changed"
    assert callable(getattr(lane, "session_journal", None)), "the contract's resolver is missing on this pin"
    assert lane.session_journal() is not None, "the real lane resolves its journal through session_journal()"


def test_FIXTURE_every_runtime_lane_class_cockpit_registers_carries_session_journal():
    """The class, not the instance: MOMENTUM/BCTROT/QC345/CRSISHORT all inherit the contract's resolver."""
    mods = {"momentum_rotation": "MomentumRotationStrategy", "qc345_rotation": "QC345RotationStrategy",
            "crsi_short": "CrsiShortStrategy"}
    seen = 0
    for mod, cls in mods.items():
        m = pytest.importorskip(f"kumo_strategies.runtime.nautilus.{mod}")
        assert callable(getattr(getattr(m, cls), "session_journal", None)), f"{cls} has no session_journal()"
        seen += 1
    assert seen == 3


# -- doubles shaped like production: the journal is reachable ONLY through session_journal() ------------

class _Journal:
    def __init__(self, *, fails: bool = False) -> None:
        self.rows: list[dict] = []
        self.fails = fails

    async def write(self, kind, summary, *, session, detail=None, symbol=None, correlation=None, slot=None):
        if self.fails:
            return None
        self.rows.append({"kind": kind, "summary": summary, "session": session, "detail": detail, "slot": slot})
        return len(self.rows)


@dataclass(frozen=True)
class _Verdict:
    state: str
    reasons: tuple[str, ...] = ()

    @property
    def acts(self) -> bool:
        return self.state == "yes"


class _Lane:
    """Production's shape: NO `journal`/`_journal` attribute; `session_journal()` answers (or None)."""

    def __init__(self, journal, **answers) -> None:
        self._resolved = journal
        for name, answer in answers.items():
            if answer is not None:
                setattr(self, name, (lambda _a=answer: _a))

    def session_journal(self):
        return self._resolved


class _NoResolver:
    """A lane object on a pin without the contract's resolver: no attribute at all."""
    def emergency_exit(self):
        return _Verdict("no")


class _Notifier:
    def __init__(self) -> None:
        self.sent: list = []
        self.cleared: list = []

    async def send(self, key, alert, *, now=None):
        self.sent.append((key, alert))
        return True

    def clear(self, key) -> None:
        self.cleared.append(key)


class _Clock:
    def __init__(self, ns: int) -> None:
        self._ns = ns

    def timestamp_ns(self) -> int:
        return self._ns


class _Fake:
    def __init__(self, *, lanes: dict, dwell: int = 3) -> None:
        self._sibling_strategies = dict(lanes)
        self._observations = Observations()
        self.clock = _Clock(1_789_070_340_000_000_000)
        self._loop = object()
        self._notifier = _Notifier()
        UiFeedStrategy._init_market_aware(self, dwell=dwell, poll_secs=60)
        for sid in lanes:
            self._observations.declare(f"market_aware:{sid}")

    def _spawn(self, coro, what: str):
        return asyncio.run(coro)

    def _safe_now(self) -> int:
        return self.clock.timestamp_ns()

    def _market_aware_notifier(self):
        return self._notifier

    _on_market_aware = UiFeedStrategy._on_market_aware
    _poll_market_aware = UiFeedStrategy._poll_market_aware
    _emit_market_aware = UiFeedStrategy._emit_market_aware
    _market_aware_frame = UiFeedStrategy._market_aware_frame
    _market_aware_journal_of = UiFeedStrategy._market_aware_journal_of
    _market_aware_journal_resolve = UiFeedStrategy._market_aware_journal_resolve


def _tick(fake: _Fake, n: int = 1) -> None:
    for _ in range(n):
        fake.clock._ns += 60_000_000_000
        fake._on_market_aware(None)


def test_a_transition_on_a_production_shaped_lane_WRITES_its_row_through_session_journal():
    j = _Journal()
    lane = _Lane(j, entries_blocked=_Verdict("yes", ("index below its 50-session average",)))
    fake = _Fake(lanes={"TECHIVOL-005": lane})
    _tick(fake)
    assert len(j.rows) == 1 and j.rows[0]["kind"] == "state" and j.rows[0]["slot"] == "poll", j.rows
    assert fake._market_aware_frame()["journal_absent"] == []


def test_journal_absent_is_evaluated_for_EVERY_lane_on_EVERY_poll_not_only_lanes_that_had_events():
    """The detector lied in the safe direction: only a lane with events was ever checked, so a quiet
    lane with no journal was ABSENT from `journal_absent` and read as 'has one'."""
    lanes = {"A": _Lane(None, entries_blocked=_Verdict("no")),          # no journal, no events
             "B": _Lane(_Journal(), entries_blocked=_Verdict("no")),    # journal, no events
             "C": _NoResolver()}                                        # no resolver at all, no events
    fake = _Fake(lanes=lanes)
    before = fake._market_aware_frame()
    assert before["journal_absent"] == [] and before["journal_unchecked"] == ["A", "B", "C"], "three states: unpolled is in NEITHER list"
    _tick(fake)
    after = fake._market_aware_frame()
    assert after["journal_absent"] == ["A", "C"] and after["journal_unchecked"] == []
    # and it TRACKS: a lane that gains a journal leaves the list on the next poll
    lanes["A"]._resolved = _Journal()
    _tick(fake)
    assert fake._market_aware_frame()["journal_absent"] == ["C"]


def test_the_poller_resolves_ONLY_through_session_journal_never_a_third_attribute_name():
    """A lane carrying a `journal` attribute but NO resolver is not a production shape and must not be
    found by a local chain — a third name is the drift this ticket exists to end."""
    class _OnlyAttr:
        def __init__(self):
            self.journal = _Journal()
        def entries_blocked(self):
            return _Verdict("yes", ("r",))
    lane = _OnlyAttr()
    fake = _Fake(lanes={"L": lane})
    _tick(fake)
    assert fake._market_aware_frame()["journal_absent"] == ["L"]
    assert lane.journal.rows == [], "the row must not be written through an attribute the contract does not define"
    import inspect
    src = (inspect.getsource(UiFeedStrategy._market_aware_journal_of)
           + inspect.getsource(UiFeedStrategy._market_aware_journal_resolve)
           + inspect.getsource(UiFeedStrategy._emit_market_aware))
    assert 'getattr(lane, "journal"' not in src and 'getattr(lane, "_journal"' not in src


def test_a_journal_that_returns_None_on_write_is_still_a_counted_failure_with_the_resolver():
    lane = _Lane(_Journal(fails=True), entries_blocked=_Verdict("yes"))
    fake = _Fake(lanes={"L": lane})
    _tick(fake)
    assert fake._market_aware_frame()["emit_failures"] == 1 and fake._market_aware_frame()["journal_absent"] == []


def test_a_RAISING_resolver_is_journal_BROKEN_not_journal_absent():
    """Cross-review of #958 (staging2 session): a resolver that RAISES and a lane that genuinely has
    no journal are DIFFERENT CONDITIONS with different fixes — a wiring defect in the lane vs a pin
    without the contract. Filing both under `journal_absent` rebuilds the two-states-for-three-things
    defect inside the detector written to end it. Third state on the frame: `journal_broken`
    {sid: "<ExcType>: <msg>"}, disjoint from `journal_absent`, and NOT unchecked."""
    class _Raises:
        def session_journal(self):
            raise TypeError("journal is not wired: runner has no 'journal'")
        def entries_blocked(self):
            return _Verdict("no")
    lane = _Raises()
    with pytest.raises(TypeError):        # fixture property: the resolver really raises
        lane.session_journal()
    lanes = {"A": _Lane(None, entries_blocked=_Verdict("no")),   # absent: resolver returns None
             "R": lane,                                           # broken: resolver raises
             "B": _Lane(_Journal(), entries_blocked=_Verdict("no"))}
    fake = _Fake(lanes=lanes)
    before = fake._market_aware_frame()
    assert before["journal_broken"] == {} and before["journal_unchecked"] == ["A", "B", "R"]
    _tick(fake)
    after = fake._market_aware_frame()
    assert after["journal_broken"] == {"R": "TypeError: journal is not wired: runner has no 'journal'"}
    assert after["journal_absent"] == ["A"], "broken is NOT absent"
    assert after["journal_unchecked"] == []
    # and it TRACKS: a resolver that stops raising leaves `journal_broken` on the next poll
    lane.session_journal = lambda: _Journal()
    _tick(fake)
    assert fake._market_aware_frame()["journal_broken"] == {} and fake._market_aware_frame()["journal_absent"] == ["A"]
