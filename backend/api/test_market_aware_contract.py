"""The notification vocabulary is a MIRROR of kumo_strategies.strategies.market_events — pinned equal
to the installed contract whenever it is present, so a kind, label, severity or reserved key added
upstream fails HERE rather than reaching the operator's phone as a blank badge or a page naming the wrong
strategy (#873).

Why a mirror and not an import: no deployed strategies pin carries the module (4d28488 and 0fbcfff
both lack it; only strategies main has it) and the #903 class guard bans a module-scope import of
the package. Two derivations of one vocabulary WILL drift — which is exactly why the equality test
below exists and why it must SKIP BY NAME rather than pass when the contract is absent.

The contract's rules this file enforces on cockpit's side, verbatim from the module docstring:
an event is a transition, not a state; severity is a budget (only EMERGENCY_EXIT pages); the
payload is flat, JSON-safe, carries BOTH states and `reasons` in full; a `detail` key colliding with
the event's own fields RAISES rather than overwrites.
"""
from __future__ import annotations

import datetime
import importlib
import json
import pathlib

import pytest

from api import market_aware as ma
from api.market_aware import (
    ACTIONS, COCKPIT_KINDS, CONTRACT_KINDS, CONTRACT_LABELS, CONTRACT_SEVERITY, CRITICAL, EVENT_KINDS,
    FAULT, HOOKS, LABELS, NO, NOTIFY, RESERVED, RESERVED_CONTRACT, SEVERITY, ASSESSMENT_RECOVERED, STAND_DOWN_REQUESTED,
    UNPOLLED, YES, Event, HookReading, as_payload, contract_state, fold, notification, poll_lane, condition_of,
    RISK_ON, RISK_OFF, IN_ENVELOPE, OUT_OF_ENVELOPE, UNKNOWN, EMERGENCY_CLEARED, EXIT_ONLY_LEFT, CONDITION_STATES,
)


def _now(**states) -> dict[str, HookReading]:
    return {h: HookReading(states.get(h, NO), observed=states.get(h, NO), asked_at_ns=1) for h in HOOKS}


# -- the mirror, against the installed contract ---------------------------------------------------------

def _installed():
    try:
        return importlib.import_module(ma.CONTRACT_MODULE)
    except ImportError:
        pytest.skip(f"installed kumo-strategies does not carry {ma.CONTRACT_MODULE} — the mirror cannot be "
                    f"checked on this pin; it is checked on every pin that carries it")


def _market_view_or_skip(*names: str):
    """The three-state `market_view` (f5895d1+). The deployed pins (4d28488, 0fbcfff) carry an older
    module without these names; the check runs on every build that has them and SKIPS BY NAME on the
    pin — never passes for the wrong reason."""
    mv = importlib.import_module("kumo_strategies.strategies.market_view")
    missing = [n for n in names if not hasattr(mv, n)]
    if missing:
        pytest.skip(f"installed kumo-strategies market_view predates the three-state contract (missing {missing}) — "
                    f"the deployed pins; checked on every build that carries it")
    return mv


#: THE INVESTIGATE SKIP EXPIRES ON A DATE, NOT ON A LIST OF REVISIONS (coordinator, 2026-09-11 08:53Z).
#: The merge gate's pin run printed "skipped: editable 7 vs pin 8" three times (PRs #970, #968, #958)
#: with the delta explained nowhere — the one test is the RESTRICTS_TRADING implication below, which
#: skipped by name on any tree lacking `SelfAction.INVESTIGATE`, for ever. The first fix bounded the
#: skip to two NAMED revisions; that premise ("the pin moves only when ks#182 lands") was false within
#: the hour — the pin moved to 06f3055 for ks#177/#185 — and the bound grew to three, which is an
#: allowlist growing one entry at a time. So: the skip is keyed on the CAPABILITY (installed
#: SelfAction has no INVESTIGATE, by name) and bounded by a DATE. As of 2026-09-11 ks#182 was unmerged
#: and INVESTIGATE was absent from every pin in force (e600383, 84d09d3, 06f3055). ONE WEEK: ks#182 is
#: on l21's unpushed commits today and its remaining work (the quarantine trigger) is scoped for this
#: week; a longer window would let "known" become permanent, a shorter one would fire on a normal review
#: cycle. Nothing here names a revision, so a pin bump for any reason cannot drift it.
_INVESTIGATE_SKIP_EXPIRES = datetime.date(2026, 9, 18)


