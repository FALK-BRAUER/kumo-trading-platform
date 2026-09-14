"""An engine that has told us NOTHING must not read as an engine reporting nothing wrong (#859).

Measured on paper 2026-09-10 at 19:51:07 SGT, ~70 s after an engine recreate and before the first
health frame: `/health` returned `reconcile_drift []`, `ownership_violations []`,
`protection_divergence []`, `naked_after_reject []`, `unpriced_positions []`, `inert []`,
`lanes_absent {}` and the `lanes` subsystem `ok: true`. Only `status: "degraded"` and the `engine`
subsystem were honest. Every list a reader came to check said "clean" off zero information.

THIS IS THE `/claims` LESSON, ONE ENDPOINT LATER. `build_breaches` reports `account_unreadable`
rather than an empty ledger because "a check that cannot see the account has not found zero
breaches, it has found nothing". The same sentence is true of every field below.

WHY THE TESTS BELOW DO NOT NAME FIELDS. Each previous fix in this family added one name to a list
nobody could keep complete — five times (#233, #322, #336, #546, #618). So the assertions enumerate
what `health()` ACTUALLY RETURNS and require the property of all of it: a field added tomorrow is
covered without editing this file, and a field that regresses cannot hide behind its siblings.

The consumer under test is the real `RedisConsumer`, freshly constructed. That is not a stand-in for
the no-frame state — it IS the no-frame state, exactly as it exists between process start and the
first frame off the bus, which is the window this ticket is about.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from api.consumer import RedisConsumer
from api.feed_config import load_feed_config

#: Fields that are legitimately NOT three-stated, each with a reason. The point of writing them down
#: is that an exemption has to be a decision someone made rather than an oversight nobody saw.
#:
#: `bridge_ok` is the answer to "do we know anything at all", so it is a real two-state boolean and
#: is the one field that must NOT go None — a reader cannot ask the question with an unknown.
NOT_THREE_STATED = {
    "bridge_ok",
}


def _fresh() -> RedisConsumer:
    """A consumer that has never received a frame — production's state at boot."""
    return RedisConsumer(load_feed_config())


def _with_frame(**over) -> RedisConsumer:
    """A consumer holding a CURRENT frame, so the two can be compared against each other."""
    c = _fresh()
    c._health = {
        "engine_ok": True,
        "last_tick_ts": 1_789_000_000_000_000_000,
        "reconcile_drift": [],
        "unpriced_positions": [],
        "failed_requests": [],
        "subscriptions": {"requested": 747, "bound": 107},
        "book_truth": {"instruments": 25, "net_disagrees": 0},
        "inferred_fills": [],
        "fills_on_terminal_orders": [],
        "fills_on_terminal_orders_dropped": 0,
        "venue_unanswered_lookups": 0,
        "observations": {"declared": 6, "ok": 3},
        "armed_lanes": {"MOMENTUM-002": True},
        "next_fire_ns": {"MOMENTUM-002": 1_789_047_300_000_000_000},
        "lanes_absent": {},
        "protection_divergence": [],
        "naked_after_reject": [],
        "unreconciled_orders": [],
        "automated_lanes_registered": 4,
        "automated_lanes_running": 4,
        "feed_stale": False,
        "realized_legs": {"state": "complete"},
        "log_compaction": None,
        "shortable": {"state": "ok", "listed": 0},        # #881: an IBKR data-node plane; a dict on a live frame
        **over,
    }
    c._health_at = time.monotonic()
    return c


def _stale(**over) -> RedisConsumer:
    """A consumer whose frame ARRIVED and then aged out — `_BRIDGE_STALE_SECS = 6.0` exceeded.

    THIS IS THE CASE THAT WAS ACTUALLY MEASURED, and the reason it is a separate fixture. On paper
    2026-09-10 the deploy readback saw a good frame at 19:50:52 (it gated on
    `automated_lanes_running` being non-null before reading), and at 19:51:07 there had been none
    for at least six seconds. The engine had spoken and then stopped — never-arrived is the boot
    window, stale is the incident.

    They are not interchangeable: `_health` still holds the last frame's contents here, so any fix
    that keys on "is the dict empty" rather than on the bridge passes the never-arrived case and
    fails this one.
    """
    c = _with_frame(**over)
    c._health_at = time.monotonic() - (RedisConsumer._BRIDGE_STALE_SECS + 60.0)
    return c


