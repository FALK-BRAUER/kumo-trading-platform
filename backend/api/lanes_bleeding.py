"""LANES BLEEDING (#1098) — a lane that cannot decide while venue-side stops drain its book.

MEASURED ON AN IBKR PAPER INSTANCE2, 2026-09-15..17. MOMENTUM-002 and BCTROT-004 logged `NO DECISION — pool sources
stale or failed: ledger_book` at every slot (the ledger source was dead, #1097) while the protection
plane's resting stops kept firing: SM, GRDN, CVE (MOMENTUM), APA, HPQ, WPM (BCTROT). Each stop is an
exit with no entry behind it. MOMENTUM went 8 -> 2 names, BCTROT 12 -> 5, in two sessions, under a
TRADING label on every surface. The journal already held both facts; nothing joined them.

THE POOL FIX STOPS THIS DRAIN, NOT THE SHAPE. Whenever a lane cannot decide — pool dead, calendar
dead, every name budget-refused — protection keeps selling and nothing buys. That is a lane going to
cash silently, and it is what this reports: `/health.lanes_bleeding` names the lane, the count of
venue exits, the count of own entries (0 by definition) and the sessions without a decision.

WHAT IS READ, AND WHY THE `detail` CODES RATHER THAN `summary` TEXT. Every runner writes through the
one PgJournal, and two rows carry the facts in machine-readable form:

    state  {"decided": false, "blocked": "<reason>", ...}       the session-outcome row, NO DECISION
    order  {"by": "protection", "phase": "terminal", ...}        a stop-out the lane did not place
    order  {"phase": "terminal", "ok": true, "side": <entry>}    the lane's OWN entry, landed

A `summary` predicate would drift the first time a runner rephrased its line; a `detail` predicate
is the contract slot_outcome.py already relies on.

A CORRECT SKIP IS NOT A MISSING DECISION. A monthly lane between rebalances writes `decided: false`
with `state: "SKIPPED"` and `skip.is_rebalance: false` (issue 250) — that is the lane
working, and its stops draining the book until the next rebalance is by design. Only a lane that
COULD NOT decide counts here: a `SKIPPED` row is excluded, whatever its `skip` says. A not-warm or
panel-lacks-due skip, when a gateway writes one, is `state: TRADING` + `decided: false` and counts.

SESSIONS, NOT SLOTS. BCTROT runs two slots a day; two NO DECISION slots can be one bad morning. The
threshold is N distinct sessions (default 2).

DEDUPE BY client_order_id. The journal writes two to four `terminal` rows per stop fill (SM x4,
GRDN x3, CVE x3 for three fills on 2026-09-16); a row count over-reports 3.3x.

AN ENTRY THAT DID NOT LAND IS NOT AN ENTRY. Own entries are `phase: terminal, ok: true` rows on the
lane's entry side (BUY for a long lane, SELL for a short one — decided by the row's side against the
stop-outs' side, never by the word SELL). A sent-then-rejected entry (the #1039 class) clears nothing.

WHAT THIS DOES NOT SEE, declared. A slot that wrote NO ROW AT ALL — TECHIVOL on 2026-09-17 16:00Z,
QC345 since 09-01 — is invisible to a fold over rows. That absence is `alerts.never_ran`'s finding
(due and produced nothing), computed against the declared slot schedule; deriving it a second time
here would be two derivations of one fact. The gateway-side row for a silent skip is a separate item.

THREE STATES on the read (`scan_bleeding`): `ok` with a list (possibly empty), or `unreadable` with
`lanes: None` and the error named. A failed read is never an empty list.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

#: Kinds the fold reads. Asserted against `BLEED_ROWS` by test — the query narrows by kind for speed,
#: the fold by kind for meaning, and if either moves alone the detector goes blind to a whole kind.
READ_KINDS = frozenset({"state", "order"})

BLEED_ROWS = text("""
    SELECT id, ts, strategy_id, session, slot, symbol, kind, detail
    FROM exec_action_log
    WHERE ts > now() - make_interval(hours => :hours)
      AND kind IN ('state', 'order')
    ORDER BY id
