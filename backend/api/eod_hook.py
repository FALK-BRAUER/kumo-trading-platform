"""The engine-side EOD capture — arming it, and everything it refuses to do (#734 step 4b).

WHAT THIS IS. Once per trading day, shortly after the venue's own close, record what each lane held.
That row is what a period Δunrealized subtracts from, and without it #699's 1W/1M/3M headline has
nothing to compute against.

NATIVE SCHEDULING, NOT A SLEEP AND NOT A CRON. `clock.set_timer` is Nautilus's own, is already what
every other periodic job in this actor uses, and works in backtest. This repo hand-rolled scheduling
with asyncio and launchd once and wrote the rule down afterwards; a wall-clock sleep on the Nautilus
thread is also how #598 parked `node.run()` entirely.

THE SCHEDULE COMES FROM THE VENUE. A fixed 16:00 ET timer is wrong on every half day, and the
operator's clock is SGT — a locally-stamped date files an ET session under the following day. A
provider that cannot supply a calendar therefore does not arm at all, rather than falling back to a
guess.

THE THREE THINGS IT WILL NOT DO:

- IT WILL NOT WRITE UNDER A KEY IT DOES NOT HAVE. When the calendar refuses a day, there is no
  session date — that IS the refusal — and every row in both tables is keyed by one. Stamping the
  operator's local date there is a defect this repo already shipped and deleted once. So a refusal
  writes NOTHING and is logged; the gap is real, visible as an absent day, and the backfill can fill
  it later. Inventing a key would make it unfillable and wrong.
- IT WILL NOT GUESS A CURRENCY. `_single_currency_amount_and_ccy` already refuses when choosing would
  be a guess, and this inherits that refusal. But it still writes the MANIFEST, because a capture
  that failed and left nothing behind is indistinguishable from a day nobody asked about.
- IT WILL NOT DEFAULT `session_fill_qty` TO ZERO. The column is NOT NULL and zero means "this lane
  traded nothing today", which is a specific false statement rather than a missing one. It is
  measured from Nautilus's own position events.

FAILURE IS REPORTED, NEVER ABSORBED. A display actor must not die on an optional job — but an
`except Exception` that logs nothing turns every failed capture into a clean-looking tick, which is
precisely the notifier-death pattern this repo has already paid for. Errors are caught AND logged,
and a failed session is NOT marked captured, so the next tick retries it.
"""

from __future__ import annotations

import asyncio
import os

import pandas as pd

from api.eod_capture import capture_enabled, should_capture
from api.eod_observation_store import (
    EodObservationStore, failed_manifest_row, manifest_rows, observation_rows,
)
from api.eod_observer import observe_lanes

EOD_TIMER = "eod_capture"

#: How often to ASK. Not how often to capture — `should_capture` fires at most once per session, and
#: the answer on nearly every tick is "wait". Five minutes is fine granularity against a 20-minute
#: post-close offset and is cheap: the tick is a calendar lookup and a set membership test.
EOD_TICK_SECS = 300

#: A live quote read shortly after the bell IS a live quote. Stamping it "close" would put it into a
#: close-to-close series indistinguishably — the defect review found when `mark_source` was derived
#: from `capture_kind` rather than stated. The KIND says what the row is; the SOURCE says where the
#: number came from.
MARK_SOURCE = "live"
CAPTURE_KIND = "close"


def schedule_capture(actor) -> bool:
    """Register the capture on the Nautilus clock. Returns whether it armed.

    ITS OWN FUNCTION SO A TEST CAN DRIVE IT. Asserting that `set_timer` appears somewhere in
    `on_start` did not bite when `_schedule_bar_drain`'s registration was disabled — the text survives
    inside a dead branch. The same lesson, applied before repeating it.

    NOTHING IS REGISTERED WHEN DISARMED, rather than a timer that returns early: the tick is where a
    refusal gets logged, so a disarmed-but-registered timer produces warnings about a job nobody
    turned on.

    Both refusals are LOGGED. "Not armed" and "armed and never fired" are identical in the table
    months later, and the boot log is the only place that difference is still recoverable.
    """
    env = getattr(actor, "_eod_env", None)
    if env is None:
        env = os.environ
    if not capture_enabled(env):
        actor.log.info(
            f"eod capture not armed: {'KUMO_EOD_CAPTURE'} is not set. No end-of-day position "
            f"observations will be written by this node."
        )
        return False
    if getattr(actor, "_venue_calendar", None) is None:
        actor.log.info(
            "eod capture not armed: this provider supplies no trading calendar, and the capture "
            "schedules off the VENUE's own close. A fixed 16:00 ET timer would be wrong on every "
            "half day, so it does not arm rather than guessing."
        )
        return False
    actor.clock.set_timer(
        name=EOD_TIMER,
        interval=pd.Timedelta(seconds=EOD_TICK_SECS),
        callback=lambda event: _on_tick(actor, event),
    )
    actor.log.info(f"eod capture armed on the venue calendar, asking every {EOD_TICK_SECS}s")
    return True


