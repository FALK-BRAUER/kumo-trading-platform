"""Prove a strategy can trade at BOOT, not on its first live decision (#438, #440).

the operator's requirement, verbatim: "We need to be sure a strategy is tradable before it is wired. Latest
when it is wired and system boots up or short after."

WHY NOT AT ATTACH, which is where this was first attempted and got stuck. Preflight needs equity and
prices, and neither exists at registration: the account snapshot and the first bars arrive afterwards.
A gate wired at attach fails every cold start, which is worse than no gate — it fires on the normal
path, and an alarm that fires on the normal path gets switched off.

So the trigger is the FIRST ACCOUNT SNAPSHOT. That is the earliest moment the questions can actually be
answered, it is "shortly after boot", and it is before any strategy has had a chance to decide
anything: QC345 decides at 09:35, and the snapshot lands at connection.

RUNS ONCE. A gate re-running on every account tick would re-alarm all day and cost a broker call per
update. The flag is checked and set by `should_run`, so the caller cannot forget it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.preflight_runner import run_preflight
from api.seam_types import Lane, LaneBroker


@dataclass
class BootGateState:
    """Carried by the node. Deliberately no timestamp anywhere in the RETRY logic — "has this settled"
    is the only question it asks, and a time invites someone to tune a window. Everything here is a
    count of account updates. (The REPORT does carry a measurement time — see `run_boot_gate` — because
    verdicts get quoted days later as if current, which is a different problem than pacing.)"""

    ran: bool = False
    degraded: list = field(default_factory=list)
    #: How many times a DEGRADED verdict has been re-probed. The first `_MAX_DEGRADED_ATTEMPTS` fire
    #: on consecutive account updates, because `_publish_account` runs on EVERY update: on paper
    #: 2026-08-25 an unbounded retry printed the same four PREFLIGHT DEGRADED lines every ~2 seconds,
    #: and an alarm that fires continuously is one an operator silences. Still a count and not a
    #: timestamp, for the reason above.
    attempts: int = 0
    #: Late-phase pacing (#604). Account updates SKIPPED since the last past-the-cap probe, and how
    #: many must be skipped before the next one. `backoff` doubles per probe up to
    #: `_MAX_BACKOFF_UPDATES`, so a still-DEGRADED verdict is re-observed a handful of times per
    #: session instead of never — counts of updates, not timestamps, same rule as above.
    deferred: int = 0
    backoff: int = 8


#: Retries of a DEGRADED verdict on CONSECUTIVE account updates, INCLUDING the first probe. Enough to
#: cover a lane that is genuinely still coming up — the case 17f5c41 was built for — without turning a
#: permanent fault into a log every couple of seconds. Past this the retries do not STOP (#604 — a
#: verdict that can never be corrected gets quoted during outages while measuring a boot race); they
#: THIN, doubling the gap between probes up to `_MAX_BACKOFF_UPDATES`.
_MAX_DEGRADED_ATTEMPTS = 5

#: Ceiling on the late-phase gap, in account updates. At the ~2s cadence 2048 updates is roughly an
#: hour: a lane that heals is re-observed (and settled CLEAN, finally) within about an hour of
#: healing, and a lane that is permanently broken re-reports about hourly — degraded LOUDLY, as its
#: own condition, rather than silenced after ten seconds of process life.
_MAX_BACKOFF_UPDATES = 2048


def should_run(state: BootGateState, *, equity) -> bool:
    """True exactly once, and only when equity is actually known.

    `equity is None` is the case that matters. On a restart with positions held, the account frame is
    withheld rather than reporting cash as equity (#382) — so an early snapshot can arrive with no
    equity at all. Running then would fail every strategy for a reason that is about the BROKER, and
    the flag would be spent: the gate would never run again, and it would have reported nonsense on its
    only attempt.

    THE TWO STAGING VERDICTS BELOW WERE NOT A RACE, AND THIS DOCSTRING SAID THEY WERE.
    `'NoneType' object has no attribute 'equity'` was the CALLER passing the account frame — a dict,
    or None on IBKR where nothing publishes that topic — where a `NautilusBroker` belongs. One wrong
    argument, explained away as a startup race, and the retry below was built on top of that
    explanation. Kept and BOUNDED rather than removed: a lane genuinely still coming up is real, and
    `armed: UNARMED (is_armed=False)` was exactly that. But `_publish_account` fires on every account
    update, so an unbounded retry printed the same four ERROR lines every ~2 seconds on paper.

    A DEGRADED VERDICT STAYS RETRYABLE — `_MAX_DEGRADED_ATTEMPTS` consecutive probes, then a doubling
    update-count backoff (#604) — AND A CLEAN ONE IS FINAL (#515). The
    gate probes lanes that may not have finished starting, and it was wrong on staging twice:

        2026-08-24 15:33  DEGRADED BCTROT-004: equity: raised AttributeError('NoneType' ...)
                          -- the lane's broker had not been attached yet
        2026-08-25 17:17  DEGRADED BCTROT-004: armed: UNARMED (is_armed=False)
                          -- the trading calendar had not resolved; it retries every 60s

    Both were contradicted within minutes — the first by another lane reading the broker fine, the
    second by BCTROT-004 trading successfully at 01:05 — and because the flag was spent, neither could
    ever be corrected. The node carried a permanent false DEGRADED, and that verdict gets QUOTED: it
    was read as evidence during an outage on 2026-08-24 and was measuring a race.

    A gate that can only ever be wrong once is worse than no gate. Clean is final because re-probing a
    healthy stack learns nothing and would eventually report a transient as news; degraded retries
    because the lane may simply still be coming up.

    THIS IS A BOOT GATE, NOT A RUNTIME REGRESSION DETECTOR, and the distinction is load-bearing
    (codex, 2026-08-25). Once a verdict is clean it is final forever: a lane that recovers and LATER
    degrades will never be re-probed here. That is deliberate — the question this answers is "could
    this lane trade when the node came up" — but it means nothing in this file will notice a lane
    breaking at 14:00. Whatever covers that has to be a different mechanism, and reading a clean boot
    verdict as evidence about the current stack is the mistake this paragraph exists to prevent.
    """
    if equity is None:
        return False
    if state.ran and not state.degraded:
        return False
    if state.ran and state.attempts >= _MAX_DEGRADED_ATTEMPTS:
        # THE FAST RETRIES ARE SPENT — THIN, DO NOT STOP (#604). This line used to `return False`
        # forever: the five consecutive attempts were gone by ~boot+10s (the account publishes every
        # ~2s), which is BEFORE the first bar can arrive, so a price-less boot produced a DEGRADED
        # verdict that stayed permanent for the process life while every claimed position had marks
        # minutes later (TECHIVOL-005, 2026-08-27 — true for three seconds, quoted all session).
        # A verdict must stay correctable while the condition it measured can still change, and the
        # only signal flowing here IS the account update — so the late retries ride it, on a
        # doubling gap: an update-count backoff, not a timer, keeping this file's counts-not-clocks
        # rule. A healed lane is re-observed and settles CLEAN (final, #515) within ~an hour; a
        # permanently broken one re-reports about hourly instead of never.
        state.deferred += 1
        if state.deferred < state.backoff:
            return False
        state.deferred = 0
        state.backoff = min(state.backoff * 2, _MAX_BACKOFF_UPDATES)
    state.ran = True
    state.attempts += 1
    return True



def _broker_of(strategy: Lane, fallback: object) -> LaneBroker | None:
    """The lane's own broker — found by CAPABILITY, never by attribute name.

    THE NAME GUESS ALREADY FAILED ONCE, IN PRODUCTION. The first fix used
    `getattr(strategy._runner, "broker", None)`, which is a guess: `QC27SessionRunner` is a dataclass
    with a `broker` FIELD, but `qc345.py:131` is `self._broker, self._limits = broker, limits`, and
    momentum's `SessionGateway` does the same. Deployed, that fixed exactly one lane of four and the
    other three silently fell back to the account dict — with the deploy still reporting VERIFIED.

    THE PREDICATE IS `callable(x.equity)`, NOT `hasattr(x, "equity")`. The account frame is a dict
    with an `equity` KEY; `hasattr` on a dict is False but any name-based scheme that reaches for a
    mapping would still pass one along. What the gate needs is an object that ANSWERS `equity()`, so
    that is what it asks. An adapter spelling the attribute a third way costs nothing here.

    Returns None rather than the fallback when nothing qualifies, so the caller can degrade the lane
    with a diagnosis instead of letting four probes raise AttributeError each.
    """
    def _usable(cand):
        return cand if callable(getattr(cand, "equity", None)) else None

    for holder in (getattr(strategy, "_runner", None), strategy):
        if holder is None:
            continue
        for name in ("broker", "_broker"):
            found = _usable(getattr(holder, name, None))
            if found is not None:
                return found
    return _usable(fallback)


def run_boot_gate(
    strategies: dict[str, Lane],
    *,
    # ANNOTATED, AND THAT IS THE WHOLE POINT (2026-08-25). This parameter carried no annotation
    # and received `self._broker_account` — the account FRAME, a dict — for as long as the gate
    # had a caller. `object` rather than `LaneBroker | None` because it is a FALLBACK that
    # `_broker_of` validates by capability before use; declaring it as a broker would be the
    # same lie in type form.
    broker: object,
    # A DICT OR A CALLABLE(strategy_id) -> dict. Per-lane by nature: `lifecycle` and `budget` differ
    # between lanes, and one DISABLED beside one TRADING is the normal state — a single shared dict
    # would report the wrong lane's answer to every lane.
    platform,
    enabled_ids,
    journal=None,
    # WHEN the verdict was measured, stamped into every degraded record (#604). Boot-gate lines get
    # QUOTED: the 2026-08-24 DEGRADED verdicts were read as evidence during an outage days later,
    # while measuring a boot race. The stamp lives in the REPORT so a reader can see it is a
    # boot-time observation, not current state — the RETRY logic above stays timestamp-free
    # (counts, not clocks; see `BootGateState`). A callable returning an ISO-8601 UTC string;
    # None means "now", which is correct everywhere but a test.
    now=None,
) -> list:
    """Preflight every ENABLED strategy once. Returns the reports that are not ready.

    NEVER RAISES. A gate that can take the node down at boot is the #377 shape returning — QC345
    resolving its universe over HTTP at build time took MANUAL, MOMENTUM and BCTROT down with it. A
    strategy that cannot be probed is reported DEGRADED, not fatal.
    """
    degraded = []
    for sid, strategy in sorted(strategies.items()):
        # EACH LANE'S OWN BROKER, NOT A SHARED ONE, AND NEVER THE ACCOUNT DICT.
        #
        # `preflight(broker)` calls `broker.equity()`, `broker.strategy_positions()` and
        # `broker.last_price(sym)` — methods of a `NautilusBroker`. The caller used to pass
        # `self._broker_account`, which is the account FRAME off the msgbus: a plain dict. So on
        # Alpaca every probe raised `AttributeError("'dict' object has no attribute 'equity'")`, and
        # on IBKR — where nothing publishes that topic, so the dict is None — the same line read
        # `'NoneType' object has no attribute 'equity'`. Two symptoms, one wrong argument, and the
        # second one got explained away as a startup race for a whole day.
        #
        # Resolved by capability — see `_broker_of`. `broker` stays a FALLBACK for lanes built
        # without a runner, and is itself accepted only if it can answer `equity()`.
        own = _broker_of(strategy, broker)
        if own is None:
            # UNPROBEABLE IS NOT HEALTHY, and it is not an exception either. Without this the lane's
            # four probes each raise AttributeError, per attempt, which is what the paper log looked
            # like on 2026-08-25: noise where a diagnosis belongs.
            degraded.append((str(sid), (
                "no broker could be resolved for this lane — nothing on it or its runner answers "
                "`equity()`, so preflight cannot observe anything. The account frame is a dict and "
                "is not a substitute")))
            continue
        try:
            probes = platform(str(sid)) if callable(platform) else (platform or {})
            report = run_preflight(
                strategy,
                broker=own,
                platform=probes,
                enabled=str(sid) in set(enabled_ids or ()),
                # NOT JUDGED AT BOOT, and passing True is how the rule is switched off.
                #
                # `owned` fails on `held and not has_filled` — a lane claiming positions it never
                # opened. That is an INTRA-SESSION staleness check, and at boot the session has not
                # started: a lane carrying an overnight book is indistinguishable from a lane carrying
                # a phantom claim. BCTROT-004 held six symbols on the morning of 2026-08-22, so the
                # honest-looking `False` would have degraded it, and every other rotation lane, on the
                # first boot after shipping. An alarm that fires on the normal path gets switched off.
                #
                # The claims ledger answers the question this cannot (`over_claimed`, #437): it
                # compares claims to the ACCOUNT rather than to the session clock, and it fires on
                # BETA/WHD/XLV today. Ownership is checked there, correctly, instead of here, wrongly.
                has_filled_this_session=True,
                strategy_id=str(sid),
            )
        except Exception as exc:  # noqa: BLE001 — the gate must not be able to kill the boot
            degraded.append((str(sid), f"preflight raised at boot: {exc!r}"))
            continue
        if not report.ready:
            # `failures` holds ProbeResult objects, not strings. Formatting them here rather than
            # str()-ing the list keeps the operator-facing line readable — the probe NAME and its
            # reason are what identify the defect ("equity: broker_equity is a property"), and a repr
            # buries both in dataclass noise.
            why = "; ".join(f"{f.name}: {f.detail or f.outcome.value}" for f in report.failures)
            degraded.append((str(sid), why or "not ready"))
    # STAMP EVERY PATH THE SAME WAY — unresolvable broker, raised preflight, failed probes — so no
    # degraded record can leave here undated. Inside the `why` string rather than a third tuple
    # element, so the journal line, the stored `BootGateState.degraded`, and anything that later
    # quotes either all carry it for free, and no consumer needs a schema change to render it.
    measured = now() if callable(now) else _utc_now_iso()
    degraded = [(sid, f"{why} (measured {measured} — boot-gate observation, not current state)")
                for sid, why in degraded]
    if journal is not None:
        for sid, why in degraded:
            journal(sid, why)
    return degraded


def _utc_now_iso() -> str:
    """Second-resolution ISO-8601 UTC — renderable as-is in a log line or a quoted verdict."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
