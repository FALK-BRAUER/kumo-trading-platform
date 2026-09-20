"""SLOT OUTCOME DETECTORS (#437) — did the strategy do what it decided?

MEASURED ON A LIVE JOURNAL, all strategies, since 2026-07-31:

    decided slots     19
    of which SHADOW    2        dry-run by design, correctly silent
    live slots        17
    bad slots          5        29% of live

MEASURED BY RUNNING THIS MODULE OVER THE LIVE JOURNAL, not by counting rows by hand. That matters:
hand counts of this same data produced 63%, then 57%, then 47%, all wrong and all stated confidently.
The lifecycle is taken from each DECISION ROW's own `state`, never from today's `exec_strategy_state`
— which reads TRADING for all five strategies and would erase the SHADOW exemption retroactively.

    MOMENTUM 08-04 open+5m     ATTEMPTED, DID NOT LAND   16 formed, 8 landed, 9 errors
    MOMENTUM 08-19 open+130m   ATTEMPTED, DID NOT LAND   rejected: insufficient qty available
    BCTROT   08-20 close-20m   DECIDED, NEVER ATTEMPTED
    QC345    08-21 open+5m     DECIDED, NEVER ATTEMPTED  (never formed an order in its life)
    TECHIVOL 08-21 open+150m   ATTEMPTED, DID NOT LAND   8 formed, 0 landed

AN EARLIER VERSION OF THIS MODULE REPORTED 10 OF 17, AND HALF OF THOSE WERE ITS OWN BUG. Folding on the
journal's `slot` COLUMN credited every order in a session to its first slot, so later slots read as
barren. MOMENTUM's 16:27 slot on 08-19 sold FSM 933 and VCTR 88 — both FILLED at the broker, 11.68 and
119.03 — and was reported DECIDED, NEVER ATTEMPTED. Attribution is by decision time now; see
`fold_slot_rows`. The five above survive because each has evidence outside this column: broker
rejections, zero order rows in the entire table, or eight results carrying ok=false. The number anyone noticed before
2026-08-22 is ZERO — every one of them reported healthy, and three were found only because two
sessions compared notes. This is not a detector for a bad day; it is a detector for the normal
condition nobody could see.

ONE SIGNATURE COVERS EVERY FAILURE OF THE LAST 24 HOURS: decided N, did fewer than N.

    QC345    08-21 09:35   decided 5 enters, formed 0 orders   (sizing collapsed to zero shares)
    QC345    08-20 09:35   errored before deciding
    TECHIVOL 08-21 14:45   formed 8, landed 0                  (all refused)
    MOMENTUM 08-19 09:35   formed 7, landed 5
    BCTROT   08-20 close   decided, formed 0                   (position cap)

WHY THE PHASE LAYER AND NOT THE DECISION RECORD. The four runners write three incompatible decision
schemas — PgSessionRunner has bar_coverage/ranking/target_book, QC345 has enter/exit/hold/scores,
QC27 has weights/cash_proxy_weight — and a detector built on those silently misses MOMENTUM and
BCTROT, which is how the first attempt at this went wrong. But every runner writes through ONE
`PgJournal`, so `kind` and `detail->>'phase'` are already uniform:

    order  {"phase":"intent"}                an action was formed
    order  {"ok":true,"phase":"result"}      it landed
    order  {"ok":false,"phase":"result"}     it did not
    error  {...}                             it failed

The contract existed one layer below where anyone looked for it. That is the whole reason this ships
today rather than after a cross-repo Protocol.

DECIDING NOTHING IS NOT FAILING. See `may_be_benign` — the one limit this cannot see, declared rather
than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text


class Verdict(str, Enum):
    DECIDED_NEVER_ATTEMPTED = "DECIDED, NEVER ATTEMPTED"
    ATTEMPTED_DID_NOT_LAND = "ATTEMPTED, DID NOT LAND"
    ERRORS = "ERRORS"


@dataclass(frozen=True)
class SlotCounts:
    """One (strategy, session, slot), counted off the journal's kind/phase columns."""

    decisions: int
    intents: int
    landed: int
    failures: int


