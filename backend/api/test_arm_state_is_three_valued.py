"""A lane's ARM is three states, and the one an operator must act on is currently invisible (#997).

MEASURED 2026-09-11 while two sessions spent an hour reasoning about QC345-003's arm from a signal
that contains no information about it:

  * `armed_lanes` reads `getattr(s, "is_armed")`, which IS the session arm — `contract.py:1046-1049`,
    `return getattr(self, "_armed_session", None) is not None`. That part was right all along.
  * But it is published as a BARE BOOL, so three things are dropped at the boundary:
      1. WHICH SESSION the lane armed for. `_armed_session` holds the date and `_armed_slot` the slot;
         a lane armed for a STALE session reads True identically to one armed for today.
      2. The difference between NOT ARMED and MID-RE-ARM. `_on_session_alert` clears `_armed_session`
         BEFORE re-arming (`fired, self._armed_session = self._armed_session, None`), so every healthy
         lane reads False at the exact instant it decides. Measured: the re-arm does NOT refetch —
         `AlpacaCalendar._ensure` returns from a cached span (10 days back / 45 forward; span on this
         date 2026-09-01→2026-10-26), cold 1068.6 ms vs warm 0.023 ms — so the window is ~20 µs, not
         the 15 s a fetch would cost. Rare, therefore never observed; a race all the same, and it is
         closed here by CONSTRUCTION (a live decision alert means armed-or-rearming) rather than by a
         tolerance.
      3. The REASON. `_arm_until_resolved`'s terminal branch logs "UNARMED — the calendar answered but
         arming FAILED … This is not a network problem and will not be retried. This strategy CANNOT
         DECIDE until the cause is fixed and it is restarted." and RETURNS — the task ends, the lane is
         permanently dead, and NOTHING is set: the mixin exposes `is_armed`, `begin_arming`,
         `cancel_arming`, `rearm_after_alert`, `warmup_bars`, `ARM_RETRY_SECS`, `REARM_ALERT` and no
         failure state at all. So cockpit CANNOT distinguish permanently-refused from still-retrying
         today; both are `armed: False, next_fire: None`. That reason is asked of kumo-trading-strategies
         (l21) and this module reports `reason: None` until it exists — NAMED as unavailable rather
         than guessed, because "no reason" and "no failure" must not look the same.

ONE DERIVATION, NOT TWO: the richer value REPLACES the bool rather than sitting beside it, and
`/strategies` serves `armed_lanes[sid]["armed"]` so the UI's bool comes from the same place.
"""
from __future__ import annotations

import pytest


class _Lane:
    """Production's shape: the mixin's own attribute names, nothing invented."""

    def __init__(self, session=None, slot=None, *, alert_ns=None):
        self._armed_session = session
        self._armed_slot = slot
        self.clock = _Clock(alert_ns)

    @property
    def is_armed(self) -> bool:
        return getattr(self, "_armed_session", None) is not None


class _Clock:
    def __init__(self, alert_ns):
        self._alert_ns = alert_ns
        self.timer_names = ["qc345_session_decide"] if alert_ns else []

    def next_time_ns(self, name):
        return self._alert_ns


def test_FIXTURE_the_double_matches_the_INSTALLED_contract_including_what_it_does_NOT_expose():
    """The double must carry production's names AND production's absence: if the installed mixin ever
    grows an arm-failure attribute, this file is measuring a world that no longer exists and the ask
    to kumo-trading-strategies has been answered."""
    from kumo_strategies.runtime.nautilus.contract import RegistrationMixin

    assert hasattr(RegistrationMixin, "is_armed") and hasattr(RegistrationMixin, "begin_arming")
    # THE REASON IS OPTIONAL BY DESIGN, and this file must pass on BOTH sides of the pin move.
    # kumo-trading-strategies is building `arm_state` ({state, reason, at_ns, attempts}) — found in l21's
    # UNCOMMITTED working tree on 2026-09-11 while this test asserted its absence, which is why the
    # assertion is a branch rather than an absence: the editable import resolves to that tree and its
    # file-save state must not decide a verdict here. `armed_by_lane` reads the attribute when the
    # installed package has it and reports `reason_available: False` when it does not.
    from api.engine_node import ARM_REASON_ATTR
    assert ARM_REASON_ATTR == "arm_state"


def test_an_ARMED_lane_carries_the_SESSION_and_SLOT_it_armed_for_not_just_True():
    """Point 1: the value that makes the answer checkable is present in `_armed_session` and was
    dropped at the boundary. A lane armed for a stale date must be readable as such."""
    from api.engine_node import armed_by_lane

    out = armed_by_lane({"QC345-003": _Lane("2026-09-11", "open+5m", alert_ns=1_789_000_000_000_000_000)})
    row = out["QC345-003"]
    assert row["armed"] is True and row["session"] == "2026-09-11" and row["slot"] == "open+5m"
    assert row["state"] == "armed" and row["reason"] is None