def _on_tick(actor, _event=None) -> None:
    """Timer callback. Nautilus timer callbacks do not run on the node's event loop, so the async work
    is handed to it — the same shape as every other async timer in this actor."""
    loop = getattr(actor, "_loop", None)
    if loop is None:
        return
    store = getattr(actor, "_eod_store", None) or EodObservationStore()
    asyncio.run_coroutine_threadsafe(capture_once(actor, store), loop)


def reason_key(reason) -> str:
    """A NORMALIZED failure identity, for deciding whether a manifest row is a new transition.

    NEVER THE FORMATTED MESSAGE. A reason that embeds a value — a quantity, a timestamp, a connection
    address with a port — produces a different string every tick, so every tick looks like a new
    condition, the forty rows come back, and the collapse READS AS WORKING while doing nothing. That
    is the shape of a dedupe that is worse than none, because it carries a receipt.

    So: the exception's TYPE for an exception, and a stable leading fragment for a plain string. Two
    ticks of one condition key the same; a genuinely different failure does not.
    """
    if isinstance(reason, BaseException):
        return f"exc:{type(reason).__name__}"
    return "detail:" + " ".join(str(reason).split()[:4]).lower()


def snapshot_ts_of(now) -> int:
    return int(now.timestamp() * 1e9)


def _known_lanes(cache) -> list[str]:
    """Every REGISTERED strategy, flat ones included.

    `cache.strategy_ids()` is Nautilus's own list. Falling back to the lanes holding positions would
    reintroduce the defect this replaced, so an unavailable list is EMPTY rather than substituted:
    no manifest rows beyond the observed lanes is a visible gap, while a silently narrowed list
    reads as a complete answer.
    """
    try:
        return sorted(str(s) for s in cache.strategy_ids())
    except Exception:                                                # noqa: BLE001
        return []


def session_fill_qty(position, session_open_ns: int) -> float:
    """Net quantity this position MOVED during the session — measured, never defaulted.

    THE SAME DEFINITION AS `session_deltas`, BY A DIFFERENT ROUTE, and that is deliberate: the
    backfill computes `book(D) - book(D-1)` from the broker's fill ledger, and Nautilus stamps
    `signed_qty` on every position event as the quantity AFTER that event — so the last event before
    the bell IS `qty_{t-1}`. One quantity under one name, which is what makes
    `qty_t == qty_{t-1} + session_fill_qty_t + transfers_t` mean anything, and what the acceptance
    gate compares at T=now.

    NO PRIOR EVENT MEANS THE LANE HELD NONE OF IT, which makes 0.0 the correct prior here — a
    measured prior, not a default. The distinction matters because the whole column exists to stop a
    default standing in for a measurement.
    """
    prior = 0.0
    for event in getattr(position, "events", ()) or ():
        ts = getattr(event, "ts_event", None)
        qty = getattr(event, "signed_qty", None)
        if ts is None or qty is None or ts >= session_open_ns:
            continue
        prior = float(qty)
    return float(position.signed_qty) - prior


async def _record_failure(actor, store, session_date, snapshot_ts, reason, detail) -> None:
    """Log EVERY attempt; write a manifest row only when the failure is a NEW transition.

    THE TABLE RECORDS TRANSITIONS, THE LOG RECORDS ATTEMPTS — two questions, two artifacts. The
    capture asks every five minutes, so an unresolved condition would otherwise write around forty
    identical `failed` rows in a day, and forty rows read as forty failures rather than one.

    THE PER-TICK LOG LINE IS NOT REDUNDANT AND MUST NOT BE DELETED. Once the rows collapse, it is the
    only thing that bounds an UNRESOLVED failure's duration: a single row at 16:20 cannot distinguish
    "the process died at 16:21" from "it retried until midnight". The resolved case brackets itself —
    the first `failed` row and the later `observed` rows fence the interval — so this is the one case
    the table genuinely loses, and the log is where it is kept.

    STATE IS PER SESSION DATE AND LIVES IN PROCESS MEMORY ONLY. Tomorrow's identical failure is a
    different day's fact and writes its own row; a restart mid-failure writing one more row is
    CORRECT, because that is a new attempt by a new process and the manifest is an attempt log.
    """
    actor.log.error(f"eod capture failed for {session_date}: {detail}")
    seen = getattr(actor, "_eod_last_reason", None)
    if seen is None:
        seen = actor._eod_last_reason = {}
    key = reason_key(reason)
    if seen.get(session_date) == key:
        return
    seen[session_date] = key
    try:
        await store.write([], [failed_manifest_row(
            session_date, CAPTURE_KIND, snapshot_ts, "*", detail)])
    except Exception as exc:                                         # noqa: BLE001
        actor.log.error(f"eod capture could not record its failure for {session_date}: {exc}")


