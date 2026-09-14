"""The `session` state frame: what the strategy is doing, for the UI (#212).

The engine already publishes `positions`, `orders`, `account` and `health` over the Redis/WS bridge.
It publishes nothing about the STRATEGY — so the journal, the lifecycle and the exit-trail are
invisible to the cockpit, and four of the six homescreen blocks in `research/homescreen.md` cannot
be built at all. This is that missing publisher, and it is one addition rather than six features.

The same payload is the evidence bundle the session-narrative feature needs
(`research/session-narrative-prompt.md`), so it is deliberately shaped to be complete enough to
explain a session, not merely enough to render a tile.

READ-ONLY, AND OFF THE TRADING PATH. Plain SELECTs against tables the strategies package owns, with
no ORM import, so a schema addition there cannot break this and a failure here cannot break a
session. The caller treats any exception as "no frame this tick".

ORDERS ARE TRUTH — the same rule the narrative checker enforces. `decision.summary` records what the
session INTENDED ("enter 8"); the submitted order rows record what happened. On 2026-08-10 those
differed, because two ranked names hit the position cap. Both are carried here, separately and
labelled, so no consumer has to guess which one it is holding.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

#: Journal rows carried per frame. Enough to explain the morning; not a log stream.
JOURNAL_LIMIT = 25

_LIFECYCLE = text("""
    SELECT state, reason, updated_at FROM exec_strategy_state WHERE strategy_id = :sid
""")

#: The session being described is the one that most recently DECIDED, found by write time.
#:
#: Not `MAX(session)`. The session column is a date STRING the writer chose, and the live journal
#: contains rows labelled `2026-08-11` that were written on 08-03 — future-dated rows from a
#: partially-completed cleanup. `MAX(session)` returns those, so the frame reported a session eight
#: days ahead with no decision, while the real last session had a full one. A homescreen built on
#: that would say "no decision today" on a day that traded.
_DECISION = text("""
    SELECT session, summary, detail, ts FROM exec_action_log
    WHERE strategy_id = :sid AND kind = 'decision'
    ORDER BY ts DESC, id DESC LIMIT 1
""")

#: Errors are found by WRITE TIME across every session, for the same reason: an error mislabelled
#: with someone else's session date is still an error, and one the operator should see.
_RECENT_ERRORS = text("""
    SELECT summary, session, ts FROM exec_action_log
    WHERE strategy_id = :sid AND kind = 'error' AND ts > now() - make_interval(hours => :hours)
    ORDER BY ts DESC LIMIT 10
""")

#: Submitted orders counted from STRUCTURED detail, not prose. The alerts service already counts
#: this way (`detail->>'phase' = 'result'` and `ok`), and matching on the word "submitted" in a
#: summary breaks the moment anyone rewords a journal line.
_SUBMITTED = text("""
    SELECT COUNT(*) FROM exec_action_log
    WHERE strategy_id = :sid AND session = :session AND kind = 'order'
      AND detail->>'phase' = 'result' AND COALESCE((detail->>'ok')::boolean, false)
""")

#: Today in EXCHANGE time. Carried so a consumer can tell whether the decision below is today's or a
#: previous session's — "the last decision" and "what happened today" are different questions, and a
#: homescreen that conflates them shows a stale rotation as if it were live.
_TODAY = text("SELECT (now() AT TIME ZONE 'America/New_York')::date::text AS today")

_ROWS = text("""
    SELECT kind, symbol, summary, ts FROM exec_action_log
    WHERE strategy_id = :sid AND session = :session AND kind IN ('order', 'risk', 'error', 'state')
    ORDER BY id DESC LIMIT :limit
""")

_TRAIL = text("""
    SELECT symbol, entry, peak, qty, sessions_held, sessions_since_high, quality, updated_at
    FROM exec_position_state WHERE strategy_id = :sid ORDER BY symbol LIMIT 200
""")


async def build(session_factory, strategy_id: str, *, journal_limit: int = JOURNAL_LIMIT,
                error_window_hours: int = 24) -> dict:
    """Assemble the frame for `strategy_id`. Raises on a database problem — the caller decides."""
    async with session_factory() as db:
        life = (await db.execute(_LIFECYCLE, {"sid": strategy_id})).first()
        d = (await db.execute(_DECISION, {"sid": strategy_id})).first()
        session = d.session if d else None

        decision: dict[str, Any] | None = None
        rows: list[dict[str, Any]] = []
        if session:
            if d:
                detail = d.detail if isinstance(d.detail, dict) else {}
                decision = {
                    "summary": d.summary,
                    # INTENT. Kept because it is what the operator sees in the journal, and dropping
                    # it would make the cockpit and the journal disagree about the same session.
                    "reasons": detail.get("reasons") or {},
                    "target_book": detail.get("target_book") or [],
                    "equity": detail.get("equity"),
                    "ranking": (detail.get("ranking") or [])[:15],
                    "blocked_by_gate": detail.get("blocked_by_gate"),
                    "ts": _iso(d.ts),
                }
            rows = [{"kind": r.kind, "symbol": r.symbol, "summary": r.summary, "ts": _iso(r.ts)}
                    for r in (await db.execute(_ROWS, {"sid": strategy_id, "session": session,
                                                       "limit": journal_limit}))]
        today = (await db.execute(_TODAY)).scalar()
        submitted_count = int((await db.execute(
            _SUBMITTED, {"sid": strategy_id, "session": session})).scalar() or 0) if session else 0
        errors = [{"summary": e.summary, "session": e.session, "ts": _iso(e.ts)}
                  for e in (await db.execute(_RECENT_ERRORS,
                                             {"sid": strategy_id, "hours": error_window_hours}))]

        trail = [{
            "symbol": t.symbol,
            "entry": _f(t.entry),
            "peak": _f(t.peak),
            "qty": _f(t.qty),
            "sessions_held": t.sessions_held,
            "sessions_since_high": t.sessions_since_high,
            # 'adopted' means the PEAK WAS NEVER OBSERVED, so peak-relative exits are inert for this
            # position (#197 B1). A UI that shows a trail without showing this would imply protection
            # that is not running — which is the exact failure the quality column was added for.
            "quality": t.quality,
            "updated_at": _iso(t.updated_at),
        } for t in (await db.execute(_TRAIL, {"sid": strategy_id}))]

    return {
        "strategy_id": strategy_id,
        # The session of the LAST DECISION, which is not necessarily today — see `_TODAY`.
        "session": session,
        "today": today,
        "decision_is_today": bool(session and today and session == today),
        "lifecycle": {"state": life.state, "reason": life.reason, "since": _iso(life.updated_at)}
        if life else None,
        "decision": decision,
        "journal": rows,
        "trail": trail,
        "submitted_count": submitted_count,
        # By write time, not by session label — see `_RECENT_ERRORS`.
        "errors": errors,
    }


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _f(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):                                 # pragma: no cover - defensive
        return None