def test_MID_REARM_is_not_a_fault_a_live_decision_alert_means_the_lane_is_scheduled():
    """Point 2, closed BY CONSTRUCTION. Between `_on_session_alert` clearing `_armed_session` and the
    re-arm setting it, a healthy lane has no armed session but still holds its decision alert. ~20 µs
    on this pin, so a poll essentially never lands here — but a rare race is still a race, and a
    detector whose False is normal at the instant decisions happen is the one you least want to page."""
    from api.engine_node import armed_by_lane

    row = armed_by_lane({"MOMENTUM-002": _Lane(None, None, alert_ns=1_789_000_000_000_000_000)})["MOMENTUM-002"]
    assert row["armed"] is False and row["state"] == "rearming", row
    assert row["next_fire_ns"] == 1_789_000_000_000_000_000
    assert row["session"] is None


def test_NOT_ARMED_with_NO_alert_is_the_state_an_operator_must_act_on_and_it_says_the_reason_is_UNAVAILABLE():
    """Point 3. No armed session AND no decision alert: the lane will never fire. Today this covers
    both "still retrying the calendar" and "refused permanently and the task ended", because the
    installed contract exposes neither — so `reason` is None and `reason_available` is False, which a
    reader can tell from a reason that exists and is empty."""
    from api.engine_node import armed_by_lane

    row = armed_by_lane({"TECHIVOL-005": _Lane(None, None, alert_ns=None)})["TECHIVOL-005"]
    assert row["armed"] is False and row["state"] == "not_armed"
    assert row["next_fire_ns"] is None
    assert row["reason"] is None and row["reason_available"] is False, (
        "a missing reason must be distinguishable from 'there is no reason'"
    )


def test_when_the_lane_CARRIES_a_reason_it_is_forwarded_verbatim_with_its_attempts():
    """The shape kumo-trading-strategies is shipping: {state, reason, at_ns, attempts}. `attempts`
    distinguishes retrying-once from retrying-forever, which are different operational facts that
    look identical on a surface. Cockpit forwards it and invents nothing."""
    from api.engine_node import armed_by_lane

    lane = _Lane(None, None, alert_ns=None)
    lane.arm_state = {"state": "refused_permanent", "attempts": 1,
                      "reason": "SlotError: 'open+45' is not a slot", "at_ns": 1_789_000_000_000_000_000}
    row = armed_by_lane({"QC345-003": lane})["QC345-003"]
    assert row["state"] == "refused_permanent", "the lane's own state must win over cockpit's inference"
    assert row["reason"] == "SlotError: 'open+45' is not a slot" and row["reason_available"] is True
    assert row["attempts"] == 1 and row["reason_at_ns"] == 1_789_000_000_000_000_000


def test_a_RETRYING_lane_is_not_the_same_as_a_permanently_refused_one():
    """The distinction the whole ticket exists for: both read `is_armed False` with no alert."""
    from api.engine_node import armed_by_lane

    retry = _Lane(None, None, alert_ns=None)
    retry.arm_state = {"state": "retrying", "attempts": 47, "reason": "URLError: timed out", "at_ns": 1}
    dead = _Lane(None, None, alert_ns=None)
    dead.arm_state = {"state": "refused_permanent", "attempts": 1, "reason": "SlotError: bad", "at_ns": 1}
    out = armed_by_lane({"A": retry, "B": dead})
    assert out["A"]["state"] == "retrying" and out["A"]["attempts"] == 47
    assert out["B"]["state"] == "refused_permanent"
    assert out["A"]["state"] != out["B"]["state"], "the two invisible states are still one state"


def test_a_lane_that_cannot_be_ASKED_is_UNKNOWN_never_armed():
    """FAIL CLOSED, unchanged from the bool version: reporting armed when we cannot tell is the
    silencing direction."""
    from api.engine_node import armed_by_lane

    class _Opaque:
        pass

    row = armed_by_lane({"X": _Opaque()})["X"]
    assert row["armed"] is None and row["state"] == "unknown"