def _today() -> datetime.date:
    """Wall-clock, isolated so the mechanism test can move the calendar."""
    return datetime.date.today()


def _installed_strategies_rev() -> str:
    """The revision the interpreter imports kumo_strategies from — THE GATE'S OWN derivation
    (`scripts/merge_gate.py::resolved_strategies_rev`, the number it prints beside its counts), imported
    rather than re-derived: one git call, in the gate, so the two cannot name different trees — and no
    test file reads a moving ref itself (api/test_no_test_reads_a_moving_git_ref.py). Named in the
    skip/fail sentences only; never a condition."""
    import sys
    from pathlib import Path
    from scripts.merge_gate import BACKEND, resolved_strategies_rev
    return resolved_strategies_rev(Path(sys.executable), cwd=BACKEND)


def _skip_only_until_the_expiry_when_INVESTIGATE_is_absent(mv) -> None:
    """Three answers: INVESTIGATE present → run; absent before the expiry → SKIP naming the installed
    revision and the date; absent on or after the expiry → FAIL naming both possibilities, including
    that this expiry itself is the bug. The merge gate's PIN run is the authority; a red on the editable
    tree alone is that tree's state (#966), not the pin's."""
    if hasattr(mv.SelfAction, "INVESTIGATE"):
        return
    rev = _installed_strategies_rev()
    if _today() < _INVESTIGATE_SKIP_EXPIRES:
        pytest.skip(f"installed kumo-strategies {rev} has no SelfAction.INVESTIGATE (ks#182 unmerged as of "
                    f"2026-09-11); this skip EXPIRES on {_INVESTIGATE_SKIP_EXPIRES.isoformat()}")
    pytest.fail(f"as of 2026-09-11 ks#182 was unmerged and INVESTIGATE was absent from every pin in force; it is "
                f"{_today().isoformat()}, past {_INVESTIGATE_SKIP_EXPIRES.isoformat()}, and the installed "
                f"kumo-strategies {rev} STILL has no SelfAction.INVESTIGATE — either ks#182 stalled or this "
                f"expiry was wrong. Decide which. (The merge gate's PIN run is the authority; a red on the "
                f"editable tree alone is that tree's state, #966.)")


def test_the_INVESTIGATE_skip_is_keyed_on_the_capability_and_FAILS_after_the_expiry_date(monkeypatch):
    """The mechanism itself, driven with a SelfAction that lacks INVESTIGATE and a movable calendar:
    before the date it skips naming the date; on/after the date it fails naming both possibilities;
    with INVESTIGATE present it returns whatever the date. No revision decides anything."""
    class _SelfAction:  # no INVESTIGATE
        STAND_DOWN = "stand_down"
    class _MV:
        SelfAction = _SelfAction
    monkeypatch.setattr("api.test_market_aware_contract._installed_strategies_rev", lambda: "abc9999")
    monkeypatch.setattr("api.test_market_aware_contract._today", lambda: datetime.date(2026, 9, 17))
    with pytest.raises(pytest.skip.Exception, match="abc9999.*ks#182.*EXPIRES on 2026-09-18"):
        _skip_only_until_the_expiry_when_INVESTIGATE_is_absent(_MV)
    monkeypatch.setattr("api.test_market_aware_contract._today", lambda: datetime.date(2026, 9, 18))
    with pytest.raises(pytest.fail.Exception, match="past 2026-09-18.*abc9999.*ks#182 stalled or this expiry was wrong"):
        _skip_only_until_the_expiry_when_INVESTIGATE_is_absent(_MV)
    class _With:
        class SelfAction:
            INVESTIGATE = "investigate"
    monkeypatch.setattr("api.test_market_aware_contract._today", lambda: datetime.date(2030, 1, 1))
    assert _skip_only_until_the_expiry_when_INVESTIGATE_is_absent(_With) is None
    # the expiry is a DATE in the source, not a set of revisions: no module-level tuple of shas exists
    # here to grow one entry per pin bump (read off the AST, not by substring — a substring would match
    # this very comment)
    import ast
    tree = ast.parse(pathlib.Path(__file__).read_text())
    sha_tuples = [t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Tuple)
                  and all(isinstance(e, ast.Constant) and isinstance(e.value, str) and len(e.value) in (7, 40)
                          and all(c in "0123456789abcdef" for c in e.value) for e in n.value.elts) and n.value.elts
                  for t in n.targets if isinstance(t, ast.Name)]
    assert sha_tuples == [], f"a revision list is back: {sha_tuples}"
    assert isinstance(_INVESTIGATE_SKIP_EXPIRES, datetime.date)


