"""A lane that STARTED but cannot DECIDE must not read as healthy (#438, kumo-strategies 7de760a).

THE TRADE THIS GUARDS AGAINST. Before 7de760a, a lane whose calendar fetch timed out took the whole
node down: `on_start -> _arm -> next_slot_fire -> AlpacaCalendar.day -> urlopen(timeout=15)`, blocking,
on the event loop. BCTROT-004's raised URLError, Nautilus re-raised it from `Trader.START`, and QC345
and TECHIVOL never started — they were queued behind it. All four lanes had the defect; only the first
one reached ever showed.

The fix moves arming off the start path: background task, retry every 60s, and a lane that cannot reach
its calendar now starts UNARMED instead of killing the node. That is strictly better — and it converts a
LOUD crash into a QUIET one. An unarmed lane is alive, counted in `automated_lanes_running`, and CAN
NEVER DECIDE.

Their words, and the reason this file exists: "if cockpit shows strategies_running: 4 while one of them
is unarmed, that is a new lie in the dashboard".

`armed` is the one capability cockpit CANNOT observe from outside — which is why it is a probe the lane
reports and the platform judges, rather than something the platform infers.
"""

from __future__ import annotations

from api.preflight import REQUIRED_FROM_STRATEGY, Outcome, Probe, judge_preflight


def _verdict(probes, name, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("has_filled_this_session", True)
    return next(v for v in judge_preflight(probes, **kw) if v.name == name)


def _full(**over):
    base = {"armed": True, "equity": 100_000.0, "price": 10.0, "owned": [],
            "universe": {"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}}}
    base.update(over)
    return [Probe(k, v) for k, v in base.items()]


def test_cockpit_requires_EXACTLY_what_the_INSTALLED_strategies_package_reports():
    """TWO DERIVATIONS OF ONE FACT, ACTUALLY COMPARED.

    The first version of this test read cockpit's own tuple and quoted kumo-strategies' in PROSE:

        # `PREFLIGHT_PROBES = ("armed", "equity", "owned", "price")` ...
        assert "armed" in REQUIRED_FROM_STRATEGY

    One derivation plus a comment. It passed unconditionally, because `armed` was added to that tuple
    three lines above — a test written to catch cross-repo drift that could not see the other repo. The
    fifth instance of this shape in one day, every one inside a guard written to prevent it.

    The real tuple imports fine, so there was never a reason to quote it. Comparing as SETS catches both
    directions, and the reverse — kumo-strategies adding a probe cockpit never requires — is the one
    nothing else would ever notice.

    THIS COUPLES THE SUITE TO THE INSTALLED PACKAGE VERSION, DELIBERATELY. `pyproject.toml` pins
    kumo-strategies by git URL, and a pinned URL plus a test that never looks is how version skew ships:
    cockpit requiring a probe no running strategy reports means preflight judges EVERY lane MISSING, and
    a detector that cries on all four is worse than no detector. Same argument as
    `test_the_installed_risklimits_actually_has_the_field`.

    RED HERE MEANS "YOU ARE AHEAD OF THE PACKAGE" and it is the signal you want BEFORE a deploy.
    """
    from kumo_strategies.runtime.nautilus.contract import RegistrationMixin

    ours, theirs = set(REQUIRED_FROM_STRATEGY), set(RegistrationMixin.PREFLIGHT_PROBES)
    assert ours == theirs, (
        f"cockpit requires {sorted(ours - theirs)} that no INSTALLED strategy reports, and strategies "
        f"report {sorted(theirs - ours)} that cockpit ignores. A probe required by one side and unknown "
        f"to the other is enforced by nobody. If this is a release-ordering problem, the strategies "
        f"package ships FIRST"
    )


def test_an_ARMED_lane_passes():
    assert _verdict(_full(), "armed").outcome is Outcome.PASS


def test_an_UNARMED_lane_FAILS_even_though_everything_else_is_healthy():
    """The whole point. Equity reads, prices read, positions read — and the lane cannot schedule a
    decision. Every other probe is green, which is exactly how this hides."""
    v = _verdict(_full(armed=False), "armed")
    assert v.outcome is Outcome.FAIL
    assert "decide" in v.detail.lower() or "arm" in v.detail.lower(), v.detail


def test_the_message_says_it_may_be_TRANSIENT_and_names_the_retry():
    """A lane arms in the background and retries every 60s, so `armed=False` seconds after boot is
    ORDINARY. An operator reading this at 04:00 needs to know whether to act now or look again — a bare
    FAIL invites a restart that fixes nothing and costs the arming attempt in flight."""
    d = _verdict(_full(armed=False), "armed").detail
    assert "retr" in d.lower() and "60" in d


def test_a_probe_that_RAISED_is_not_reported_as_unarmed():
    """A raise and a False are different diagnoses: one is a broken lane, the other a lane waiting on a
    venue. Collapsing them sends whoever reads it to the wrong repo — the same distinction preflight
    already draws between a raise and a None."""
    v = _verdict([Probe("armed", None, "AttributeError('is_armed')"),
                  *[p for p in _full() if p.name != "armed"]], "armed")
    assert v.outcome is Outcome.FAIL
    assert "raised" in v.detail.lower()


def test_a_MISSING_armed_probe_is_MISSING_not_a_pass():
    """A lane on an older build that does not report `armed` must not be judged healthy on the strength
    of what it does not say. Silence is not a pass — that is the whole reason the platform judges."""
    v = _verdict([p for p in _full() if p.name != "armed"], "armed")
    assert v.outcome is Outcome.MISSING


def test_a_DISABLED_lane_is_not_judged_on_arming():
    """A lane nobody switched on has not breached anything, and probing it would light up every
    disabled strategy the day this ships."""
    out = judge_preflight(_full(armed=False), enabled=False, has_filled_this_session=False)
    assert all(v.outcome is not Outcome.FAIL for v in out), out


def test_the_HEALTHY_fixture_covers_EVERY_required_probe():
    """Aimed at the class, because this exact drift just happened: adding `armed` to the contract broke
    seven tests whose doubles could no longer represent a conforming strategy.

    That was the contract meeting drifted doubles, and the doubles were fixed rather than the contract
    loosened. This makes the next such addition fail HERE — one assertion naming the gap — instead of
    scattering across every suite that happens to build a lane."""
    import pathlib
    import re

    from api.preflight import REQUIRED_PROBES

    block = (pathlib.Path(__file__).parent / "test_preflight.py").read_text()
    block = block.split("healthy = [")[1].split("]")[0]
    have = set(re.findall(r'p\("([a-z_]+)"', block))
    assert have, "could not read the healthy fixture — this test is blind"
    assert not set(REQUIRED_PROBES) - have, (
        f"the canonical healthy fixture omits {sorted(set(REQUIRED_PROBES) - have)}, so every "
        f"assertion built on it judges an incomplete lane"
    )