# ==================================================================================================
# The fixture's own properties, first. A test whose fixture cannot violate the invariant passes for
# free — kumo-strategies shipped one that stayed green with the look-ahead deliberately reintroduced.
# ==================================================================================================


def test_the_fresh_consumer_really_has_no_frame():
    """Vacuity guard. If a bare consumer somehow counted as bridged, every assertion below would be
    checking the HEALTHY path and would pass no matter how the absent path behaved."""
    c = _fresh()
    # THE SENTINEL IS `0.0`, NOT `None` — asserted here because getting it wrong is how this guard
    # would pass while testing nothing. `_bridge_ok` reads it as `if self._health_at else None`, so a
    # falsy zero means "never received"; it survives only because `time.monotonic()` is never 0.
    assert not c._health_at, "a fresh consumer already carries a frame timestamp"
    assert c._bridge_ok() is False, (
        "a consumer that has never received a frame reports the bridge as live — the no-frame state "
        "this whole file is about cannot be reached, so nothing below asserts anything"
    )


def test_the_framed_consumer_really_IS_bridged():
    """The other half of the fixture. Without this, `_with_frame` could be silently producing the
    same no-frame state and the disagreement test below would compare a thing against itself."""
    assert _with_frame()._bridge_ok() is True, (
        "the framed fixture is not bridged, so 'with a frame' and 'without a frame' are the same "
        "case and the comparison proves nothing"
    )


def test_the_two_fixtures_DISAGREE_on_something():
    """Verification by disagreement, applied to the harness itself.

    Two derivations of one fact that agree when one of them is severed is a dead mechanism. If the
    framed and unframed payloads were identical, this file would be unable to detect the defect it
    exists for — and would say so here rather than in a green run somewhere else."""
    absent, present = _fresh().health(), _with_frame().health()
    differing = {k for k in present if absent.get(k) != present.get(k)}
    assert differing, (
        "a consumer with a frame and a consumer without one produce byte-identical health payloads "
        "— the frame is not reaching the projection at all"
    )
    # NOT `differing` ALONE (codex, test-coverage review). `bridge_ok` differs between these two by
    # construction, so the assertion above is satisfied before any container is looked at — it would
    # stay green with every list degrading to empty, which is the entire defect. The guard has to be
    # about the fields under test.
    containers = differing & set(_container_fields(present))
    assert containers, (
        f"the two payloads differ only in {sorted(differing)} — no CONTAINER field changes between a "
        f"live frame and no frame at all, so this file cannot detect what it was written for"
    )


# ==================================================================================================
# The property itself
# ==================================================================================================


def _container_fields(payload: dict) -> list[str]:
    """Every key whose WITH-FRAME value is a container — the fields that can degrade to empty.

    Derived from the framed payload rather than listed, because listing is the failure mode this
    family keeps repeating. A scalar cannot be "empty when it means unknown", so only containers are
    in scope here; the scalars in this payload already degrade to None and are pinned below.
    """
    return sorted(k for k, v in payload.items()
                  if isinstance(v, (list, dict, set, tuple)) and k not in NOT_THREE_STATED)


#: The container fields as they stood when this ticket was filed. FROZEN, because the sweep derives
#: its cases from the LIVE payload — so an implementation that wrongly returns None on the live path
#: would silently remove that field from its own examination (codex, test-coverage review). A count
#: guard prevents total collapse; only a named set prevents losing one.
#:
#: Adding a field here is correct and expected. REMOVING one must be a decision someone writes down.
CONTAINERS_AT_FILING = frozenset({
    "armed_lanes", "book_truth", "failed_requests", "fills_on_terminal_orders", "inferred_fills",
    "lanes_absent", "naked_after_reject", "next_fire_ns", "observations", "protection_divergence",
    "reconcile_drift", "subscriptions", "unpriced_positions", "unreconciled_orders",
    # #881 added `shortable` (IBKR data nodes; None on Alpaca). Frozen here the day it landed so it
    # cannot drop out of the sweep silently — the "looking at less" failure this file names.
    "shortable",
})


def test_the_fixture_finds_container_fields():
    """Vacuity guard for the parametrisation below: an empty list of fields would make every one of
    those cases vacuously pass."""
    fields = set(_container_fields(_with_frame().health()))
    assert len(fields) >= 8, (
        f"only {len(fields)} container fields found ({sorted(fields)}) — the framed fixture is not "
        f"carrying the payload this ticket is about, so the sweep below is not covering it"
    )
    lost = sorted(CONTAINERS_AT_FILING - fields)
    assert not lost, (
        f"{lost} no longer present as containers on a LIVE frame, so the sweep below silently "
        f"stopped examining them. A field that drops out of its own oracle is how this family keeps "
        f"shipping: the test goes green by looking at less."
    )