def test_FIXTURE_the_installed_check_is_a_named_skip_not_a_pass_when_the_contract_is_absent():
    """A mirror test that passes on a pin without the contract pins nothing. The skip names the module."""
    assert ma.CONTRACT_MODULE == "kumo_strategies.strategies.market_events"
    st = contract_state()
    assert st["state"] in ("present", "absent") and st["module"] == ma.CONTRACT_MODULE


def test_the_mirror_equals_the_installed_contract_kinds_labels_severities_and_reserved_keys():
    me = _installed()
    assert tuple(k.value for k in me.EventKind) == CONTRACT_KINDS, "a kind was added or renamed upstream"
    assert {k.value: v for k, v in me.LABELS.items()} == CONTRACT_LABELS
    assert {k.value: v.value for k, v in me.SEVERITY.items()} == CONTRACT_SEVERITY
    assert me._RESERVED == RESERVED_CONTRACT
    assert RESERVED_CONTRACT <= RESERVED


def test_the_action_union_spans_BOTH_upstream_enums():
    """`MarketAction` (what a lane does on risk-off) and `SelfAction` (its own self-report, STAND_DOWN)
    are separate types upstream so a market view can never be configured to stand a lane down. The
    union here must equal their members, and must not fold either into the other."""
    mv = _market_view_or_skip("MarketAction")
    members = {m.value for m in mv.MarketAction}
    self_action = getattr(mv, "SelfAction", None)
    if self_action is None:
        pytest.skip("installed kumo-strategies has no SelfAction yet (l21wvpmj is adding STAND_DOWN)")
    members |= {m.value for m in self_action}
    # An installed member the mirror lacks is the defect (a blank badge tomorrow). The mirror may be
    # AHEAD of an older pin (84d09d3 has no INVESTIGATE) — a superset there, equality where the
    # installed enums carry everything the mirror names.
    assert members <= set(ACTIONS), f"upstream actions {sorted(members)} not all in mirror {ACTIONS}"
    if hasattr(self_action, "INVESTIGATE"):
        assert set(ACTIONS) == members, f"upstream actions {sorted(members)} vs mirror {ACTIONS}"


def test_the_payload_keys_are_a_superset_of_the_contracts_payload():
    me = _installed()
    mv = importlib.import_module("kumo_strategies.strategies.market_view")
    theirs = me.transition("X", previous=mv.RISK_ON, current=mv.RISK_OFF, reasons=("r",),
                           action=mv.MarketAction.EXIT_ONLY).payload()
    ours = notification("X", Event(kind=ma.EXIT_ONLY_ENTERED, hook="entries_blocked", reasons=("r",),
                                   previous=RISK_ON, current=RISK_OFF))
    assert set(theirs) <= set(ours), sorted(set(theirs) - set(ours))
    assert (ours["kind"], ours["severity"], ours["label"], ours["action"]) == (
        theirs["kind"], theirs["severity"], theirs["label"], theirs["action"])


# -- the budget and the vocabulary's closure -------------------------------------------------------------

def test_SEVERITY_IS_A_BUDGET_exactly_one_kind_pages_and_it_is_emergency_exit():
    critical = [k for k, s in SEVERITY.items() if s == CRITICAL]
    assert critical == ["emergency_exit"], critical
    assert NOTIFY[ma.EMERGENCY_TRIGGERED][0] == "emergency_exit"
    assert SEVERITY["hook_fault"] != CRITICAL, "a hook that RAISED is a broken check, not an emergency"


def test_every_fold_kind_can_be_notified_and_every_notification_kind_has_a_label_and_a_severity():
    assert set(NOTIFY) == set(EVENT_KINDS)
    targets = {kind for kind, _ in NOTIFY.values()}
    assert targets <= set(LABELS) and targets <= set(SEVERITY)
    assert set(LABELS) == set(CONTRACT_KINDS) | set(COCKPIT_KINDS) == set(SEVERITY)
    assert all(a is None or a in ACTIONS for _, a in NOTIFY.values())


# -- the payload: flat, JSON-safe, both states, reasons in full, reserved keys refused ------------------