@dataclass(frozen=True)
class SlotVerdict:
    verdict: Verdict
    detail: str
    #: True when this verdict has a KNOWN false-positive mode. Declared, never hidden: an alarm with
    #: an undeclared floor teaches people to ignore it, which is how twelve slots went unseen.
    may_be_benign: bool

    def alert_key(self, strategy_id: str, session: str, slot: str) -> str:
        """`Notifier` dedupes on this and `_GATES` splits on the FIRST colon, so the prefix must stay
        `strategy_degraded` to reach `notify_strategy_degraded`. Everything identifying WHICH slot goes
        after it — a key that collapsed to the prefix would let one bad slot mute every other."""
        return f"strategy_degraded:{strategy_id}:{session}:{slot}"


def judge_slot(c: SlotCounts, *, may_submit: bool = True) -> SlotVerdict | None:
    """None means nothing to report. A slot that simply did not run is the common case and is silent.

    `may_submit` is the lifecycle's own `may_submit_entries`. SHADOW is the DRY-RUN state — it "runs
    the full decision path and publishes what it WOULD do without acting" (lifecycle.py:10) — so a
    SHADOW session decides and forms no orders BY DESIGN. Flagging that would fire on every session of
    the one path an operator uses to try a strategy safely, and an alarm that fires on the intended
    safe path teaches people to ignore it before it ever catches a real failure.

    It silences ONLY the decided-never-attempted verdict. A SHADOW run that ERRORED is exactly the
    signal a dry-run exists to produce, and orders appearing under SHADOW is a containment failure —
    both still report.
    """
    counts = f"dec {c.decisions} int {c.intents} land {c.landed} fail {c.failures}"

    # Ordered by how much the reader can act on. A slot that formed orders and lost them is a
    # different problem from one that never formed any, and both differ from an outright error.
    if c.intents > c.landed:
        return SlotVerdict(Verdict.ATTEMPTED_DID_NOT_LAND, counts, may_be_benign=False)
    if c.decisions > 0 and c.intents == 0 and may_submit:
        # THE DECLARED LIMIT. This cannot yet tell "decided to do NOTHING, correctly" from "decided to
        # act and formed no order". BCTROT close-20m on 2026-08-20 was `enter 0 · exit 0` — genuinely
        # nothing to do — and it lands here. Telling them apart needs the decision record, the one
        # thing the phase layer cannot see. Until then the caveat travels WITH the alert.
        return SlotVerdict(
            Verdict.DECIDED_NEVER_ATTEMPTED,
            f"{counts} — may be benign if the strategy had nothing to do this slot",
            may_be_benign=True,
        )
    if c.failures > 0:
        # Last, because a slot that errored before deciding has `decisions == 0` and would otherwise
        # read as healthy — which is exactly what QC345's 08-20 session did.
        return SlotVerdict(Verdict.ERRORS, counts, may_be_benign=False)
    return None


# ==================================================================================================
# FOLDING JOURNAL ROWS INTO COUNTS. `judge_slot` above is the predicate; this is what feeds it.
#
# Kept pure — rows in, counts out — so the counting rules are testable without a database. The SQL
# that produces `rows` selects kind and detail only; it never touches the decision blob.
# ==================================================================================================
#: Journal rows that carry no outcome signal. `pool` and `risk` alone are the bulk of the table —
#: MOMENTUM wrote 451 pool rows in a week — so they are dropped explicitly rather than by omission,
#: to make it obvious that ignoring them is a decision and not an oversight.
_IGNORED_KINDS = ("pool", "risk", "state", "position")

#: The kinds that move a counter. Declared once and asserted against the SQL below, because the query
#: narrows by kind for speed while the fold counts by kind for meaning — two derivations of one fact,
#: and if either moves alone the detector goes blind to a whole kind without failing anything.
COUNTED_KINDS = frozenset({"decision", "order", "error"})


