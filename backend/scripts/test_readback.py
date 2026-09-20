"""The post-deploy readback asserts COMPLETENESS against the running api's own schema (#887).

Measured 2026-09-10/11 on paper: 55 naked shares sat invisible for five hours because the only /health
field that knew (`failed_requests`) was one neither readback read; the hand-list read 12 of 32 fields.
That is a missing PROPERTY, not a missing field. The field list here is DERIVED from `/openapi.json`
served by the api under test — the deployed schema, never the checkout's model — and every property
must be present, every container three-stated, every unknown key a failure.
"""
from __future__ import annotations

import pytest

from scripts.readback import Readback, readback

SCHEMA = {"components": {"schemas": {"HealthResponse": {"properties": {
    "status": {}, "bridge_ok": {}, "automated_lanes_running": {}, "cockpit_sha": {}, "strategies_sha": {},
    "reconcile_drift": {}, "failed_requests": {}, "protection_divergence": {}, "naked_after_reject": {},
    "inert": {}, "observations": {}, "subsystems": {},
}}}}}

def _health(**over) -> dict:
    h = {"status": "ok", "bridge_ok": True, "automated_lanes_running": 4, "cockpit_sha": "abc1234",
         "strategies_sha": "def5678", "reconcile_drift": [], "failed_requests": [], "protection_divergence": [],
         "naked_after_reject": [], "inert": [], "observations": {"failing": 0, "never_ran": 0, "never_ran_names": []},
         "subsystems": [{"name": "engine", "ok": True}]}
    h.update(over); return h

def _fetch(schema=SCHEMA, health=None):
    body = {"/openapi.json": schema, "/health": health if health is not None else _health()}
    return lambda path: body[path]


def test_the_fixture_schema_and_payload_AGREE_when_nothing_is_wrong():
    """Fixture property: the clean case really is clean — every schema property present, every container
    empty — so the failures below are attributable to the one thing each case changes."""
    props = set(SCHEMA["components"]["schemas"]["HealthResponse"]["properties"])
    assert props == set(_health()), "fixture drift: schema and payload disagree before any case runs"
    r = readback(fetch=_fetch(), expect={"cockpit_sha": "abc1234", "strategies_sha": "def5678"})
    assert r.verdict == "OK", r.findings


def test_a_schema_property_MISSING_from_the_payload_FAILS_and_is_named():
    h = _health(); del h["failed_requests"]
    r = readback(fetch=_fetch(health=h), expect={})
    assert r.verdict == "FAIL" and any("failed_requests" in f and "MISSING" in f for f in r.findings), r.findings


def test_a_payload_key_NOT_in_the_schema_FAILS_and_is_named():
    """A new engine field cannot be silently unread — that is the defect this file exists for."""
    r = readback(fetch=_fetch(health=_health(brand_new_field=[1])), expect={})
    assert r.verdict == "FAIL" and any("brand_new_field" in f and "UNKNOWN" in f.upper() for f in r.findings), r.findings


def test_a_NON_EMPTY_container_without_an_allow_entry_FAILS_with_the_content_printed():
    r = readback(fetch=_fetch(health=_health(failed_requests=[{"kind": "protection", "subject": "CRAK.ARCX"}])), expect={})
    assert r.verdict == "FAIL" and any("failed_requests" in f and "CRAK.ARCX" in f for f in r.findings), r.findings


def test_a_NON_EMPTY_container_WITH_a_dated_allow_entry_passes_and_is_still_printed():
    allow = {"observations": "2026-09-11 event-driven observers never_ran right after boot"}
    r = readback(fetch=_fetch(health=_health(observations={"failing": 0, "never_ran": 3, "never_ran_names": ["x", "y", "z"]})),
                 expect={}, allow=allow)
    assert r.verdict == "OK", r.findings
    assert any("observations" in f and "ALLOWED" in f for f in r.findings), "an allowed finding must still be visible"


def test_a_NULL_container_while_the_bridge_is_up_FAILS():
    """Three states: null means the engine did not say. With bridge_ok true that is its own alarm."""
    r = readback(fetch=_fetch(health=_health(reconcile_drift=None)), expect={})
    assert r.verdict == "FAIL" and any("reconcile_drift" in f and "null" in f.lower() for f in r.findings), r.findings


def test_a_payload_with_NO_engine_frame_is_REFUSED_before_any_list_is_read():
    h = _health(status="degraded", bridge_ok=False, automated_lanes_running=None, reconcile_drift=None,
                failed_requests=None, protection_divergence=None, naked_after_reject=None, inert=None,
                subsystems=[{"name": "engine", "ok": False}])
    r = readback(fetch=_fetch(health=h), expect={})
    assert r.verdict == "REFUSED" and len(r.findings) == 1 and "no engine frame" in r.findings[0].lower(), r.findings