""")

#: Window by WRITE TIME, wide enough for two sessions plus a weekend (Fri + Mon = 4 calendar days).
DEFAULT_HOURS = 96
DEFAULT_MIN_SESSIONS = 2


@dataclass(frozen=True)
class LaneBleed:
    strategy_id: str
    venue_exits: int
    #: THREE STATES (9q02jges, #1116 review). `int` when every landed own order in the window carries
    #: a side; `None` = UNKNOWN when any lacks one. momentum.py:90 and qc345.py:203 write terminal
    #: rows as `{"phase": "terminal", "ok": ok}` — no side, no coid — so for those lanes an entry
    #: cannot be told from an exit, and "0 entries" would be a false number: paper's MOMENTUM landed
    #: five BUY fills on 2026-09-14 and read `0 entries` under the first version of this fold. An
    #: unknown count does NOT clear the lane (it may be bleeding) and is rendered as `entries unknown`.
    own_entries: int | None
    no_decision_sessions: int
    since: str
    blocked: str

    def line(self) -> str:
        since = self.since[:16].replace("T", " ") + "Z" if self.since else "?"
        entries = "entries unknown" if self.own_entries is None else f"{self.own_entries} entries"
        return (f"{self.strategy_id}: {self.venue_exits} exits, {entries}, "
                f"{self.no_decision_sessions} sessions without a decision, since {since}"
                f"{' — ' + self.blocked if self.blocked else ''}")

    def as_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "venue_exits": self.venue_exits,
                "own_entries": self.own_entries, "no_decision_sessions": self.no_decision_sessions,
                "since": self.since, "blocked": self.blocked, "line": self.line()}

    def alert_key(self) -> str:
        return f"lanes_bleeding:{self.strategy_id}"


def _detail(r) -> dict:
    d = r.get("detail")
    return d if isinstance(d, dict) else {}


def _is_missing_decision(r) -> bool:
    if r.get("kind") != "state":
        return False
    d = _detail(r)
    if d.get("decided") is not False:
        return False
    # issue 250: a SKIPPED row is a correct non-decision, never a missing one.
    return str(d.get("state") or "").upper() != "SKIPPED"


def _is_stop_out(r) -> bool:
    d = _detail(r)
    return r.get("kind") == "order" and d.get("by") == "protection" and d.get("phase") == "terminal"


def fold_bleeding(rows, *, min_sessions: int = DEFAULT_MIN_SESSIONS) -> list[LaneBleed]:
    """Pure. Rows in write order (`ORDER BY id`), dicts with the `BLEED_ROWS` columns."""
    missing: dict[str, dict[str, str]] = {}          # sid -> {session: blocked}
    decided: dict[str, set[str]] = {}                # sid -> sessions with ANY decided:true state row
    latest_state: dict[str, str] = {}                # sid -> the latest session carrying a state row
    stops: dict[str, dict[str, tuple[str, str]]] = {}  # sid -> {coid: (ts, side)}
    landed: dict[str, list[str]] = {}                # sid -> sides of own terminal ok rows ("" = unknown)
    for r in rows or ():
        sid = str(r.get("strategy_id") or "")
        if not sid:
            continue
        d = _detail(r)
        session = str(r.get("session") or "")
        if r.get("kind") == "state" and "decided" in d:
            latest_state[sid] = max(latest_state.get(sid, ""), session)
            if d.get("decided") is True:
                decided.setdefault(sid, set()).add(session)
        if _is_missing_decision(r):
            missing.setdefault(sid, {}).setdefault(session, str(d.get("blocked") or ""))
        elif _is_stop_out(r):
            coid = str(d.get("client_order_id") or f"{sid}:{r.get('symbol')}:{r.get('id')}")
            stops.setdefault(sid, {}).setdefault(coid, (str(r.get("ts") or ""), str(d.get("side") or "")))
        elif r.get("kind") == "order" and d.get("phase") == "terminal" and d.get("ok") is True:
            landed.setdefault(sid, []).append(str(d.get("side") or ""))
    out: list[LaneBleed] = []
    for sid in sorted(missing):
        # A SESSION WITH ANY `decided: true` ROW IS DECIDED (9q02jges, #1116 review). The duplicate-run
        # row (#831: `blocked: "already decided <session>/<slot> (concurrent run)"`, `decided: false`)
        # sits beside the real decision for the same session — paper's BCTROT read 6 sessions
        # without a decision where 3 were that row. Removed here rather than by matching the text.
        sessions = {s: why for s, why in missing[sid].items() if s not in decided.get(sid, set())}
        exits = stops.get(sid, {})
        if len(sessions) < min_sessions or not exits:
            continue
        # THE LATEST SESSION MUST ITSELF BE A MISSING ONE. A lane that could not decide Mon and Tue
        # and decided Wed — with Wed's stop-outs in the window — is not going to cash; it recovered.
        if latest_state.get(sid, "") not in sessions:
            continue
        # The lane's ENTRY side is the opposite of what its stops did. Never the word SELL.
        exit_sides = {side for _ts, side in exits.values() if side}
        entry_side = "BUY" if exit_sides == {"SELL"} else "SELL" if exit_sides == {"BUY"} else None
        sides = landed.get(sid, [])
        if any(not side for side in sides):
            own: int | None = None                    # a side-less landed row: entries UNKNOWN
        else:
            own = sum(1 for side in sides if entry_side is None or side == entry_side)
        if own:
            continue
        first = min(ts for ts, _side in exits.values())
        latest_session = max(sessions)
        out.append(LaneBleed(strategy_id=sid, venue_exits=len(exits), own_entries=own,
                             no_decision_sessions=len(sessions), since=first,
                             blocked=sessions[latest_session]))
    return out


async def scan_bleeding(session_factory, *, hours: int = DEFAULT_HOURS,
                        min_sessions: int = DEFAULT_MIN_SESSIONS) -> dict:
    """Read the journal and fold. NEVER RAISES; a failed read is `unreadable`, not clean."""
    try:
        async with session_factory() as db:
            rows = [dict(r._mapping) for r in (await db.execute(BLEED_ROWS, {"hours": hours}))]
    except Exception as exc:                                            # noqa: BLE001
        return {"status": "unreadable", "lanes": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok", "lanes": [b.as_dict() for b in fold_bleeding(rows, min_sessions=min_sessions)],
            "error": None}