def test_the_notification_is_flat_json_safe_and_carries_both_states_and_every_reason_verbatim():
    reasons = ("index 0.91 below its 50-session average 0.97", "breadth 31% (< 40%)", "third reason kept")
    ev = Event(kind=ma.EMERGENCY_TRIGGERED, hook="emergency_exit", reasons=reasons, previous=RISK_ON,
               current=RISK_OFF, polls=3, faults=0, reading=YES)
    body = notification("TECHIVOL-005", ev, detail={"dwell": 3})
    json.dumps(body)
    assert all(not isinstance(v, dict) for v in body.values()), "flat: it crosses a process boundary"
    assert body["reasons"] == list(reasons), "reasons in FULL — QC27's valve went unexplained for months"
    assert (body["lane"], body["kind"], body["severity"], body["label"]) == (
        "TECHIVOL-005", "emergency_exit", CRITICAL, "closing its book")
    assert body["previous"] == RISK_ON and body["current"] == RISK_OFF and body["polls"] == 3, "the dwell-th yes fires the CONDITION"
    assert body["reading"] == YES
    assert body["hook"] == "emergency_exit" and body["action"] == "liquidate" and body["dwell"] == 3


def _own_keys() -> list[str]:
    """Every key `notification()` writes itself — derived from the function, not from RESERVED, so a
    member dropped from RESERVED is a key that can now be overwritten and this test sees it."""
    ev = Event(kind=ma.EXIT_ONLY_ENTERED, hook="entries_blocked", previous=RISK_ON, current=RISK_OFF)
    return sorted(notification("MOMENTUM-002", ev))


def test_FIXTURE_the_notification_writes_the_contracts_eight_fields_and_cockpits_three():
    assert set(_own_keys()) == RESERVED_CONTRACT | {"hook", "reading", "polls", "faults"}


@pytest.mark.parametrize("key", _own_keys())
def test_a_detail_key_colliding_with_ANY_field_the_notification_writes_is_REFUSED_not_overwritten(key):
    ev = Event(kind=ma.EXIT_ONLY_ENTERED, hook="entries_blocked", previous=RISK_ON, current=RISK_OFF)
    with pytest.raises(ValueError, match=key):
        notification("MOMENTUM-002", ev, detail={key: "BCTROT-004"})


def test_FIXTURE_a_first_poll_event_carries_UNPOLLED_as_previous_so_previous_is_never_defaulted():
    _, ev = fold(None, _now(entries_blocked=YES), dwell=3, ts_ns=1)
    assert ev and ev[0].previous == UNPOLLED and ev[0].current == RISK_OFF and ev[0].reading == YES
    assert notification("L", ev[0])["previous"] == UNPOLLED


# -- fault: WARN, and the COUNT travels so a day of raises is visible as a number ----------------------

def test_a_sustained_fault_pages_once_at_WARN_and_its_count_climbs_on_every_surface():
    st, ev = fold(None, _now(emergency_exit=FAULT), dwell=3, ts_ns=1)
    assert [e.kind for e in ev] == [ma.HOOK_FAULT] and ev[0].faults == 1
    body = notification("L", ev[0])
    assert body["severity"] == "warn" and body["faults"] == 1
    for i in (2, 3):
        st, ev = fold(st, _now(emergency_exit=FAULT), dwell=3, ts_ns=i)
        assert ev == (), "no page per poll"
    assert as_payload(st, dwell=3)["emergency_exit"]["faults"] == 3
    assert st.emergency_exit.streak_fault == 3


def test_a_fault_that_clears_resets_the_count_and_the_next_fault_pages_again():
    st, _ = fold(None, _now(entries_blocked=FAULT), dwell=3, ts_ns=1)
    st, _ = fold(st, _now(entries_blocked=NO), dwell=3, ts_ns=2)
    assert st.entries_blocked.streak_fault == 0
    st, ev = fold(st, _now(entries_blocked=FAULT), dwell=3, ts_ns=3)
    assert [e.kind for e in ev] == [ma.HOOK_FAULT] and ev[0].faults == 1


# -- stand down: the lane's self-report uses STAND_DOWN, never cockpit's "quarantine" -----------------