@pytest.mark.parametrize("field", sorted(CONTAINERS_AT_FILING))
def test_a_container_field_is_NOT_None_when_a_LIVE_frame_carries_it(field):
    """The mirror of the sweep, and the reason the fix cannot be "return None always".

    Only a couple of live cases were pinned before this (codex, test-coverage review): a fix that
    answered None for most live containers would have satisfied the file while destroying the very
    distinction it was written to create.
    """
    value = _with_frame().health()[field]
    assert value is not None, (
        f"`{field}` is None on a LIVE frame — unknown has swallowed the measurement, which is this "
        f"ticket's defect with its sign flipped"
    )


@pytest.mark.parametrize("field", _container_fields(_with_frame().health()))
def test_a_container_field_is_NONE_when_no_frame_has_arrived(field):
    """`[]` is a measurement. `None` is the absence of one. They must not be the same value.

    On 2026-09-10 a reader checking `reconcile_drift`, `protection_divergence` and `inert` got three
    empty lists from an engine that had published nothing, ~70 s after a recreate. The deploy
    readback is exactly such a reader.
    """
    value = _fresh().health()[field]
    assert value is None, (
        f"`{field}` is {value!r} with no frame on the bus. An empty container is what the engine "
        f"says when it has looked and found nothing; this engine has not spoken. `0 of 0` is not "
        f"`0 of 4`."
    )


@pytest.mark.parametrize("field", sorted({
    "automated_lanes_registered", "automated_lanes_running", "venue_unanswered_lookups",
    "feed_stale", "realized_legs", "log_compaction",
}))
def test_the_scalars_that_already_degrade_correctly_STAY_that_way(field):
    """These were right before this ticket and are the model the containers are being moved to.

    Pinned so the fix cannot regress them on its way past — the #568 shape, where a fix reintroduced
    the very bug it was written for.
    """
    assert _fresh().health()[field] is None, (
        f"`{field}` no longer degrades to None with no frame — this ticket moved the containers TO "
        f"this behaviour and must not have moved this one away from it"
    )


def test_bridge_ok_IS_the_question_and_must_stay_a_real_boolean():
    """The one field that must not become None.

    Everything else says "I do not know"; this is the field that says WHY, and a reader cannot
    interrogate an unknown. It is False with no frame and True with one — never None.
    """
    assert _fresh().health()["bridge_ok"] is False
    assert _with_frame().health()["bridge_ok"] is True


def test_an_EMPTY_frame_still_reports_EMPTY_and_not_unknown():
    """The third state has to be reachable in BOTH directions or this is just a rename.

    A live engine that genuinely has no drift publishes `reconcile_drift: []`, and that must survive
    as `[]`. If the fix turned every empty container into None it would destroy the distinction it
    was written to create — the same defect wearing the opposite sign.
    """
    payload = _with_frame(reconcile_drift=[], naked_after_reject=[]).health()
    assert payload["reconcile_drift"] == [], (
        "a frame that says 'no drift' now reads as 'unknown' — the fix inverted the defect instead "
        "of removing it"
    )
    assert payload["naked_after_reject"] == []


# ==================================================================================================
# The ENDPOINT hop — where an empty dict became `lanes: ok=true`
# ==================================================================================================


def _subsystems_with(observed: dict, monkeypatch) -> dict:
    """`_probe_subsystems` with redis/postgres stubbed healthy, so only the ENGINE half is under test.

    Both are probed live over the network. Stubbing them green is deliberate: it isolates the field
    this ticket is about, and it makes the `lanes` verdict the only thing that can move the result.
    """
    import api.app as app_mod

    async def _ok_redis(_host, _port):
        return True, ""

    async def _ok_pg():
        return True, ""

    monkeypatch.setattr(app_mod, "check_redis", _ok_redis)
    monkeypatch.setattr(app_mod, "check_postgres", _ok_pg)
    import api.db.engine as db_engine

    monkeypatch.setattr(db_engine, "pool_stats", lambda: {"checked_out": None})
    return {s.name: s
            for s in asyncio.run(app_mod._probe_subsystems(observed))}