async def capture_once(actor, store, *, now=None):
    """One tick's worth of work. Returns the `WriteResult`, or None when nothing was written.

    None covers three different situations and the log distinguishes them: not yet time, already
    captured, and the calendar refused. Only the last is a problem, and only it is logged as one.
    """
    from api.engine_node import _single_currency_amount_and_ccy      # local: avoids an import cycle

    captured = getattr(actor, "_eod_captured", None)
    if captured is None:
        captured = actor._eod_captured = set()
    if now is None:
        now = pd.Timestamp.utcnow().to_pydatetime()

    decision = should_capture(now, actor._venue_calendar, already_captured=captured)
    if decision.action == "refuse":
        # NOTHING IS WRITTEN, and the docstring at the top says why: a refusal has no session date,
        # and every row in both tables is keyed by one. The gap is honest and backfillable; a row
        # under an invented key is neither.
        actor.log.warning(f"eod capture refused, nothing written: {decision.reason}")
        return None
    if not decision.should_fire:
        return None

    session_date = decision.session_date
    snapshot_ts = snapshot_ts_of(now)
    day = actor._venue_calendar.day(now.date()) if hasattr(actor._venue_calendar, "day") else None
    open_at = getattr(day, "open_at", None)
    if open_at is None:
        # NOT `else 0`. Every position event postdates the epoch, so a zero open makes every prior
        # read 0.0 and `session_fill_qty` become the ENTIRE position — every lane looking like it
        # opened its whole book that session, and the corporate-action invariant violated on every
        # row. A fallback that produces a confident wrong number, in the column added to make a
        # number trustworthy. Unreachable while `VenueCalendar.day()` supplies `open_at`, which is
        # exactly the kind of unreachable this repo keeps being wrong about.
        detail = (
            "the calendar day carries no open time, so the session's start cannot be dated and "
            "session_fill_qty would report the whole book as traded today"
        )
        await _record_failure(actor, store, session_date, snapshot_ts, "no open time", detail)
        return None
    session_open_ns = int(open_at.timestamp() * 1e9)

    accounts = actor.cache.accounts()
    # THE CURRENCY IS THE ACCOUNT'S OR THE CAPTURE FAILS. `observation_rows` has no default for it,
    # deliberately — it used to default to "USD" beside a column comment saying staging is SGD.
    currency = None
    if accounts:
        from nautilus_trader.model.objects import Currency

        currency = _single_currency_amount_and_ccy(
            accounts[0].balances_free(), prefer=Currency.from_str("USD"),
        )[1]
    if currency is None:
        detail = (
            "no account currency could be established without guessing — the account reports either "
            "no balances or two legs with no USD, and a plausible currency on a P&L row is worse "
            "than none"
        )
        await _record_failure(actor, store, session_date, snapshot_ts, "currency unchoosable", detail)
        return None

    try:
        observation = observe_lanes(actor.cache)
        lanes = observation.lanes
        fills = {
            (p.strategy_id, p.instrument_id): session_fill_qty(p, session_open_ns)
            for p in actor.cache.positions_open()
            if getattr(p, "strategy_id", None) is not None
        }
        fills = {(str(k[0]), str(k[1])): v for k, v in fills.items()}
        result = await store.write(
            observation_rows(
                session_date, CAPTURE_KIND, snapshot_ts, lanes,
                currency=str(currency), mark_source=MARK_SOURCE, session_fills=fills,
            ),
            manifest_rows(
                session_date, CAPTURE_KIND, snapshot_ts, lanes,
                # EVERY REGISTERED STRATEGY, not just the ones holding something. Deriving this
                # from `positions_open()` made `observed, 0 positions` UNREACHABLE — a flat lane was
                # indistinguishable from a lane the capture never asked about, which is the exact
                # distinction this table exists to make. `cache.strategy_ids()` is Nautilus's own
                # answer and includes the flat ones.
                known_lanes=_known_lanes(actor.cache),
                skipped=observation.skipped,
            ),
        )
    except Exception as exc:                                         # noqa: BLE001 — see module doc
        # CAUGHT AND LOGGED, and the session is NOT marked captured, so the next tick retries. An
        # `except` that logged nothing would make every failed capture look like a clean tick.
        await _record_failure(actor, store, session_date, snapshot_ts, exc,
                              f"{type(exc).__name__}: {exc}")
        return None

    captured.add(session_date)
    actor.log.info(
        f"eod capture {session_date}: {result.written} row(s) written, {result.skipped} already "
        f"present, {result.manifests_written} manifest row(s) across {result.lanes_attempted} lane(s)"
    )
    return result
