"""The market-aware protocol, cockpit's half (#873): read three optional hooks on a lane, fold the
answers into readings, and turn TRANSITIONS — never states — into events.

Three surfaces derive from this one fold so they cannot disagree: the UI badge renders the READINGS,
the journal writes a `state` row per EVENT, the notifier pages per EVENT. A poll that produces no
event touches nothing but the reading. That is the rate discipline (Operator: notify on state
TRANSITIONS, never per poll or per slot — a channel that speaks every five minutes is one nobody
reads, which is worse than no channel because it looks like coverage).

FIVE reading states, not three. `not_asked` (the lane does not implement the hook), `unknown` (the
hook said so — the NORMAL case for a newly registered lane, for months), `fault` (the hook RAISED —
a defect, never folded into unknown), `yes`, `no`. Collapsing raise into unknown makes a broken view
indistinguishable from an honest "not yet", and the honest one is the common case.

`.acts` IS THE SINGLE DERIVATION. kumo-strategies' `Verdict.acts` / `Assessment.acts` (f5895d1) say
whether the answer is one to act on; `.state` says what was seen. `Assessment(OUT_OF_ENVELOPE,
evidence_sufficient=False)` has `.acts False` and must not trigger — a fold reading `.state` would
fire on the first bad window, exactly the case the threshold exists to suppress. This module never
imports those classes: it duck-types `.acts`/`.state`/`.reasons`, so the poller does not depend on
the strategies pin (no deployed pin carries the contract today — see `contract_state`).

NOTIFICATION VOCABULARY. Kinds, labels, severities and the payload shape MIRROR
`kumo_strategies.strategies.market_events` (`EventKind`, `LABELS`, `SEVERITY`, `MarketEvent.payload`)
rather than import it: the #903 class guard bans a module-scope import of the strategies package,
and the module is absent from every deployed pin. `test_market_aware_contract.py` pins the mirror
EQUAL to the installed contract whenever it is present — a kind or label added upstream fails a test
here instead of reaching the operator's phone as a blank badge. When the pin carries the contract, the
mirror can become a call-time import; the tests do not change.

The severity BUDGET is the contract's and it is not negotiable: only EMERGENCY_EXIT is CRITICAL. If
three things page, the channel gets muted and the one that mattered goes with it. A hook that
RAISED is a broken check, not an emergency — WARN, with a consecutive-fault COUNT carried on every
surface so a check that has raised all day is visible as "protection absent", not quiet.

"quarantine" is NOT used here for a lane's self-report: cockpit already uses that word for the #79
plane (foreign broker activity, "UNCLAIMED · foreign strategy"). The lane self-report action is
STAND_DOWN, a separate upstream type (`SelfAction`) from `MarketAction`.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field, replace

__all__ = [
    "HOOKS", "NOT_ASKED", "UNKNOWN", "FAULT", "YES", "NO", "UNPOLLED", "READING_STATES",
    "EXIT_ONLY_ENTERED", "EXIT_ONLY_LEFT", "EMERGENCY_TRIGGERED", "EMERGENCY_CLEARED",
    "STAND_DOWN_REQUESTED", "ASSESSMENT_RECOVERED", "HOOK_UNKNOWN_TWICE", "HOOK_FAULT", "HOOK_RECOVERED", "EVENT_KINDS",
    "RISK_ON", "RISK_OFF", "IN_ENVELOPE", "OUT_OF_ENVELOPE", "CONDITION_STATES", "condition_of",
    "HookReading", "LaneReadings", "Event", "probe", "read_hook", "fold", "poll_lane", "as_payload",
    "CONTRACT_KINDS", "CONTRACT_LABELS", "CONTRACT_SEVERITY", "COCKPIT_KINDS", "LABELS", "SEVERITY",
    "ACTIONS", "RESTRICTS_TRADING", "NOTIFY", "RESERVED", "RESERVED_CONTRACT", "INFO", "WARN", "CRITICAL", "notification", "contract_state",
    "CONTRACT_MODULE",
]

#: The three hooks of the protocol, by the NAME that says what each gates. Never "veto"/"gate".
HOOKS: tuple[str, ...] = ("entries_blocked", "emergency_exit", "self_assessment")

# -- reading states ------------------------------------------------------------------------------------
NOT_ASKED = "not_asked"   # the lane does not implement the hook
UNKNOWN = "unknown"       # the hook answered "cannot compute" — honest, and common early on
FAULT = "fault"           # the hook RAISED, or returned something without `.acts` — a defect
YES = "yes"               # `.acts` True
NO = "no"                 # `.acts` False on a computable answer
READING_STATES: tuple[str, ...] = (NOT_ASKED, UNKNOWN, FAULT, YES, NO)
#: `previous` on a first-poll event. The contract requires `previous` explicitly (defaulting it would
#: turn every lane's first poll into an event); before the first poll there is no reading at all.
UNPOLLED = "unpolled"

# -- CONDITION states: what a notification's `previous`/`current` carry -----------------------------------
# The contract's one runtime invariant: `MarketEvent` REFUSES previous == current ("a STATE, not a
# transition"). Reading states cannot satisfy it — the dwell-th `yes` follows a `yes`. So the fields
# carry the CONDITION the hook describes, in the contract's own words: the two market hooks read
# RISK_ON / RISK_OFF / UNKNOWN, the self-assessment IN_ENVELOPE / OUT_OF_ENVELOPE / UNKNOWN; cockpit
# adds `fault`, `not_asked` and `unpolled`. `emergency_exit` is RISK_OFF only once the dwell is met —
# below it the condition has not fired, whatever the last answer was. Pinned against the real
# constructor in `test_market_aware_contract.py`.
RISK_ON = "risk_on"
RISK_OFF = "risk_off"
IN_ENVELOPE = "in_envelope"
OUT_OF_ENVELOPE = "out_of_envelope"
CONDITION_STATES: tuple[str, ...] = (RISK_ON, RISK_OFF, IN_ENVELOPE, OUT_OF_ENVELOPE, UNKNOWN, FAULT,
                                     NOT_ASKED, UNPOLLED)

# -- cockpit event kinds (what the FOLD emits) ----------------------------------------------------------
EXIT_ONLY_ENTERED = "exit_only_entered"
EXIT_ONLY_LEFT = "exit_only_left"
EMERGENCY_TRIGGERED = "emergency_triggered"
EMERGENCY_CLEARED = "emergency_cleared"
STAND_DOWN_REQUESTED = "stand_down_requested"
#: The ASSESSMENT went back inside its envelope. NOT "stand-down cleared": upstream
#: (`market_view.py`) says a stand-down self-clears NEVER — a lane cannot certify its own recovery
#: with the evidence that condemned it. The reading recovering is a fact worth one page; the
#: stand-down (a phase-2 lifecycle consequence) does not end because of it.
ASSESSMENT_RECOVERED = "assessment_recovered"
HOOK_UNKNOWN_TWICE = "hook_unknown_twice"
HOOK_FAULT = "hook_fault"
#: A hook that had RAISED, or answered unknown twice running, answers again. Its own event because the
#: pager dedupes a key for hours: without a recovery to clear it, a fault that resolves and recurs the
#: same session is news to the fold and silence on the phone — two derivations of "is this news"
#: disagreeing. Emitted only when no other event fired for that hook on the poll (an episode
#: opening from unknown carries the recovery itself).
HOOK_RECOVERED = "hook_recovered"
EVENT_KINDS: tuple[str, ...] = (
    EXIT_ONLY_ENTERED, EXIT_ONLY_LEFT, EMERGENCY_TRIGGERED, EMERGENCY_CLEARED,
    STAND_DOWN_REQUESTED, ASSESSMENT_RECOVERED, HOOK_UNKNOWN_TWICE, HOOK_FAULT, HOOK_RECOVERED,
)


@dataclass(frozen=True)
class HookReading:
    """One hook's answer at one poll, plus the streaks the fold keeps across polls.

    `state` is what cockpit branches on (five values). `observed` is what the hook SAID (`yes`,
    `out_of_envelope`, …) — display only; the thin-evidence assessment reads `state NO` with
    `observed out_of_envelope`, which is the badge's business, not the trigger's.
    """

    state: str
    observed: str
    reasons: tuple[str, ...] = ()
    asked_at_ns: int = 0
    #: The action the answer named (`Assessment.action` / a verdict's action), as its value string,
    #: or None — display only; the badge words the high tail ("investigate") differently from the
    #: low tail ("stand_down") and neither is a trigger here.
    action: str | None = None
    streak_yes: int = 0
    streak_unknown: int = 0
    streak_fault: int = 0
    #: An EPISODE is open from the hook's trigger until the hook says NO. `unknown`/`fault` inside an
    #: episode are gaps — neither a recovery nor a second episode.
    in_episode: bool = False
    #: The last CONDITION a computable answer produced (RISK_ON, OUT_OF_ENVELOPE, …), carried across
    #: unknown/fault gaps so a page out of a gap can say where the lane was — `previous` on
    #: `hook_unknown_twice` is this, never the unknown it followed.
    last_computable: str = UNPOLLED


@dataclass(frozen=True)
class LaneReadings:
    entries_blocked: HookReading
    emergency_exit: HookReading
    self_assessment: HookReading
    polled_at_ns: int

    def hook(self, name: str) -> HookReading:
        return getattr(self, name)


@dataclass(frozen=True)
class Event:
    """A transition on one hook of one lane. `previous`/`current` are CONDITION states (see
    `CONDITION_STATES`) — never equal, which the contract's constructor enforces — both carried
    because a notification that says only where a lane ended up cannot tell an operator whether
    anything happened. `reading` is the five-state answer this poll produced."""

    kind: str
    hook: str
    reasons: tuple[str, ...] = ()
    previous: str = UNPOLLED
    current: str = NOT_ASKED
    polls: int = 0
    faults: int = 0
    reading: str = NOT_ASKED

    def __post_init__(self) -> None:
        """The contract's refusal, enforced where the event is BUILT rather than inferred from the
        fold's control flow: today only `needed = 1` on two hooks keeps an entering event's
        previous (the last condition) from equalling its current; a future per-hook dwell would
        reintroduce the collision silently. Same rule as `MarketEvent.__post_init__` upstream."""
        if self.previous == self.current:
            raise ValueError(f"{self.hook}: a {self.kind} event with previous == current ({self.current!r}) "
                             f"is a STATE, not a transition")


def probe(lane: object) -> frozenset[str]:
    """Which of the three hooks the lane carries. `hasattr`, no call."""
    return frozenset(h for h in HOOKS if callable(getattr(lane, h, None)))


def read_hook(lane: object, name: str, *, ts_ns: int) -> HookReading:
    """Ask one hook once. Never raises: a raising hook is a FAULT reading that names the exception,
    and a return without `.acts` is a FAULT too — `Verdict(answer=False)` from the two-state era is
    a truthy dataclass, and reading truthiness would turn it into YES."""
    hook = getattr(lane, name, None)
    if not callable(hook):
        return HookReading(NOT_ASKED, observed=NOT_ASKED, asked_at_ns=ts_ns)
    try:
        answer = hook()
    except Exception as exc:  # noqa: BLE001 — the whole point: a raise is a named reading, not a crash
        return HookReading(FAULT, observed=FAULT, reasons=(f"{type(exc).__name__}: {exc}",), asked_at_ns=ts_ns)
    acts = getattr(answer, "acts", None)
    if not isinstance(acts, bool):
        return HookReading(FAULT, observed=FAULT, asked_at_ns=ts_ns,
                           reasons=(f"hook returned {type(answer).__name__} without a boolean `.acts`",))
    observed = str(getattr(answer, "state", "") or "")
    reasons = tuple(str(r) for r in (getattr(answer, "reasons", ()) or ()))
    raw_action = getattr(answer, "action", None)
    action = getattr(raw_action, "value", raw_action) if raw_action is not None else None
    action = str(action) if action is not None else None
    if acts:
        return HookReading(YES, observed=observed, reasons=reasons, asked_at_ns=ts_ns, action=action)
    if observed == UNKNOWN:
        return HookReading(UNKNOWN, observed=observed, reasons=reasons, asked_at_ns=ts_ns, action=action)
    return HookReading(NO, observed=observed, reasons=reasons, asked_at_ns=ts_ns, action=action)


#: What opens and closes each hook's episode. Emergency opens only after `dwell` consecutive YES.
_ENTER = {"entries_blocked": EXIT_ONLY_ENTERED, "emergency_exit": EMERGENCY_TRIGGERED,
          "self_assessment": STAND_DOWN_REQUESTED}
_LEAVE = {"entries_blocked": EXIT_ONLY_LEFT, "emergency_exit": EMERGENCY_CLEARED,
          "self_assessment": ASSESSMENT_RECOVERED}
_OFF = {"entries_blocked": RISK_OFF, "emergency_exit": RISK_OFF, "self_assessment": OUT_OF_ENVELOPE}
_ON = {"entries_blocked": RISK_ON, "emergency_exit": RISK_ON, "self_assessment": IN_ENVELOPE}


def condition_of(name: str, state: str, *, in_episode: bool) -> str:
    """The CONDITION a reading describes, in the contract's words. `emergency_exit` answering yes
    below its dwell is still RISK_ON — the condition has not fired."""
    if state == YES:
        return _OFF[name] if (name != "emergency_exit" or in_episode) else _ON[name]
    if state == NO:
        return _ON[name]
    return state                      # unknown / fault / not_asked are their own conditions


def fold(prev: LaneReadings | None, now: dict[str, HookReading], *, dwell: int, ts_ns: int
         ) -> tuple[LaneReadings, tuple[Event, ...]]:
    """The ONE transition predicate. Returns the next readings and the events this poll produced —
    an empty tuple for "nothing changed", which is the default and the common case. A hook absent
    from `now` reads NOT_ASKED (a poller asking only the hooks `probe()` found must not crash).

    Rules, each pinned by `test_market_aware_fold.py`:
    - `unknown`, `fault` and `not_asked` all break a YES streak: nothing acts on unknown (rule 4),
      and a dwell that accumulated unknowns would turn a data outage into a trigger by patience.
    - Inside an open episode they are GAPS: no recovery is reported (the lane did not say NO) and
      YES again does not start a second episode.
    - `hook_unknown_twice` fires when the unknown streak becomes exactly 2 (BROKEN_CHECK_POLLS: a
      check that cannot answer must report itself), `hook_fault` on ENTRY to fault; the fault streak
      keeps counting so a day of raises is visible as a number, not as quiet.
    - Every event's `previous`/`current` are CONDITIONS that differ (the contract refuses equal ones).
    """
    if dwell < 1:
        raise ValueError(f"dwell must be >= 1 poll, got {dwell!r} — 0 would trigger on nothing")
    events: list[Event] = []
    readings: dict[str, HookReading] = {}
    for name in HOOKS:
        r = now.get(name) or HookReading(NOT_ASKED, observed=NOT_ASKED, asked_at_ns=ts_ns)
        p = prev.hook(name) if prev is not None else None
        prev_state = p.state if p is not None else UNPOLLED
        prev_condition = condition_of(name, p.state, in_episode=p.in_episode) if p is not None else UNPOLLED
        last_computable = p.last_computable if p is not None else UNPOLLED
        streak_yes = (p.streak_yes if p else 0) + 1 if r.state == YES else 0
        streak_unknown = (p.streak_unknown if p else 0) + 1 if r.state == UNKNOWN else 0
        streak_fault = (p.streak_fault if p else 0) + 1 if r.state == FAULT else 0
        in_episode = p.in_episode if p else False

        if r.state == YES and not in_episode:
            needed = dwell if name == "emergency_exit" else 1
            if streak_yes >= needed:
                in_episode = True
        elif r.state == NO and in_episode:
            in_episode = False
        condition = condition_of(name, r.state, in_episode=in_episode)
        if r.state in (YES, NO):
            last_computable = condition

        def ev(kind: str, previous: str, current: str) -> None:
            events.append(Event(kind=kind, hook=name, reasons=r.reasons, previous=previous,
                                current=current, polls=streak_yes, faults=streak_fault, reading=r.state))

        before = len(events)
        if r.state == FAULT and prev_state != FAULT:
            ev(HOOK_FAULT, prev_condition, FAULT)
        if r.state == UNKNOWN and streak_unknown == 2:
            # Out of a gap: `previous` is where the lane was when it could still answer.
            ev(HOOK_UNKNOWN_TWICE, last_computable, UNKNOWN)
        if in_episode and not (p.in_episode if p else False):
            # `previous` is where the lane WAS (UNPOLLED on a first poll — the contract forbids
            # defaulting it), `current` the condition that just fired.
            ev(_ENTER[name], prev_condition, _OFF[name])
        elif (p.in_episode if p else False) and not in_episode:
            ev(_LEAVE[name], _OFF[name], _ON[name])
        was_broken = p is not None and (p.state == FAULT or p.streak_unknown >= 2)
        if was_broken and r.state in (YES, NO) and len(events) == before:
            ev(HOOK_RECOVERED, prev_condition, condition)
        readings[name] = replace(r, streak_yes=streak_yes, streak_unknown=streak_unknown,
                                 streak_fault=streak_fault, in_episode=in_episode,
                                 last_computable=last_computable)
    return LaneReadings(polled_at_ns=ts_ns, **readings), tuple(events)


def poll_lane(lane: object, prev: LaneReadings | None, *, dwell: int, ts_ns: int
              ) -> tuple[LaneReadings, tuple[Event, ...]]:
    """THE SEAM: ask every hook the lane carries and fold — so the engine poller is a caller of this
    module, not an author of the composition."""
    now = {h: read_hook(lane, h, ts_ns=ts_ns) for h in HOOKS}
    return fold(prev, now, dwell=dwell, ts_ns=ts_ns)


def as_payload(state: LaneReadings | None, *, dwell: int) -> dict | None:
    """Per-lane surface. `None` before the first poll (three-state at the container: never polled is
    not "nothing to report"). Per hook: state, observed, reasons in full, polls (the YES streak),
    faults (the fault streak), dwell, in_episode."""
    if state is None:
        return None
    out: dict = {"polled_at_ns": state.polled_at_ns}
    for name in HOOKS:
        r = state.hook(name)
        out[name] = {
            "state": r.state, "observed": r.observed, "reasons": list(r.reasons), "action": r.action,
            "condition": condition_of(name, r.state, in_episode=r.in_episode),
            "asked_at_ns": r.asked_at_ns, "polls": r.streak_yes, "unknowns": r.streak_unknown,
            "faults": r.streak_fault, "in_episode": r.in_episode, "dwell": dwell,
        }
    return out


# -- the notification vocabulary: a MIRROR of kumo_strategies.strategies.market_events ---------------------

INFO, WARN, CRITICAL = "info", "warn", "critical"

#: The contract's kinds and labels, verbatim. Pinned equal to the installed module by
#: `test_market_aware_contract.py`; a kind added upstream fails there.
CONTRACT_KINDS: tuple[str, ...] = (
    "entries_blocked", "entries_unblocked", "emergency_exit", "emergency_cleared", "view_unreadable",
    "view_recovered", "out_of_envelope_observed", "out_of_envelope_confirmed", "back_in_envelope",
)
CONTRACT_LABELS: dict[str, str] = {
    "entries_blocked": "not opening new positions",
    "entries_unblocked": "opening positions again",
    "emergency_exit": "closing its book",
    "emergency_cleared": "no longer closing its book",
    "view_unreadable": "market view unreadable",
    "view_recovered": "market view readable again",
    "out_of_envelope_observed": "outside its own envelope (one window)",
    "out_of_envelope_confirmed": "outside its own envelope (confirmed)",
    "back_in_envelope": "back inside its own envelope",
}
CONTRACT_SEVERITY: dict[str, str] = {
    "emergency_exit": CRITICAL,
    "emergency_cleared": WARN,
    "entries_blocked": WARN,
    "entries_unblocked": INFO,
    "view_unreadable": WARN,
    "view_recovered": INFO,
    "out_of_envelope_observed": INFO,
    "out_of_envelope_confirmed": WARN,
    "back_in_envelope": INFO,
}
#: Kinds the contract has no word for. `hook_fault`: the hook RAISED (a broken check — WARN, never
#: CRITICAL, with the fault count carried); `hook_unknown_twice`: the operator's "unknown twice consecutively
#: is its own alert" — NOT the contract's `view_unreadable`, whose trigger is "fell into unknown from
#: a computable state"; the two rules differ and are not pretended equal.
#: (`emergency_cleared` was a cockpit-only split for one hour: the contract folded RISK_OFF → RISK_ON
#: under LIQUIDATE into `entries_unblocked`; the table-driven test against `transition()` recorded the
#: divergence and kumo-strategies 80f6eae added the kind — same words. The mirror adopted it.)
COCKPIT_KINDS: tuple[str, ...] = ("hook_fault", "hook_unknown_twice", "hook_recovered")
LABELS: dict[str, str] = {
    **CONTRACT_LABELS,
    "hook_fault": "hook raising — its protection is absent",
    "hook_unknown_twice": "answering unknown twice running",
    "hook_recovered": "hook answering again",
}
SEVERITY: dict[str, str] = {**CONTRACT_SEVERITY, "hook_fault": WARN, "hook_unknown_twice": WARN,
                            "hook_recovered": INFO}
#: The union of `MarketAction` (what a lane does on risk-off) and `SelfAction` (what it does on its
#: own self-report) — TWO upstream enums, deliberately, so a market view can never be configured to
#: stand a lane down. Pinned to the installed members when present.
#: `investigate` (kumo-strategies, 2026-09-11): the lane sits ABOVE its own envelope — beating its
#: band is not doing what it said; the live hypotheses are a sizing bug, a data error or a config
#: change. A reason to LOOK, never a reason to reduce. `SelfAction.reduces` is a property on the
#: enum; anything in cockpit that ever reduces must gate on `reduces`, never on "an action is set" —
#: a truthy action read as "reduce something" would flatten a WINNING lane (kumo-strategies#144).
ACTIONS: tuple[str, ...] = ("exit_only", "liquidate", "stand_down", "investigate")
#: Which actions RESTRICT a lane's trading — cap it (`exit_only`: no new entries) or shrink it
#: (`liquidate`, `stand_down`). NOT `SelfAction.reduces`, deliberately: that property answers "does
#: acting on this make the book SMALLER?" for kumo-strategies' own enum, and EXIT_ONLY caps a book
#: without shrinking it — the same name over both enums would make `reduces` mean two things
#: (ks#177 review, l21). Every action that reduces restricts; `investigate` does neither. Phase 1 acts
#: on nothing; this is the table phase 2 gates on — never "an action is set".
RESTRICTS_TRADING: dict[str, bool] = {"exit_only": True, "liquidate": True, "stand_down": True, "investigate": False}
#: Cockpit event → (notification kind, action). Every cockpit kind has a row; a fold kind without one
#: is a page that cannot be built, caught by the test rather than at 3 am.
NOTIFY: dict[str, tuple[str, str | None]] = {
    EXIT_ONLY_ENTERED: ("entries_blocked", "exit_only"),
    EXIT_ONLY_LEFT: ("entries_unblocked", "exit_only"),
    EMERGENCY_TRIGGERED: ("emergency_exit", "liquidate"),
    EMERGENCY_CLEARED: ("emergency_cleared", "liquidate"),
    STAND_DOWN_REQUESTED: ("out_of_envelope_confirmed", "stand_down"),
    # The assessment recovered; the stand-down itself never self-clears (upstream's rule).
    ASSESSMENT_RECOVERED: ("back_in_envelope", "stand_down"),
    HOOK_UNKNOWN_TWICE: ("hook_unknown_twice", None),
    HOOK_FAULT: ("hook_fault", None),
    HOOK_RECOVERED: ("hook_recovered", None),
}
#: Payload keys the notification owns. `detail` is spread over the top, so a colliding key is REFUSED
#: rather than overwritten — a detail key named `lane` would re-attribute the page to another strategy.
#: `RESERVED_CONTRACT` is the contract's own set (pinned equal to `market_events._RESERVED`);
#: `RESERVED` adds the fields cockpit writes beside them, for the same reason.
RESERVED_CONTRACT: frozenset[str] = frozenset({"lane", "kind", "severity", "label", "previous", "current",
                                               "reasons", "action"})
RESERVED: frozenset[str] = RESERVED_CONTRACT | frozenset({"hook", "reading", "polls", "faults"})


def notification(lane: str, event: Event, *, detail: dict[str, object] | None = None) -> dict[str, object]:
    """The flat, JSON-safe notification body for one event — the contract's `payload()` shape plus the
    hook name and the streaks. `reasons` in full, never summarised: the reason a lane de-risked is the
    first thing asked afterwards and the first thing lost."""
    kind, action = NOTIFY[event.kind]
    extra = dict(detail or {})
    clash = RESERVED & set(extra)
    if clash:
        raise ValueError(f"{lane}: detail keys {sorted(clash)} collide with the notification's own fields")
    return {
        "lane": lane,
        "kind": kind,
        "severity": SEVERITY[kind],
        "label": LABELS[kind],
        "previous": event.previous,
        "current": event.current,
        "reasons": list(event.reasons),
        "action": action,
        "hook": event.hook,
        "reading": event.reading,
        "polls": event.polls,
        "faults": event.faults,
        **extra,
    }


CONTRACT_MODULE = "kumo_strategies.strategies.market_events"


def contract_state() -> dict[str, str]:
    """Whether the installed strategies package carries the contract — nameable from OUTSIDE, on the
    health frame, so "the plane is present and reading nothing YET" cannot be read as "present and
    clean". `find_spec`, not an import of the MODULE — it does import the PARENT packages
    (`kumo_strategies`, `kumo_strategies.strategies`), which the engine imports anyway, and never the
    contract module itself; no module-scope import here (#903). A pin that lacks the module answers
    `absent` with the module named; a parent package that fails to import answers `absent` WITH the
    reason. When the pin moves to a build that carries it, this is the readback: absent -> present."""
    try:
        present = importlib.util.find_spec(CONTRACT_MODULE) is not None
    except (ImportError, ValueError) as exc:   # a broken parent package is `absent` WITH its reason
        return {"state": "absent", "module": CONTRACT_MODULE, "reason": f"{type(exc).__name__}: {exc}"}
    return {"state": "present" if present else "absent", "module": CONTRACT_MODULE}