def test_the_LANES_subsystem_is_not_OK_when_no_frame_has_arrived(monkeypatch):
    """The sharp end of #859, and the one that is worse than an ambiguous empty.

    `absent = observed.get("lanes_absent") or {}` then `ok = not absent` does not merely RENDER an
    unknown as empty — it MANUFACTURES a positive verdict out of it. On 2026-09-10 the lanes
    subsystem read `ok: true` while the engine had published nothing at all, next to an engine
    subsystem that correctly said "no fresh engine health frames on the bus".

    `ok` must be None — unknown — never True. The engine subsystem alone is what says why.
    """
    by_name = _subsystems_with(_fresh().health(), monkeypatch)
    assert by_name["engine"].ok is False, (
        "fixture is inert: the engine subsystem must already be reporting the bridge down, or this "
        "is not the no-frame case at all"
    )
    assert by_name["lanes"].ok is not True, (
        "the lanes subsystem reports OK from a payload with no engine frame in it — a positive "
        "verdict manufactured out of an absent one, which is how #539 shipped `status=ok, 3/3` for "
        "a session with a lane that never built"
    )
    assert by_name["lanes"].ok is None, (
        f"lanes.ok is {by_name['lanes'].ok!r}; with nothing known it must be None (unknown), not a "
        f"verdict in either direction"
    )


def test_the_LANES_subsystem_still_answers_when_a_frame_IS_present(monkeypatch):
    """The other direction, so the fix cannot be "return None always".

    A live engine reporting no absent lanes is a real ok, and a live engine reporting an absent lane
    is a real not-ok. Both must survive.
    """
    healthy = _subsystems_with(_with_frame().health(), monkeypatch)
    assert healthy["lanes"].ok is True, (
        "a live frame saying no lane is absent must read as OK — the fix turned a measurement into "
        "an unknown, which is the defect with its sign flipped"
    )
    broken = _subsystems_with(
        _with_frame(lanes_absent={"QC345-003": "did not build"}).health(), monkeypatch)
    assert broken["lanes"].ok is False, "a live frame naming an absent lane must read as NOT ok"
    assert "QC345-003" in broken["lanes"].detail


def test_bridge_ok_REACHES_THE_RESPONSE():
    """`consumer.health()`'s own comment tells readers to distinguish "no divergence" from "no
    engine" via `bridge_ok` — and `/health` never carried the field, so that instruction was
    unfollowable. Eighth field to die in this hop after `last_equity`, `realized_session`,
    `realized_periods`, `next_fire_ns`, `unpriced_positions`, `failed_requests` and `feed_stale`.
    """
    from api.models import HealthResponse

    assert "bridge_ok" in HealthResponse.model_fields, (
        "`bridge_ok` is not a field on HealthResponse, so a reader cannot make the distinction the "
        "consumer's comment instructs them to make"
    )


# ==================================================================================================
# THE SEAM, not the unit. A consumer-only fix would pass everything above and change nothing.
# ==================================================================================================
#
# `app.py` re-defaults on its own: `reconcile_drift=observed.get("reconcile_drift", []) or []` and
# `protection_divergence=... or []`. That `or []` converts a None the consumer just started
# returning straight back into an empty list, so the endpoint keeps reporting a clean book from an
# absent engine while every consumer-level assertion above goes green.
#
# This is the shape CLAUDE.md calls out: a passing test on a helper says nothing about whether
# anything calls it correctly. The defect was REPORTED at /health, so it has to be pinned at /health.


def _health_response(observed: dict, monkeypatch, inert=None):
    """The real `/health` handler, driven with a node whose observation is `observed`.

    Only the two halves that need a database are stubbed — the claims split and the inert-lane
    contradiction probe. Everything the ticket is about is the real code path.
    """
    import api.app as app_mod

    class _Node:
        def health(self):
            return observed

        def positions(self):
            return []

    async def _no_split(_node):
        return {"status": "ok", "pairs": [], "error": None}

    async def _no_inert(_observed):
        return []

    monkeypatch.setattr(app_mod.app.state, "node", _Node(), raising=False)
    monkeypatch.setattr(app_mod, "_split_divergence", _no_split)
    # THE CALLER'S OVERRIDE WINS. This helper used to install `_no_inert` unconditionally, so a test
    # that had already patched in a spy had it silently replaced and asserted on an empty dict — the
    # harness quietly disabling the observation the test was built around.
    monkeypatch.setattr(app_mod, "_inert_contradictions", inert or _no_inert)

    async def _ok_redis(_host, _port):
        return True, ""

    async def _ok_pg():
        return True, ""

    monkeypatch.setattr(app_mod, "check_redis", _ok_redis)
    monkeypatch.setattr(app_mod, "check_postgres", _ok_pg)
    import api.db.engine as db_engine

    monkeypatch.setattr(db_engine, "pool_stats", lambda: {"checked_out": None})
    return asyncio.run(app_mod.health()).model_dump()


