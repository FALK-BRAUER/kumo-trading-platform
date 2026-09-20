"""PREFLIGHT JUDGEMENT (#438) — is this strategy actually able to trade?

2026-08-22: "I need to be able to reliably deploy strategies. Which is not the case right now."

`build_optional_strategy` (engine_node.py:5216) verifies a strategy CONSTRUCTS. `RUNNING` in the log
means it REGISTERED. Neither says it can do anything. Everything between construction and a filled
order was unverified, and the cost is measurable: 12 of 19 decided slots since 2026-07-31 ended in
nothing or in failure, and nobody noticed one of them.

EVERY DEFECT OF THE LAST 48 HOURS IS CAUGHT HERE, and none needed a new subsystem:

    broker_equity was a @property  (ccea7b4)   equity   -> raises
    portfolio_value vs equity      (b0593af)   equity   -> None
    sizing collapsed                           slot_size-> 0 shares at $90.32
    may_submit 4-positional        (#431)      budget   -> raises
    positions() read as ours       (578fbb6)   owned    -> 8 foreign symbols, NO error
    cap == book size               (b33a75f)   slot_size-> 0 on a full book
    TECHIVOL budget never set                  budget   -> 0
    missing lifecycle row                      lifecycle-> DISABLED   [absent-row: historical]

THE STRATEGY OBSERVES. THE PLATFORM JUDGES. kumo-trading-strategies' contract.py states the rule and this
module is its extension:

    "THE STRATEGY DECLARES, THE PLATFORM DECIDES ... There is deliberately no `check_your_own_budget`
     here and there must never be one: a strategy polices its own allocation correctly right up until
     the day it has a bug, and then holds more than it was granted, silently."

So `Probe` carries a VALUE and an ERROR and no verdict. Asking a component for a boolean asks the
broken thing whether it is broken — and every defect above was a component wrong about itself while
reporting healthy. `owned` is the proof: TECHIVOL returned eight symbols and no error, and it is only
wrong in the light of "this lane has never filled anything", which the lane does not know and the
platform does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: WHO OBSERVES WHAT (boundary set with kumo-trading-strategies, 2026-08-22).
#:
#: The strategy reports only what it learns THROUGH THE BROKER. `lifecycle` and `budget` are COCKPIT'S
#: OWN STATE, and asking a strategy to report them is asking it to attest to something it does not own
#: — the same principle as forbidding `check_your_own_budget`, one notch further out. `slot_size` is
#: arithmetic over equity and price, so cockpit computes it and the strategy cannot be wrong about it.
#:
#: These are the runner<->broker interface rather than per-lane capabilities: none of the eight defects
#: this catches is specific to a strategy, which is the evidence the seam is in the right place.
#: `armed` FIRST, matching kumo-trading-strategies' `PREFLIGHT_PROBES`. It is the one capability cockpit
#: cannot observe from outside: a lane whose calendar never answered reads healthy on every other
#: probe while being unable to ever decide (kumo-trading-strategies 7de760a).
REQUIRED_FROM_STRATEGY = ("armed", "equity", "price", "owned", "universe")
#: `universe` (issue 80): {source, requested, resolved, unresolved, ambiguous}. Counts,
#: not a verdict — the detector is the relationship requested == resolved + len(unresolved), and
#: `ambiguous` is a SUBSET of resolved. Required here so a lane that stops emitting it fails a
#: probe instead of going quiet — the QC345 `price` shape (#532). It is also what answers "does
#: this tenant carry duplicate symbol identities" per lane, without grepping a container (#625).

#: Cockpit reads these itself and judges them alongside the strategy's three.
SUPPLIED_BY_PLATFORM = ("lifecycle", "budget", "slot_size")

REQUIRED_PROBES = REQUIRED_FROM_STRATEGY + SUPPLIED_BY_PLATFORM


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    #: Declared but with no rule here. Never a pass — a lane that adds a seventh capability must not
    #: have it silently ignored while believing it is checked.
    UNKNOWN = "UNKNOWN"
    #: Required and not reported. Absence must not read as agreement; that is exactly how
    #: `test_broker_equity_is_a_METHOD_on_every_strategy` missed QC27.
    MISSING = "MISSING"
    #: The lane is switched off. Not a breach.
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class Probe:
    """One observation from the strategy. No verdict field, deliberately — see the module docstring."""

    name: str
    value: object = None
    error: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    name: str
    outcome: Outcome
    detail: str


def judge_preflight(probes, *, enabled: bool, has_filled_this_session: bool) -> list[ProbeResult]:
    """Judge observations against what the platform knows. Never raises."""
    by_name = {p.name: p for p in probes}

    if not enabled:
        # A lane nobody switched on has not breached anything. Failing it would light up every disabled
        # strategy the day this ships, and an alarm that fires on the normal state gets switched off
        # wholesale — the same reasoning budget_gate.py applies to an unknown sleeve.
        return [ProbeResult(p.name, Outcome.SKIPPED, "lane is disabled") for p in probes]

    out: list[ProbeResult] = []
    for name, probe in by_name.items():
        out.append(_judge_one(name, probe, by_name, has_filled_this_session))
    # A MISSING PROBE NAMES WHOSE BUG IT IS. The two halves fail for different reasons and want
    # different readers: a lane that stopped reporting `equity` is kumo-trading-strategies' problem, a missing
    # `budget` is cockpit failing to supply its own state. One message for both would send whoever
    # reads it to the wrong repo.
    for missing in REQUIRED_FROM_STRATEGY:
        if missing not in by_name:
            out.append(ProbeResult(missing, Outcome.MISSING,
                                   "the strategy reported no such probe"))
    for missing in SUPPLIED_BY_PLATFORM:
        if missing not in by_name:
            out.append(ProbeResult(missing, Outcome.MISSING,
                                   "cockpit did not supply this probe — platform-side, not the lane's"))
    return out


def _judge_one(name, probe, by_name, has_filled) -> ProbeResult:
    # A RAISE AND A None ARE DIFFERENT DIAGNOSES and must not collapse. ccea7b4 was a broken call;
    # b0593af was a call that worked and returned a key nobody publishes. Same symptom, same probe,
    # different fix — the detail is what tells them apart.
    if probe.error:
        return ProbeResult(name, Outcome.FAIL, f"raised: {probe.error}")

    v = probe.value
    if name == "armed":
        # A LANE THAT STARTED BUT CANNOT DECIDE. Before kumo-trading-strategies 7de760a a lane whose calendar
        # fetch timed out took the NODE down — blocking urlopen on the event loop inside `on_start`,
        # which Nautilus re-raised from `Trader.START`, so three healthy lanes never started because
        # they were queued behind one dead socket. Arming now happens in the background and a lane
        # that cannot reach its calendar starts UNARMED instead.
        #
        # That is strictly better and it converts a loud crash into a quiet one: an unarmed lane is
        # alive, counted as running, and will never schedule anything. This is the probe that makes it
        # visible.
        if v is True:
            return ProbeResult(name, Outcome.PASS, "armed")
        # THE MESSAGE SAYS WHETHER TO ACT NOW. Arming retries every 60s in the background, so False a
        # few seconds after boot is ORDINARY. A bare FAIL invites a restart that fixes nothing and
        # throws away the attempt in flight.
        return ProbeResult(
            name, Outcome.FAIL,
            f"UNARMED (is_armed={v!r}) — the lane is running and cannot decide until its calendar "
            f"resolves. Arming retries every 60s, so this is expected briefly after boot and is a "
            f"defect if it persists",
        )

    if name == "equity":
        if v is None:
            return ProbeResult(name, Outcome.FAIL, "returned None — the call worked and the value did not")
        if float(v) <= 0:
            return ProbeResult(name, Outcome.FAIL, f"equity={v}")
        return ProbeResult(name, Outcome.PASS, f"equity={v}")

    if name == "price":
        if v is None or float(v) <= 0:
            return ProbeResult(name, Outcome.FAIL, f"price={v} for a claimed instrument")
        return ProbeResult(name, Outcome.PASS, f"price={v}")

    if name == "slot_size":
        priced = (by_name.get("price") or Probe("price")).value
        budget = (by_name.get("budget") or Probe("budget")).value
        if int(v or 0) <= 0 and (priced or 0) and (budget or 0):
            # The QC345 signature: money and a price, and still nothing to buy.
            return ProbeResult(name, Outcome.FAIL,
                               f"sized {v} shares at {priced} with budget {budget}")
        return ProbeResult(name, Outcome.PASS, f"slot={v} shares")

    if name == "budget":
        if float(v or 0) <= 0:
            return ProbeResult(name, Outcome.FAIL, f"budget={v} while the lane is enabled")
        return ProbeResult(name, Outcome.PASS, f"budget={v}")

    if name == "lifecycle":
        if str(v).upper() in ("DISABLED", "NONE", ""):
            return ProbeResult(name, Outcome.FAIL, f"lifecycle={v} while the lane is enabled")
        return ProbeResult(name, Outcome.PASS, f"lifecycle={v}")

    if name == "owned":
        # JUDGED AGAINST EXPECTATION, not for a non-error return. TECHIVOL reported eight symbols and
        # no error and looked healthy; it had read the ACCOUNT book as its own and went on to form
        # eight liquidation orders against other strategies' positions.
        held = v or {}
        if held and not has_filled:
            return ProbeResult(name, Outcome.FAIL,
                               f"claims {len(held)} position(s) having never filled: "
                               f"{sorted(held)[:8]}")
        return ProbeResult(name, Outcome.PASS, f"owns {len(held)} position(s)")

    if name == "universe":
        # issue 80. Counts, not a verdict — THE RELATIONSHIP IS THE DETECTOR:
        # requested == resolved + len(unresolved). It disagrees loudly if any resolution path stops
        # recording. `ambiguous` is a SUBSET of resolved (those symbols DID resolve, to an identity
        # chosen among several) so it never enters the identity.
        try:
            req = int(v.get("requested")); res = int(v.get("resolved"))
            unres = list(v.get("unresolved") or []); amb = dict(v.get("ambiguous") or {})
        except Exception:                                               # noqa: BLE001
            return ProbeResult(name, Outcome.FAIL, f"malformed universe probe: {v!r}")
        if req != res + len(unres):
            return ProbeResult(name, Outcome.FAIL,
                               f"universe identity broken: requested {req} != resolved {res} + "
                               f"unresolved {len(unres)} — a resolution path stopped recording")
        if req > 0 and res == 0:
            # A lane whose whole pool failed to resolve can trade NOTHING while reporting armed —
            # the state TECHIVOL sat in for days. Distinct from a deliberate empty pool (req == 0).
            return ProbeResult(name, Outcome.FAIL,
                               f"0 of {req} symbols resolved — the lane cannot trade anything")
        detail = f"{res} of {req} resolved"
        if unres:
            detail += f"; NOT trading {len(unres)}: {', '.join(map(str, unres[:6]))}"
        if amb:
            detail += f"; {len(amb)} ambiguous (dual identity): {', '.join(list(amb)[:4])}"
        return ProbeResult(name, Outcome.PASS, detail)

    return ProbeResult(name, Outcome.UNKNOWN, f"no rule for this probe (value={v!r})")
