"""The boot gate runs, once, at the first usable account snapshot (#438/#440)."""

from __future__ import annotations


class _CapableBroker:
    """A broker double that can answer what `preflight` actually asks.

    NOT `object()`, WHICH IS WHAT THESE TESTS USED TO PASS. `run_boot_gate` now resolves a lane's
    broker by CAPABILITY — an object that answers `equity()` — because the shipped code passed the
    ACCOUNT DICT and every probe raised `AttributeError("'dict' object has no attribute 'equity'")`
    on paper, 2026-08-25. A bare `object()` has no `equity` either, so it is the same defect wearing
    a test's clothes, and these tests would have gone on passing over it.

    A double that cannot represent production is the bug. Production passes a `NautilusBroker`.
    """

    def equity(self):
        return 100_000.0

    def strategy_positions(self):
        return {}

    def last_price(self, _sym):
        return 10.0


from api.boot_gate import BootGateState, run_boot_gate, should_run


def test_the_gate_runs_once():
    s = BootGateState()
    assert should_run(s, equity=100_000.0) is True
    assert should_run(s, equity=100_000.0) is False, "a gate that re-runs re-alarms on every tick"


def test_an_equity_less_snapshot_does_NOT_spend_the_run():
    """The case this exists for. On a restart holding positions the account frame is withheld rather
    than reporting cash as equity (#382), so an early snapshot can carry no equity. Running then would
    fail every strategy for a reason about the BROKER — and worse, spend the one run, so the gate would
    never fire again after reporting nonsense."""
    s = BootGateState()
    assert should_run(s, equity=None) is False
    assert s.ran is False, "the run was spent on a snapshot that could not answer anything"
    assert should_run(s, equity=100_000.0) is True


def test_zero_equity_is_a_REAL_answer_and_does_run():
    """Discriminating half: `None` means unknown, `0.0` means a blown account. Treating them alike
    would silence the gate exactly when it matters most."""
    assert should_run(BootGateState(), equity=0.0) is True


#: THE THREE PROBES COCKPIT OWES, per the contract agreed with kumo-trading-strategies: the lane reports
#: equity/price/owned (what it can observe), the platform supplies lifecycle/budget/slot_size (what
#: only cockpit knows). Passing `platform=_PLATFORM` made every healthy lane report DEGRADED — correctly,
#: and blaming the platform rather than the lane, which is why the first version of this fixture was
#: the bug and not the code.
_PLATFORM = {"lifecycle": "TRADING", "budget": 25_000.0, "slot_size": 5_000.0}