def fold_slot_rows(rows) -> dict[tuple[str, str, str], SlotCounts]:
    """Count journal rows into one `SlotCounts` per (strategy_id, session, slot).

    Keyed on all three because a strategy can run several slots a day with different outcomes —
    BCTROT ran open+150m and close-20m on 2026-08-20 and only the second one failed. Folding by
    strategy alone would average them and hide it.

    Reads `kind`, `detail->>'phase'` and — since #512 — `symbol`. A terminal FILL is still not
    counted as a landing: the journal writes intent -> result -> terminal for a single order, so
    crediting the terminal too would make `landed` exceed `intents` and paper over a genuine
    shortfall. A terminal REJECTION used to count as a failure regardless, and that asymmetry is what
    made BCTROT-004's 2026-08-24 open+215m slot read ERRORS on a session where all eight orders
    filled. Terminal rows are now resolved together, per symbol, last writer wins.
    """
    acc: dict[tuple[str, str, str], list[int]] = {}
    #: (key) -> {symbol: ok-of-its-LAST-terminal-row}. Terminal outcomes cannot be counted as they
    #: arrive because a later row for the same symbol can reverse them; they are resolved after the
    #: whole window has been read. See the `terminal` branch below.
    terminals: dict[tuple[str, str, str], dict[str, bool]] = {}
    # THE SLOT COLUMN IS ONLY TRUSTWORTHY ON DECISION ROWS. `pgjournal.write` defaults
    # `slot=slot or DEFAULT_SLOT`, and pgrunner's ORDER/RISK/ERROR writes omit `slot=` — so every order
    # in a session wears the FIRST slot's label while the decision beside it carries the real one.
    #
    # MOMENTUM-002 on 2026-08-19 decided three times and its 16:27 slot SOLD FSM 933 and VCTR 88, both
    # FILLED at the broker. Folding on the column reported that slot DECIDED, NEVER ATTEMPTED and put
    # its orders under open+5m. The measured failure rate fell from 10/17 to 5/17 when this was fixed.
    #
    # So attribution is by DECISION TIME: a row belongs to the most recent decision before it. REQUIRES
    # ROWS IN WRITE ORDER, which `SLOT_ROWS` guarantees with `ORDER BY id`.
    current: dict[tuple[str, str], str] = {}
    # Sessions that decide AT ALL, computed up front. The distinction matters and it is not the same as
    # "no decision yet":
    #
    #   * a session that DOES decide — rows before that decision are pre-slot noise. The live journal
    #     carries a 07:15 ledger_book refresh failure ahead of a 13:35 decision, and charging that to
    #     the day's first slot is the same misattribution this function exists to fix.
    #   * a session that NEVER decides — QC345 on 2026-08-20 errored before deciding. There is no
    #     decision to attribute to and the failure is real, so the row's own slot is used. Dropping it
    #     would hide a strategy that fell over before it got started, which is exactly the condition
    #     worth an alarm.
    decides = {(r.get("strategy_id") or "", r.get("session") or "")
               for r in rows or () if r.get("kind") == "decision"}
    for r in rows or ():
        kind = r.get("kind")
        sid_session = (r.get("strategy_id") or "", r.get("session") or "")
        if kind == "decision":
            current[sid_session] = r.get("slot") or ""
        # BEFORE the slot is constructed, not after. These kinds match no counting branch anyway, so
        # skipping them later would be dead code; skipping them here is what stops a slot that saw
        # nothing but pool chatter from existing as an all-zero row for the judge to weigh.
        if kind in _IGNORED_KINDS:
            continue
        slot = current.get(sid_session)
        if slot is None and sid_session not in decides:
            slot = r.get("slot") or ""      # never decides — the row's own slot is all there is
        if slot is None:
            # Activity BEFORE any decision — the live journal carries a 07:15 pool-refresh error hours
            # ahead of a 13:35 decision. Attributing it to the day's first slot would blame that slot
            # for a failure which preceded it.
            continue
        key = sid_session + (slot,)
        c = acc.setdefault(key, [0, 0, 0, 0])
        detail = r.get("detail") or {}
        phase = detail.get("phase")
        symbol = r.get("symbol")
        if kind == "decision":
            c[0] += 1
        elif kind == "order" and phase == "intent":
            c[1] += 1
        elif kind == "order" and phase == "result":
            # `ok` absent is treated as NOT landed. A result row that cannot say it succeeded is not
            # evidence that it did, and the safe direction for a health check is to under-report
            # health rather than over-report it.
            if detail.get("ok") is True:
                c[2] += 1
            else:
                c[3] += 1
        elif phase == "terminal":
            # RETRACTABLE, BECAUSE THE VENUE RETRACTS THEM (#512). A terminal row is the venue's
            # later answer about one order, and the journal can hold BOTH answers: BCTROT-004's
            # open+215m slot on 2026-08-24 wrote eight `rejected` terminals at 17:05:41 and the same
            # eight symbols `filled` at 17:17:51. All eight orders filled. The fold read only the
            # first set and reported ERRORS on a session that traded — the whole of #512.
            #
            # Keyed on the symbol and LAST WRITER WINS, so a fill answers the rejection before it and
            # a late refusal after a fill is still heard. Resolved once per symbol at the end, never
            # per row: CGAU alone wrote eighteen terminal rows for one order.
            #
            # THE SYMBOL, NOT THE CLIENT ORDER ID, BECAUSE THE ROW CANNOT CARRY ONE. #512 asked for
            # coid keying; `record_terminal` (kumo-trading-strategies pgrunner.py) writes
            # `detail={"phase","ok"}` and leaves `correlation` unset, so the coid is physically
            # absent from the journal and adding it is a cross-repo writer change. The weaker key
            # leaves one hole — a fill on order X retracting a rejection on order Y for the same
            # symbol in the same folded slot — which these runners cannot reach today: one DAY
            # market order per symbol per slot, and a retry of the same intent SHOULD be retracted
            # by its fill.
            if symbol:
                terminals.setdefault(key, {})[symbol] = detail.get("ok") is True
            elif detail.get("ok") is not True:
                # Unpairable, so it cannot be retracted by anything and keeps the pre-fix behaviour.
                # Dropping it would make a rejection nobody can answer invisible, which is the
                # opposite of what this detector is for.
                c[3] += 1
        elif kind == "error":
            c[3] += 1

    # A symbol whose LAST terminal did not succeed failed, once, however many rows said so. Terminal
    # FILLS deliberately add nothing to `landed` — the journal writes intent -> result -> terminal for
    # one order, so crediting the terminal too would push `landed` past `intents` and paper over a
    # genuine shortfall. Rejections were counted while fills were not, and that asymmetry WAS the bug;
    # one rule now governs both halves of the pair.
    for key, by_symbol in terminals.items():
        acc[key][3] += sum(1 for ok in by_symbol.values() if not ok)

    return {k: SlotCounts(*v) for k, v in acc.items()}


