"""The market-aware container travels engine → consumer → HealthResponse → /strategies rows (#873).

The pydantic DTO has dropped an engine-published field three times (#233/#322/#336) and the consumer
forwards health keys by allow-list, so a key not named there reports into a void. Three-state at
every hop: None = the frame could not be read or the build lacks the plane; a dict whose `lanes[sid]`
is None = the plane is up and this lane not yet polled; readings otherwise.
"""
from __future__ import annotations

import inspect
from datetime import date

import pytest

import api.consumer as consumer_mod
from api.models import HealthResponse
from api.test_cadence_on_strategies import _drive_strategies

_FRAME = {"contract": {"state": "absent", "module": "kumo_strategies.strategies.market_events"}, "dwell": 7,
          "poll_secs": 60, "polled_at_ns": 5, "emit_failures": 0, "journal_absent": [], "journal_broken": {}, "journal_unchecked": [],
          "lanes": {"TECHIVOL-005": {"polled_at_ns": 5, "entries_blocked": {"state": "not_asked"},
                                     "emergency_exit": {"state": "yes", "polls": 2, "dwell": 7},
                                     "self_assessment": {"state": "unknown"}},
                    "QC345-003": None}}


def test_the_CONSUMER_forwards_it_and_GATES_it_on_the_bridge():
    src = inspect.getsource(consumer_mod.RedisConsumer.health)
    assert '"market_aware": self._health.get("market_aware") if bridge_ok else None' in src


def test_the_DTO_declares_it_optional_with_no_default_other_than_None():
    f = HealthResponse.model_fields["market_aware"]
    assert f.default is None
    assert HealthResponse.model_validate({**_minimal_health(), "market_aware": _FRAME}).market_aware == _FRAME


def _minimal_health() -> dict:
    return {"status": "ok", "subsystems": [], "feed_last_tick_ts": 0}


def _rows(health):
    mp = pytest.MonkeyPatch()
    try:
        return _drive_strategies(mp, today=date(2026, 9, 11), decided={}, health=health)
    finally:
        mp.undo()


def test_strategies_rows_carry_THREE_states_outside_and_inside():
    base = {"armed_lanes": {}, "next_fire_ns": {}}
    unread = _rows(base)
    assert all(r["market_aware"] is None for r in unread.values()), "no frame → None, never a clean-looking dict"
    rows = _rows({**base, "market_aware": _FRAME})
    t = rows["TECHIVOL-005"]["market_aware"]
    assert t["contract"]["state"] == "absent" and t["dwell"] == 7 and t["polled_at_ns"] == 5
    assert t["lane"]["emergency_exit"]["state"] == "yes" and t["lane"]["emergency_exit"]["polls"] == 2
    q = rows["QC345-003"]["market_aware"]
    assert q["contract"]["state"] == "absent" and q["lane"] is None, "plane up, lane not yet polled"
    other = [r for sid, r in rows.items() if sid not in ("TECHIVOL-005", "QC345-003")]
    assert other and all(r["market_aware"]["lane"] is None for r in other), "an unregistered lane reads lane None with the contract still named"
