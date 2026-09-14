"""PREFLIGHT RUNNER (#438) — collect, judge, report. The half that turns the judge into an answer.

Operator, 2026-08-22: "I need to be able to reliably deploy strategies. Which is not the case right now."

`build_optional_strategy` verifies a lane CONSTRUCTS; `RUNNING` means it REGISTERED. This asks the
question neither answers — can it actually act? — by making the lane OBSERVE each capability once and
having cockpit judge what comes back.

THE SPLIT, agreed with kumo-strategies 2026-08-22 and enforced by `api.preflight`:

    the lane reports    equity · price · owned          learned through the broker
    cockpit supplies    lifecycle · budget · slot_size  its own state, plus arithmetic

A lane may not report cockpit's state — that is `check_your_own_budget` one notch out — and cockpit
computes `slot_size` so no lane can be wrong about it, only about its inputs.

THREE WAYS TO BE NOT-READY, and all three must be distinguishable in the report:

    a probe FAILED            equity=None, budget=0, owned=[8 symbols] having never filled
    preflight RAISED          the lane's own collection exploded
    preflight DOES NOT EXIST  unprobeable, which must never read as probed-and-healthy

The last is the shape of every defect this week: absence read as agreement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # cycle-free: these are structural types, needed only by mypy
    from api.seam_types import Lane, LaneBroker

from dataclasses import dataclass, field

from api.preflight import Outcome, Probe, ProbeResult, judge_preflight


@dataclass(frozen=True)
class PreflightReport:
    ready: bool
    results: list[ProbeResult] = field(default_factory=list)
    failures: list[ProbeResult] = field(default_factory=list)
    alert_key: str = ""
    #: DISABLED BY OPERATOR (#638) — the THIRD verdict beside ready/degraded. A lifecycle row
    #: saying DISABLED is a decision, not a fault: reporting it as degradation paged ERROR every
    #: retry on staging while the operator's own note explained the zero allocation. Loud, not
    #: absent: the report still carries a result naming the decision, so disabled is never
    #: indistinguishable from probed-and-healthy.
    disabled: bool = False


def run_preflight(
    strategy: Lane,
    *,
    broker: LaneBroker,
    platform: dict,
    enabled: bool,
    has_filled_this_session: bool,
    strategy_id: str = "",
) -> PreflightReport:
    """Never raises. A preflight that could take the caller down would be the #377 shape again — QC345
    resolving its universe over HTTP at build time took MANUAL, MOMENTUM and BCTROT down with it."""
    if not enabled:
        # A lane nobody switched on has not breached anything. Probing it would light up every disabled
        # strategy the day this ships, and an alarm that fires on the normal state gets switched off.
        return PreflightReport(ready=True)

    # THE OPERATOR'S ROW WINS, AND IT WINS HERE — the one place (#638). `enabled` above is the
    # DEPLOYMENT's word (the env gate built the lane); the lifecycle row is the OPERATOR's word.
    # On staging they disagreed (KUMO_MOMENTUM_ENABLED=true beside a DISABLED row with a signed
    # note), and judging the built lane against its deliberate zero budget produced PREFLIGHT
    # DEGRADED at ERROR on every retry. DISABLED short-circuits before any probe is judged; the
    # judge's own lifecycle rule still covers a row going bad on a lane nobody disabled.
    lifecycle = str(platform.get("lifecycle", "") or "").upper() if isinstance(platform, dict) else ""
    if lifecycle == "DISABLED":
        note = ProbeResult("lifecycle", Outcome.PASS,
                           "DISABLED by operator — lane built but not judged; the lifecycle row is "
                           "the decision, not a degradation (#638)")
        return PreflightReport(ready=True, disabled=True, results=[note])

    probes: list[Probe] = []
    probe_fn = getattr(strategy, "preflight", None)
    if not callable(probe_fn):
        # UNPROBEABLE IS NOT HEALTHY. A lane predating the contract satisfies every older Protocol
        # while being unmeasurable, and to anything reading this report that would be indistinguishable
        # from a clean pass.
        fail = ProbeResult("preflight", Outcome.MISSING,
                           "the lane has no `preflight` — unprobeable, which is not the same as healthy")
        return PreflightReport(ready=False, results=[fail], failures=[fail],
                               alert_key=_key(strategy_id))
    try:
        probes = list(probe_fn(broker) or [])
    except Exception as exc:  # noqa: BLE001 — the most broken lane must not be the one that escapes
        fail = ProbeResult("preflight", Outcome.FAIL, f"raised while collecting probes: {exc!r}")
        return PreflightReport(ready=False, results=[fail], failures=[fail],
                               alert_key=_key(strategy_id))

    # Cockpit's own three, added here so the lane cannot report — or be wrong about — platform state.
    for name in ("lifecycle", "budget", "slot_size"):
        if name in platform:
            probes.append(Probe(name, platform[name]))

    results = judge_preflight(probes, enabled=True, has_filled_this_session=has_filled_this_session)
    # MISSING and UNKNOWN count against readiness alongside FAIL: a probe the lane silently stopped
    # reporting, and one cockpit has no rule for, are both "nobody checked this" wearing a friendlier
    # word.
    failures = [r for r in results if r.outcome in (Outcome.FAIL, Outcome.MISSING, Outcome.UNKNOWN)]
    return PreflightReport(ready=not failures, results=results, failures=failures,
                           alert_key=_key(strategy_id) if failures else "")


def _key(strategy_id: str) -> str:
    """`_GATES` splits on the first colon, so the prefix must stay `strategy_degraded` to reach
    `notify_strategy_degraded`; the id goes after it so one bad lane cannot mute another."""
    return f"strategy_degraded:preflight:{strategy_id}"