def test_the_ASSESSMENT_recovering_is_one_page_and_is_NOT_a_stand_down_clearing():
    """Upstream: a stand-down self-clears NEVER (a lane cannot certify its own recovery with the
    evidence that condemned it). What cockpit reports on the next NO is that the ASSESSMENT went back
    inside its envelope — the contract's `back_in_envelope` — and the event's name says so."""
    st, ev = fold(None, _now(self_assessment=YES), dwell=3, ts_ns=1)
    assert [e.kind for e in ev] == [STAND_DOWN_REQUESTED]
    assert notification("L", ev[0])["kind"] == "out_of_envelope_confirmed"
    st, ev = fold(st, _now(self_assessment=NO), dwell=3, ts_ns=2)
    assert [e.kind for e in ev] == [ASSESSMENT_RECOVERED]
    body = notification("L", ev[0])
    assert body["kind"] == "back_in_envelope" and body["action"] == "stand_down"
    assert "clear" not in ASSESSMENT_RECOVERED and "clear" not in body["label"]
    st, ev = fold(st, _now(self_assessment=NO), dwell=3, ts_ns=3)
    assert ev == ()


def test_the_word_quarantine_does_not_name_the_lane_self_report_anywhere_in_this_module():
    """cockpit's "quarantine" is the #79 plane (foreign broker activity). Two meanings of one word
    across two repos must not meet on one surface."""
    import ast, inspect
    for name in EVENT_KINDS + tuple(LABELS) + tuple(LABELS.values()) + ACTIONS + CONDITION_STATES:
        assert "quarantine" not in name.lower()
    # By AST — identifiers and short string constants — not a `#`-stripped uppercase grep: the first
    # version would have let a lowercase use in code hide behind the docstring that explains the rule.
    tree = ast.parse(inspect.getsource(ma))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert "quarantine" not in node.id.lower(), node.id
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) <= 120:
            assert "quarantine" not in node.value.lower(), node.value


# -- the contract's presence is nameable from outside --------------------------------------------------

def test_contract_state_names_the_module_and_is_absent_WITH_A_REASON_when_the_package_is_broken(monkeypatch):
    import importlib.util as iu
    monkeypatch.setattr(iu, "find_spec", lambda name: None)
    assert contract_state() == {"state": "absent", "module": ma.CONTRACT_MODULE}
    monkeypatch.setattr(iu, "find_spec", lambda name: object())
    assert contract_state() == {"state": "present", "module": ma.CONTRACT_MODULE}

    def broken(name):
        raise ImportError("kumo_strategies parent package failed to import")
    monkeypatch.setattr(iu, "find_spec", broken)
    st = contract_state()
    assert st["state"] == "absent" and "ImportError" in st["reason"]


def test_the_module_never_imports_the_strategies_package_at_module_scope():
    """#903's class guard covers backend/; this pins the reason locally: the contract is absent from
    every deployed pin, so an import here would take the engine down at import time."""
    import ast, inspect
    tree = ast.parse(inspect.getsource(ma))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) else [])
            assert not any(str(n).startswith("kumo_strategies") for n in names), ast.unparse(node)


# -- the contract's runtime invariant, and its transition() semantics, checked against the REAL module ----

def _every_cockpit_event() -> list[Event]:
    """One real event per cockpit kind, produced by the fold, not hand-built."""
    out = []
    st, ev = fold(None, _now(entries_blocked=YES, self_assessment=YES), dwell=1, ts_ns=1); out += ev
    st, ev = fold(st, _now(entries_blocked=NO, self_assessment=NO, emergency_exit=YES), dwell=1, ts_ns=2); out += ev
    st, ev = fold(st, _now(emergency_exit=NO), dwell=1, ts_ns=3); out += ev
    st, ev = fold(st, _now(emergency_exit=UNKNOWN), dwell=1, ts_ns=4); out += ev
    st, ev = fold(st, _now(emergency_exit=UNKNOWN), dwell=1, ts_ns=5); out += ev
    st, ev = fold(st, _now(emergency_exit=FAULT), dwell=1, ts_ns=6); out += ev
    st, ev = fold(st, _now(emergency_exit=NO), dwell=1, ts_ns=7); out += ev      # fault → answering again
    assert {e.kind for e in out} == set(EVENT_KINDS), {e.kind for e in out}
    return out


def test_FIXTURE_the_fold_can_produce_every_event_kind_and_none_has_previous_equal_to_current():
    for e in _every_cockpit_event():
        assert e.previous != e.current, (e.kind, e.previous, e.current)
        assert e.previous in CONDITION_STATES and e.current in CONDITION_STATES, e


