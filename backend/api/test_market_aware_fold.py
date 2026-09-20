"""The ONE transition predicate behind the #873 badge, the journal row and the Telegram page.

Three surfaces derive from one fold so they cannot disagree: the UI badge shows `readings`, the
journal writes a `state` row per EVENT, the notifier pages per EVENT. A poll that produces no event
touches nothing but the reading — that is the rate discipline (Operator: notify on state TRANSITIONS,
never per poll or per slot; a muted alarm is worse than none).

FIVE reading states, not three. `not_asked` (the lane does not implement the hook), `unknown` (the
hook said so — the NORMAL case for a newly registered lane, for months), `fault` (the hook RAISED —
a defect, never folded into unknown), `yes`, `no`. Collapsing raise into unknown would make a broken
view indistinguishable from an honest "not yet", and the honest one is the common case.

The doubles carry the shape kumo-trading-strategies f5895d1 gives `Verdict`/`Assessment`: `.state`, `.acts`,
`.reasons`. Cockpit branches on `.acts` ONLY — `.state` is display. `Assessment(OUT_OF_ENVELOPE,
evidence_sufficient=False)` has `.acts False` and must not trigger; a fold that reads `.state` would
fire on the first bad window, which is precisely the case the threshold exists to suppress.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.market_aware import (
    EMERGENCY_CLEARED, EMERGENCY_TRIGGERED, EXIT_ONLY_ENTERED, EXIT_ONLY_LEFT, FAULT, HOOKS,
    HOOK_FAULT, HOOK_UNKNOWN_TWICE, NO, NOT_ASKED, STAND_DOWN_REQUESTED, UNKNOWN, YES,
    HookReading, as_payload, fold, probe, read_hook,
)


# -- doubles: the shape of kumo-trading-strategies f5895d1, and they CAN say unknown -------------------------

@dataclass(frozen=True)
class _Verdict:
    state: str
    reasons: tuple[str, ...] = ()

    @property
    def acts(self) -> bool:
        return self.state == "yes"


@dataclass(frozen=True)
class _Assessment:
    state: str
    reasons: tuple[str, ...] = ()
    evidence_sufficient: bool = True

    @property
    def acts(self) -> bool:
        return self.state == "out_of_envelope" and self.evidence_sufficient


class _Lane:
    """Answers scripted per hook; `None` for a hook means the lane does not implement it."""

    def __init__(self, **answers):
        self.calls: list[str] = []
        for name, answer in answers.items():
            if answer is None:
                continue

            def hook(_answer=answer, _name=name):
                self.calls.append(_name)
                if isinstance(_answer, Exception):
                    raise _answer
                return _answer
            setattr(self, name, hook)


def test_fixture_property_the_doubles_can_answer_UNKNOWN_and_can_refuse_to_act():
    """If the fixture cannot produce unknown, the unknown path — production's most common path early
    on — is untested. And OUT_OF_ENVELOPE with thin evidence must read as not acting."""
    assert _Verdict("unknown", ("12 sessions, 51 needed",)).acts is False
    assert _Verdict("yes", ("index below",)).acts is True
    assert _Assessment("out_of_envelope", ("sharpe -0.4",), evidence_sufficient=False).acts is False
    assert _Assessment("out_of_envelope", ("sharpe -0.4",)).acts is True
    lane = _Lane(entries_blocked=_Verdict("no"), emergency_exit=None, self_assessment=None)
    assert hasattr(lane, "entries_blocked") and not hasattr(lane, "emergency_exit")


# -- probe and read: five states -------------------------------------------------------------------------

def test_probe_reports_exactly_the_hooks_the_lane_carries():
    assert probe(_Lane()) == frozenset()
    assert probe(_Lane(self_assessment=_Assessment("unknown"))) == frozenset({"self_assessment"})
    assert probe(_Lane(entries_blocked=_Verdict("no"), emergency_exit=_Verdict("no"))) == \
        frozenset({"entries_blocked", "emergency_exit"})
    assert probe(_Lane(entries_blocked=_Verdict("no"), emergency_exit=_Verdict("no"),
                       self_assessment=_Assessment("in_envelope"))) == frozenset(HOOKS)


def test_a_missing_hook_reads_NOT_ASKED_and_is_never_called():
    lane = _Lane()
    r = read_hook(lane, "emergency_exit", ts_ns=5)
    assert r.state == NOT_ASKED and r.reasons == () and lane.calls == []


def test_yes_and_no_come_from_acts_and_carry_the_reason():
    lane = _Lane(emergency_exit=_Verdict("yes", ("index 0.91 below its 50-session average 0.97",)))
    r = read_hook(lane, "emergency_exit", ts_ns=5)
    assert (r.state, r.observed, r.asked_at_ns) == (YES, "yes", 5)
    assert r.reasons == ("index 0.91 below its 50-session average 0.97",)
    assert read_hook(_Lane(emergency_exit=_Verdict("no", ("above",))), "emergency_exit", ts_ns=1).state == NO


def test_an_UNKNOWN_verdict_reads_UNKNOWN_with_its_reason_not_no():
    r = read_hook(_Lane(entries_blocked=_Verdict("unknown", ("no price panel",))), "entries_blocked", ts_ns=1)
    assert r.state == UNKNOWN and r.reasons == ("no price panel",)


def test_an_out_of_envelope_assessment_with_thin_evidence_reads_NO_but_shows_its_observed_state():
    """`.acts` is the single derivation. The badge may still say what was observed."""
    lane = _Lane(self_assessment=_Assessment("out_of_envelope", ("3 windows",), evidence_sufficient=False))
    r = read_hook(lane, "self_assessment", ts_ns=1)
    assert r.state == NO and r.observed == "out_of_envelope"
    sufficient = _Lane(self_assessment=_Assessment("out_of_envelope", ("9 windows",)))
    assert read_hook(sufficient, "self_assessment", ts_ns=1).state == YES


def test_a_RAISING_hook_reads_FAULT_never_unknown_and_names_the_exception():
    lane = _Lane(emergency_exit=RuntimeError("bar deque is empty"))
    r = read_hook(lane, "emergency_exit", ts_ns=9)
    assert r.state == FAULT
    assert r.state != UNKNOWN
    assert "RuntimeError" in r.reasons[0] and "bar deque is empty" in r.reasons[0]


def test_a_hook_returning_something_without_acts_is_a_FAULT_not_a_guess():
    """A bare bool, an old two-state Verdict, a None: none of them carries `.acts`, and reading
    truthiness would turn `Verdict(answer=False)` (a truthy dataclass) into YES."""
    class _OldVerdict:
        answer = False
        reasons = ()
    for bad in (True, False, None, object(), _OldVerdict()):
        r = read_hook(_LaneReturning(bad), "emergency_exit", ts_ns=1)
        assert r.state == FAULT, f"{bad!r} read as {r.state}"


class _LaneReturning:
    def __init__(self, value):
        self._value = value

    def emergency_exit(self):
        return self._value


# -- the fold: events on transitions only ----------------------------------------------------------------

def _poll(prev, *, veto="no", emergency="no", assessment="no", dwell=3, ts=1):
    now = {
        "entries_blocked": HookReading(veto, observed=veto, asked_at_ns=ts),
        "emergency_exit": HookReading(emergency, observed=emergency, asked_at_ns=ts),
        "self_assessment": HookReading(assessment, observed=assessment, asked_at_ns=ts),
    }
    return fold(prev, now, dwell=dwell, ts_ns=ts)


def _kinds(events):
    return [e.kind for e in events]


def test_fixture_property_two_yes_polls_under_dwell_three_cannot_trigger():
    """The trigger must be REACHABLE by the fixture and NOT reached below the dwell, or the
    dwell test below is vacuous."""
    st, ev1 = _poll(None, emergency=YES)
    st, ev2 = _poll(st, emergency=YES)
    assert st.emergency_exit.streak_yes == 2
    assert EMERGENCY_TRIGGERED not in _kinds(ev1) + _kinds(ev2)


def test_emergency_triggers_ONCE_on_the_dwell_th_consecutive_yes_and_not_again_while_it_holds():
    st, _ = _poll(None, emergency=YES)
    st, _ = _poll(st, emergency=YES)
    st, ev3 = _poll(st, emergency=YES)
    assert _kinds(ev3) == [EMERGENCY_TRIGGERED]
    assert ev3[0].hook == "emergency_exit"
    st, ev4 = _poll(st, emergency=YES)
    st, ev5 = _poll(st, emergency=YES)
    assert _kinds(ev4) == [] and _kinds(ev5) == [], "a page per poll is an alarm that gets muted"
    st, ev6 = _poll(st, emergency=NO)
    assert _kinds(ev6) == [EMERGENCY_CLEARED]


def test_dwell_is_the_knob_seven_polls_not_three():
    """A value the default cannot produce, so a fold ignoring `dwell` is caught."""
    st = None
    for _ in range(6):
        st, ev = _poll(st, emergency=YES, dwell=7)
        assert EMERGENCY_TRIGGERED not in _kinds(ev)
    st, ev = _poll(st, emergency=YES, dwell=7)
    assert _kinds(ev) == [EMERGENCY_TRIGGERED]


@pytest.mark.parametrize("breaker", [UNKNOWN, FAULT, NOT_ASKED])
def test_UNKNOWN_FAULT_and_NOT_ASKED_break_a_yes_streak_nothing_acts_on_unknown(breaker):
    st, _ = _poll(None, emergency=YES)
    st, _ = _poll(st, emergency=YES)
    st, ev = _poll(st, emergency=breaker)
    assert EMERGENCY_TRIGGERED not in _kinds(ev)
    assert st.emergency_exit.streak_yes == 0
    st, ev = _poll(st, emergency=YES)
    assert EMERGENCY_TRIGGERED not in _kinds(ev), "the streak restarted; one yes after a gap is not three"


def test_exit_only_pages_on_ENTRY_and_on_LEAVING_never_per_poll():
    st, ev = _poll(None, veto=YES)
    assert _kinds(ev) == [EXIT_ONLY_ENTERED]
    for _ in range(50):
        st, ev = _poll(st, veto=YES)
        assert ev == ()
    st, ev = _poll(st, veto=NO)
    assert _kinds(ev) == [EXIT_ONLY_LEFT], "the recovery is as informative as the onset"


def test_a_veto_that_goes_UNKNOWN_is_not_a_recovery():
    """Leaving EXIT_ONLY means the lane said NO. Unknown is a third thing: the badge changes, the
    veto is not reported cleared, and nothing pages 'recovered'."""
    st, _ = _poll(None, veto=YES)
    st, ev = _poll(st, veto=UNKNOWN)
    assert EXIT_ONLY_LEFT not in _kinds(ev)
    assert st.entries_blocked.state == UNKNOWN


def test_unknown_twice_in_a_row_is_its_own_alert_exactly_once():
    """BROKEN_CHECK_POLLS: a check that cannot answer must report ITSELF, or 'no alarms' reads as
    'nothing wrong'. Once at the second, not again at the third."""
    st, ev1 = _poll(None, emergency=UNKNOWN)
    st, ev2 = _poll(st, emergency=UNKNOWN)
    st, ev3 = _poll(st, emergency=UNKNOWN)
    assert HOOK_UNKNOWN_TWICE not in _kinds(ev1)
    assert _kinds(ev2) == [HOOK_UNKNOWN_TWICE]
    assert _kinds(ev3) == []
    st, _ = _poll(st, emergency=NO)
    st, _ = _poll(st, emergency=UNKNOWN)
    st, ev = _poll(st, emergency=UNKNOWN)
    assert _kinds(ev) == [HOOK_UNKNOWN_TWICE], "a fresh run of two unknowns is news again"


def test_a_FAULT_pages_on_entry_to_fault_once_per_hook():
    st, ev = _poll(None, veto=FAULT)
    assert _kinds(ev) == [HOOK_FAULT] and ev[0].hook == "entries_blocked"
    st, ev = _poll(st, veto=FAULT)
    assert ev == ()


def test_stand_down_is_requested_on_the_assessment_turning_YES_once():
    st, ev = _poll(None, assessment=YES)
    assert _kinds(ev) == [STAND_DOWN_REQUESTED]
    st, ev = _poll(st, assessment=YES)
    assert ev == ()


def test_events_carry_the_reasons_the_hook_gave():
    now = {
        "entries_blocked": HookReading(YES, observed="yes", reasons=("index below its 50-session average",), asked_at_ns=1),
        "emergency_exit": HookReading(NO, observed="no", asked_at_ns=1),
        "self_assessment": HookReading(NOT_ASKED, observed=NOT_ASKED, asked_at_ns=1),
    }
    _, ev = fold(None, now, dwell=3, ts_ns=1)
    assert ev[0].reasons == ("index below its 50-session average",)


# -- the payload: three states at every level ----------------------------------------------------------

def test_the_payload_is_None_before_the_first_poll_and_not_asked_per_absent_hook():
    assert as_payload(None, dwell=3) is None
    st, _ = fold(None, {
        "entries_blocked": HookReading(NOT_ASKED, observed=NOT_ASKED, asked_at_ns=7),
        "emergency_exit": HookReading(NOT_ASKED, observed=NOT_ASKED, asked_at_ns=7),
        "self_assessment": HookReading(NOT_ASKED, observed=NOT_ASKED, asked_at_ns=7),
    }, dwell=3, ts_ns=7)
    p = as_payload(st, dwell=3)
    assert p["entries_blocked"]["state"] == NOT_ASKED and p["emergency_exit"]["state"] == NOT_ASKED
    assert p["self_assessment"]["state"] == NOT_ASKED
    assert p["emergency_exit"]["dwell"] == 3 and p["polled_at_ns"] == 7


def test_the_payload_carries_streak_reasons_and_observed_state():
    st, _ = _poll(None, emergency=YES)
    st, _ = _poll(st, emergency=YES)
    p = as_payload(st, dwell=3)
    assert p["emergency_exit"]["polls"] == 2 and p["emergency_exit"]["state"] == YES
    assert "reasons" in p["emergency_exit"] and "observed" in p["emergency_exit"]


# -- coverage review round 1: non-adjacent transitions, edges, reasons on the way OUT -------------------

def test_every_entering_event_names_its_hook():
    """One fold serves three hooks; an event without its hook is a page that cannot say which
    condition fired, and a badge that lights the wrong chip."""
    st, ev = _poll(None, veto=YES)
    assert ev[0].hook == "entries_blocked"
    st, ev = _poll(None, assessment=YES)
    assert ev[0].hook == "self_assessment"
    st = None
    for _ in range(3):
        st, ev = _poll(st, emergency=YES)
    assert ev[0].hook == "emergency_exit"


def test_a_FIRST_poll_straight_into_FAULT_pages():
    _, ev = _poll(None, emergency=FAULT)
    assert _kinds(ev) == [HOOK_FAULT] and ev[0].hook == "emergency_exit"


def test_leaving_events_carry_the_recovery_reason():
    now_yes = {"entries_blocked": HookReading(YES, observed="yes", reasons=("below",), asked_at_ns=1),
               "emergency_exit": HookReading(NO, observed="no", asked_at_ns=1),
               "self_assessment": HookReading(NOT_ASKED, observed=NOT_ASKED, asked_at_ns=1)}
    st, _ = fold(None, now_yes, dwell=3, ts_ns=1)
    now_no = dict(now_yes, entries_blocked=HookReading(NO, observed="no", reasons=("index above its 50-session average",), asked_at_ns=2))
    _, ev = fold(st, now_no, dwell=3, ts_ns=2)
    assert _kinds(ev) == [EXIT_ONLY_LEFT] and ev[0].reasons == ("index above its 50-session average",)


def test_after_a_trigger_UNKNOWN_or_FAULT_is_a_gap_not_a_recovery_and_not_a_second_episode():
    """yes×3 → triggered. Then unknown: no CLEARED (unknown is not a recovery). Then yes×3 again:
    no second TRIGGERED — the episode never ended. Then NO: CLEARED, once."""
    st = None
    for _ in range(3):
        st, _ = _poll(st, emergency=YES)
    for gap in (UNKNOWN, FAULT):
        st, ev = _poll(st, emergency=gap)
        assert EMERGENCY_CLEARED not in _kinds(ev)
        for _ in range(3):
            st, ev = _poll(st, emergency=YES)
            assert EMERGENCY_TRIGGERED not in _kinds(ev), "still inside the first episode"
    st, ev = _poll(st, emergency=NO)
    assert _kinds(ev) == [EMERGENCY_CLEARED]
    st, ev = _poll(st, emergency=NO)
    assert ev == ()


def test_a_trigger_then_a_recovery_then_a_new_dwell_run_is_a_SECOND_episode():
    st = None
    for _ in range(3):
        st, _ = _poll(st, emergency=YES)
    st, _ = _poll(st, emergency=NO)
    st, ev = _poll(st, emergency=YES)
    st, ev = _poll(st, emergency=YES)
    assert EMERGENCY_TRIGGERED not in _kinds(ev), "dwell counts from zero after a recovery"
    st, ev = _poll(st, emergency=YES)
    assert _kinds(ev) == [EMERGENCY_TRIGGERED]


def test_yes_then_FAULT_then_yes_restarts_the_dwell_from_zero():
    st, _ = _poll(None, emergency=YES)
    st, _ = _poll(st, emergency=YES)
    st, _ = _poll(st, emergency=FAULT)
    st, ev = _poll(st, emergency=YES)
    assert st.emergency_exit.streak_yes == 1 and EMERGENCY_TRIGGERED not in _kinds(ev)


def test_FAULT_after_unknown_twice_pages_again_and_UNKNOWN_after_FAULT_counts_from_one():
    st, _ = _poll(None, veto=UNKNOWN)
    st, ev = _poll(st, veto=UNKNOWN)
    assert _kinds(ev) == [HOOK_UNKNOWN_TWICE]
    st, ev = _poll(st, veto=FAULT)
    assert _kinds(ev) == [HOOK_FAULT]
    st, ev = _poll(st, veto=UNKNOWN)
    assert ev == () and st.entries_blocked.streak_unknown == 1


def test_emergency_and_stand_down_page_on_transition_only_across_fifty_polls():
    st = None
    total = []
    for _ in range(50):
        st, ev = _poll(st, emergency=YES, assessment=YES)
        total += _kinds(ev)
    assert sorted(total) == sorted([STAND_DOWN_REQUESTED, EMERGENCY_TRIGGERED])


def test_dwell_one_triggers_on_the_first_yes_and_dwell_zero_is_REFUSED():
    _, ev = _poll(None, emergency=YES, dwell=1)
    assert _kinds(ev) == [EMERGENCY_TRIGGERED]
    with pytest.raises(ValueError, match="dwell"):
        _poll(None, emergency=YES, dwell=0)


def test_the_payload_after_a_FAULT_carries_the_exception_text_and_each_state_renders_as_itself():
    now = {"entries_blocked": HookReading(FAULT, observed=FAULT, reasons=("RuntimeError: bar deque is empty",), asked_at_ns=1),
           "emergency_exit": HookReading(UNKNOWN, observed="unknown", reasons=("no price panel",), asked_at_ns=1),
           "self_assessment": HookReading(NO, observed="in_envelope", reasons=("fine",), asked_at_ns=1)}
    st, _ = fold(None, now, dwell=3, ts_ns=1)
    p = as_payload(st, dwell=3)
    assert p["entries_blocked"]["state"] == FAULT and "bar deque is empty" in p["entries_blocked"]["reasons"][0]
    assert p["emergency_exit"]["state"] == UNKNOWN and p["emergency_exit"]["reasons"] == ["no price panel"]
    assert p["self_assessment"]["state"] == NO and p["self_assessment"]["observed"] == "in_envelope"
    assert len({p["entries_blocked"]["state"], p["emergency_exit"]["state"], p["self_assessment"]["state"]}) == 3


# -- recovery: a broken hook answering again is its own (INFO) event, once, and never beside another ------

def test_a_hook_that_recovers_from_fault_or_unknown_twice_emits_HOOK_RECOVERED_once_and_not_beside_an_episode_event():
    from api.market_aware import HOOK_RECOVERED
    st, _ = _poll(None, veto=FAULT)
    st, ev = _poll(st, veto=NO)
    assert _kinds(ev) == [HOOK_RECOVERED] and ev[0].previous == FAULT and ev[0].current == "risk_on"
    st, ev = _poll(st, veto=NO)
    assert ev == ()
    st, _ = _poll(st, veto=UNKNOWN)
    st, _ = _poll(st, veto=UNKNOWN)
    st, ev = _poll(st, veto=YES)
    assert _kinds(ev) == [EXIT_ONLY_ENTERED], "an episode opening out of a gap carries the recovery itself"
    st, _ = _poll(None, veto=UNKNOWN)
    st, ev = _poll(st, veto=NO)
    assert ev == (), "one unknown is not a broken hook; answering again is not news"
