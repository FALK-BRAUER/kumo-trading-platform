"""THE SEAM (#873 phase 1): the engine's timer actually ASKS every registered lane's hooks, folds the
answers, journals a `state` row per TRANSITION, pages per TRANSITION, and publishes a three-state
container on the health frame. Driven through the REAL methods bound to a double that carries only
what they touch — a passing test on `fold()` says nothing about whether anything calls it.

What this file must prove, from the ticket's own list:
- a registered lane with the three hooks IS called by the tick (a record of the attempt, not an
  exception), and its observation `market_aware:<sid>` reads ran-ok; a lane without them reads
  `not_asked` on all three and is still observed;
- the container is three-state: `None` only on a build without the plane; a dict whose `lanes[sid]`
  is None before the first poll; the payload after it; `contract` present/absent NAMED at every stage;
- notify on transition only: fifty polls in EXIT_ONLY → one page; leaving → one page with a different
  key and the entering key cleared so a re-entry pages again; only `emergency_exit` is critical;
- a `fault` writes an `error` row and a WARN page carrying the fault count;
- a journal or notifier that RAISES is counted on the frame, never lost, and never stops the next lane;
- the dwell knob travels: 7, not 3, read back on the frame and in the trigger.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from api.engine_node import UiFeedStrategy
from api.market_aware import EMERGENCY_TRIGGERED, FAULT, HOOKS, NOT_ASKED, YES
from api.observation import Observations


@dataclass(frozen=True)
class _Verdict:
    state: str
    reasons: tuple[str, ...] = ()

    @property
    def acts(self) -> bool:
        return self.state == "yes"


class _Journal:
    """Production's contract, not a convenient one: `PgJournal.write` catches `Exception` and RETURNS
    None on failure — it never raises. `fails=True` reproduces that; `raises=True` exists only to pin
    that a journal which does not follow the contract is counted too."""

    def __init__(self, *, fails: bool = False, raises: bool = False) -> None:
        self.rows: list[dict] = []
        self.fails, self.raises = fails, raises

    async def write(self, kind, summary, *, session, detail=None, symbol=None, correlation=None, slot=None):
        if self.raises:
            raise RuntimeError("postgres away")
        if self.fails:
            return None
        self.rows.append({"kind": kind, "summary": summary, "session": session, "detail": detail, "slot": slot})
        return len(self.rows)


class _Lane:
    """A registered lane: hooks scripted per name (None = not implemented), a journal, a call log.

    PRODUCTION'S SHAPE (#955): the journal is reachable ONLY through the contract's `session_journal()`
    — a Nautilus lane exposes neither `journal` nor `_journal`; the first version of this double did,
    and it is why the poller shipped green while writing no rows on any lane."""

    def __init__(self, *, journal=None, **answers) -> None:
        self.calls: list[str] = []
        self._resolved_journal = journal
        for name, answer in answers.items():
            if answer is None:
                continue

            def hook(_a=answer, _n=name):
                self.calls.append(_n)
                if isinstance(_a, Exception):
                    raise _a
                return _a() if callable(_a) else _a
            setattr(self, name, hook)

    def session_journal(self):
        return self._resolved_journal


class _Notifier:
    def __init__(self, *, raises: bool = False) -> None:
        self.sent: list[tuple[str, object]] = []
        self.cleared: list[str] = []
        self.raises = raises

    async def send(self, key, alert, *, now=None):
        if self.raises:
            raise RuntimeError("telegram away")
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
    """Only what the market-aware methods touch. The methods are the REAL ones."""

    def __init__(self, *, lanes: dict, dwell: int = 3, poll_secs: int = 60, notifier=None, ts_ns: int = 1_789_070_340_000_000_000) -> None:
        self._sibling_strategies = dict(lanes)
        self._observations = Observations()
        self.clock = _Clock(ts_ns)
        self._loop = object()
        self.spawned: list = []
        self._notifier = notifier if notifier is not None else _Notifier()
        UiFeedStrategy._init_market_aware(self, dwell=dwell, poll_secs=poll_secs)
        for sid in lanes:
            self._observations.declare(f"market_aware:{sid}")

    def _spawn(self, coro, what: str):
        self.spawned.append(what)
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


# -- fixture properties -----------------------------------------------------------------------------------

def test_FIXTURE_the_double_binds_the_real_engine_methods_and_the_lane_double_can_raise_and_answer_unknown():
    for name in ("_on_market_aware", "_poll_market_aware", "_emit_market_aware", "_market_aware_frame"):
        assert getattr(_Fake, name) is getattr(UiFeedStrategy, name)
    lane = _Lane(entries_blocked=_Verdict("unknown", ("no panel",)), emergency_exit=RuntimeError("boom"))
    assert lane.entries_blocked().acts is False and lane.entries_blocked().state == "unknown"
    with pytest.raises(RuntimeError):
        lane.emergency_exit()


# -- the seam: the tick asks every registered lane --------------------------------------------------------

def test_the_tick_CALLS_every_hook_of_every_registered_lane_and_the_observation_reads_ran_ok():
    full = _Lane(journal=_Journal(), entries_blocked=_Verdict("no"), emergency_exit=_Verdict("no"),
                 self_assessment=_Verdict("no"))
    bare = _Lane(journal=_Journal())
    fake = _Fake(lanes={"TECHIVOL-005": full, "MOMENTUM-002": bare})
    _tick(fake)
    assert sorted(full.calls) == sorted(HOOKS), "every hook asked, once, on the tick"
    assert bare.calls == []
    s = fake._observations.summary()
    assert s["failing"] == 0 and s["never_ran"] == 0, s
    frame = fake._market_aware_frame()
    assert all(frame["lanes"]["MOMENTUM-002"][h]["state"] == NOT_ASKED for h in HOOKS)
    assert all(frame["lanes"]["TECHIVOL-005"][h]["state"] == "no" for h in HOOKS)


def test_the_container_is_three_state_contract_named_lanes_None_before_the_first_poll():
    fake = _Fake(lanes={"TECHIVOL-005": _Lane(journal=_Journal())}, dwell=7, poll_secs=45)
    before = fake._market_aware_frame()
    assert before is not None, "None is reserved for a build WITHOUT the plane"
    assert before["contract"]["state"] in ("present", "absent") and before["contract"]["module"]
    assert before["lanes"] == {"TECHIVOL-005": None} and before["polled_at_ns"] is None
    assert before["dwell"] == 7 and before["poll_secs"] == 45, "the knobs, at values the defaults cannot produce"
    _tick(fake)
    after = fake._market_aware_frame()
    assert after["lanes"]["TECHIVOL-005"] is not None and after["polled_at_ns"] == fake.clock.timestamp_ns()
    assert after["lanes"]["TECHIVOL-005"]["emergency_exit"]["dwell"] == 7


# -- transitions: journal + page, once ------------------------------------------------------------------------

def test_entering_EXIT_ONLY_writes_one_state_row_and_one_page_then_fifty_quiet_polls():
    j = _Journal()
    lane = _Lane(journal=j, entries_blocked=_Verdict("yes", ("index 0.91 below its 50-session average",)))
    n = _Notifier()
    fake = _Fake(lanes={"TECHIVOL-005": lane}, notifier=n)
    _tick(fake)
    assert len(j.rows) == 1 and j.rows[0]["kind"] == "state" and j.rows[0]["slot"] == "poll"
    assert j.rows[0]["session"] == "2026-09-10", "the ET session date of the poll, not a default slot"
    d = j.rows[0]["detail"]
    assert d["lane"] == "TECHIVOL-005" and d["kind"] == "entries_blocked" and d["hook"] == "entries_blocked"
    assert d["reasons"] == ["index 0.91 below its 50-session average"] and d["previous"] == "unpolled" and d["current"] == "risk_off"
    assert d["reading"] == YES
    assert len(n.sent) == 1
    key, alert = n.sent[0]
    assert key == "market_aware:TECHIVOL-005:entries_blocked:entries_blocked"
    assert alert.critical is False and "TECHIVOL-005" in alert.title and "not opening new positions" in alert.title
    assert "index 0.91 below" in alert.body
    spawned_before = len(fake.spawned)
    _tick(fake, 50)
    assert len(j.rows) == 1 and len(n.sent) == 1, "a page per poll is an alarm that gets muted"
    assert len(fake.spawned) == spawned_before, "a quiet poll does not even hop to the loop"


def test_leaving_pages_on_a_different_key_and_clears_the_entering_key_so_a_re_entry_pages_again():
    answers = {"v": "yes"}
    lane = _Lane(journal=_Journal(), entries_blocked=lambda: _Verdict(answers["v"], ("r",)))
    n = _Notifier()
    fake = _Fake(lanes={"L": lane}, notifier=n)
    _tick(fake)
    answers["v"] = "no"
    _tick(fake)
    assert [k for k, _ in n.sent] == ["market_aware:L:entries_blocked:entries_blocked",
                                      "market_aware:L:entries_blocked:entries_unblocked"]
    assert "market_aware:L:entries_blocked:entries_blocked" in n.cleared, "the entering key is cleared on leave"
    assert all(k.startswith("market_aware:L:entries_blocked:") for k in n.cleared), "only this hook's keys"
    answers["v"] = "yes"
    _tick(fake)
    assert len(n.sent) == 3 and n.sent[-1][0] == "market_aware:L:entries_blocked:entries_blocked"


def test_only_emergency_exit_is_critical_and_it_needs_the_dwell():
    lane = _Lane(journal=_Journal(), emergency_exit=_Verdict("yes", ("crash",)), entries_blocked=_Verdict("yes"),
                 self_assessment=_Verdict("yes"))
    n = _Notifier()
    fake = _Fake(lanes={"L": lane}, notifier=n, dwell=3)
    _tick(fake, 2)
    assert not any("emergency" in k for k, _ in n.sent), "two yes under dwell three cannot page"
    _tick(fake)
    crit = [(k, a) for k, a in n.sent if a.critical]
    assert [k for k, _ in crit] == ["market_aware:L:emergency_exit:emergency_exit"]
    assert all(not a.critical for k, a in n.sent if "emergency" not in k)
    assert "closing its book" in crit[0][1].title and "crash" in crit[0][1].body


def test_a_fault_writes_an_error_row_and_a_WARN_page_carrying_the_fault_count():
    j = _Journal()
    lane = _Lane(journal=j, emergency_exit=RuntimeError("bar deque is empty"))
    n = _Notifier()
    fake = _Fake(lanes={"L": lane}, notifier=n)
    _tick(fake, 3)
    assert [r["kind"] for r in j.rows] == ["error"] and "RuntimeError: bar deque is empty" in j.rows[0]["detail"]["reasons"][0]
    assert len(n.sent) == 1 and n.sent[0][1].critical is False and "raising" in n.sent[0][1].title
    assert fake._market_aware_frame()["lanes"]["L"]["emergency_exit"]["faults"] == 3


def test_FIXTURE_the_REAL_PgJournal_returns_None_on_failure_it_never_raises():
    """The double's `fails=True` claims production's contract; this drives the installed PgJournal with
    a session factory that raises and pins that the caller sees None, not an exception."""
    pgjournal = pytest.importorskip("kumo_strategies.runtime.executor.pgjournal")

    class _Broken:
        def __call__(self):
            raise RuntimeError("postgres away")

    j = pgjournal.PgJournal(sessionmaker=_Broken(), strategy_id="TECHIVOL-005")
    assert asyncio.run(j.write("state", "x", session="2026-09-10", slot="poll")) is None


def test_a_journal_that_FAILS_the_way_production_fails_returning_None_is_counted():
    lane = _Lane(journal=_Journal(fails=True), entries_blocked=_Verdict("yes"))
    n = _Notifier()
    fake = _Fake(lanes={"L": lane}, notifier=n)
    _tick(fake)
    assert fake._market_aware_frame()["emit_failures"] == 1, "None IS the failure — a dead Postgres must not read 0"
    assert len(n.sent) == 1


def test_a_fault_that_resolves_and_recurs_pages_AGAIN_because_the_recovery_cleared_its_key():
    answers = {"v": RuntimeError("boom")}
    lane = _Lane(journal=_Journal(), emergency_exit=lambda: (_ for _ in ()).throw(answers["v"]) if isinstance(answers["v"], Exception) else answers["v"])
    n = _Notifier()
    fake = _Fake(lanes={"L": lane}, notifier=n)
    _tick(fake, 2)
    assert [k for k, _ in n.sent] == ["market_aware:L:emergency_exit:hook_fault"]
    answers["v"] = _Verdict("no")
    _tick(fake)
    assert n.sent[-1][0] == "market_aware:L:emergency_exit:hook_recovered" and n.sent[-1][1].critical is False
    assert "market_aware:L:emergency_exit:hook_fault" in n.cleared
    answers["v"] = RuntimeError("boom again")
    _tick(fake)
    assert [k for k, _ in n.sent].count("market_aware:L:emergency_exit:hook_fault") == 2, "recurrence is news"


def test_a_raising_journal_or_notifier_is_COUNTED_on_the_frame_and_does_not_stop_the_next_lane():
    j_ok = _Journal()
    lanes = {"A": _Lane(journal=_Journal(raises=True), entries_blocked=_Verdict("yes")),
             "B": _Lane(journal=j_ok, entries_blocked=_Verdict("yes"))}
    fake = _Fake(lanes=lanes, notifier=_Notifier(raises=True))
    _tick(fake)
    frame = fake._market_aware_frame()
    assert frame["emit_failures"] >= 2, frame
    assert len(j_ok.rows) == 1, "lane B still journalled"
    assert fake._observations.summary()["failing"] == 0, "the poll itself did not fail — the emit did, and is counted"


def test_a_journal_that_raises_alone_is_counted_ONCE_and_the_page_still_goes_out():
    lane = _Lane(journal=_Journal(raises=True), entries_blocked=_Verdict("yes"))
    n = _Notifier()
    fake = _Fake(lanes={"L": lane}, notifier=n)
    _tick(fake)
    assert fake._market_aware_frame()["emit_failures"] == 1
    assert len(n.sent) == 1, "the journal failing must not swallow the page"


def test_a_lane_without_a_journal_is_a_NAMED_state_not_a_silent_skip():
    lane = _Lane(entries_blocked=_Verdict("yes"))
    fake = _Fake(lanes={"L": lane})
    _tick(fake)
    frame = fake._market_aware_frame()
    assert frame["journal_absent"] == ["L"]


def test_the_dwell_knob_travels_seven_not_three():
    lane = _Lane(journal=_Journal(), emergency_exit=_Verdict("yes"))
    n = _Notifier()
    fake = _Fake(lanes={"L": lane}, notifier=n, dwell=7)
    _tick(fake, 6)
    assert n.sent == []
    _tick(fake)
    assert [k for k, _ in n.sent] == ["market_aware:L:emergency_exit:emergency_exit"]
    assert fake._market_aware_frame()["dwell"] == 7


def test_register_strategy_declares_the_lanes_observation_so_never_ran_is_visible():
    class _Reg:
        def __init__(self):
            self._sibling_strategies = {}
            self._observations = Observations()
            self._trade_cycles = {}
            self._exec_client_id = None
        def _pace_lane(self, strategy): ...
        register_strategy = UiFeedStrategy.register_strategy
    r = _Reg()
    r.register_strategy("TECHIVOL-005", _Lane())
    s = r._observations.summary()
    assert s["declared"] >= 1 and s["never_ran"] >= 1


def test_the_ABSENT_contract_path_is_driven_explicitly_not_left_to_the_environment(monkeypatch):
    """The deployed pins (4d28488, 0fbcfff) do not carry market_events; the dev venv does. The path
    that runs on paper tonight must be exercised by the test, not by whichever module happens to be
    missing: the contract reads absent WITH the module named, the hooks are still polled and read
    not_asked, the container is a dict, and nothing pages."""
    import importlib.util as iu
    monkeypatch.setattr(iu, "find_spec", lambda name: None)
    lane = _Lane(journal=_Journal())
    n = _Notifier()
    fake = _Fake(lanes={"TECHIVOL-005": lane}, notifier=n)
    _tick(fake, 3)
    frame = fake._market_aware_frame()
    assert frame["contract"] == {"state": "absent", "module": "kumo_strategies.strategies.market_events"}
    assert all(frame["lanes"]["TECHIVOL-005"][h]["state"] == NOT_ASKED for h in HOOKS)
    assert n.sent == [] and fake._observations.summary()["failing"] == 0
    assert frame["polled_at_ns"] == fake.clock.timestamp_ns()


def test_the_OLD_two_state_verdict_on_the_deployed_pin_reads_FAULT_through_the_poller_and_pages_WARN():
    """4d28488's market_view has the two-state `Verdict(answer=...)` with no `.acts`. A lane on that pin
    that did implement a hook would hand the poller a truthy dataclass; it must read fault, not yes."""
    class _Old:
        answer = False
        reasons = ()
    lane = _Lane(journal=_Journal(), emergency_exit=_Old())
    n = _Notifier()
    fake = _Fake(lanes={"L": lane}, notifier=n)
    _tick(fake)
    assert fake._market_aware_frame()["lanes"]["L"]["emergency_exit"]["state"] == FAULT
    assert len(n.sent) == 1 and n.sent[0][1].critical is False
