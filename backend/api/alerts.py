"""What is worth telling the operator, and when (#199). Delivery lives in `api/notify`.

Runs in the API process, not the engine, for one decisive reason: **the engine dying is the alert**.
A watcher inside it cannot report its own death. The API can observe the engine (through the health
frames it publishes on the bus), and on 6–9 Aug 2026 the whole stack was down for 2d 18h with nothing
to announce it and a trading session missed.

The obvious remaining hole is that nothing here survives the API itself dying. That needs an external
check and is deliberately out of scope — this catches the failure that actually happened, and an
external prober is a separate moving part with its own operational cost.

Shape follows the rest of the codebase: pure decision functions that take a snapshot and return what
is true, plus a thin loop that does I/O. `Notifier` handles dedupe, so `conditions()` may return the
same thing every tick — it is a description of NOW, not an event.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from api.notify import Alert, Notifier
from api.notify.telegram import SGT

_log = logging.getLogger(__name__)

POLL_SECONDS = 30.0
"""Slow on purpose. Health is not a trading signal — this is a human-latency surface, and a tighter
loop only adds load and sharpens the edges around a flapping dependency."""

_CLOCK_TIMEOUT = 8.0
"""Hard deadline on the venue clock call, so a slow endpoint cannot stall the alert loop behind it."""

_DOWN_POLLS_BEFORE_CLEAR = 2

_DIGEST_RETRY_WINDOW_S = 3600
"""How long a digest whose delivery failed keeps being retried (#651 item 3). Comfortably above the
notifier's 15-minute failed-send TTL, so at least three retries reach the transport before the poll
loop gives up; an hour-late digest is still the day's summary, and the abandon is logged loudly."""

#: Consecutive polls a DRIFT must persist before it is announced. Two, so a fold race cannot page.
#:
#: The broker reports a fill before the cockpit's projection folds it, so a drift row is TRANSIENTLY
#: TRUE on the way through — on every entry, for every lane. On 2026-08-24 TECHIVOL-005 filled HPE 36
#: and the operator got "Broker sync drift · HPE" seconds later; by the time it was read, `/health`
#: said `reconcile_drift: []` and the position was there. Three lanes entered that night.
#:
#: The alert's wording is deliberately alarming and stays that way — it exists for #363, where a
#: corrupted cached fill left the engine inert with an empty book while reporting RUNNING. Which is
#: exactly why it must not also fire routinely: an alert that cries wolf on every fill is one nobody
#: reads by the third day, and this is the message that must never be ignored.
#:
#: ONLY DRIFT SETTLES. A subsystem outage is announced on the first poll — delaying it would blunt
#: the thing this service is for, and on 2026-08-24 the whole stack was down 43 minutes with nobody
#: told. Drift is the one condition whose transient truth is a normal part of trading.
_DRIFT_POLLS_BEFORE_ALERT = 2
"""Consecutive clean polls before a resolved condition is forgotten.

One clean poll used to clear immediately, so a dependency flapping across the poll boundary
re-alerted on every down transition. Requiring two means a blip has to persist for a full extra
interval before it counts as recovered — and a condition that is genuinely fixed is forgotten one
poll later, which costs nothing."""


def health_conditions(health: dict[str, Any]) -> dict[str, Alert]:
    """Currently-true problems, keyed by condition. Empty when everything is fine.

    Returned rather than sent so the caller can diff against what it announced last tick and clear
    what has resolved — which is what turns a polled state into an event.
    """
    out: dict[str, Alert] = {}

    down = [s for s in health.get("subsystems", []) if not s.get("ok")]
    for s in down:
        name = s.get("name", "?")
        detail = s.get("detail") or "no detail reported"
        # Critical: these ignore quiet hours. An engine that is not running is not trading, and every
        # hour it stays down is an hour of sessions missed — exactly the 6 Aug failure.
        out[f"subsystem_down:{name}"] = Alert(
            title=f"{name} is down",
            body=f"The cockpit reports `{name}` unavailable.\n{detail}\n\n"
                 f"No sessions will run until this is back.",
            critical=True,
        )

    for d in health.get("reconcile_drift", []) or []:
        sym = d.get("symbol")
        if not sym:
            # Without a symbol every malformed row keys to the same string, so the last one silently
            # overwrites the rest and only one of them is ever announced. Skipping is honest: a drift
            # row with no symbol says nothing actionable anyway.
            _log.warning("drift row with no symbol, skipped: %r", d)
            continue
        broker, cockpit = d.get("broker_qty", 0), d.get("cockpit_qty", 0)
        # Per SYMBOL, so a second name drifting is its own alert and each clears on its own. A single
        # `drift` key would announce the first and stay silent through everything after it.
        if cockpit == 0:
            body = (f"The broker holds *{sym} {broker}* that the cockpit is not showing.\n"
                    f"An empty book must never be mistaken for a flat account.")
        elif broker == 0:
            body = (f"The cockpit shows *{sym} {cockpit}* that the broker does not have.\n"
                    f"Apparent size that does not exist — do not trade against it.")
        else:
            body = f"*{sym}* — broker {broker}, cockpit {cockpit}."
        out[f"drift:{sym}"] = Alert(title=f"Broker sync drift · {sym}", body=body)

    # Orders reconciliation had to SKIP because no report can represent them (#643) — e.g. a
    # dollar-based order whose `qty` is null. The skip is what saved the batch from the #613 inert
    # shape, but the skipped order is INVISIBLE to the engine: shares it reserves and protection it
    # provides are not being counted. Per VENUE ORDER ID, like drift is per symbol, so a second
    # offender is its own alert and each clears on its own when the provider states clean ([]).
    for row in health.get("unreconciled_orders", []) or []:
        void = row.get("venue_order_id")
        if not void:
            # Same reasoning as the drift rows above: without an id every malformed row keys to one
            # string and only the last is announced; a row that names nothing says nothing actionable.
            _log.warning("unreconciled-order row with no venue_order_id, skipped: %r", row)
            continue
        sym = row.get("symbol") or "?"
        reason = row.get("reason") or "no reason recorded"
        out[f"unreconciled_order:{void}"] = Alert(
            title=f"Reconciliation skipped an order · {sym}",
            body=(f"Order `{void}` ({sym}) could not be represented and was SKIPPED:\n{reason}\n\n"
                  f"The batch continued, so the engine is trading — but this order is invisible to "
                  f"it. Any shares it reserves or protection it provides are NOT being counted."),
        )

    return out


def _as_dict(obj: Any) -> dict:
    """Pydantic DTO, plain dict, or None -> dict. `None` account is normal on a synthetic node."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    return dump() if callable(dump) else dict(getattr(obj, "__dict__", {}) or {})


def _money(x: Any) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def account_digest(kind: str, account: dict, positions: list[dict], drift: list[dict],
                   session: dict | None = None) -> Alert:
    """The open/close account summary.

    Reports the ACCOUNT, not the strategy, and deliberately does not depend on any strategy being
    enabled: equity and drift are account-level facts, and the day a strategy is halted is a day the
    close digest matters most.
    """
    held = [p for p in positions if p.get("side") != "FLAT" and p.get("quantity")]
    lines = [
        f"Equity: {_money(account.get('equity'))}",
        f"Cash: {_money(account.get('cash'))}   Buying power: {_money(account.get('buying_power'))}",
        f"Positions: {len(held)}",
    ]
    for p in sorted(held, key=lambda x: str(x.get("instrument_id"))):
        sym = str(p.get("instrument_id", "?")).split(".")[0]
        lines.append(f"  {sym} {p.get('side')} {p.get('quantity')}  ({p.get('strategy_id', '?')})")

    if session:
        # "entered 0 · exited 6" is the line that would have caught 6 Aug the same evening.
        #
        # Populated from `exec_action_log` — the strategy journals every decision and both processes
        # share the database, so the API can read the fact without a message path from the engine.
        lines.append("")
        lines.append(f"Session: entered {session.get('entered', 0)} · "
                     f"exited {session.get('exited', 0)}")

    if drift:
        lines.append("")
        lines.append(f"⚠️ Drift outstanding on {len(drift)} symbol(s): "
                     f"{', '.join(str(d.get('symbol')) for d in drift[:5])}")

    return Alert(title=f"Account · {kind}", body="\n".join(lines))


from api.notify.telegram import Alert

#: Consecutive failed polls before a check reports ITSELF as broken. Two so a single transient — a
#: Postgres blip, a slow venue clock — stays quiet, and three would let a permanently dead check hide
#: for another minute. On 2026-08-23 both new detectors raised on EVERY poll for an hour and the only
#: trace was a log warning nobody was reading: a swallowed exception every time is indistinguishable
#: from a clean poll, which is the failure mode the detectors themselves exist to end.
BROKEN_CHECK_POLLS = 2
from api.ownership import short_violations
from api.slot_outcome import RTH_MINUTES


def may_submit_from_rows(rows, state_enum) -> dict:
    """(strategy_id, state) rows -> {strategy_id: may_submit_entries}. Pure, so it can be driven.

    Split out because the version inline in `_may_submit_map` needed a database to reach, so nothing
    reached it — and a mutation that returned a flat True for every lane left the whole suite green.
    A flat True alarms on every SHADOW session, which is the dry-run path an operator uses to try a
    strategy safely; an alarm that fires there is one they switch off before it ever catches a real
    failure.

    An UNRECOGNISED state falls back to True on purpose: a missed alert is worse than a spurious one,
    the same choice `scan_slots` already documents for an absent entry.
    """
    out: dict[str, bool] = {}
    for sid, state in rows or ():
        try:
            out[str(sid)] = bool(state_enum(str(state)).may_submit_entries)
        except Exception:                                               # noqa: BLE001
            out[str(sid)] = True
    return out


class AlertsService:
    """Polls health, announces what changed, and fires the open/close digests.

    Digests are driven by a TRANSITION in the venue's own clock (`/v2/clock`), never by a local time
    check: a weekday-and-time rule fires on Thanksgiving reporting a session that never happened, and
    is three hours wrong on the early closes. No open transition on a holiday means no digest, which
    is the correct behaviour for free.
    """

    def __init__(self, node, notifier: Notifier | None = None, http=None,
                 poll_seconds: float = POLL_SECONDS) -> None:
        self._node = node
        self._n = notifier or Notifier()
        self._http = http
        self._poll = poll_seconds
        self._announced: set[str] = set()
        # (instrument, side, qty-sign) triples already alarmed — a standing phantom must not
        # re-page every poll, but a NEW one must (#639).
        self._external_alarmed: set[tuple] = set()
        #: (strategy_id, instrument_id) already paged for a long-only short (#437).
        #: Symbols currently paged as FROZEN. Forgotten when the freeze clears, so a recurrence is
        #: news again — these self-heal when the stale claimant reconciles, and a second occurrence
        #: means the retirement did not hold.
        self._stranded_alarmed: set = set()
        #: symbols the stranded check is NOT computing because broker and cache disagree on them —
        #: the named third state, so silence there is never read as "nothing frozen" (#843).
        self._stranded_excluded: set = set()
        self._short_alarmed: set[tuple] = set()
        self._terminal_alarmed: set[str] = set()  # client_order_ids already paged (#807 item 4)
        self._split_alarmed: set = set()
        self._split_pending: dict = {}
        self._clean_polls: dict[str, int] = {}  # consecutive clean polls per resolved condition
        #: consecutive polls a drift key has been true. See _DRIFT_POLLS_BEFORE_ALERT.
        self._dirty_polls: dict[str, int] = {}
        self._was_open: bool | None = None      # None = not yet observed; avoids a boot-time digest
        #: (kind, key, deadline) of a digest owed but not yet delivered — see _digests (#651 item 3).
        self._pending_digest: tuple[str, str, datetime] | None = None
        #: check name -> (consecutive failures, last reason). See BROKEN_CHECK_POLLS.
        self._check_failures: dict[str, tuple[int, str]] = {}
        #: pool symbols already alarmed as unlisted — the standing set must page once, a NEW one
        #: must page (#723). Same shape as `_external_alarmed`, and pruned for the same reason.
        self._pool_alarmed: set[str] = set()
        #: whether the "no catalog configured" line has been said. It is permanent and by design on
        #: an IBKR instance, so it is said once rather than 2,880 times a day (#723 review).
        self._warned_no_catalog = False

    def _enabled(self) -> bool:
        """Whether alerting is on at all. Errs toward doing nothing: an unreadable settings file
        should cost alerts, never a busy loop against every dependency."""
        try:
            return bool(self._n._config().get("enabled"))
        except Exception:                                               # noqa: BLE001
            return False

    async def _health(self) -> dict:
        from api.app import _probe_subsystems  # local: avoids a circular import at module load

        observed = self._node.health()
        subsystems = await _probe_subsystems(observed)
        return {
            "subsystems": [{"name": s.name, "ok": s.ok, "detail": s.detail} for s in subsystems],
            "reconcile_drift": observed.get("reconcile_drift", []) or [],
            # THE KEY `health_conditions` HAS ALWAYS READ AND THIS DICT NEVER SUPPLIED (#643).
            # `health.get("unreconciled_orders", []) or []` iterated nothing on every poll since the
            # alert shipped, so an order reconciliation had to skip was announced by no one — and an
            # empty result is indistinguishable from a clean one, which is the failure the detector
            # existed to end. A test now derives the required keys from `health_conditions` itself,
            # because naming them is what left this one behind.
            "unreconciled_orders": observed.get("unreconciled_orders", []) or [],
        }

    async def _announce_health(self) -> None:
        health = await self._health()
        current = health_conditions(health)
        for key, alert in current.items():
            if key.startswith("drift:"):
                # SETTLE FIRST. Counted per key, so one symbol settling says nothing about another.
                self._dirty_polls[key] = self._dirty_polls.get(key, 0) + 1
                if self._dirty_polls[key] < _DRIFT_POLLS_BEFORE_ALERT:
                    continue
            await self._n.send(key, alert)
            self._clean_polls.pop(key, None)     # true again — restart its recovery count
        # A drift that is no longer true forgets its run. Without this, two unrelated one-poll blips
        # hours apart accumulate into an alert nobody can explain, which is the same "counter that
        # only ever goes up" defect the clean-poll counter above already avoids.
        for key in [k for k in self._dirty_polls if k not in current]:
            self._dirty_polls.pop(key, None)
        # A condition absent now has PROBABLY resolved, and clearing makes its next occurrence news
        # again. But clearing on a single clean poll turned a flapping dependency into an alert per
        # down-transition: down, up, down, up each read as a fresh incident. Require it to stay clean
        # for consecutive polls, so a blip has to persist before it counts as recovery.
        for gone in self._announced - set(current):
            self._clean_polls[gone] = self._clean_polls.get(gone, 0) + 1
            if self._clean_polls[gone] >= _DOWN_POLLS_BEFORE_CLEAR:
                self._n.clear(gone)
                self._clean_polls.pop(gone, None)
                self._announced.discard(gone)
        self._announced |= set(current)

    async def _last_session(self) -> dict | None:
        """The most recent strategy session, counted from ORDERS ACTUALLY SUBMITTED.

        The session line — "entered 0 · exited 6" — is what would have caught 6 Aug the same evening,
        and until now the digest could not carry it: sessions run in the ENGINE process and this runs
        in the API, which cannot see the result. Both talk to the same database, and the strategy
        journals every order, so the fact is available without inventing a message path.

        Counted from order RESULTS, deliberately, not from the decision row. The decision records
        INTENT: on 6 Aug it read "enter 6 · exit 6" while every one of those six entries was then
        blocked by the position cap and never submitted. A digest built on the decision would have
        reported "entered 6" on the evening the book went to cash — the exact opposite of the signal
        it exists to give.
        """
        try:
            from sqlalchemy import text

            from api.db.engine import session_factory

            async with session_factory() as s:
                sess = (await s.execute(text(
                    "SELECT session FROM exec_action_log WHERE kind = 'decision' "
                    "ORDER BY ts DESC LIMIT 1"))).scalar()
                if not sess:
                    return None
                rows = (await s.execute(text(
                    "SELECT split_part(summary, ' ', 1) AS side, count(DISTINCT symbol) "
                    "FROM exec_action_log WHERE kind = 'order' AND session = :s "
                    "AND detail->>'phase' = 'result' "
                    "AND COALESCE((detail->>'ok')::boolean, false) GROUP BY 1"),
                    {"s": sess})).all()
            by_side = {r[0]: r[1] for r in rows}
            return {"session": sess,
                    "entered": int(by_side.get("BUY", 0)),
                    "exited": int(by_side.get("SELL", 0))}
        except Exception as exc:                                        # noqa: BLE001
            _log.debug("no session detail for the digest: %r", exc)
            return None

    async def _digests(self, now: datetime) -> None:
        if self._http is None:
            return
        # BOUNDED. `run()` awaits this serially after the health alerts, and the Alpaca session has no
        # per-call deadline — so a hung clock request would stall every future health and drift alert
        # behind it. A notifier that goes quiet because an unrelated endpoint is slow is the failure
        # mode this whole feature exists to prevent (codex review). A missed digest costs a summary;
        # a stalled loop costs the engine-down alert.
        clock = await asyncio.wait_for(self._http.get_clock(), timeout=_CLOCK_TIMEOUT)
        is_open = bool(clock.get("is_open"))
        was, self._was_open = self._was_open, is_open
        if was is not None and was != is_open:
            kind = "open" if is_open else "close"
            # Dated key: idempotent across a restart within the same day, without blocking tomorrow's.
            # PENDING UNTIL DELIVERED (#651 item 3). The transition used to be consumed here and the
            # send attempted once: `Notifier.send` returning False lost that day's digest permanently,
            # because the notifier's 15-minute retry TTL is only reachable by CALLING send again — and
            # nothing ever did. The transition now parks a pending digest that each poll retries until
            # it delivers or the window closes.
            self._pending_digest = (kind,
                                    f"digest:{kind}:{now.astimezone(SGT).date().isoformat()}",
                                    now + timedelta(seconds=_DIGEST_RETRY_WINDOW_S))
        if self._pending_digest is None:
            return
        kind, key, deadline = self._pending_digest
        if not self._n.allowed(key):
            # Gated off — off must mean off, not a retry loop building account frames every poll.
            self._pending_digest = None
            return
        if now > deadline:
            # BOUNDED, and loud on the way out: giving up is its own condition, never silence. The
            # window comfortably exceeds the notifier's 15-minute failed-send TTL, so a transport
            # outage shorter than the window still delivers; only a dead transport (or a key already
            # delivered before a restart, which the dedupe rightly refuses) ends up here.
            self._pending_digest = None
            _log.warning("digest %s abandoned after %ss of failed sends — the day's %s summary "
                         "was not delivered", key, _DIGEST_RETRY_WINDOW_S, kind)
            return
        # node.account() and node.positions() return PYDANTIC models (AccountDTO / PositionDTO), not
        # dicts. `.get()` on a BaseModel raises AttributeError, which the run loop would swallow —
        # the digest would then never send, silently, forever. `_as_dict` normalises both, and the
        # service tests use the real DTOs so a fake returning plain dicts cannot hide this again.
        account = _as_dict(self._node.account()) if hasattr(self._node, "account") else {}
        positions = [_as_dict(p) for p in self._node.positions()]
        drift = self._node.health().get("reconcile_drift", []) or []
        session = await self._last_session()
        if await self._n.send(key, account_digest(kind, account, positions, drift, session=session),
                              now=now):
            self._pending_digest = None

    async def _may_submit_map(self) -> dict:
        """strategy_id -> `may_submit_entries`, from the lifecycle's own state machine.

        NOT reimplemented here: SHADOW decides and forms no orders BY DESIGN, and `judge_slot`
        silences `DECIDED_NEVER_ATTEMPTED` for exactly that case. Getting this wrong in either
        direction is worse than not scanning — a wrong True alarms on every dry-run session, a wrong
        False hides the live failure this exists to catch. An unreadable state falls back to True
        because a missed alert is worse than a spurious one, which is the same choice `scan_slots`
        already documents.
        """
        from sqlalchemy import text as sa_text

        from api.db.engine import session_factory

        try:
            from kumo_strategies.runtime.executor.lifecycle import State
        except Exception:                                               # noqa: BLE001
            return {}
        async with session_factory() as db:
            rows = (await db.execute(sa_text("SELECT strategy_id, state FROM exec_strategy_state"))).all()
        return may_submit_from_rows(rows, State)

    async def _announce_slots(self) -> None:
        """Every strategy slot that decided and did nothing, attempted and lost it, or errored.

        WIRED 2026-08-23, AFTER THE OPERATOR ASKED WHY QC345 HAVING NEVER PLACED AN ORDER WAS NEWS TO ME. The
        predicate was already right — QC345's 2026-08-21 session is `decisions 1, intents 0` with the
        lifecycle in TRADING, which is `DECIDED_NEVER_ATTEMPTED` exactly — and `alert_key` was already
        prefixed `strategy_degraded` so the Notifier routes it. Nothing called it. Seven mechanisms in
        this session were built, tested, documented with the incident they caught, and driven by
        nothing; this is the one that would have made the other six unnecessary, because it watches
        OUTCOMES rather than any particular cause.

        `_n.send` dedupes on the key, so a slot that stays broken alarms once and not every poll.
        """
        # FAIL-SOFT, and the seam is a bound attribute so a test can make it fail. `_announce_slots`
        # sits between `_announce_health` and `_digests`; an unhandled raise here skips the digests
        # for that poll, so a NEW check would take down two WORKING ones.
        try:
            for sid, session, slot, v in await self._scan_slots_impl():
                # AN `Alert`, NOT AN f-STRING. `Notifier.send` reads `.critical` and hands the object
                # to the transport, so a str raises AttributeError and the `except` below eats it —
                # which is exactly how this shipped inert on 2026-08-23 and was found in the api log
                # rather than by any test.
                #
                # `critical` comes from the verdict's OWN `may_be_benign`, which exists so this is not
                # a guess: DECIDED-NEVER-ATTEMPTED can legitimately mean "nothing to do this slot", and
                # waking someone for that is how an alarm gets muted.
                await self._n.send(
                    v.alert_key(sid, session, slot),
                    Alert(title=f"{sid} {slot}: {v.verdict.value}",
                          body=f"{sid} · session {session} · slot {slot}\n{v.detail}",
                          critical=not v.may_be_benign))
        except Exception as exc:                                        # noqa: BLE001
            self._check_failed("slot_scan", exc)
        else:
            self._check_ok("slot_scan")

    async def _scan_slots_impl(self):
        """The scan itself, split out so the failure path above has something to drive."""
        from api.db.engine import session_factory
        from api.slot_outcome import scan_slots

        return await scan_slots(session_factory, may_submit=await self._may_submit_map())

    async def _minutes_since_open(self) -> int | None:
        """Minutes into today's regular session, or None when there is no session to be late for.

        THE VENUE'S OWN CLOCK, not a local weekday test. `_us_market_open` in engine_node is
        weekday-based and holiday-unaware, and a holiday-unaware "should have run by now" alarms on
        every lane on Thanksgiving. Alpaca's `/v2/clock` knows the calendar; without it this returns
        None and the check stays silent, which is the safe direction — a missing alarm beats a daily
        false one.
        """
        # NO `if self._http is None` GUARD. A mutation proved it dead: attribute access on None
        # raises inside the try and returns None by the same path, so the explicit check and its
        # absence are byte-identical in behaviour. Identical-when-it-should-differ is a dead
        # mechanism (CLAUDE.md), and a guard that cannot be shown to do anything is worse than none —
        # it reads as protection. The `except` IS the guard, for a missing client and a hung venue
        # alike, and `test_no_venue_clock_means_SILENT_not_ASSUMED_OPEN` drives both.
        try:
            clock = await asyncio.wait_for(self._http.get_clock(), timeout=_CLOCK_TIMEOUT)
        except Exception:                                               # noqa: BLE001
            return None
        if not clock.get("is_open"):
            # Closed now. AFTER the close is exactly when a missed slot is most worth reporting, so
            # a full session is assumed once today's close has passed; before the open, nothing is
            # due yet. `timestamp` and `next_open` are the venue's, so both are holiday-correct.
            ts, nxt = str(clock.get("timestamp") or ""), str(clock.get("next_open") or "")
            if not (ts and nxt) or ts[:10] == nxt[:10]:
                return None
            # `ts date != next_open date` covers TWO states the instantaneous clock cannot tell
            # apart: post-close on a trading day (full session elapsed — report the misses) and a
            # day with NO session at all (Saturday, Sunday, a holiday — next_open is Monday all
            # weekend). Assuming the first paged NEVER RAN critically every weekend (#645), which
            # is how the one alert meaning "no alarm means nothing ran" gets muted before the
            # Monday it is real. Only the venue's CALENDAR knows which state this is.
            return RTH_MINUTES if await self._venue_had_session(ts[:10]) else None
        try:
            now = datetime.fromisoformat(str(clock["timestamp"]))
        except Exception:                                               # noqa: BLE001
            return None
        return int((now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() // 60)

    async def _venue_had_session(self, day: str) -> bool:
        """Whether `day` (YYYY-MM-DD) had a regular session — the venue's calendar, not weekday().

        The window is WIDENED around `day` on purpose: the calendar enumerates trading days only,
        so "closed" is inferred from ABSENCE — and an empty enumeration (a broken read, a malformed
        body) is indistinguishable from "every day is closed" and would silently disarm the
        never-ran detector on a real Monday. No 9-day span of the US equity calendar has zero
        sessions, so zero rows is a broken answer and RAISES — into `_announce_never_ran`'s
        `_check_failed`, which self-reports after BROKEN_CHECK_POLLS. Absence must not be readable
        as permission, in either direction.
        """
        d = date.fromisoformat(day)
        rows = await asyncio.wait_for(
            self._http.get_calendar(start=(d - timedelta(days=4)).isoformat(),
                                    end=(d + timedelta(days=4)).isoformat()),
            timeout=_CLOCK_TIMEOUT)
        days = {str(r.get("date")) for r in (rows or [])}
        if not days:
            raise RuntimeError(
                f"venue calendar returned no sessions in {d - timedelta(days=4)}..{d + timedelta(days=4)}"
                f" — cannot tell a closed {day} from a broken read")
        return day in days

    def _registered_strategies(self):
        """Which lanes this stack actually runs, or None when that cannot be established.

        None is the third state and it matters: it means "could not enumerate", and the caller then
        expects every KNOWN lane rather than none — refusing to watch anything because the registry
        was unreadable would be absence read as permission, on the detector whose whole job is to
        notice absence.
        """
        try:
            # `armed_lanes` on the ENGINE'S OWN health frame, which is keyed by lane and differs
            # per stack — staging carries two, paper four (read from both live frames). The first
            # version called `self._node.strategies()`, a method NodeManager does not have: it
            # raised, this except returned None, and the scoping did nothing on either stack.
            # `NodeManager.health()` exists and is already called elsewhere in this class.
            frame = self._node.health() or {}
            if frame.get("bridge_ok") is False:
                # CANNOT SEE THE ENGINE is a third state, not "expect everything". Falling back to
                # every known lane with the bridge down maximises pages at the moment they are
                # least informative — a dead engine has decided nothing and we know it (review
                # round 3). `False` is the caller's signal to stay quiet this poll.
                return False
            # FLAT FRAME. `RedisConsumer.health()` returns top-level keys with no `engine` wrapper
            # (verified in both running containers); the previous `frame.get("engine")` branch was
            # always False and worked by accident, which is a guess reading as knowledge.
            #
            # UNION with `lanes_absent`: `armed_lanes` is built from `_sibling_strategies`, which
            # holds lanes that BUILT. A lane that failed to build is in `lanes_absent` (#539) — and
            # dropping it here would lose exactly the outage this detector sits nearest to.
            names = {str(k) for k in (frame.get("armed_lanes") or {})}
            names |= {str(k) for k in (frame.get("lanes_absent") or {})}


            return names or None
        except Exception:  # noqa: BLE001
            return None

    async def _announce_never_ran(self) -> None:
        """Lanes that were DUE and produced nothing (#378). The fourth outcome, and the silent one.

        BCTROT missed both decision slots on 2026-08-19 and journalled nothing; it went unnoticed for
        three days. `judge_slot` cannot see it — it folds rows, and there are none — so the only thing
        that makes the absence checkable is that the expectation is DECLARED in settings.
        """
        from api.db.engine import session_factory
        from api.slot_outcome import SLOT_ROWS, fold_slot_rows, never_ran

        # The seam is checked FIRST so a test can drive the send path directly. In production the
        # default returns [] and the real computation below runs.
        seeded = await self._never_ran_impl()
        if seeded:
            await self._send_never_ran(seeded)
            return
        try:
            from api.settings import resolve

            cfg = resolve("strategies")
            # WHAT THE LANES WILL ACTUALLY RUN, not what settings literally say (#378). Every
            # `*_SLOTS` is `[]` live, which the lanes read as "keep the built-in schedule" and this
            # detector used to read as "nothing is due" — so it could not fire on any lane it
            # exists to watch.
            from api.slot_outcome import effective_expected_slots

            # Scoped to the lanes this stack registered: staging runs BCTROT-004 alone, and the
            # settings schema materialises all four keys everywhere (review, 2026-08-29).
            registered = self._registered_strategies()
            if registered is False:
                # The bridge is down: nothing can be said about what did or did not decide.
                return
            expected = effective_expected_slots(cfg, registered=registered)
            if not expected:
                return
            minutes = await self._minutes_since_open()
            if minutes is None:
                return
            async with session_factory() as db:
                rows = [dict(r._mapping) for r in (await db.execute(SLOT_ROWS, {"hours": 24}))]
            seen = {(sid, slot) for (sid, _session, slot) in fold_slot_rows(rows)}
            missing = never_ran(expected, seen, minutes_since_open=minutes)
        except Exception as exc:                                        # noqa: BLE001
            self._check_failed("never_ran", exc)
            return
        self._check_ok("never_ran")
        await self._send_never_ran(missing)

    def _check_failed(self, name: str, exc: BaseException) -> None:
        """Count a check's consecutive failures and remember the last reason."""
        n, _ = getattr(self, "_check_failures", {}).get(name, (0, ""))
        self._check_failures[name] = (n + 1, repr(exc))
        _log.warning("%s check failed (other alerts unaffected): %r", name, exc)

    def _check_ok(self, name: str) -> None:
        """A clean poll clears the count — the alarm must not outlive the fault."""
        getattr(self, "_check_failures", {}).pop(name, None)

    async def _announce_broken_checks(self) -> None:
        """Report the CHECKS themselves when they stop working.

        Deduped on the key like every other alert, so a check that stays broken pages once.
        """
        for name, (count, reason) in sorted(getattr(self, "_check_failures", {}).items()):
            if count < BROKEN_CHECK_POLLS:
                continue
            await self._n.send(
                f"subsystem_down:check:{name}",
                Alert(title=f"the {name} check is broken",
                      body=f"`{name}` has failed {count} polls in a row and is reporting nothing.\n"
                           f"{reason}\n\nSilence from this check means it is not running.",
                      critical=True))

    async def _never_ran_impl(self):
        """Split so the send path can be driven without a database or a venue clock — the reason the
        f-string defect survived two deploys is that nothing ever reached `send`."""
        return []

    async def _announce_split_divergence(self) -> None:
        """A claim the cache contradicts, per (strategy, symbol) — the BDX shape, paged (#692).

        Totals agreed (55 both ways) while the SPLIT did not (cache MOMENTUM 55/BCTROT 0, claims
        45/10), so over_claimed and reconcile_drift stayed green while BCTROT sized an exit off
        shares Nautilus says it does not hold. Only a resting cross-lane stop blocked the sell; two
        of four lanes rest no stop at all. Scheduled here, not on the order path — by sizing time
        the decision is already made.
        """
        try:
            from sqlalchemy import text as _text

            from api.claims_invariant import CLAIMS_SQL, claims_by_strategy
            from api.db.engine import session_factory
            from api.split_divergence import split_divergence

            # ONE query and ONE fold, shared with `/claims` and `/health` (#843). This method carried
            # its own copy of both, and the copy OVERWROTE a duplicate key where the shared fold sums.
            async with session_factory() as db:
                rows = (await db.execute(_text(CLAIMS_SQL))).all()
            cache_rows = [_as_dict(pos) for pos in self._node.positions()]
            diverged = split_divergence(cache_rows, claims_by_strategy(rows))
        except Exception as exc:                                        # noqa: BLE001
            self._check_failed("split_divergence", exc)
            return
        self._check_ok("split_divergence")
        # DEBOUNCED: claims are written asynchronously around fills, so a poll landing mid-update
        # sees a one-tick divergence that self-heals — the first live deploy paged 22 pairs in one
        # poll, most of them exactly that race. A pair pages only when the SAME reading stands on
        # two consecutive polls; healed or changed pairs reset.
        current = {(sym, sid, pair["claim"], pair["cache"])
                   for sym, lanes in diverged.items() for sid, pair in lanes.items()}
        pending = getattr(self, "_split_pending", {})
        self._split_pending = {k: True for k in current}
        for sym, lanes in sorted(diverged.items()):
            for sid, pair in sorted(lanes.items()):
                key = (sym, sid, pair["claim"], pair["cache"])
                if key not in pending:
                    continue
                if key in self._split_alarmed:
                    continue
                self._split_alarmed.add(key)
                await self._n.send(
                    f"split_divergence:{sym}:{sid}",
                    Alert(
                        critical=True,
                        title=f"SPLIT DIVERGENCE {sym}: {sid} claims {pair['claim']:g}, "
                              f"cache attributes {pair['cache']:g}",
                        body=("The claims ledger and the engine cache disagree about this lane's "
                              "share of the position while the totals may agree. An exit sized off "
                              "the claim sells another lane's shares under NETTING — the BDX shape "
                              "(#692). Do not adopt or exit this symbol until the split is "
                              "reconciled."),
                    ),
                )

    async def _announce_external_positions(self) -> None:
        """A NON-FLAT position under `StrategyId("EXTERNAL")` is a standing hazard, said out loud (#639).

        Four phantom SHORTs sat on paper for a DAY — minted by reconciliation after the 08-27 inert
        restart — and were found by a human noticing a side label contradicting the arithmetic
        beside it. `/health` said ok throughout. Detection existed (the external_activity plane);
        a plane a human must open is not a detector.

        CRITICAL because of the adopt affordance: the UNCLAIMED row invites "tap to move to a
        strategy", and adopting a phantom SHORT makes the adopting lane's exit path BUY — doubling
        a real long at market. The alarm must arrive before the tap.

        FLAT EXTERNAL rows do not alarm: 24 of paper's 25 are history, not hazard, and alarming on
        them is the always-on trap (#644/#645) that turns the channel into wallpaper.
        """
        try:
            rows = [_as_dict(p) for p in self._node.positions()]
            live = [r for r in rows
                    if r.get("strategy_id") == "EXTERNAL" and r.get("side") != "FLAT"]
        except Exception as exc:                                        # noqa: BLE001
            self._check_failed("external_positions", exc)
            return
        self._check_ok("external_positions")
        for r in live:
            key = (r.get("instrument_id"), r.get("side"),
                   1 if float(r.get("quantity") or 0) >= 0 else -1)
            if key in self._external_alarmed:
                continue
            self._external_alarmed.add(key)
            await self._n.send(
                f"external_position:{r.get('instrument_id')}:{r.get('side')}",
                Alert(title=f"EXTERNAL holds {r.get('instrument_id')} {r.get('side')} "
                            f"{r.get('quantity')}",
                      body=f"A non-flat position is booked under EXTERNAL — no strategy claims it.\n"
                           f"{r.get('instrument_id')} {r.get('side')} {r.get('quantity')}.\n\n"
                           f"Likely a reconciliation-generated phantom (#635): verify the BROKER's "
                           f"side before acting. Do NOT adopt (\"move to a strategy\") until "
                           f"verified — adopting a phantom SHORT makes the exit path BUY, which "
                           f"DOUBLES a real long at market.",
                      critical=True))

    async def _announce_stranded_claims(self) -> None:
        """Shares NO LANE CAN SELL, said out loud.

        Measured live on paper 2026-08-31, hours before an open:

            ARKK  claimed 46  held 23  BCTROT-004=23 MOMENTUM-002=23  sellable 0  stranded 23
            VCTR  claimed 32  held 16  BCTROT-004=16 MOMENTUM-002=16  sellable 0  stranded 16

        `own_ceiling` gives each lane `min(my_claim, acct - other_claims)`, so two lanes each claiming
        the whole position floor BOTH to zero. That arithmetic is CORRECT and exists for the WHD
        incident, where one lane's exit sold shares another had bought — refusing is the safe
        direction. What it produces here is a FREEZE: 39 real shares unreachable by either claimant on
        any path, including LIQUIDATING.

        AND NOTHING ALARMED ON IT. `exit_ceilings` has computed `stranded` all along, and only the
        `/claims` endpoint called it — so the freeze was visible to whoever thought to curl and to
        nobody else, while every sibling condition in this file pages. An over-claimed book is not a
        tidiness problem; it is a position that cannot be exited, and the lane will keep sizing
        decisions against a holding it cannot act on.

        NOT THE SAME CHECK AS `over_claimed`. That reports an inconsistent LEDGER — RBRK claiming 14
        against 0 held is inconsistent and strands nothing, because there are no shares to freeze.
        Paging on that would fire for every stale claim on a closed position, which is most of them.
        This fires only where `stranded > 0`.

        FORGOTTEN WHEN IT CLEARS, so a recurrence pages again: these resolve when the stale claimant
        next reconciles, and a second occurrence means the retirement did not hold — which is the
        thing an operator most needs told twice.
        """
        from sqlalchemy import text as _text

        from api.claims_endpoint import account_from_positions
        from api.claims_invariant import CLAIMS_SQL, claims_by_strategy, exit_ceilings
        from api.db.engine import session_factory

        # READ THE BOOK THE WAY `/claims` DOES (#843). This method used to read
        # `self._node.account_positions` and `self._node.claims_by_strategy` — attributes that exist
        # on the venue test double and on NOTHING `create_node()` returns — so it failed every poll
        # in production since it shipped, and its absent-on-both guard was the only reason that was
        # a warning line rather than a green tick. Now: the account is the engine's netted legs
        # (`account_from_positions`, verified equal to the broker's net on 2026-08-22), the claims
        # are the one query and the one fold every other consumer uses.
        #
        # NO try/except HERE. `run()` calls this through `_checked`, which counts a raise against
        # BROKEN_CHECK_POLLS — and which calls `_check_ok` on a normal return. The old internal
        # handler recorded the failure and RETURNED, so `_checked` cleared it on the same poll: the
        # self-report never reached two polls and the "reports itself" contract was a log line.
        # A STALE BRIDGE IS AN UNKNOWN BOOK, NOT AN EMPTY ONE (codex, implementation review).
        # `positions()` serves the last frame regardless of age; `health()` gates `reconcile_drift`
        # to [] on a stale bridge. Read together, a dead engine would yield a stale account, no drift,
        # no frozen symbols — and `_checked` would mark the poll ok. Raise instead: the wrapper counts
        # it, BROKEN_CHECK_POLLS says so, and nothing alarmed earlier is cleared.
        health = self._node.health() or {}
        if health.get("bridge_ok") is False:
            raise RuntimeError("engine bridge stale — the book is unknown, not clean")
        account = account_from_positions(self._node.positions())
        async with session_factory() as db:
            rows = (await db.execute(_text(CLAIMS_SQL))).all()
        claims = claims_by_strategy(rows)
        # THE BROKER IS THE ONLY HARD ANCHOR. Where the exec adapter reports the broker and the
        # cache disagree on a symbol, a freeze computed on the cache is computed on a number the
        # broker contradicts. Those symbols are EXCLUDED and named, never silently computed and never
        # reported as clean — the third state.
        #
        # Drift entries carry a BARE symbol (`exec_client.py` emits `symbol`/`broker_qty`/
        # `cockpit_qty`, `models.py` pins it) — the same key `account_from_positions` produces.
        # `symbol_of` is for instrument ids and would turn a bare `BRK.B` into `BRK` (codex).
        drift = health.get("reconcile_drift", []) or []
        drifted = {str(d.get("symbol") or "") for d in drift if isinstance(d, dict)}
        drifted.discard("")
        if drifted != self._stranded_excluded:
            _log.warning("stranded_claims: %d symbol(s) NOT computed — broker and cache disagree: %s",
                         len(drifted), ", ".join(sorted(drifted)) or "-")
            # SAID OUT LOUD, ONCE PER CHANGE, or a persistent exclusion is a fourth silent state
            # visible only in a log line (codex). Forgotten when it clears, like `_stranded_alarmed`.
            if not drifted:
                # FORGOTTEN WHEN IT CLEARS: the notifier holds a 12h dedupe on the key, and a return
                # after clearing must be news again — same rule as `_stranded_alarmed`.
                self._n.clear("stranded_claims_excluded")
            else:
                await self._n.send(
                    "stranded_claims_excluded",
                    Alert(critical=False,
                          title=f"stranded-claims check is NOT computing {len(drifted)} symbol(s)",
                          body=(f"Broker and cache disagree on {', '.join(sorted(drifted))}; a freeze "
                                f"there cannot be judged from the cache. Silence on these symbols is "
                                f"NOT 'nothing frozen'. Clears when reconcile_drift clears.")))
            self._stranded_excluded = drifted
        account = {s: q for s, q in account.items() if s not in drifted}
        claims = {sid: {s: q for s, q in c.items() if s not in drifted} for sid, c in claims.items()}
        claims = {sid: c for sid, c in claims.items() if c}
        # ALWAYS THROUGH THE PREDICATE, even with no claims (#903): `… if claims else {}` never
        # consulted it, so a pin without `own_ceiling` read as "nothing stranded". `exit_ceilings`
        # raises `PredicateAbsent` by name and `_checked` counts it — the loud path.
        ceilings = exit_ceilings(account, claims)

        frozen = {sym: v for sym, v in ceilings.items() if (v or {}).get("stranded", 0) > 0}
        for sym, v in sorted(frozen.items()):
            if sym in self._stranded_alarmed:
                continue
            who = " ".join(f"{k}={c:g}" for k, c in sorted((v.get("by_strategy") or {}).items()))
            held = v.get("held", 0)
            # MARK ON DELIVERY, NEVER ON ATTEMPT — same rule as every sibling here.
            if await self._n.send(
                f"stranded_claim:{sym}",
                Alert(
                    critical=True,
                    title=f"FROZEN POSITION — no lane can sell {v.get('stranded', 0):g} {sym}",
                    body=(f"{sym}: {held:g} held, {v.get('sellable', 0):g} sellable, "
                          f"{v.get('stranded', 0):g} STRANDED. Ceilings: {who}.\n\n"
                          f"Each lane's sell size is capped at `account - other lanes' claims`, so "
                          f"two lanes claiming the same shares floor both to zero. The shares are "
                          f"unreachable on every path INCLUDING liquidating, and the lanes will keep "
                          f"sizing against a holding they cannot act on.\n\n"
                          f"It clears when the stale claimant next reconciles. Do not write to the "
                          f"claims ledger by hand — one writer only."),
                ),
            ):
                self._stranded_alarmed.add(sym)

        # A symbol that is no longer frozen forgets its alarm, so a RETURN is news again — these
        # clear when the stale claimant reconciles, and a recurrence means the retirement did not
        # hold. THE NOTIFIER'S DURABLE KEY GOES WITH IT: forgetting only the local set would leave
        # the repeat suppressed one layer down for the full TTL, which is the same silence relocated.
        #
        # PRUNED ONLY AGAINST A READ THAT SAW THE LEDGER. `claims` empty means the read returned
        # nothing — a bounce, a seeding frame — not that every freeze resolved. Pruning on that would
        # forget every standing freeze and re-page them all as fresh criticals when it came back.
        if claims:
            for gone in self._stranded_alarmed - set(frozen):
                self._n.clear(f"stranded_claim:{gone}")
            self._stranded_alarmed &= set(frozen)

    async def _announce_short_violations(self) -> None:
        """A SHORT in a long-only lane, said out loud (#437).

        Nothing in this cockpit shorts, so a negative quantity in any lane is a defect on its face —
        most often one lane's exit selling shares another lane owns, which under NETTING closes the
        seller's position and MINTS a short with the excess. The owner's long is left untouched and
        its cycle stays HELD forever.

        THE POINT IS THAT NO OTHER CHECK SEES THIS. `_report_reconcile_drift` sums signed_qty per
        SYMBOL, so a mirrored pair cancels to the broker's own figure and reports green — correctly,
        the account IS flat. `_announce_external_positions` (#639) covers only the EXTERNAL leg; on
        paper, four of the eight mirrored shorts sat under MOMENTUM-002 and alarmed nowhere. Eight
        pairs, $18,218 of mis-attributed ownership, nine days, /health ok throughout.

        CRITICAL: a lane that believes it holds shares it does not will size and exit off that
        belief, and the short leg is unexitable by a long-only system — it can only be closed by
        buying, which doubles a real long at market.

        NO DEBOUNCE, deliberately. `_announce_split_divergence` waits two polls because it compares
        two independently-written sources and races their fold; this reads ONE source, and a negative
        quantity in a long-only lane is never transiently correct.

        KNOWN LIMIT: the key carries no quantity, so a short that GROWS under the same lane and
        symbol does not page again. Deliberate — quantity churns on every netting change, and
        re-paging while an operator unwinds the position is the wallpaper trap. If it is ever wanted,
        page on an increase in |qty| only, never on any change.

        AN EXTERNAL SHORT PAGES TWICE — here and from #639 — and that is intended. The two carry
        different remediation ("do not adopt" there, "find the stranded long" here) and are gated
        separately, so neither silences the other. #639 fires first and its text is the more
        actionable for that row.
        """
        try:
            rows = [_as_dict(p) for p in self._node.positions()]
            violations = short_violations(rows)
        except Exception as exc:                                        # noqa: BLE001
            self._check_failed("short_violations", exc)
            return
        self._check_ok("short_violations")
        for v in violations:
            key = (v.strategy_id, v.instrument_id)
            if key in self._short_alarmed:
                continue
            # MARK ON DELIVERY, NEVER ON ATTEMPT — the rule stated in full at `_announce_pool_unlisted`.
            # `send` returns False for a refused gate, a still-holding dedupe AND a failed transport,
            # and only the last is a "we tried". Marking first would kill the notifier's 15-minute
            # retry-after-failure and run()'s re-read of the settings each poll: one Telegram blip, or
            # one poll with the flag off, would mute THIS alert — the one added because eight shorts
            # stood nine days unannounced — for the life of the process.
            if await self._n.send(
                f"short_violation:{v.instrument_id}:{v.strategy_id}",
                Alert(
                    critical=True,
                    title=f"SHORT IN A LONG-ONLY LANE — {v.strategy_id} holds "
                          f"{v.signed_qty:+g} {v.instrument_id}",
                    body=(f"{v}. No strategy here shorts, so this is a booking error, not a "
                          f"position: almost certainly a closing SELL attributed to the wrong "
                          f"owner, leaving a mirrored LONG stranded in a sibling lane.\n\n"
                          f"The netted book still matches the broker, so reconcile_drift will "
                          f"NOT show this. Check the offsetting long before acting, and do not "
                          f"exit or adopt either leg until ownership is reconciled."),
                ),
            ):
                self._short_alarmed.add(key)
        # A lane that no longer holds the short forgets its alarm, so a RECURRENCE is news again.
        # This matters more here than for the siblings: the residue repair and the claims-on-fill
        # hardening are both still outstanding, so a second occurrence is the expected next event.
        # The notifier's durable key goes with it — forgetting only the local set would leave the
        # repeat suppressed one layer down in redis for the full TTL, the same silence relocated.
        # PRUNE ONLY AGAINST A READ THAT SAW THE BOOK, the clause `_announce_pool_unlisted` states
        # for the same reason. `consumer.py` starts `_positions` at [] and swaps it wholesale per
        # engine frame, so a bounce or a redis flush reads as an EMPTY book — no error, nothing
        # raised, nothing marked broken. Pruning on that would forget every standing short and clear
        # its durable key, and all of them would re-page as fresh criticals when the frame returned.
        # A genuine repair still leaves this account's ~17 legitimate longs; a wholly empty book is
        # stale or seeding, never good news.
        if not rows:
            return
        standing = {(v.strategy_id, v.instrument_id) for v in violations}
        for sid, iid in self._short_alarmed - standing:
            self._n.clear(f"short_violation:{iid}:{sid}")
        self._short_alarmed &= standing

    async def _announce_terminal_fills(self) -> None:
        """A fill arrived for an order the cache holds TERMINAL (#807 item 4).

        Nautilus refuses the fill on the order and applies it to the position anyway, so from this
        second on the lane's position and the order plane disagree, and every restart replays it.
        PATH took five on 2026-09-04 and nothing paged; the lane read SHORT 192 for five days. CRITICAL,
        once per order — the count grows on the health frame, the page does not repeat.
        """
        try:
            rows = list(self._node.health().get("fills_on_terminal_orders", []) or [])
        except Exception as exc:                                        # noqa: BLE001
            self._check_failed("terminal_fills", exc)
            return
        self._check_ok("terminal_fills")
        for r in rows:
            coid = str(r.get("client_order_id", ""))
            if not coid or coid in self._terminal_alarmed:
                continue
            if await self._n.send(
                f"terminal_fill:{coid}",
                Alert(
                    critical=True,
                    title=f"FILL ON A {r.get('order_status')} ORDER — {coid} "
                          f"({r.get('instrument_id')}, {r.get('strategy_id')})",
                    body=(f"{r.get('count')} fill(s) totalling {r.get('quantity')} arrived for an order the "
                          f"cache holds {r.get('order_status')}. Nautilus applied them to the position and "
                          f"NOT to the order, so {r.get('strategy_id')}'s book on {r.get('instrument_id')} "
                          f"is now wrong and will be re-minted on every restart (#807). The order is a "
                          f"corpse: it rested at the venue while the cache called it dead (#354/#791). "
                          f"Repair with scripts/repair_cache_807.py with the engine stopped; do not "
                          f"transfer or contra-close, the replay undoes both."),
                ),
            ):
                self._terminal_alarmed.add(coid)

    async def _announce_pool_unlisted(self) -> None:
        """Symbols in the composed pool the venue does not list, named (#723).

        #663 guards cockpit's `POST /pool/source/{name}`, which is not the door the symbols come
        through: the scheduled refresh runs in the ENGINE (`pgjobs.py:128`), calls the same writer
        directly, and never touches HTTP. `george_book` refreshed at 10:32 on 2026-08-29, an hour
        after that validation deployed, still carrying BLLLN, IQVIA, JEPO and OVVI.

        Reads `effective()` — the COMPOSED pool, sources plus pins minus excludes — so it covers the
        engine door, the manual seed door and the operator-PIN door (GTLAB) at once, and does not
        report a name an operator has already excluded. Reporting only: an automatic exclude is a
        trading-data change, and `must_liquidate` turns one into a sale at the next session.
        """
        from api.app import app as _api_app
        from api.app import pool as _pool
        from api.pool_sweep import pool_sweep_alerts
        from api.pool_validation import validate_pool_symbols

        entries = await _pool.effective()
        symbols = sorted(entries)
        # THE SAME VALIDATOR THE INGESTION DOOR USES. A second implementation of "does the venue
        # list this" would drift from the first, which is the defect class this repo keeps finding.
        catalog = getattr(_api_app.state, "search", None)
        verdict = validate_pool_symbols(symbols, catalog)
        if verdict.status != "ok":
            # DEGRADE LOUDLY, AND SAY WHICH ABSENCE THIS IS. Two very different facts reach here
            # and a single message would merge them: an IBKR instance has NO catalog and never
            # will, which is its permanent normal; an Alpaca instance whose catalog is configured
            # but never loaded is a broken oracle wearing the same silence.
            #
            # NO LOAD IS TRIGGERED FROM HERE, deliberately. `known_symbols()` is synchronous and
            # non-loading by contract (`instrument_search.py:167`) so pool validation cannot block a
            # request path on a REST call; making a 30-second alert poll the thing that warms the
            # catalog would put an Alpaca round trip on a loop whose whole job is to survive
            # dependencies being down. The catalog has its own hourly TTL refresh and loads on
            # demand for search.
            #
            # THE RESIDUAL HOLE IS REAL AND IS LEFT OPEN ON PURPOSE: an Alpaca instance whose
            # startup warm failed AND which serves no search traffic stays unchecked indefinitely,
            # visible only here. Making that page needs a persistence rule that will not fire on
            # every cold boot before warm completes, which is its own change with its own test.
            #
            # BY-DESIGN ABSENCE IS SAID ONCE; A BROKEN ORACLE IS SAID EVERY POLL. On staging-ibkr
            # `search is None` is permanent and correct, so a per-poll WARNING there is ~2,880 a day
            # describing a healthy state, at the same level as `_check_failed`'s real ones — which is
            # how a log earns a mute, and it would undercut the very loudness this branch is for.
            # Same shape and same remedy as `telegram.py`'s `_warned_unconfigured`.
            if catalog is None:
                if not self._warned_no_catalog:
                    self._warned_no_catalog = True
                    _log.warning(
                        "pool sweep: no venue catalog is configured on this instance — %d pool "
                        "symbol(s) will go UNCHECKED (this is not the same as clean). Expected on "
                        "an IBKR instance; said once, not per poll.", len(symbols))
                else:
                    _log.debug("pool sweep: no catalog configured — %d symbol(s) unchecked",
                               len(symbols))
            else:
                # Configured and not loaded is the state that wants attention: a broken oracle
                # wearing the same silence as the one above. Every poll, so it cannot be missed.
                _log.warning(
                    "pool sweep: the venue catalog is configured but has not loaded — %d pool "
                    "symbol(s) UNCHECKED (this is not the same as clean)", len(symbols))
            return
        current = pool_sweep_alerts(verdict, total=len(symbols))
        for key, alert in current.items():
            sym = key.split(":", 1)[1]
            if sym in self._pool_alarmed:
                continue
            # MARK ON DELIVERY, NEVER ON ATTEMPT. `send` returns False when the gate refuses it,
            # when the notifier's own dedupe still holds it, and when the transport fails — and only
            # the last of those is a "we tried". Marking before asking overrode two deliberate
            # behaviours from above: the notifier shortens a FAILED send's mark to 15 minutes so a
            # Telegram blip is retried rather than held for the full repeat window, and `run()`
            # re-reads settings every poll so turning a switch on takes effect without a restart.
            # Both were dead for this alert — one blip, or one poll with the flag off, muted a
            # symbol for the life of the process.
            if await self._n.send(key, alert):
                self._pool_alarmed.add(sym)
        # A symbol that is no longer unlisted forgets its alarm, so its RETURN is news again — a
        # corrected feed that regresses a week later must not be silent. The notifier's durable key
        # is cleared with it: forgetting only the local set would leave the repeat suppressed there
        # instead, which is the same silence one layer down.
        #
        # PRUNED ONLY AGAINST AN `ok` VERDICT, which is what the early return above protects. An
        # unavailable verdict carries `refused=()`, so pruning on one would forget the whole standing
        # list and re-page every name the moment the catalog came back.
        for gone in self._pool_alarmed - set(verdict.refused):
            self._n.clear(f"pool_unlisted:{gone}")
        self._pool_alarmed &= set(verdict.refused)

    async def _send_never_ran(self, missing) -> None:
        for sid, slot in missing:
            # ALWAYS CRITICAL. Every other verdict describes something a lane DID; this one is the
            # absence, and the absence is indistinguishable from a healthy quiet day unless it says so.
            await self._n.send(
                f"strategy_degraded:never_ran:{sid}:{slot}",
                Alert(title=f"{sid} {slot}: NEVER RAN",
                      body=f"{sid} was due at {slot} and has written nothing today.\n\n"
                           f"No alarm from this lane means NOTHING RAN — not that nothing is wrong.",
                      critical=True))

    async def _checked(self, name: str, announce) -> None:
        """One check's failure is counted and contained — never allowed to skip its siblings (#651 item 2).

        `_check_failed` was wired only for slot_scan / never_ran / external_positions, so a
        persistently raising health probe propagated to `run()`'s catch and skipped every OTHER
        announce that poll, uncounted by BROKEN_CHECK_POLLS — the counter that exists precisely so a
        broken check reports itself. Every check in the poll goes through here or through its own
        internal `_check_failed` guard; nothing may reach `run()`'s catch as a matter of course.
        """
        try:
            await announce()
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                        # noqa: BLE001
            self._check_failed(name, exc)
        else:
            self._check_ok(name)

    async def run(self) -> None:
        """Never raises. A failure here must cost an alert, never the API process."""
        while True:
            try:
                # "Default off" has to mean off, not "does the work then declines to send". Probing
                # Redis and Postgres and calling the venue clock every 30s for a feature nobody
                # enabled is load and surprise for no benefit (codex review). Checked per iteration
                # rather than once, so enabling it in the UI takes effect without a restart.
                if self._enabled():
                    await self._checked("health", self._announce_health)
                    # INSIDE the gate, with health and the digests, for the reason stated above it:
                    # "default off" has to mean off, and this one queries Postgres.
                    await self._announce_slots()
                    await self._announce_never_ran()
                    await self._announce_external_positions()
                    await self._announce_short_violations()
                    await self._announce_terminal_fills()
                    await self._announce_split_divergence()
                    # A position no lane can sell. Wrapped in `_checked` because the claims read is
                    # the kind that can wedge, and silence from this sweep is otherwise
                    # indistinguishable from a book with nothing frozen (#723's rule).
                    await self._checked("stranded_claims", self._announce_stranded_claims)
                    # Reads Postgres, so it belongs inside the gate with the rest. Wrapped in
                    # `_checked` because a pool read that wedges must report ITSELF — silence from
                    # this sweep is otherwise indistinguishable from a clean pool (#723).
                    await self._checked("pool_unlisted", self._announce_pool_unlisted)

                    await self._checked("digests",
                                        lambda: self._digests(datetime.now(SGT)))
                    # LAST, so a digest failure this poll is already counted when the self-report
                    # runs (it still needs BROKEN_CHECK_POLLS consecutive failures to page).
                    await self._announce_broken_checks()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                                    # noqa: BLE001
                _log.warning("alerts poll failed (will retry): %r", exc)
            await asyncio.sleep(self._poll)