def test_the_endpoint_fixture_can_express_the_bug(monkeypatch):
    """Vacuity guard. A framed observation must produce a MEASURED payload here, or the no-frame
    assertions below are comparing two unknowns and would pass against any implementation."""
    body = _health_response(_with_frame().health(), monkeypatch)
    assert body["reconcile_drift"] == [], (
        "the endpoint fixture cannot even render a live frame's empty list — the assertions below "
        "would be pinning a broken harness rather than the defect"
    )
    assert body["status"] in ("ok", "degraded")


@pytest.mark.parametrize("field", sorted({
    "reconcile_drift", "protection_divergence", "naked_after_reject", "unpriced_positions",
    "book_truth", "subscriptions", "observations", "inferred_fills", "fills_on_terminal_orders",
    "armed_lanes", "next_fire_ns",
    # ADDED AFTER THE TEST-COVERAGE REVIEW (codex). `app.health()` serialises this one itself with
    # `observed.get("lanes_absent") or {}` (app.py:659), so a None arriving from the consumer is
    # erased at the endpoint — the field is exactly where the two hops disagree.
    "lanes_absent",
}))
def test_the_ENDPOINT_reports_unknown_not_empty_when_no_frame_has_arrived(field, monkeypatch):
    """The defect as it was actually MEASURED — at `/health`, not at the consumer.

    Named explicitly rather than swept, and that is deliberate here: the endpoint's own defaults are
    the thing under test, so deriving the list from the endpoint's output would let a field that
    defaults to `[]` vouch for itself.
    """
    value = _health_response(_fresh().health(), monkeypatch)[field]
    assert value is None, (
        f"/health reports `{field}` as {value!r} with no engine frame on the bus. This is the exact "
        f"reading taken on paper 2026-09-10 19:51:07 SGT, and it is what makes an absent engine look "
        f"like a clean book."
    )


def test_the_endpoint_STATUS_is_degraded_when_nothing_is_known(monkeypatch):
    """The one honest field on 2026-09-10, pinned so it stays honest.

    `status` is the only thing a reader can gate on before any of this is trustworthy, and the
    ticket asks the post-deploy readback to refuse on it.
    """
    body = _health_response(_fresh().health(), monkeypatch)
    assert body["status"] == "degraded", (
        "with no engine frame, redis and postgres stubbed healthy, /health still says ok — the "
        "readback has nothing left to refuse on"
    )
    assert body["bridge_ok"] is False


# ==================================================================================================
# The STALE case — the one that was actually measured, and a different bug from never-arrived
# ==================================================================================================


def test_the_stale_fixture_really_IS_stale_and_still_HOLDS_its_last_frame():
    """Vacuity guard with TWO halves, because either one alone lets a wrong fix through.

    Stale must read as not-bridged, or this is the healthy path in disguise. And `_health` must
    still CARRY the old frame's contents, or stale collapses into never-arrived and a fix that keys
    on "the dict is empty" instead of on the bridge would pass both.
    """
    c = _stale()
    assert c._bridge_ok() is False, "the stale fixture still reads as bridged"
    assert c._health, (
        "the stale fixture dropped its frame contents, so it is indistinguishable from never-arrived "
        "— and a fix that checks `if not self._health` would pass this file while leaving the "
        "measured incident unfixed"
    )
    assert c._health.get("automated_lanes_running") == 4, (
        "the retained frame no longer carries the values that make staleness dangerous"
    )


@pytest.mark.parametrize("field", _container_fields(_with_frame().health()))
def test_a_container_field_is_NONE_when_the_frame_went_STALE(field):
    """A frame that stopped arriving is not a measurement of now.

    Every one of these already reads `... if bridge_ok else []` — the gate is right and the VALUE it
    degrades to is the defect. `lanes_absent` is the single exception and is argued separately below.
    """
    if field == "lanes_absent":
        pytest.skip("deliberately retained on a stale frame — see the test below")
    value = _stale().health()[field]
    assert value is None, (
        f"`{field}` is {value!r} six seconds after the engine stopped publishing. The engine said "
        f"this once; it is not saying it now."
    )


