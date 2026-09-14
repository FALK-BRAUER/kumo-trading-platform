"""The never-ran detector must expect the slots the lanes ACTUALLY run (#378).

Measured 2026-08-29 on paper: every `*_SLOTS` setting is `[]`.

    the LANES read []  ->  "keep the built-in schedule"  (slots_from_settings: `if not raw: return
                            default`; kumo-strategies does the same, `if not got: return`)
    the DETECTOR reads []  ->  "nothing is due"          (never_ran declares nothing for an empty list)

So four lanes decide on their built-in schedules while the detector expects nothing from any of
them, and "armed and silent" reads exactly like "healthy". That is the outage this ticket was filed
for — BCTROT missed both slots on 2026-08-19 and nobody knew for three days — with the detector
built, wired, green in its own tests, and unable to fire.

Two derivations of "which slots are due". This pins that there is ONE: the detector resolves
through `slots_from_settings`, the same function the lanes resolve through.
"""

from __future__ import annotations

from api.slot_outcome import effective_expected_slots


def test_the_fixture_is_the_live_shape():
    """FIXTURE PROPERTY, and the first version of THIS test was vacuous (review, 2026-08-29): it
    built a dict of empty lists and asserted they were empty — a literal compared with itself.

    What it must establish is that an empty override is the shape that USED to blind the detector:
    resolving it through the OLD rule (raw settings) yields nothing, so the assertions below are
    about a real change of behaviour rather than a tautology."""
    cfg = {"BCTROT-004_SLOTS": [], "MOMENTUM-002_SLOTS": [], "QC345-003_SLOTS": []}
    old_rule = {k[: -len("_SLOTS")]: v for k, v in cfg.items() if v}
    assert old_rule == {}, "the old raw-settings rule already declared something — wrong fixture"
    assert effective_expected_slots(cfg), "the new rule declares nothing either — nothing changed"


def test_an_EMPTY_override_expects_the_lanes_BUILT_IN_slots():
    cfg = {"BCTROT-004_SLOTS": []}
    out = effective_expected_slots(cfg, defaults={"BCTROT-004": ("open+150m", "close-20m")})
    assert out == {"BCTROT-004": ("open+150m", "close-20m")}, (
        "an empty override still declares nothing — the detector cannot fire on the lanes it "
        "exists to watch (#378)"
    )


def test_a_REAL_override_wins_over_the_default():
    cfg = {"BCTROT-004_SLOTS": ["open+215m"]}
    out = effective_expected_slots(cfg, defaults={"BCTROT-004": ("open+150m", "close-20m")})
    assert out == {"BCTROT-004": ("open+215m",)}


def test_a_MALFORMED_override_falls_back_like_the_lane_does():
    """The lanes fall back to the built-in on a bad slot rather than refusing to run; the detector
    must expect what the lane will actually do, not what the operator typed."""
    cfg = {"BCTROT-004_SLOTS": ["not-a-slot"]}
    out = effective_expected_slots(cfg, defaults={"BCTROT-004": ("open+150m",)})
    assert out == {"BCTROT-004": ("open+150m",)}


def test_a_lane_with_NO_KNOWN_DEFAULT_declares_nothing_rather_than_guessing():
    """Three states: overridden / built-in / unknown. A strategy cockpit has no default for is not
    silently assumed to decide at the open — that would alarm on every lane it does not understand."""
    cfg = {"MYSTERY-009_SLOTS": []}
    assert effective_expected_slots(cfg, defaults={}) == {}


def test_the_ANNOUNCER_uses_the_effective_map_not_raw_settings():
    """THE WIRING. The helper being right proves nothing while `_announce_never_ran` still reads
    `cfg[...]` directly — which is exactly how this shipped."""
    import ast
    import inspect
    import textwrap

    import api.alerts as al

    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(al.AlertsService._announce_never_ran))))
    assert "effective_expected_slots(" in src, (
        "the announcer still builds `expected` from raw settings — empty overrides blind it"
    )


def test_the_BUILDERS_use_the_SHARED_defaults_not_literals():
    """The map only prevents drift while the builders read it. A literal tuple at a builder call
    site is a second declaration, and the next edit moves one without the other — which is the
    whole defect, one level up."""
    import inspect

    from strategies import momentum

    src = inspect.getsource(momentum)
    for lane in ("MOMENTUM-002", "BCTROT-004"):
        assert f'BUILTIN_SLOTS["{lane}"]' in src, f"{lane}'s builder does not read the shared map"
    from strategies import crsi_short

    assert "BUILTIN_SLOTS[STRATEGY_ID]" in inspect.getsource(crsi_short), (
        "CRSISHORT-006's builder does not read the shared map (#858)")
    assert '"BCTROT-004", ("open+150m", "close-20m")' not in src, (
        "a literal built-in schedule survives at a builder call site (#378)"
    )


