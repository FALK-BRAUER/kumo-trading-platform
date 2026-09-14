"""WHEN to capture the book, decided separately from HOW (#734 step 4).

THE SCHEDULE COMES FROM THE VENUE, NOT FROM A CRON. A fixed 16:00 ET timer is wrong on every half
day, and the operator's clock is SGT so a locally-stamped date puts a 04:00 SGT close on the wrong
calendar day. `VenueCalendar.day()` already answers both questions and RAISES on a day the venue
never described — `next_fire` looks 14 days ahead while IB's `liquidHours` is a rolling ~6-day
window, which is a defect class this repo has already paid for.

THREE STATES, NEVER TWO. `should_capture` returns a decision object, not a bool: fire, wait, or
REFUSE-because-the-day-is-unknown. An unknown day must not collapse into "not yet" — that is
"absence readable as permission" and it would silently skip captures forever on an instance whose
calendar went stale.

PURE. No clock, no cache, no database. The engine hook is a few lines that call this and then do what
it says, so the decision is testable without a running node — which matters because the thing being
scheduled runs once a day and a bug in it would take a day to observe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

#: OPT-IN, like every automation gate in this repo. An unset variable means OFF: a capture job that
#: started writing the moment it was deployed would be a behaviour change nobody asked for, on an
#: instance whose operator has not decided they want it.
_GATE = "KUMO_EOD_CAPTURE"

#: How long after the close to capture. The book must be QUIESCENT: engine-vs-venue mismatches during
#: in-flight fills are normal (continuous reconciliation runs on 5s/10s loops), and an alarm that
#: cannot tolerate them cries wolf and gets muted. Late enough to settle, early enough that a machine
#: going down for the night has not yet taken the process with it.
CAPTURE_OFFSET_MINUTES = 20


def capture_enabled(env=None, *, var: str = _GATE) -> bool:
    """The gate. Absent or unparseable means OFF.

    `var` names WHICH gate, because there are two and they must be independently settable: the live
    capture writes one row per day going forward, while the backfill writes history in bulk on an
    operator's say-so. Enabling one must not enable the other — an instance running the nightly
    capture has not thereby asked for its past to be rewritten.
    """
    raw = (env or os.environ).get(var, "")
    return str(raw).strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class CaptureDecision:
    """What to do right now, and WHY — the reason travels so a log line can explain a quiet day."""

    #: fire | wait | refuse
    action: str
    #: The ET trading date this capture belongs to. None unless `action == "fire"`.
    session_date: str | None
    reason: str

    @property
    def should_fire(self) -> bool:
        return self.action == "fire"


def should_capture(
    now: datetime,
    calendar,
    *,
    already_captured: set[str] | None = None,
    offset_minutes: int = CAPTURE_OFFSET_MINUTES,
) -> CaptureDecision:
    """Is it time to capture, for which trading date?

    `already_captured` is the set of session dates this process has already written. The DATABASE is
    the real guard — `uq_eod_observation_base` refuses a duplicate base row — so this is an
    optimisation, not the guarantee. Belt and braces in that order, never the other way round.

    RAISES NOTHING. A calendar that cannot describe today returns `refuse` with the reason, because
    the caller runs on Nautilus's clock thread and an exception there is not a thing to risk over a
    reporting feature.
    """
    already = already_captured or set()
    local = now
    try:
        day = calendar.day(local.date())
    except Exception as exc:                                            # noqa: BLE001
        # The venue never described this day. NOT "no", and not a trading day either — the third
        # state. Refusing loudly is what stops a stale calendar silently skipping every capture.
        return CaptureDecision("refuse", None, f"the venue has not described {local.date()}: {exc}")

    if day is None:
        return CaptureDecision("wait", None, f"{local.date()} is not a trading day at this venue")

    close_at = getattr(day, "close_at", None)
    # REFUSE, DO NOT STAMP THE LOCAL DATE. This was `getattr(day, "session", "") or
    # local.date().isoformat()` — a fallback putting the OPERATOR's date on the row, and this
    # module's own docstring names that exact hazard: the operator is in SGT and the venue in ET, so
    # a 04:00 SGT capture would have filed an ET session under the following day. A calendar row that
    # cannot name its own session is a broken calendar, not a licence to guess.
    session = str(getattr(day, "session", "") or "")
    if not session:
        return CaptureDecision(
            "refuse", None,
            f"the calendar row for {local.date()} does not name its session — refusing rather than "
            f"stamping the operator's local date, which is a different day at this venue",
        )
    if close_at is None:
        return CaptureDecision("refuse", None, f"{session} has no close time on the calendar")

    fire_at = close_at + timedelta(minutes=offset_minutes)
    # MIXED awareness is what raises; both-aware and both-naive are each comparable. The first cut
    # refused both-naive with the message "one side is timezone-naive", which is simply false for that
    # case — a wrong reason attached to a conservative outcome, which is how a reader later "fixes"
    # the wrong thing. Refusing the MIXED case beats guessing a zone: the operator is in SGT and the
    # venue in ET, and picking wrong is a whole-day error.
    if (local.tzinfo is None) != (fire_at.tzinfo is None):
        return CaptureDecision(
            "refuse", None,
            f"cannot compare capture time: now is {'naive' if local.tzinfo is None else 'aware'} and "
            f"the venue close is {'naive' if fire_at.tzinfo is None else 'aware'}",
        )
    reached = local >= fire_at

    if not reached:
        return CaptureDecision("wait", None, f"{session} closes at {close_at}; capture at {fire_at}")
    if session in already:
        return CaptureDecision("wait", None, f"{session} already captured by this process")
    return CaptureDecision("fire", session, f"{session} closed at {close_at}; capturing")