def test_the_contracts_OWN_constructor_accepts_every_cockpit_event_as_a_transition():
    """The contract's one runtime invariant: `MarketEvent.__post_init__` refuses previous == current.
    Every event cockpit emits, with its own previous/current, must construct."""
    me = _installed()
    for e in _every_cockpit_event():
        kind, action = NOTIFY[e.kind]
        if kind in COCKPIT_KINDS:
            continue
        me.MarketEvent(lane="L", kind=me.EventKind(kind), severity=me.Severity(SEVERITY[kind]),
                       previous=e.previous, current=e.current, reasons=e.reasons)


def test_cockpits_NOTIFY_target_agrees_with_the_contracts_transition_for_the_same_pair():
    """Table-driven against the real `transition()`: for each cockpit event's previous/current/action,
    the contract's answer must be cockpit's. (For one hour this recorded a divergence — the contract
    folded RISK_OFF → RISK_ON under LIQUIDATE into `entries_unblocked`; kumo-strategies 80f6eae added
    `emergency_cleared` and the mirror adopted it. A drift in `transition()`'s semantics lands here.)"""
    me = _installed()
    mv = _market_view_or_skip("MarketAction", "RISK_ON")
    action_of = {"exit_only": mv.MarketAction.EXIT_ONLY, "liquidate": mv.MarketAction.LIQUIDATE}
    seen = set()
    for e in _every_cockpit_event():
        kind, action = NOTIFY[e.kind]
        if kind in COCKPIT_KINDS:
            continue
        envelope = e.hook == "self_assessment"
        theirs = me.transition("L", previous=e.previous, current=e.current, reasons=e.reasons,
                               action=None if envelope else action_of[action], envelope=envelope,
                               acts=envelope and e.current == OUT_OF_ENVELOPE)
        assert theirs is not None, (e.kind, e.previous, e.current)
        assert theirs.kind.value == kind, (e.kind, theirs.kind, kind)
        seen.add(e.kind)
    assert EMERGENCY_CLEARED in seen and EXIT_ONLY_LEFT in seen


def test_emergency_cleared_and_exit_only_left_are_DISTINGUISHABLE_notifications():
    """Measured before the fix: both were kind=entries_unblocked, label "opening positions again",
    info — a lane that just stopped liquidating paged as "opening positions again". Now the contract's
    own `emergency_cleared` (WARN)."""
    by_kind = {e.kind: e for e in _every_cockpit_event()}
    a = notification("L", by_kind[EXIT_ONLY_LEFT]); b = notification("L", by_kind[EMERGENCY_CLEARED])
    assert (a["kind"], a["label"]) != (b["kind"], b["label"])
    assert b["label"] == "no longer closing its book" and b["severity"] == "warn"


def test_cockpits_state_literals_ARE_the_contracts_so_an_upstream_rename_cannot_turn_unknown_into_no():
    mv = _market_view_or_skip("YES", "NO", "UNKNOWN", "RISK_ON", "RISK_OFF", "IN_ENVELOPE", "OUT_OF_ENVELOPE")
    assert (YES, NO, UNKNOWN) == (mv.YES, mv.NO, mv.UNKNOWN)
    assert (RISK_ON, RISK_OFF, IN_ENVELOPE, OUT_OF_ENVELOPE) == (mv.RISK_ON, mv.RISK_OFF, mv.IN_ENVELOPE, mv.OUT_OF_ENVELOPE)


def test_poll_lane_is_the_seam_and_a_lane_carrying_only_some_hooks_does_not_crash_the_fold():
    class _V:
        def __init__(self, s): self.state, self.reasons = s, ("r",)
        @property
        def acts(self): return self.state == "yes"
    class _Lane:
        def entries_blocked(self): return _V("yes")
    st, ev = poll_lane(_Lane(), None, dwell=3, ts_ns=1)
    assert st.entries_blocked.state == YES and st.emergency_exit.state == "not_asked" and st.self_assessment.state == "not_asked"
    assert [e.kind for e in ev] == [ma.EXIT_ONLY_ENTERED]
    st2, ev2 = fold(st, {"entries_blocked": HookReading(YES, observed="yes", asked_at_ns=2)}, dwell=3, ts_ns=2)
    assert ev2 == () and st2.emergency_exit.state == "not_asked", "a partial `now` reads NOT_ASKED, never KeyError"


def test_the_unknown_streak_climbs_past_two_on_the_payload():
    st = None
    for i in range(5):
        st, _ = fold(st, _now(entries_blocked=UNKNOWN), dwell=3, ts_ns=i)
    assert as_payload(st, dwell=3)["entries_blocked"]["unknowns"] == 5