def test_the_LIVE_shape_would_now_fire():
    """END TO END on the measured production values: four lanes, every override empty. Before the
    fix this produced {} and the detector was blind; it must now expect each lane's built-ins."""
    from strategies.decision_slots import BUILTIN_SLOTS

    cfg = {f"{sid}_SLOTS": [] for sid in BUILTIN_SLOTS}
    out = effective_expected_slots(cfg)
    # NOT `out == BUILTIN_SLOTS` — the first version of this line compared the map with itself
    # through the resolver and passed with ANY contents, including the wrong TECHIVOL-005 entry it
    # was supposed to be exercising (review, 2026-08-29). The values are stated literally instead,
    # which is what a reader can check against the lanes and the journal.
    assert out == {
        "MOMENTUM-002": ("open+5m",),
        # CRSISHORT-006 (#858): SHADOW-only, one slot, mirrored from the adapter's default.
        "CRSISHORT-006": ("open+5m",),
        # Three decisions a day since 2026-09-04, not two. The detector pages when a lane does not
        # decide at an expected slot, so this literal is what makes a MISSED 09:35 visible — leaving
        # it at two would have made the new slot silently optional.
        "BCTROT-004": ("open+5m", "open+150m", "close-20m"),
        "QC345-003": ("open+5m",),
        "TECHIVOL-005": ("open+150m",),
        # SMHGLD-007 (#965): decides at close-20m and fills market-on-close; mirrored from the
        # upstream adapter's signature default (kumo-strategies#177), stated literally here.
        "SMHGLD-007": ("close-20m",),
    }


def test_every_builtin_matches_what_the_LANE_actually_declares():
    """THE DEFECT THIS FILE SHIPPED (review, 2026-08-29). BUILTIN_SLOTS said TECHIVOL-005 decides at
    open+5m; the lane decides at open+150m — verified in the running container and in the paper
    journal (6 open+150m decisions, zero at open+5m). A typed copy of somebody else's constant.

    Consequence of the wrong value, both directions at once: a CRITICAL never-ran page for a slot
    the lane never runs, repeating every 12h on a healthy lane, AND no expectation at the slot it
    does run — so a genuine miss stays invisible. The outage #378 was filed for, moved one lane over
    by the fix for it.

    kumo-strategies' own OPEN_OFFSET_MINUTES docstring records the SAME literal drifting once
    before. So this test does not check the value, it checks the DERIVATION: every entry must equal
    what the lane's own module declares. A hand-typed schedule cannot pass.
    """
    from strategies.decision_slots import BUILTIN_SLOTS, COCKPIT_DECLARED, lane_declared_slots

    mirrored = 0
    for sid in BUILTIN_SLOTS:
        declared = lane_declared_slots(sid)
        if declared is None and sid == "CRSISHORT-006":
            # The adapter lives in a kumo-strategies revision this venv may not carry yet (#858);
            # its absence is reported by ONE loud test, not by every reader of it.
            from strategies.test_installed_strategies_carry_crsishort import crsishort_installed

            if not crsishort_installed():
                continue
        if declared is None and sid == "SMHGLD-007":
            from strategies.test_smhgld_builder import smhgld_installed   # same rule as CRSISHORT (#965)

            if not smhgld_installed():
                continue
        if declared is None:
            # Only a lane cockpit itself declares may have no source to mirror, and the builders
            # test pins that those read the map. Anything else is a second declaration.
            assert sid in COCKPIT_DECLARED, (
                f"{sid} declares its own schedule but cockpit cannot read it — the map is a "
                "hand-typed copy, which is exactly how TECHIVOL-005 shipped wrong"
            )
            continue
        mirrored += 1
        assert tuple(BUILTIN_SLOTS[sid]) == tuple(declared), (
            f"{sid}: the detector expects {BUILTIN_SLOTS[sid]} while the lane declares {declared}"
        )
    # NON-EMPTY IS NOT COMPLETE, but empty is certainly vacuous: if no entry could be compared the
    # loop above proves nothing at all.
    assert mirrored >= 3, f"only {mirrored} entries were actually compared against a lane's source"


def test_TECHIVOL_specifically_is_open_150m():
    """Pinned by name because it is what shipped wrong, with the number stated so the next reader
    knows what was measured rather than assumed: qc27_runner.DECISION_SLOT is built from
    OPEN_OFFSET_MINUTES = 150, and paper's journal carries 6 decisions at open+150m."""
    from strategies.decision_slots import BUILTIN_SLOTS

    assert BUILTIN_SLOTS["TECHIVOL-005"] == ("open+150m",)