#: Rows for the fold. Deliberately NOT `detail->>'phase'`: the phase logic stays in Python where it is
#: testable without a database, and this query stays a plain projection of the columns `fold_slot_rows`
#: reads. `summary` is absent for the same reason the decision blob is — the detector works precisely
#: because it never looks at either.
#:
#: Window is by WRITE TIME, not by session. The live journal holds rows labelled with a session date
#: eight days ahead of when they were written (see `session_state._DECISION`), so a session-based
#: window silently drops real slots.
SLOT_ROWS = text("""
    SELECT strategy_id, session, slot, symbol, kind, detail
    FROM exec_action_log
    WHERE ts > now() - make_interval(hours => :hours)
      AND kind IN ('decision', 'order', 'error')
    ORDER BY id
""")


async def scan_slots(session_factory, *, hours: int = 24, may_submit=None) -> list:
    """Fold the last `hours` of journal into slots and judge each one.

    `may_submit` maps strategy_id -> bool, taken from the lifecycle. A strategy in SHADOW decides and
    deliberately submits nothing, so `judge_slot` must not read that as a failure — this is the whole
    reason two of the twelve slots in the original count were not defects at all. An absent entry is
    treated as True, because a strategy the caller could not resolve is more likely live than shadow
    and a missed alert is worse than a spurious one.
    """
    async with session_factory() as db:
        rows = [dict(r._mapping) for r in (await db.execute(SLOT_ROWS, {"hours": hours}))]
    verdicts = []
    for (sid, session, slot), counts in sorted(fold_slot_rows(rows).items()):
        v = judge_slot(counts, may_submit=(may_submit or {}).get(sid, True))
        if v is not None:
            verdicts.append((sid, session, slot, v))
    return verdicts