def test_the_STRATEGIES_row_serves_its_bool_from_THIS_value_one_derivation_not_two():
    """The UI's per-lane `armed` bool must be read out of the same OBJECT, never computed again.

    ASSERTS THE STRUCTURE, NOT THE SPELLING. The first version matched two exact source strings and
    went red the moment the two calls were collapsed into one binding — which was the review fix that
    made "one derivation" true BY CONSTRUCTION rather than by the accident of a pure helper. A
    detector aimed at where the code happened to be rather than at what must hold: the same shape
    this file's own fixture test warns about.

    The property: `_arm_row` is called ONCE, its result is bound to a name, and both the bool and the
    row are served from THAT name.
    """
    import ast
    import inspect

    from api import app

    tree = ast.parse(inspect.getsource(app))

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_arm_row"]
    assert len(calls) == 1, (
        f"_arm_row is called {len(calls)} times — two calls are two objects, and 'one derivation' is "
        "then true only while the helper stays pure"
    )

    bound = [t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
             and n.value.func.id == "_arm_row"
             for t in n.targets if isinstance(t, ast.Name)]
    assert len(bound) == 1, "the single _arm_row call is not bound to a name the row can serve from"
    name = bound[0]

    # FIXTURE PROPERTY FIRST: a dict carrying BOTH keys must exist, or every assertion below is
    # vacuous because there is nothing to violate.
    rows = [d for d in ast.walk(tree) if isinstance(d, ast.Dict)
            and {k.value for k in d.keys if isinstance(k, ast.Constant)} >= {"armed", "arm"}]
    assert rows, "no /strategies row dict carries both 'armed' and 'arm' — the test cannot fail"

    for d in rows:
        served = {k.value: v for k, v in zip(d.keys, d.values) if isinstance(k, ast.Constant)}
        arm = served["arm"]
        assert isinstance(arm, ast.Name) and arm.id == name, (
            "the row does not carry the full arm state (session, slot, reason) from the bound row"
        )
        bool_ = served["armed"]
        assert (isinstance(bool_, ast.Subscript) and isinstance(bool_.value, ast.Name)
                and bool_.value.id == name), (
            "the /strategies row does not read its bool OUT of the arm row — two derivations of one state"
        )


def test_the_health_model_and_the_consumer_carry_the_RICH_value_across_the_process_split():
    from api.consumer import RedisConsumer
    from api.feed_config import load_feed_config

    c = RedisConsumer(load_feed_config())
    rich = {"QC345-003": {"armed": True, "state": "armed", "session": "2026-09-11", "slot": "open+5m",
                          "next_fire_ns": 1, "reason": None, "reason_available": False}}
    import time
    c._health = {"engine_ok": True, "armed_lanes": rich}
    c._health_at = time.monotonic()          # the bridge-liveness clock the consumer actually reads
    out = c.health()
    assert out["armed_lanes"] == rich, "the consumer flattened or dropped the rich arm value"


def test_a_MIXED_VERSION_frame_from_an_older_ENGINE_still_serves_a_bool_and_says_the_rest_is_unknown():
    """THE ROLLING-DEPLOY CASE, and it is a production one rather than a test artefact: the api and the
    engine are separate containers that recreate seconds apart, so a NEW api reads the OLD engine's
    frame, where `armed_lanes` values are bare bools. Found when this change broke
    `test_cadence_on_strategies` — whose doubles publish the old shape, exactly as a lagging engine
    would. The bool must still serve, and everything the old frame cannot carry must read UNKNOWN
    rather than be invented."""
    from api.app import _arm_row

    old = _arm_row(True)
    assert old["armed"] is True and old["state"] == "unknown" and old["session"] is None
    assert old["reason_available"] is False
    assert _arm_row(False)["armed"] is False
    assert _arm_row(None)["armed"] is None and _arm_row(None)["state"] == "unknown"
    new = _arm_row({"armed": True, "state": "armed", "session": "2026-09-11", "slot": "open+5m",
                    "next_fire_ns": 1, "reason": None, "reason_available": False})
    assert new["state"] == "armed" and new["session"] == "2026-09-11"


def test_a_reason_whose_clock_could_not_answer_carries_at_ns_None_and_is_still_a_reason():
    """l21 shipped `at_ns: None` deliberately (6dcdd27): the first version read the clock unguarded
    inside the failure handlers and RAISED OVER THE ARMING FAILURE IT WAS REPORTING. A reason without
    a timestamp is still the state an operator must act on, and cockpit must not drop it for lacking
    one, nor invent a time."""
    from api.engine_node import armed_by_lane

    lane = _Lane(None, None, alert_ns=None)
    lane.arm_state = {"state": "refused_permanent", "reason": "SlotError: bad", "at_ns": None, "attempts": 1}
    row = armed_by_lane({"Q": lane})["Q"]
    assert row["state"] == "refused_permanent" and row["reason_available"] is True
    assert row["reason_at_ns"] is None


def test_attempts_counts_the_CURRENT_condition_not_the_lanes_history():
    """Also l21's, and worth pinning because the number invites the other reading: a lane that retried
    fifty times and then failed permanently reports attempts 1 against the NEW state. Cockpit forwards
    it verbatim and must not accumulate its own count beside it."""
    from api.engine_node import armed_by_lane

    lane = _Lane(None, None, alert_ns=None)
    lane.arm_state = {"state": "refused_permanent", "reason": "SlotError: bad", "at_ns": 1, "attempts": 1}
    assert armed_by_lane({"Q": lane})["Q"]["attempts"] == 1