def test_hook_unknown_twice_names_where_the_lane_WAS_as_previous():
    st, _ = fold(None, _now(entries_blocked=YES), dwell=3, ts_ns=1)
    st, _ = fold(st, _now(entries_blocked=UNKNOWN), dwell=3, ts_ns=2)
    st, ev = fold(st, _now(entries_blocked=UNKNOWN), dwell=3, ts_ns=3)
    assert [e.kind for e in ev] == [ma.HOOK_UNKNOWN_TWICE]
    assert ev[0].previous == RISK_OFF and ev[0].current == UNKNOWN


def test_an_emergency_answer_BELOW_the_dwell_is_still_RISK_ON_on_every_surface():
    """The condition fires with the dwell, not with the answer: two yes polls under dwell three read
    RISK_ON on the payload and via `condition_of`, and no event exists to carry RISK_OFF."""
    st, ev1 = fold(None, _now(emergency_exit=YES), dwell=3, ts_ns=1)
    st, ev2 = fold(st, _now(emergency_exit=YES), dwell=3, ts_ns=2)
    assert ev1 == () and ev2 == ()
    assert as_payload(st, dwell=3)["emergency_exit"]["condition"] == RISK_ON
    assert condition_of("emergency_exit", YES, in_episode=False) == RISK_ON
    assert condition_of("emergency_exit", YES, in_episode=True) == RISK_OFF
    assert condition_of("entries_blocked", YES, in_episode=False) == RISK_OFF, "no dwell on the veto"


def test_an_Event_with_previous_equal_to_current_REFUSES_to_be_built_like_the_contracts_own():
    """Enforced at construction, not inferred from the fold: the only thing keeping an entering
    event's previous from equalling its current is `needed = 1` on two hooks, which a per-hook dwell
    would silently undo."""
    with pytest.raises(ValueError, match="STATE, not a transition"):
        Event(kind=ma.EXIT_ONLY_ENTERED, hook="entries_blocked", previous=RISK_OFF, current=RISK_OFF)


def test_the_action_union_carries_investigate_and_RESTRICTS_TRADING_is_implied_by_the_installed_reduces():
    """kumo-strategies added `SelfAction.INVESTIGATE` (the high tail: above its own envelope — look,
    never reduce). `reduces` is a property on THEIR enum answering "smaller book?"; cockpit's table
    answers "restricted trading?" over BOTH enums and is named differently on purpose — `exit_only`
    restricts without reducing. Pinned as an IMPLICATION: every installed action that reduces
    restricts; `investigate` does neither; phase 2 gates on this table, never on "an action is set"."""
    assert "investigate" in ACTIONS and ma.RESTRICTS_TRADING["investigate"] is False
    assert ma.RESTRICTS_TRADING["stand_down"] is True and ma.RESTRICTS_TRADING["exit_only"] is True
    assert set(ma.RESTRICTS_TRADING) == set(ACTIONS)
    assert not hasattr(ma, "REDUCES"), "one name per question — `reduces` is kumo-strategies' word for a smaller book"
    mv = _market_view_or_skip("MarketAction", "SelfAction")
    _skip_only_until_the_expiry_when_INVESTIGATE_is_absent(mv)
    for m in mv.SelfAction:
        if bool(m.reduces):
            assert ma.RESTRICTS_TRADING[m.value] is True, m
        else:
            assert m.value == "investigate", f"a non-reducing SelfAction cockpit has no ruling for: {m}"
    for m in mv.MarketAction:
        assert ma.RESTRICTS_TRADING[m.value] is True, m


def test_read_hook_carries_the_answers_ACTION_for_the_badge_without_making_it_a_trigger():
    class _Answer:
        state = "out_of_envelope"; reasons = ("return +30% above p90",); evidence_sufficient = True
        class _A:  # an enum member's shape: `.value`
            value = "investigate"
        action = _A()
        @property
        def acts(self): return True
    class _Lane:
        def self_assessment(self): return _Answer()
    r = ma.read_hook(_Lane(), "self_assessment", ts_ns=1)
    assert r.state == YES and r.action == "investigate"
    st, ev = fold(None, {"self_assessment": r}, dwell=3, ts_ns=1)
    assert as_payload(st, dwell=3)["self_assessment"]["action"] == "investigate"