# ==================================================================================================
# THE FOURTH OUTCOME: NEVER RAN (#378).
#
# `judge_slot` folds rows. A lane that never ran wrote none, so it cannot be judged — its docstring
# says so plainly. That leaves the WORST outcome as the only silent one: BCTROT missed both decision
# slots on 2026-08-19 and journalled nothing, and it went unnoticed for three days (#349).
#
# Checkable only because the expectation is DECLARED: `QC345-003_SLOTS: ['open+300m']`,
# `TECHIVOL-005_SLOTS: ['open+315m']`. Absence is a finding when something said it would be there.
# ==================================================================================================

#: Minutes in a US regular session, 09:30-16:00 ET. `close-20m` is measured from the END of this.
RTH_MINUTES = 390

#: How long after its due minute a slot may stay silent before it counts as missing. A session takes
#: real time to reach its first journal write — the decision needs a panel, and a late feed defers it
#: to the next bar — so alarming AT the due minute would fire on lanes that are merely starting.
DEFAULT_GRACE_MINUTES = 15


def slot_due_minutes(slot) -> int | None:
    """`open+300m` -> 300, `close-20m` -> 370. None when the name cannot be read.

    None and NOT 0 for an unparseable name: 0 means "due at the open", so an unrecognised slot would
    be reported missing on every poll from 09:45 onward. An alarm that fires on a name nobody
    recognises is one an operator mutes, and then it is not an alarm.
    """
    if not isinstance(slot, str):
        return None
    for prefix, base, sign in (("open+", 0, 1), ("close-", RTH_MINUTES, -1)):
        if slot.startswith(prefix) and slot.endswith("m"):
            body = slot[len(prefix):-1]
            if body.isdigit():
                return base + sign * int(body)
    return None


def effective_expected_slots(cfg: dict, defaults: dict | None = None, registered=None) -> dict:
    """What the lanes will actually run — see `strategies.decision_slots` (#378). Re-exported here
    because this module is the detector's vocabulary and the announcer imports from it."""
    from strategies.decision_slots import effective_expected_slots as _resolve

    return _resolve(cfg, defaults, registered=registered)


def never_ran(expected: dict, seen, *, minutes_since_open: int | None,
              grace: int = DEFAULT_GRACE_MINUTES) -> list:
    """(strategy_id, slot) pairs that were DUE and produced nothing. Pure.

    `minutes_since_open is None` means there was no session today — a weekend or a holiday — and a
    lane that did not run on a day it was never due is not a finding.

    An empty slot list here declares nothing — but the CALLER no longer passes one (#378). Every
    `*_SLOTS` value is `[]` live, and the lanes read that as "keep the built-in schedule", so
    `effective_expected_slots` resolves the built-ins BEFORE this function sees them. The prose
    that used to sit here said the opposite — that expecting anything from an empty list would
    alarm on working lanes — and it survived as a safety rationale for the blindness it described.
    A comment that reads as safety is the most dangerous kind of wrong.
    """
    if minutes_since_open is None:
        return []
    missing = []
    for sid, slots in sorted((expected or {}).items()):
        for slot in slots or ():
            due = slot_due_minutes(slot)
            if due is None or minutes_since_open < due + grace:
                continue
            if (sid, slot) not in (seen or set()):
                missing.append((sid, slot))
    return missing