def test_lanes_absent_is_RETAINED_when_stale_but_UNKNOWN_when_never_told():
    """The one field that must behave DIFFERENTLY in the two cases, and the argument for each.

    consumer.py deliberately does not gate `lanes_absent` on the bridge: "a lane that failed to
    build is still absent when the engine dies, and forgetting it on staleness would turn a real
    outage into a clean-looking one at exactly the wrong moment". That argument is correct — and it
    is an argument about STALE, where we were told once and the fact has not expired.

    It says nothing about NEVER TOLD. At boot there is no last known value to retain, so `{}` there
    is not "no lane failed to build", it is "nobody has looked yet" — which is the whole ticket.
    Three states, and this field is where the difference is sharpest.
    """
    stale = _stale(lanes_absent={"QC345-003": "did not build"}).health()["lanes_absent"]
    assert stale == {"QC345-003": "did not build"}, (
        "a lane known to have failed to build was forgotten because the frame went stale — that "
        "turns a real outage into a clean-looking one, which is what the no-gate decision prevents"
    )
    assert _fresh().health()["lanes_absent"] is None, (
        "at boot, before any frame, `lanes_absent` reports {} — an empty dict claiming no lane "
        "failed to build, from a process that has never been told anything"
    )


# ==================================================================================================
# Fields the ENDPOINT computes for itself — invisible to the consumer sweep by construction
# ==================================================================================================


def test_feed_last_tick_ts_reports_ZERO_from_a_frameless_engine():
    """`feed_last_tick_ts` is not gated at all: `int(self._health.get("last_tick_ts", 0))` runs
    outside the bridge check (consumer.py), and the endpoint reads it the same way.

    Zero is the epoch, which every consumer ages into "infinitely stale" — so this one happens to
    fail SAFE, and that is the only reason it is not in the sweep above. It is pinned here so the
    reason is written down rather than rediscovered: if anything ever starts treating 0 as "no data
    yet, nothing to worry about", this is the field that will carry the lie.

    RECORDED, NOT DEMANDED. Changing it to None is a UI-visible change and belongs to whoever owns
    the freshness banner, not to this ticket.
    """
    assert _fresh().health()["last_tick_ts"] == 0
    assert _stale().health()["last_tick_ts"] != 0, (
        "a stale frame forgot its last tick — staleness is measured FROM that number, so dropping it "
        "would make a dead feed unmeasurable rather than obviously old"
    )


def test_the_INERT_check_gets_UNKNOWN_rather_than_an_empty_lane_set(monkeypatch):
    """The knock-on this ticket unblocks, and the reason it is more than cosmetic.

    `_inert_contradictions` already has the guard: its own comment says a stale bridge means UNKNOWN
    and that reading `{}` as "no lanes are registered" turned the detector into the liar — minutes
    after the deploy that shipped it, /health announced four lanes "set to TRADING but never
    registered" while /strategies listed five, and it FAILED `make up`. The guard never fired
    because `{}` is not `None`.

    So the consumer answering None for `armed_lanes` is what makes that existing guard work. This
    pins the wiring rather than the guard: the value reaching `_inert_contradictions` from a
    frameless consumer must be None, not {}.
    """
    seen = {}

    async def _spy(observed):
        seen["armed_lanes"] = observed.get("armed_lanes")
        return []

    import api.app as app_mod

    _health_response(_fresh().health(), monkeypatch, inert=_spy)
    assert "armed_lanes" in seen, "the inert check was never reached — this test asserts nothing"
    assert seen["armed_lanes"] is None, (
        f"the inert check received {seen['armed_lanes']!r} from an engine that has published "
        f"nothing. Its unknown-guard tests for None, so {{}} walks straight past it and the check "
        f"reports contradictions it cannot possibly know about — the exact failure its own comment "
        f"describes."
    )


def test_the_DROPPED_counter_says_unknown_rather_than_zero(monkeypatch):
    """A defence that hides what it discarded is a second bug — and so is one that cannot say whether
    it was ever asked. `int(... or 0)` at the endpoint made "no frame" and "nothing dropped" the same
    number. 0 of 0 is not 0 of 4.
    """
    assert _fresh().health()["fills_on_terminal_orders_dropped"] is None
    assert _health_response(_fresh().health(), monkeypatch)["fills_on_terminal_orders_dropped"] is None
    live = _health_response(_with_frame(fills_on_terminal_orders_dropped=3).health(), monkeypatch)
    assert live["fills_on_terminal_orders_dropped"] == 3, (
        "a real dropped count no longer survives — unknown has swallowed the measurement"
    )