def test_a_lane_that_is_NOT_REGISTERED_on_this_stack_is_not_expected():
    """REVIEW FINDING (2026-08-29). The settings SCHEMA supplies `default: []` for every
    `*_SLOTS` key, so `resolve()` materialises all four on every stack — including staging, which
    registers BCTROT-004 alone. Iterating the settings dict therefore expects four lanes there and
    would page daily for three that do not exist (latent only because staging has notifications
    disabled). Expectation must follow what this stack actually RUNS.

    The mirror hazard is why this iterates the known lanes rather than the settings keys: a lane
    whose `*_SLOTS` key ever went missing would silently stop being watched — absence read as
    'nothing due', which is #378's own bug one level up.
    """
    cfg = {f"{sid}_SLOTS": [] for sid in ("BCTROT-004", "MOMENTUM-002", "QC345-003")}
    out = effective_expected_slots(cfg, registered=("BCTROT-004",))
    assert set(out) == {"BCTROT-004"}, (
        "the detector expects lanes this stack never registered — a daily page for a lane that "
        "cannot possibly decide here"
    )


def test_an_UNKNOWN_registration_expects_everything_known():
    """Three states: registered set given (use it), not given (fall back to the known lanes — the
    old behaviour, so a caller that cannot enumerate is no worse off), never a guess at zero."""
    cfg = {"BCTROT-004_SLOTS": [], "MOMENTUM-002_SLOTS": []}
    assert set(effective_expected_slots(cfg)) == {"BCTROT-004", "MOMENTUM-002"}


def test_the_REGISTRATION_SOURCE_actually_answers_on_the_real_node_type():
    """REVIEW ROUND 2, and this is the finding's whole point. `_registered_strategies` called
    `self._node.strategies()` — a method `NodeManager` does not have. Proven False at class level
    in both running containers, so the call raised, the bare `except` returned None, and the
    scoping I shipped did NOTHING on either stack: staging still expected four lanes and registers
    two.

    The fallback semantics ("None means expect every known lane") were chosen so an un-enumerable
    caller is harmless — which is exactly what made a permanently broken call indistinguishable
    from a working one. So this asserts against the REAL NodeManager type, not a double: whatever
    source is used must exist on it.
    """
    import api.alerts as al
    from api.node_factory import create_node

    # THE RUNTIME TYPE, not NodeManager: the API process runs a RedisConsumer, and the first
    # version of this test asserted against a class production never instantiates — true, and
    # irrelevant (review round 3).
    node = create_node()

    # COMMENTS STRIPPED. The first run of this test matched the fix's own comment, which mentions
    # the broken call by name — a source grep satisfied by prose about the bug, which is the trap
    # this repo has paid for repeatedly.
    import ast
    import inspect
    import textwrap

    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(al.AlertsService._registered_strategies))))
    called = [name for name in ("strategies", "health", "session") if f"_node.{name}(" in src]
    assert called, "the enumeration calls nothing on the node at all"
    # AND THE KEY, not just the method (review round 3): `armed_lanes` is in
    # `RedisConsumer.health()`'s allow-list only because an earlier ticket put it there, and that
    # allow-list is the seam that silently dropped naked_after_reject. A test that stops at
    # `hasattr(node, "health")` passes identically when the key is gone.
    import ast as _ast
    import inspect as _i
    import textwrap as _t

    from api.consumer import RedisConsumer

    proj = _ast.unparse(_ast.parse(_t.dedent(_i.getsource(RedisConsumer.health))))
    for key in ("armed_lanes", "lanes_absent"):
        assert key in proj, (
            f"the live health projection drops {key} — the enumeration reads an empty dict and "
            "the scoping silently expects every known lane"
        )
    for name in called:
        assert hasattr(node, name), (
            f"_registered_strategies calls self._node.{name}(), which "
            f"{type(node).__name__} does not have — the except swallows it and the scoping is "
            "dead on arrival"
        )


def test_a_lane_that_FAILED_TO_BUILD_is_still_expected():
    """REVIEW ROUND 3. `armed_lanes` is built from `_sibling_strategies` — lanes that BUILT. A lane
    that failed to build lands in `lanes_absent` (#539), so scoping to armed_lanes alone would drop
    exactly the outage this detector sits nearest to: a lane that should exist, does not, and
    therefore decides nothing. The bite for the union initially did NOT bite, which is how this gap
    was found — an unprotected mechanism."""
    import api.alerts as al
    from types import SimpleNamespace as NS

    host = NS(_node=NS(health=lambda: {
        "bridge_ok": True,
        "armed_lanes": {"BCTROT-004": True},
        "lanes_absent": {"MOMENTUM-002": "build raised: HTTPError"},
    }))
    names = al.AlertsService._registered_strategies(host)
    assert names == {"BCTROT-004", "MOMENTUM-002"}, (
        "a lane that failed to build dropped out of the expectation — the detector stops watching "
        "the very lane that is missing"
    )


def test_a_DEAD_BRIDGE_stops_the_check_rather_than_expecting_everything():
    """Three states: enumerated / cannot-enumerate / cannot-see-the-engine. The last returns False
    so the announcer returns early — with the bridge down nothing has decided and we know it, so
    expecting every lane would maximise pages exactly when they carry no information."""
    import api.alerts as al
    from types import SimpleNamespace as NS

    host = NS(_node=NS(health=lambda: {"bridge_ok": False, "armed_lanes": {}}))
    assert al.AlertsService._registered_strategies(host) is False