def test_a_DECLARED_reason_carrying_non_JSON_types_cannot_take_the_WHOLE_FRAME_DOWN():
    """ffv73l93's #998 review, and the hazard is proved by the one field that WAS coerced.

    `_as_text` exists because `_armed_session` is a pandas Timestamp and a frame carrying one cannot
    be serialised to the bridge. That establishes the rule for this boundary. `state` is coerced with
    `str(...)`. `reason`, `attempts` and `reason_at_ns` were forwarded VERBATIM out of
    `kumo_strategies`' `arm_state` — a dict this repo does not own and whose contract is young enough
    that its `at_ns` is nullable only because an early version read a clock unguarded inside its own
    failure handler.

    A `Decimal`, a `Timestamp` or an exception object in any of those three serialises to nothing, and
    because `armed_by_lane` builds ONE frame for ALL lanes, the failure takes `armed_lanes` down for
    every lane — not just the one that declared it. So the change's failure mode would be strictly
    worse than the bare bool it replaces, in exactly the case it exists to report.

    THE FIXTURE PROPERTY IS ASSERTED FIRST. The existing `_Lane` double returns clean primitives, so
    it CANNOT express this bug: a test built on it would pass with the coercion removed and would be
    an assertion about nothing.
    """
    import json
    from decimal import Decimal

    from api.engine_node import ARM_REASON_ATTR, armed_by_lane

    class _Boom(Exception):
        def __str__(self) -> str:
            return "the calendar never answered"

    lane = _Lane("2026-09-11", "open+5m")
    setattr(lane, ARM_REASON_ATTR, {
        "state": "refused_permanent",
        "reason": _Boom("the calendar never answered"),   # upstream may hand us the exception itself
        "attempts": Decimal("3"),                          # a Decimal from a DB-backed counter
        "at_ns": Decimal("1789133700000000000"),           # nullable by design, not always an int
    })

    # FIXTURE PROPERTY: the declared dict really does carry values json cannot encode. Without this
    # the test below could pass because nothing violated the invariant, not because it held.
    declared = getattr(lane, ARM_REASON_ATTR)
    for key in ("reason", "attempts", "at_ns"):
        with pytest.raises(TypeError):
            json.dumps(declared[key])

    rows = armed_by_lane({"QC345-003": lane})

    # THE PROPERTY: the frame survives the bridge. Asserted on the whole frame, not on one field,
    # because the failure this guards against is frame-wide.
    json.dumps(rows)

    row = rows["QC345-003"]
    assert row["state"] == "refused_permanent"
    assert row["reason_available"] is True
    # The reason must still SAY something — coercing must not silently empty the one field an
    # operator acts on.
    assert "calendar never answered" in row["reason"]


def test_a_NEWER_ENGINE_whose_row_lacks_armed_cannot_raise_inside_the_route():
    """The reverse mixed-version window (ffv73l93, #998 re-review).

    `_arm_row` already covers NEW api / OLD engine: a bare bool becomes the UNKNOWN shape. The reverse
    — OLD api reading a NEWER engine's frame — was not covered, because a dict was returned VERBATIM
    and `/strategies` then indexed `arm["armed"]` directly. A newer engine that renames or drops that
    key raises `KeyError` inside the route, and both containers recreate seconds apart at every deploy,
    so this window happens on purpose every time.

    Normalising the SHAPE beats guarding the one index: every consumer is then safe, not just the
    caller that happens to be written today. Unknown keys from a newer engine are KEPT, because
    discarding them would make this the thing that breaks the next field.
    """
    from api.app import _arm_row

    # FIXTURE PROPERTY: the frame really is missing the key, or there is nothing to raise on.
    newer = {"state": "window_pending", "session": "2026-09-11", "attempts": 2,
             "calendar_window_ends_ns": 1789200000000000000}
    assert "armed" not in newer

    row = _arm_row(newer)

    assert row["armed"] is None, "a row that cannot say whether it armed must read None, never False"
    assert row["state"] == "window_pending", "the newer engine's own account must survive"
    assert row["calendar_window_ends_ns"] == 1789200000000000000, (
        "a field this api does not know about was DISCARDED — the next engine field breaks here"
    )
    # Every key the row contract promises is present, whatever the engine sent.
    for key in ("armed", "state", "session", "slot", "next_fire_ns", "reason", "reason_available",
                "attempts", "reason_at_ns"):
        assert key in row, f"{key} missing — a consumer indexing it raises inside the route"