# ==================================================================================================
# Two mechanisms that a mutation bite found UNEARNED — caught the same way #748's `lane_of` was
# ==================================================================================================


def test_bridge_ok_is_FORWARDED_and_not_merely_defaulted(monkeypatch):
    """Deleting `bridge_ok=bool(observed.get("bridge_ok"))` from /health killed NOTHING.

    Because the model's default is False and every assertion tested the frameless case, where a
    forwarded False and a defaulted False are the same value. Agreement is not connection: it is the
    exact condition under which a severed wire is invisible.

    So this asserts the value the default CANNOT produce. A live frame must make it True.
    """
    live = _health_response(_with_frame().health(), monkeypatch)
    assert live["bridge_ok"] is True, (
        "bridge_ok is False on a LIVE frame — the endpoint is reporting the model's default rather "
        "than the consumer's answer, and every reader told to gate on it is gating on a constant"
    )
    assert _health_response(_fresh().health(), monkeypatch)["bridge_ok"] is False


def _apply_health(c: RedisConsumer, ts: int) -> None:
    """One health frame through the REAL arrival path, not by assigning `_health`.

    The gap is computed inside `_apply`, so a test that set the attributes directly would measure
    nothing — the seam, not the unit.
    """
    c._apply({"type": "health", "payload": json.dumps({"engine_ok": True, "ts": ts})})


def test_the_gap_counter_counts_EVERY_outage_and_keeps_the_WORST(monkeypatch):
    """Deleting `self._bridge_gaps += 1` killed nothing either — the counter shipped with no test.

    It is modelled on the measurement that motivated it: the bridge FLAPS. Three outages inside one
    span, of different widths, one of them below the threshold. A counter that recorded only the last
    gap would report the final 7 s and hide that it happened three times; one that recorded only a
    max would say 9 s and hide the same thing.
    """
    c = _fresh()
    _apply_health(c, 1)
    assert c._bridge_gaps == 0 and c._bridge_gap_max_s == 0.0, (
        "the FIRST frame was counted as an outage — there is no previous arrival to measure from, "
        "and a permanent 1 destroys a counter whose value is that non-zero is unusual"
    )

    for i, gap in enumerate((9.0, 3.0, 7.0), start=2):
        c._health_at -= gap          # the bridge was quiet this long before the next frame landed
        _apply_health(c, i)

    assert c._bridge_gaps == 2, (
        f"expected 2 reader-visible outages (9 s and 7 s exceed _BRIDGE_STALE_SECS = "
        f"{RedisConsumer._BRIDGE_STALE_SECS}; the 3 s one does not), got {c._bridge_gaps}"
    )
    assert 8.9 < c._bridge_gap_max_s < 9.2, (
        f"the worst gap should be the 9 s one, got {c._bridge_gap_max_s} — a max that tracks the LAST "
        f"gap rather than the worst would report ~7"
    )
    out = _health_response(c.health(), monkeypatch)
    assert out["bridge_gaps"] == 2 and 8.9 < out["bridge_gap_max_s"] < 9.2, (
        "the counter does not survive the endpoint hop — the field-list trap this whole family keeps "
        "falling into, one more time"
    )


def test_the_gap_counter_SURVIVES_the_outage_it_is_measuring(monkeypatch):
    """It must be readable exactly when the bridge is down, which is when a reader comes looking.

    Gating it on `bridge_ok` like its neighbours would blank the number during the very event it
    exists to record — so it is deliberately NOT gated, and that has to be pinned or someone will
    "fix" the inconsistency.
    """
    c = _fresh()
    _apply_health(c, 1)
    c._health_at -= 20.0
    _apply_health(c, 2)
    c._health_at -= 3600.0           # now stale
    assert c._bridge_ok() is False, "fixture is inert: the bridge is not actually down"
    assert c.health()["bridge_gaps"] == 1, (
        "the outage count was blanked because the bridge is down — hiding the measurement at exactly "
        "the moment it is being asked for"
    )
    assert _health_response(c.health(), monkeypatch)["bridge_gaps"] == 1
