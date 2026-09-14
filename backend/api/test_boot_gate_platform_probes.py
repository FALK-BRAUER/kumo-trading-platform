"""Cockpit must SUPPLY its own three probes, or the gate can never report clean (#532).

`preflight.py:55` — `SUPPLIED_BY_PLATFORM = ("lifecycle", "budget", "slot_size")`. The judge counts a
missing platform probe against readiness, and `_boot_gate_platform()` returns `{}`.

So every lane is DEGRADED on three probes, on every tenant, always. Live on paper 2026-08-25, on all
four lanes, repeating:

    PREFLIGHT DEGRADED QC345-003:
      lifecycle: cockpit did not supply this probe — platform-side, not the lane's;
      budget:    cockpit did not supply this probe — platform-side, not the lane's;
      slot_size: cockpit did not supply this probe — platform-side, not the lane's

The message is already self-aware — it says *platform-side, not the lane's* — and the judge still
counts it against the lane.

WHY THIS MATTERS MORE THAN THE NOISE. The boot gate has now had three fixes (#515: never ran on
Alpaca; then it ran with the account DICT; then it resolved the broker by NAME and worked for one lane
of four). Each made it less wrong. **None could make it report clean**, because these three are
missing by construction. A reader seeing a green suite and three shipped fixes would reasonably
conclude the gate works. It cannot.

That is `agreement-is-not-connection`: three independent fixes all agreeing the gate is improving,
while a fourth severed wire holds the answer constant.

WHAT EACH PROBE IS FOR — from `preflight.py`'s own header, each with the incident it caught:

    lifecycle   a missing lifecycle row -> DISABLED     [absent-row: historical]
    budget      TECHIVOL's budget never set -> 0;  may_submit 4-positional (#431) -> raises
    slot_size   sizing collapsed -> 0 shares at $90.32;  cap == book size -> 0 on a full book
"""

from __future__ import annotations

from types import SimpleNamespace

from api.preflight import SUPPLIED_BY_PLATFORM


def test_the_fixture_knows_what_the_judge_requires():
    """FIXTURE FIRST. If this tuple moved, every assertion below would be about nothing."""
    assert set(SUPPLIED_BY_PLATFORM) == {"lifecycle", "budget", "slot_size"}, (
        f"the platform's probe set changed to {SUPPLIED_BY_PLATFORM} — re-read this file")


def _node(**over):
    """A host shaped like the engine node where `_boot_gate_platform` runs."""
    base = dict(
        _sibling_strategies={"QC345-003": SimpleNamespace()},
        _platform_probes={},
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_the_platform_supplies_ALL_THREE_probes():
    """THE DEFECT. `return {}` means every lane degrades on three counts, forever."""
    from api.engine_node import UiFeedStrategy

    node = _node(_platform_probes={
        "QC345-003": {"lifecycle": "TRADING", "budget": 20_000.0, "slot_size": 5},
    })
    probes = UiFeedStrategy._boot_gate_platform(node, "QC345-003")

    missing = [p for p in SUPPLIED_BY_PLATFORM if p not in probes]
    assert not missing, (
        f"cockpit still does not supply {missing}. The judge counts each against the LANE, so every "
        f"lane reads DEGRADED regardless of its own health — which is why three fixes to the boot "
        f"gate could not make it report clean")


def test_probes_are_PER_LANE_not_one_set_for_all():
    """`lifecycle` and `budget` differ per lane by definition — one lane DISABLED and another TRADING
    is the normal state, and a shared dict would report the wrong lane's answer."""
    from api.engine_node import UiFeedStrategy

    node = _node(_platform_probes={
        "A-001": {"lifecycle": "TRADING", "budget": 20_000.0, "slot_size": 5},
        "B-002": {"lifecycle": "DISABLED", "budget": 0.0, "slot_size": 0},
    })
    assert UiFeedStrategy._boot_gate_platform(node, "A-001")["lifecycle"] == "TRADING"
    assert UiFeedStrategy._boot_gate_platform(node, "B-002")["lifecycle"] == "DISABLED"


def test_a_lane_with_NO_cached_probes_gets_an_EMPTY_dict_not_invented_values():
    """UNKNOWN IS NOT HEALTHY, and it is not zero either.

    `budget -> 0` is a REAL failure the judge exists to catch (TECHIVOL's budget never set). Defaulting
    an unread probe to 0 would make "not measured yet" indistinguishable from "measured, and it is
    zero" — the silencing direction, and the one that made TECHIVOL look healthy while it formed
    nothing.
    """
    from api.engine_node import UiFeedStrategy

    probes = UiFeedStrategy._boot_gate_platform(_node(), "NEVER-SEEN-001")
    assert probes == {}, f"invented values for an unmeasured lane: {probes}"


def test_it_NEVER_RAISES_on_a_malformed_cache():
    """It runs inside the boot gate, which runs inside `_publish_account`, which runs on a msgbus
    callback. #377: one strategy's failure must not reach the others."""
    from api.engine_node import UiFeedStrategy

    assert UiFeedStrategy._boot_gate_platform(_node(_platform_probes=None), "A-001") == {}
    assert UiFeedStrategy._boot_gate_platform(_node(_platform_probes="nonsense"), "A-001") == {}


def test_the_probes_are_ACTUALLY_REFRESHED_and_fired_at_start():
    """THE SEAM. A cache nobody fills reports the same three missing probes forever — which is the
    defect, not the fix.

    Fired once immediately AND on a timer: the boot gate runs on the first account snapshot, seconds
    after start, so a timer-only refresh would leave it reading an empty cache exactly when it looks.
    And `lifecycle`/`budget` change while the node runs — an operator disables a lane, a sleeve moves —
    so a boot-only read would answer for a world that no longer exists.
    """
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy)
    assert "_refresh_platform_probes()" in src, (
        "nothing calls _refresh_platform_probes — the cache stays empty and every lane reports all "
        "three probes missing, exactly as before")
    assert "_PLATFORM_PROBE_TIMER" in src, "no timer: probes cached at boot go stale as lanes change"


def test_run_boot_gate_ACCEPTS_A_PER_LANE_RESOLVER():
    """The bug I nearly shipped. My caller passed `self._boot_gate_platform(sid)` where `sid` was not
    in scope — inside a `try/except Exception` that would have swallowed the NameError silently, which
    is the `_maybe_receipt(order)` shape that went unnoticed for weeks.

    Every test I had passed, because they exercised the FUNCTION and not the CALL. A resolver removes
    the class: `run_boot_gate` is the only thing that knows which lane it is looking at, so it is the
    only thing that can ask.
    """
    from api.boot_gate import run_boot_gate

    seen: list[str] = []

    # THE LANE NEEDS A RESOLVABLE BROKER or `run_boot_gate` degrades it before it ever asks for the
    # platform probes — my first fixture used `broker=None` and `seen` stayed empty, which would have
    # read as "the resolver is not wired" when in fact the double never reached that line.
    class _Broker:
        def equity(self): return 100_000.0
        def strategy_positions(self): return {}
        def last_price(self, _s): return 10.0

    lane = SimpleNamespace(preflight=lambda b: [], _runner=SimpleNamespace(broker=_Broker()))

    run_boot_gate({"A-001": lane, "B-002": lane},
                  broker=None,
                  platform=lambda sid: seen.append(sid) or {},
                  enabled_ids=["A-001", "B-002"])

    assert seen == ["A-001", "B-002"], (
        f"the resolver was not called per lane: {seen}. A single shared dict would report one lane's "
        f"lifecycle and budget to every lane")