class _Ok:
    external_id = label = "S"
    def preflight(self, ctx):
        from api.preflight import Probe
        return [Probe(name="armed", value=True), Probe(name="equity", value=100_000.0), Probe(name="price", value=10.0),
                Probe(name="owned", value=[]), Probe(name="universe", value={"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}})]


class _Broken:
    external_id = label = "B"
    def preflight(self, ctx):
        from api.preflight import Probe
        return [Probe(name="equity", error="broker_equity is a property, called as a method"),
                Probe(name="price", value=10.0), Probe(name="owned", value=[]), Probe(name="universe", value={"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}})]


class _Explodes:
    external_id = label = "X"
    def preflight(self, ctx):
        raise RuntimeError("universe resolution over HTTP at build time")


def test_a_healthy_enabled_strategy_is_not_degraded():
    assert run_boot_gate({"S": _Ok()}, broker=_CapableBroker(), platform=_PLATFORM, enabled_ids=["S"]) == []


def test_a_failing_probe_is_reported_and_names_the_reason():
    """QC345's actual 2026-08-21 failure: `broker_equity` is a property and was called as a method, so
    it decided 5 entries and submitted 0. At boot that is a probe error, hours before the decision."""
    out = run_boot_gate({"B": _Broken()}, broker=_CapableBroker(), platform=_PLATFORM, enabled_ids=["B"])
    assert len(out) == 1 and out[0][0] == "B"
    assert "broker_equity" in out[0][1], out


def test_a_strategy_whose_preflight_RAISES_degrades_it_and_not_the_boot():
    """#377: QC345 resolving its universe over HTTP at build time took MANUAL, MOMENTUM and BCTROT down
    with it. A gate that can kill the node is worse than the thing it guards against."""
    out = run_boot_gate({"X": _Explodes(), "S": _Ok()}, broker=_CapableBroker(), platform=_PLATFORM,
                        enabled_ids=["X", "S"])
    assert [sid for sid, _ in out] == ["X"], "the healthy strategy must still have been probed"


def test_a_DISABLED_strategy_is_not_probed():
    """A lane nobody switched on has breached nothing. Probing it would light up every disabled
    strategy the day this ships."""
    assert run_boot_gate({"B": _Broken()}, broker=_CapableBroker(), platform=_PLATFORM, enabled_ids=[]) == []


def test_every_degraded_strategy_reaches_the_JOURNAL():
    """The predicate is not the point; the record is. A gate whose findings live only in a return value
    is the same 'built, never executed' shape as the detector that had no caller."""
    seen = []
    run_boot_gate({"B": _Broken()}, broker=_CapableBroker(), platform=_PLATFORM, enabled_ids=["B"],
                  journal=lambda sid, why: seen.append((sid, why)))
    assert [s for s, _ in seen] == ["B"]


def test_a_missing_PLATFORM_probe_is_blamed_on_the_platform_not_the_lane():
    """The distinction is the contract. A perfectly healthy strategy reported DEGRADED because cockpit
    forgot its own three probes must say so — otherwise an operator spends the morning debugging a lane
    that is fine, which is precisely what the first run of this fixture did to me."""
    out = run_boot_gate({"S": _Ok()}, broker=_CapableBroker(), platform={}, enabled_ids=["S"])
    assert len(out) == 1
    assert "platform-side" in out[0][1], out


class _HoldsOvernight:
    """The normal condition at boot: a rotation lane holding positions opened on previous sessions.
    BCTROT-004 held six symbols on the morning of 2026-08-22."""
    external_id = label = "H"
    def preflight(self, ctx):
        from api.preflight import Probe
        return [Probe(name="armed", value=True), Probe(name="equity", value=100_000.0), Probe(name="price", value=10.0),
                Probe(name="owned", value=["AEM", "BETA", "CGAU", "WPM", "BDX", "AMGN"]), Probe(name="universe", value={"source": "symbols", "requested": 1, "resolved": 1, "unresolved": [], "ambiguous": {}})]


def test_the_fixture_holds_positions_and_has_NOT_filled_this_session():
    """Fixture property first, because this is the exact combination the `owned` rule fails on:
    `if held and not has_filled: FAIL`. At 04:00 ET, before the open, that describes every lane
    carrying an overnight book — the normal state, not a defect."""
    from api.preflight import judge_preflight
    probes = _HoldsOvernight().preflight(None)
    verdicts = judge_preflight(probes, enabled=True, has_filled_this_session=False)
    owned = [v for v in verdicts if v.name == "owned"][0]
    assert owned.outcome.value == "FAIL", "the rule no longer fires here — this test is now vacuous"


def test_a_lane_holding_an_OVERNIGHT_book_is_not_degraded_at_boot():
    """THE FALSE POSITIVE THIS GATE ALMOST SHIPPED WITH.

    `has_filled_this_session` is an INTRA-SESSION staleness check: it catches a lane claiming positions
    it never opened. At boot the session has not started, so it cannot discriminate — every lane with
    an overnight book looks identical to a lane with a phantom claim. Judging it there would have fired
    on all six of BCTROT's symbols on the first morning, and an alarm that fires on the normal path is
    one an operator learns to scroll past.

    Caught by a surviving mutation, not by review: flipping the flag changed no test, because every
    fixture reported `owned=[]`."""
    out = run_boot_gate({"H": _HoldsOvernight()}, broker=_CapableBroker(), platform=_PLATFORM,
                        enabled_ids=["H"])
    assert out == [], f"a lane holding an overnight book was degraded at boot: {out}"


def test_run_preflight_ITSELF_never_raises_which_is_what_the_gate_relies_on():
    """Two derivations of one fact, pinned together. The gate wraps every call in `except Exception`,
    and narrowing that to `except ValueError` changed NO test — because `run_preflight` already
    catches internally, so the gate's guard is unreachable by contract.

    Unreachable-by-contract is fine; unreachable-and-unpinned is not. If run_preflight ever starts
    propagating, this fails here and names the gate as the thing that depended on it."""
    from api.preflight_runner import run_preflight

    report = run_preflight(_Explodes(), broker=_CapableBroker(), platform=_PLATFORM, enabled=True,
                           has_filled_this_session=True, strategy_id="X")
    assert report.ready is False, "a strategy whose preflight raised must not be reported READY"


# -- a DEGRADED verdict must be retryable (#515) -----------------------------------------------------
#
# `should_run` spends the single run on the FIRST snapshot that carries equity, and the gate probes
# lanes that may not be ready yet. Measured on an IBKR paper instance twice:
#
#   2026-08-24 15:33  PREFLIGHT DEGRADED BCTROT-004: equity: raised AttributeError('NoneType' ...
#                     -- the lane's broker had not been attached yet
#   2026-08-25 17:17  PREFLIGHT DEGRADED BCTROT-004: armed: UNARMED (is_armed=False)
#                     -- the trading calendar had not resolved yet; it retries every 60s
#
# BOTH VERDICTS WERE WRONG. The first was contradicted minutes later by MOMENTUM-002 reading the
# broker successfully; the second by BCTROT-004 trading successfully at 01:05. And because the flag is
# spent, neither could ever be corrected — the node carried a permanent false DEGRADED.
#
# A gate that can only ever be wrong once is worse than no gate, because its output gets quoted. So a
# CLEAN verdict is final (nothing more to learn) and a DEGRADED one is retryable (the lane may simply
# not have finished starting).


def test_a_DEGRADED_verdict_does_not_spend_the_run():
    """The lane may still be starting. Arming retries every 60s and brokers attach after the first
    account frame, so the first probe is the one most likely to be early."""
    st = BootGateState()
    assert should_run(st, equity=100.0)
    st.degraded = [{"strategy_id": "BCTROT-004", "why": "armed: UNARMED"}]
    assert should_run(st, equity=100.0), (
        "a degraded verdict was permanent — the node cannot ever correct a false DEGRADED")


def test_a_CLEAN_verdict_IS_final():
    """Nothing further is learned by re-probing a healthy stack, and a gate that re-runs forever is a
    gate that eventually reports a transient as news."""
    st = BootGateState()
    assert should_run(st, equity=100.0)
    st.degraded = []
    assert not should_run(st, equity=100.0), "a clean gate kept re-running"


def test_a_verdict_that_RECOVERS_becomes_final():
    """down -> up must settle, or the retry never terminates."""
    st = BootGateState()
    should_run(st, equity=100.0)
    st.degraded = [{"strategy_id": "X", "why": "armed: UNARMED"}]
    assert should_run(st, equity=100.0)         # retry while degraded
    st.degraded = []
    assert not should_run(st, equity=100.0)     # recovered — stop


def test_an_equity_less_snapshot_still_does_not_spend_a_RETRY():
    """The original guard, preserved under the new rule: probing without equity fails every lane for a
    reason about the BROKER, and must not consume the retry either."""
    st = BootGateState()
    should_run(st, equity=100.0)
    st.degraded = [{"strategy_id": "X", "why": "armed: UNARMED"}]
    assert not should_run(st, equity=None)
    assert should_run(st, equity=100.0), "the equity-less snapshot swallowed the retry"


# -- a still-DEGRADED verdict must remain CORRECTABLE after the fast retries (#604) ------------------
#
# TECHIVOL-005, 2026-08-27 13:04:16 UTC: `PREFLIGHT DEGRADED ... price: price=None for a claimed
# instrument`, logged 3 seconds after RUNNING — at the one moment no mark can exist, because the bars
# were still being requested. The account publishes every ~2s, so the five fast retries were spent by
# roughly boot+10s, still before any bar. All nine claimed positions had marks minutes later, and the
# verdict was permanent anyway: true for about three seconds, false for the rest of the session, and
# QUOTED during an outage as if it were current. The verdict must stay correctable for as long as it
# is DEGRADED — clean remains final.


def _exhausted_while_degraded() -> BootGateState:
    """The ticket's precondition: the bounded fast retry is SPENT while the verdict is still DEGRADED."""
    from api.boot_gate import _MAX_DEGRADED_ATTEMPTS

    st = BootGateState()
    for _ in range(_MAX_DEGRADED_ATTEMPTS):
        assert should_run(st, equity=100_000.0), "fixture broke: a fast retry was refused early"
        st.degraded = [("TECHIVOL-005", "price: price=None for a claimed instrument")]
    return st


def test_the_fixture_property_the_fast_retries_really_ARE_spent():
    """Fixture property first (#604 can only exist past the cap). The very next account update must
    NOT probe — under the old code because the gate gave up forever, under the fix because the late
    retries are paced. A fixture that had not reached the cap would make every test below vacuous."""
    from api.boot_gate import _MAX_DEGRADED_ATTEMPTS

    st = _exhausted_while_degraded()
    assert st.attempts >= _MAX_DEGRADED_ATTEMPTS
    assert st.degraded, "fixture broke: the verdict healed itself"
    assert should_run(st, equity=100_000.0) is False, (
        "the cap is not binding here — nothing below can distinguish the fix from the bug")


def test_a_still_DEGRADED_verdict_is_NOT_permanent_for_the_process_life():
    """THE #604 DEFECT. After the five fast attempts, `should_run` returned False on every one of the
    ~43,000 account updates a session brings — the verdict could never be corrected even when the lane
    became healthy. Some later update must be allowed to re-probe."""
    st = _exhausted_while_degraded()
    fired = sum(1 for _ in range(5_000) if should_run(st, equity=100_000.0))
    assert fired > 0, (
        "after the fast retries were spent, a still-DEGRADED verdict was permanent for the "
        "process life — 5,000 account updates (~2.7h at the ~2s cadence) and not one re-probe")


def test_the_late_retries_THIN_instead_of_firing_every_update():
    """The bound exists because an unbounded retry printed the same four ERROR lines every ~2 seconds
    on paper 2026-08-25, and an alarm that fires continuously is one an operator silences. The late
    retries must be RARE and must get rarer: gaps never below the first backoff, never shrinking."""
    from api.boot_gate import _MAX_BACKOFF_UPDATES

    st = _exhausted_while_degraded()
    fires = [i for i in range(12_000) if should_run(st, equity=100_000.0)]
    assert fires, "vacuous: no late retry fired at all, so there are no gaps to judge"
    assert len(fires) <= 14, f"{len(fires)} probes in 12,000 updates re-alarms all session: {fires[:20]}"
    # Anchor at -1: `fires` holds 0-based indices, so a first probe on the 8th update is index 7.
    gaps = [b - a for a, b in zip([-1] + fires, fires)]
    assert min(gaps) >= 8, f"a late retry fired within 8 updates of the previous one: {gaps}"
    assert gaps == sorted(gaps), f"the gaps must never shrink: {gaps}"
    # The CEILING is load-bearing in the other direction: without it the gap keeps doubling and a
    # lane that heals late in the session is never re-observed before the process dies — the #604
    # permanence returning asymptotically. ~2048 updates is about an hour at the ~2s cadence.
    assert gaps[-2:] == [_MAX_BACKOFF_UPDATES] * 2, (
        f"the gap must settle at the ceiling, not grow without bound: {gaps}")


def test_a_lane_that_heals_AFTER_the_fast_retries_reaches_its_clean_final_verdict():
    """The whole point of correctability: TECHIVOL had marks minutes after boot. The next scheduled
    probe must be allowed to observe that, and the clean verdict it returns is FINAL — the #515 rule
    survives the fix unchanged."""
    st = _exhausted_while_degraded()
    for _ in range(5_000):
        if should_run(st, equity=100_000.0):
            st.degraded = []  # the probe now observes a healthy lane
            break
    else:
        raise AssertionError("no late retry ever fired — the lane could never be re-observed")
    assert not any(should_run(st, equity=100_000.0) for _ in range(5_000)), (
        "clean stopped being final: a healthy stack was re-probed")


def test_an_equity_less_snapshot_does_not_advance_the_late_retry_schedule():
    """The original #382 guard, preserved one phase further out: a snapshot that cannot answer
    anything is not progress toward the next probe, and must not spend one either."""
    st = _exhausted_while_degraded()
    before = (st.attempts, st.degraded)
    assert not any(should_run(st, equity=None) for _ in range(5_000)), (
        "an equity-less snapshot spent a late retry")
    assert (st.attempts, st.degraded) == before


def test_a_degraded_report_carries_WHEN_it_was_measured():
    """Boot-gate verdicts get QUOTED — 2026-08-24's DEGRADED lines were read as evidence during an
    outage while measuring a boot race days old. The REPORT (journal line and stored verdict) must
    carry its measurement time so a reader can see it is a boot-time observation, not current state.
    The retry logic itself stays timestamp-free — counts, not clocks, per this file's own rule.

    The injected value is one no real clock can produce today (agreement-is-not-connection: a stamp
    the default could also have written would prove nothing about the knob)."""
    seen: list[str] = []
    out = run_boot_gate(
        {"B": _Broken()}, broker=_CapableBroker(), platform=_PLATFORM, enabled_ids=["B"],
        journal=lambda sid, why: seen.append(why),
        now=lambda: "1999-12-31T23:59:59+00:00",
    )
    assert "1999-12-31T23:59:59" in out[0][1], f"the stored verdict carries no measurement time: {out}"
    assert "1999-12-31T23:59:59" in seen[0], f"the journaled line carries no measurement time: {seen}"


def test_the_measurement_stamp_defaults_to_a_real_utc_time():
    """Without an injected clock the stamp must still exist and be renderable — an empty or missing
    stamp is the old undated verdict wearing a new parameter."""
    from datetime import datetime, timezone

    out = run_boot_gate({"B": _Broken()}, broker=_CapableBroker(), platform=_PLATFORM, enabled_ids=["B"])
    year = str(datetime.now(timezone.utc).year)
    assert "measured " in out[0][1] and year in out[0][1], out
