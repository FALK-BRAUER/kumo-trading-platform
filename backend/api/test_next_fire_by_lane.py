"""The deploy must be able to see when a lane is next due — from the LANE'S OWN alert.

WHAT THIS COST. On 2026-08-25 I scheduled deploys around "the next decision is 12:00 ET", having
assumed QC345-003 shared TECHIVOL-005's `open+150m`. It fires at `open+100m` — 11:10 ET — and I
recreated the engine at exactly 11:10. The alert never fired, `_arm` scheduled tomorrow's, and
`exec_action_log` for QC345 is empty for the whole day. TECHIVOL decided normally, so nothing at the
stack level looked wrong.

kumo-strategies confirmed: there is no catch-up, and skipping late is CORRECT — a decision fired
hours after its slot trades a stale ranking at moved prices. They also found this is the THIRD lane to
hit it: `momentum_rotation._report_missed_on_start` was built for the identical incident on
2026-08-17 and never propagated to the two adapters that schedule off an offset.

SO THE FIX IS NOT TO DEPLOY INTO THE WINDOW, and that requires knowing the window.

READ THE CLOCK, DO NOT RE-DERIVE THE SLOT. Each lane arms itself — `_arm()` computes `fire_at` from
its calendar and its own offset, then calls `clock.set_time_alert(...)`. A slot table in cockpit would
be a second derivation of that fact and would reproduce the exact assumption that caused this.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.engine_node import next_fire_by_lane


def _lane(*due_ns, name="qc345_session_decide"):
    """A lane shaped like Nautilus's: named timers, each with a next time.

    THE NAMES MATTER AND MY FIRST DOUBLE USED `t0`, `t1`. Production timer names are the adapters'
    own constants — `session_decide`, `qc27_session_decide`, `qc345_session_decide` — and the
    function now selects on them. A double naming its timers arbitrarily passed while the deployed
    gate refused every deploy with "MOMENTUM-002 decides in 1s", because it was taking the minimum of
    ROUTINE timers. A double that cannot represent production is the bug.
    """
    times = {f"{name}-{i}" if i else name: v for i, v in enumerate(due_ns)}
    return SimpleNamespace(clock=SimpleNamespace(
        timer_names=list(times),
        next_time_ns=lambda n: times[n],
    ))


def test_the_fixture_exposes_timers_the_way_nautilus_does():
    """FIXTURE FIRST. `next_fire_by_lane` swallows everything and returns None, so a double that
    cannot answer is indistinguishable from a lane with no alert."""
    lane = _lane(1_700_000_000_000_000_000)
    assert lane.clock.timer_names, "the double has no timers; every assertion below would read None"
    assert lane.clock.next_time_ns("qc345_session_decide") == 1_700_000_000_000_000_000


def test_it_reports_the_lane_s_OWN_next_alert():
    out = next_fire_by_lane({"QC345-003": _lane(1_700_000_000_000_000_000)})
    assert out["QC345-003"] == 1_700_000_000_000_000_000


def test_the_EARLIEST_alert_wins_when_a_lane_has_several():
    """A lane may hold more than one timer. The deploy cares about the SOONEST — taking any other
    would wave through a deploy into the window it exists to protect."""
    out = next_fire_by_lane({"A-001": _lane(500, 100, 900)})
    assert out["A-001"] == 100


def test_a_lane_with_NO_alert_is_None_not_zero():
    """Unknown must be distinguishable from due-now. `0` would read as "overdue" and refuse every
    deploy forever, and a gate that always refuses gets switched off."""
    assert next_fire_by_lane({"B-002": _lane()})["B-002"] is None


def test_a_lane_that_CANNOT_be_asked_is_None_and_does_not_raise():
    """Health must never raise. A lane without a clock is unknown, not an outage."""
    assert next_fire_by_lane({"C-003": SimpleNamespace()})["C-003"] is None

    class _Hostile:
        @property
        def clock(self):
            raise RuntimeError("gone")

    assert next_fire_by_lane({"D-004": _Hostile()})["D-004"] is None


def test_every_lane_appears_even_when_it_cannot_answer():
    """A missing KEY and a None VALUE read differently to the gate: absent looks like a lane that does
    not exist, which is how a deploy would skip checking one."""
    out = next_fire_by_lane({"A-001": _lane(100), "B-002": SimpleNamespace()})
    assert set(out) == {"A-001", "B-002"}


def test_the_CONSUMER_carries_next_fire_ns_or_the_deploy_gate_reads_nothing():
    """THE SEAM THAT ATE IT ONCE ALREADY, within one deploy cycle of writing the producer.

    `consumer.py`'s health dict is an ALLOW-LIST: it names each key it forwards from the engine's
    frame. `next_fire_ns` was published by the engine, reached the bridge, and `/strategies` still
    read `None` — because a key nobody names there is silently gone.

    That is the FOURTH field lost to this seam, after `last_equity`, `realized_session` and
    `realized_periods`. `AccountDTO`'s own docstring warns about it. Knowing about a trap is not the
    same as being caught by it, apparently.

    Asserted on source because the alternative is standing up a consumer with a live bridge.
    """
    import pathlib

    src = (pathlib.Path(__file__).parent / "consumer.py").read_text()
    assert '"next_fire_ns"' in src, (
        "consumer.py does not forward `next_fire_ns`. The engine publishes it, the bridge carries it, "
        "and the deploy gate reads None — so every lane reports 'no next fire' and the gate warns "
        "instead of protecting"
    )
    # Dropped on a stale frame, like `armed_lanes` — a fire time from a dead engine is not a schedule.
    line = next(l for l in src.splitlines() if '"next_fire_ns"' in l and "self._health" in l)
    assert "bridge_ok" in line, (
        f"`next_fire_ns` is carried forward on a stale frame: {line.strip()}. The gate would wave a "
        f"deploy through on a dead engine's 'nothing due for hours'"
    )


def test_ROUTINE_timers_are_ignored_only_the_decision_alert_counts():
    """THE DEFECT THE DEPLOYED GATE HIT. A lane's clock carries bar-aggregation and housekeeping
    timers that are always seconds away. Taking the minimum of all of them made the gate refuse at
    12:58 ET on a lane that decides at 09:35 — and a gate that fires on the normal path gets switched
    off, which is the whole thing this mechanism exists to avoid."""
    from types import SimpleNamespace as NS

    times = {"bar-1min": 1_000, "some-housekeeping": 2_000, "qc27_session_decide": 9_999_000}
    lane = NS(clock=NS(timer_names=list(times), next_time_ns=lambda n: times[n]))

    assert next_fire_by_lane({"TECHIVOL-005": lane})["TECHIVOL-005"] == 9_999_000, (
        "a routine timer was reported as the next DECISION — the gate would refuse every deploy"
    )


def test_every_adapters_alert_name_is_recognised():
    """One substring, three adapters. A hardcoded list would miss the next one silently."""
    from types import SimpleNamespace as NS

    for alert in ("session_decide", "qc27_session_decide", "qc345_session_decide"):
        times = {"bar-1min": 10, alert: 5_000}
        lane = NS(clock=NS(timer_names=list(times), next_time_ns=lambda n: times[n]))
        assert next_fire_by_lane({"X": lane})["X"] == 5_000, f"{alert} not recognised"


def test_a_lane_with_ONLY_routine_timers_reports_None():
    """Unknown, not "due in 1s". The gate names an unknown lane out loud instead of blocking on it."""
    from types import SimpleNamespace as NS

    times = {"bar-1min": 1_000}
    lane = NS(clock=NS(timer_names=list(times), next_time_ns=lambda n: times[n]))
    assert next_fire_by_lane({"Y": lane})["Y"] is None