def test_an_expected_scalar_that_DISAGREES_fails_by_name():
    r = readback(fetch=_fetch(), expect={"strategies_sha": "0000000"})
    assert r.verdict == "FAIL" and any("strategies_sha" in f and "0000000" in f and "def5678" in f for f in r.findings), r.findings


def test_a_subsystem_not_ok_and_a_failing_observation_are_findings():
    r = readback(fetch=_fetch(health=_health(subsystems=[{"name": "engine", "ok": True}, {"name": "postgres", "ok": False, "detail": "refused"}],
                                             observations={"failing": 1, "never_ran": 0, "never_ran_names": []})), expect={})
    assert r.verdict == "FAIL"
    assert any("postgres" in f for f in r.findings) and any("observations" in f and "failing" in f for f in r.findings), r.findings


def test_armed_lanes_and_next_fire_are_HEALTHY_when_non_empty_and_a_False_lane_is_a_finding():
    """Two containers whose non-empty state is the healthy one. Fixture property first: the payload
    carries them non-empty and the run is OK; then one lane False is a named finding."""
    ok = readback(fetch=_fetch(schema=_schema_with("armed_lanes", "next_fire_ns"),
                               health=_health(armed_lanes={"A-001": True}, next_fire_ns={"A-001": 1})), expect={})
    assert ok.verdict == "OK", ok.findings
    bad = readback(fetch=_fetch(schema=_schema_with("armed_lanes", "next_fire_ns"),
                                health=_health(armed_lanes={"A-001": True, "B-002": False}, next_fire_ns={"A-001": 1})), expect={})
    assert bad.verdict == "FAIL" and any("B-002" in f for f in bad.findings), bad.findings


def test_the_READBACK_reads_the_ROW_shape_and_does_not_call_every_lane_unarmed(): 
    """#997/#998 turn `armed_lanes` values from a bare bool into a ROW. This script reads the VALUE:

        unarmed = {lane: on for lane, on in v.items() if on is not True}

    A row dict is never `True`, so after that change EVERY lane lands in `unarmed` and the post-deploy
    readback reports all of them not armed. It fails LOUD rather than silently — `is not True` rather
    than a truthiness test, which is the one thing that saves it from being a silent wrong answer — but
    it false-alarms on every lane at the one moment the script exists for, the boot.

    BOTH SHAPES MUST WORK. The api and the engine recreate seconds apart, so a readback taken during
    that window sees the OLD bare-bool frame from a lagging engine and the NEW row frame after it
    catches up. A fix that reads only the row would break the readback in the other direction.
    """
    row = lambda armed: {"armed": armed, "state": "armed" if armed else "not_armed", "session": "2026-09-11",
                         "slot": "open+5m", "next_fire_ns": 1, "reason": None, "reason_available": False,
                         "attempts": None, "reason_at_ns": None}

    # FIXTURE PROPERTY: the row really is not a bool, or the assertion below could not fail.
    assert row(True) is not True and row(True)["armed"] is True

    ok = readback(fetch=_fetch(schema=_schema_with("armed_lanes", "next_fire_ns"),
                               health=_health(armed_lanes={"A-001": row(True), "B-002": row(True)},
                                              next_fire_ns={"A-001": 1})), expect={})
    assert ok.verdict == "OK", ok.findings

    # An unarmed lane inside a row is still the finding, and it must NAME that lane and not the others.
    bad = readback(fetch=_fetch(schema=_schema_with("armed_lanes", "next_fire_ns"),
                                health=_health(armed_lanes={"A-001": row(True), "B-002": row(False)},
                                               next_fire_ns={"A-001": 1})), expect={})
    assert bad.verdict == "FAIL", bad.findings
    assert any("B-002" in f for f in bad.findings), bad.findings
    assert not any("A-001" in f for f in bad.findings), (
        "an ARMED lane was reported as a finding — the readback is calling every lane unarmed"
    )

    # `armed: None` is UNKNOWN — the lane could not be asked — and must not read as armed.
    unknown = readback(fetch=_fetch(schema=_schema_with("armed_lanes", "next_fire_ns"),
                                    health=_health(armed_lanes={"A-001": row(None)},
                                                   next_fire_ns={"A-001": 1})), expect={})
    assert unknown.verdict == "FAIL", unknown.findings


def _schema_with(*extra):
    import copy
    s = copy.deepcopy(SCHEMA)
    for k in extra: s["components"]["schemas"]["HealthResponse"]["properties"][k] = {}
    return s


def test_the_result_type_is_frozen_and_carries_everything_the_operator_needs():
    r = readback(fetch=_fetch(), expect={})
    assert isinstance(r, Readback) and r.verdict in ("OK", "FAIL", "REFUSED") and isinstance(r.findings, list)
    with pytest.raises(Exception):
        r.verdict = "OK"  # type: ignore[misc]
