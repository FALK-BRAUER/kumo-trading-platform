"""What the engine ASKED the venue to stream, against what has actually arrived (#618).

WHY THIS EXISTS. On 2026-08-31, 392 subscriptions went out and the venue refused a large subset —
275 x `10089 requires additional subscription`, 136 x `10189 no permissions`. Each refusal was
logged. Nothing anywhere held both numbers, so a venue serving half the book was indistinguishable
from a quiet market, and every automated surface said healthy.

#618 designed exactly this ("the adapter answers with what it actually bound... throttling downgrades
the tier and SAYS SO rather than dropping the need silently") and was closed with a `docs(plan)`
commit and one test file. There was no tier code in the backend.

IT WATCHES THE SYMPTOM, NOT NAUTILUS INTERNALS. The IB adapter's error codes reach its own logger,
not a callback the strategy can subscribe to, so a ledger built on those codes would be pinned to one
adapter's log format and would go silent the moment it changed. What we CAN observe is the thing that
actually matters: we asked for a stream, and nothing has come down it. That is true whatever the venue
said, and it is the same measurement on Alpaca, IBKR and Databento. The same choice is made and
written down at engine_node.py's subscription watchdog.

THREE STATES, NEVER TWO — which is the whole ticket. A subscription is BOUND (data has arrived),
SILENT (asked, and nothing yet, past the point where nothing is meaningful), or UNKNOWN (asked, but
not enough trading time has passed to say). `0 of 0` is not `0 of 4`, and neither is "we have not
looked yet".

SILENCE IS MEASURED IN TRADING MINUTES, NOT WALL CLOCK. A subscription that has produced nothing over
a weekend has produced nothing correctly. `trading_minutes_between` returns None where the venue never
described the span, and that None propagates to UNKNOWN rather than collapsing into either answer —
the `nan <= 0` family, where a comparison that cannot be evaluated silently reads as permission.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _trading_minutes(start_ns: int, end_ns: int, calendar) -> float | None:
    """Minutes the venue was open in the span, or None where it cannot say — AND IT DOES NOT SWALLOW.

    `feed_staleness.trading_minutes_between` deliberately catches everything, because a calendar that
    cannot answer says so by returning None and a stale-feed banner must not take the frame down.
    Routing this ledger through it made a BROKEN calendar indistinguishable from a venue that never
    described the span: every subscription read UNKNOWN forever and the detector was dead, reported
    as a clean read. That is the same defect as the notifier whose `except Exception` on every poll
    meant "no alarms" read as "nothing wrong".

    So a raise propagates. `_subscription_summary` catches it at the health boundary and reports the
    failure AS ITS OWN CONDITION, which is where the fail-soft belongs — one place that names what
    broke, not one silent swallow per row.
    """
    if calendar is None or not start_ns or not end_ns or end_ns <= start_ns:
        return None
    from api.venue_calendar import calendar_minutes

    # THROUGH THE SHARED ADAPTER, not by asking for one method by name. Two calendar implementations
    # reach this and they do not share an interface: a paper instance gets the broker's
    # `AlpacaCalendar`, which has `day` and no `trading_minutes_between`. Asking by name made paper
    # report `requested: null` with an AttributeError for the ledger's whole life — caught only
    # because the summary reports its own failures instead of returning an empty pair.
    minutes = calendar_minutes(calendar, start_ns, end_ns)
    # A calendar RETURNING None has answered: it does not cover this span. Only a RAISE is a broken
    # calendar, and only that propagates. `float(None)` collapsed the two back into one.
    return None if minutes is None else float(minutes)


#: Trading minutes a subscription may produce nothing before it counts as SILENT. Deliberately longer
#: than the feed-staleness threshold (15): that one asks "has the WHOLE feed died", which any single
#: tick disproves, while this one asks about ONE instrument, and a thin name can legitimately go
#: half an hour without a print near the open.
SILENT_AFTER_TRADING_MINUTES = 30

BOUND = "bound"
SILENT = "silent"
UNKNOWN = "unknown"


@dataclass
class Subscription:
    """One stream the engine asked for."""

    kind: str
    subject: str
    requested_ns: int
    #: When the first datum arrived. None means none has — NOT that it arrived at time zero.
    first_seen_ns: int | None = None


@dataclass
class SubscriptionLedger:
    """Requested-versus-bound as standing state.

    Deliberately NOT a counter pair. "392 requested, 275 bound" cannot tell an operator which 117 are
    dark, and the whole cost of #618 was that the missing ones could not be named.
    """

    _subs: dict[tuple[str, str], Subscription] = field(default_factory=dict)

    def requested(self, kind: str, subject: str, now_ns: int) -> None:
        """Record that a stream was asked for.

        Re-requesting an ALREADY BOUND subscription does not reset it. The engine re-subscribes on
        reconnect and on the refetch heal, and treating that as a fresh request would restart the
        clock on every stream every time one flaked — so a permanently dark subscription would
        never age into SILENT. It would look exactly like a healthy one that had just been renewed.
        """
        key = (kind, subject)
        if key not in self._subs:
            self._subs[key] = Subscription(kind, subject, now_ns)

    def bound(self, kind: str, subject: str, now_ns: int) -> None:
        """Record that data arrived on a stream.

        A datum for something never requested is still recorded — as a subscription that was bound
        without being asked for. Dropping it would hide a real disagreement between what we believe
        we asked for and what the venue is sending, which is the other half of the same question.
        """
        key = (kind, subject)
        sub = self._subs.get(key)
        if sub is None:
            sub = self._subs[key] = Subscription(kind, subject, requested_ns=now_ns)
        if sub.first_seen_ns is None:
            sub.first_seen_ns = now_ns

    def state_of(self, kind: str, subject: str, now_ns: int, calendar) -> str | None:
        """BOUND, SILENT or UNKNOWN — or None where nothing was ever asked for.

        None is the fourth answer and it is not a variant of UNKNOWN: "we never asked" and "we asked
        and cannot yet say" are different facts about the system, and an absent lifecycle row reading
        as a state is how a 0-allocation lane came to read as armed.
        """
        sub = self._subs.get((kind, subject))
        if sub is None:
            return None
        if sub.first_seen_ns is not None:
            return BOUND
        elapsed = _trading_minutes(sub.requested_ns, now_ns, calendar)
        if elapsed is None:
            # The venue never described this span. Not "fine", not "broken" — never told us.
            return UNKNOWN
        return SILENT if elapsed >= SILENT_AFTER_TRADING_MINUTES else UNKNOWN

    def summary(self, now_ns: int, calendar) -> dict:
        """The pair, plus the names. `silent` is the list #618 asked for and could not produce."""
        counts = {BOUND: 0, SILENT: 0, UNKNOWN: 0}
        silent: list[str] = []
        for (kind, subject) in self._subs:
            state = self.state_of(kind, subject, now_ns, calendar)
            if state is None:
                continue
            counts[state] += 1
            if state == SILENT:
                silent.append(f"{kind}:{subject}")
        return {
            "requested": len(self._subs),
            "bound": counts[BOUND],
            "silent": counts[SILENT],
            "unknown": counts[UNKNOWN],
            # Bounded, because a surface that grows to 392 rows is scrolled past — which is the same
            # silence at the other extreme. The COUNT above is never truncated.
            "silent_subjects": sorted(silent)[:20],
        }
