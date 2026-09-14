"""SLOT OUTCOME DETECTORS (#437) — did the strategy do what it decided? Tests precede the module.

WHY. Measured on the live journal, all strategies, since 2026-07-31:

    decided slots   19
    bad slots       12          63%

Twelve slots in three weeks ended in nothing or in failure, and the number anyone noticed before
2026-08-22 is ZERO. Every one reported healthy. Three of them were only found because two sessions
compared notes.

WHY THIS SHAPE. Every failure of the last 24h has ONE signature: decided N, did fewer than N.

    QC345    08-21 09:35   decided 5 enters, formed 0 orders   (sizing collapsed to zero shares)
    QC345    08-20 09:35   errored before deciding
    TECHIVOL 08-21 14:45   formed 8, landed 0                  (all refused)
    MOMENTUM 08-19 09:35   formed 7, landed 5
    BCTROT   08-20 close   decided, formed 0                   (position cap)

WHY IT READS THE PHASE LAYER AND NOT THE DECISION RECORD. The four runners write three incompatible
decision schemas, and a detector built on those silently misses MOMENTUM and BCTROT. But every runner
writes through ONE `PgJournal`, so `kind` and `detail->>'phase'` are already uniform:

    order  {"phase":"intent"}                an action was formed
    order  {"ok":true,"phase":"result"}      it landed
    order  {"ok":false,"phase":"result"}     it did not
    error  {...}                             it failed

The contract existed one layer below where anyone looked for it.
"""

from __future__ import annotations

import re

import pytest

from api.slot_outcome import (
    _IGNORED_KINDS,
    COUNTED_KINDS,
    SLOT_ROWS,
    SlotCounts,
    Verdict,
    fold_slot_rows,
    judge_slot,
    scan_slots,
)

OK = SlotCounts(decisions=1, intents=3, landed=3, failures=0)


def test_the_fixture_expresses_a_HEALTHY_slot():
    # Fixture property first: if no input were healthy, "healthy is silent" would pass against a
    # detector that flagged everything.
    assert judge_slot(OK) is None


def test_DECIDED_THEN_FORMED_NOTHING_is_flagged():
    """QC345 08-21: decided 5 entries, every one sized to zero shares, zero orders formed. Also
    MOMENTUM/BCTROT's position-cap refusals, which refuse BEFORE an order exists — so they land here
    without the detector needing to know what a cap is."""
    v = judge_slot(SlotCounts(decisions=1, intents=0, landed=0, failures=0))
    assert v is not None and v.verdict == Verdict.DECIDED_NEVER_ATTEMPTED


def test_FORMED_BUT_DID_NOT_LAND_is_flagged():
    """TECHIVOL 08-21: eight sell orders formed against a book it did not own, none landed."""
    v = judge_slot(SlotCounts(decisions=1, intents=8, landed=0, failures=8))
    assert v is not None and v.verdict == Verdict.ATTEMPTED_DID_NOT_LAND


def test_a_PARTIAL_landing_is_flagged_too():
    """MOMENTUM 08-19: 7 formed, 5 landed. A partial is not a success — two orders vanished and the
    session summary said nothing."""
    v = judge_slot(SlotCounts(decisions=1, intents=7, landed=5, failures=3))
    assert v is not None and v.verdict == Verdict.ATTEMPTED_DID_NOT_LAND


def test_errors_alone_are_flagged_even_with_no_decision():
    """QC345 08-20 errored before it decided anything, so `decisions` is 0. A detector keyed only on
    decisions would call that slot healthy."""
    v = judge_slot(SlotCounts(decisions=0, intents=0, landed=0, failures=1))
    assert v is not None and v.verdict == Verdict.ERRORS


def test_a_slot_that_never_ran_is_SILENT_not_flagged():
    """Nothing decided, nothing attempted, nothing failed. Most slots on most days. Flagging these
    would bury the twelve real ones in noise, which is the failure mode that lets people stop reading
    an alarm channel."""
    assert judge_slot(SlotCounts(decisions=0, intents=0, landed=0, failures=0)) is None


def test_the_verdict_carries_the_COUNTS_so_it_is_diagnosable_at_a_glance():
    """A bare verdict string sends the reader to SQL. "dec 1 int 8 land 0 fail 8" does not."""
    v = judge_slot(SlotCounts(decisions=1, intents=8, landed=0, failures=8))
    assert "1" in v.detail and "8" in v.detail
    for token in ("dec", "int", "land", "fail"):
        assert token in v.detail.lower()


def test_a_KNOWN_false_positive_is_declared_rather_than_hidden():
    """THE HONEST LIMIT, and the peer review that found it.

    `DECIDED_NEVER_ATTEMPTED` cannot yet distinguish "decided to do NOTHING, correctly" from "decided
    to act and formed no order". BCTROT close-20m on 2026-08-20 was `enter 0 · exit 0` — legitimately
    nothing to do — and it is flagged. Telling them apart needs the decision record, which is the one
    thing the phase layer cannot see.

    So the verdict SAYS SO. An alarm with an undeclared false-positive floor teaches people to ignore
    it, which is how twelve slots went unnoticed for three weeks in the first place.
    """
    v = judge_slot(SlotCounts(decisions=1, intents=0, landed=0, failures=0))
    assert v.may_be_benign is True
    assert "nothing to do" in v.detail.lower()

    # The other two verdicts are unambiguous and must NOT carry the caveat, or it becomes wallpaper.
    assert judge_slot(SlotCounts(decisions=1, intents=8, landed=0, failures=8)).may_be_benign is False
    assert judge_slot(SlotCounts(decisions=0, intents=0, landed=0, failures=1)).may_be_benign is False


def test_the_alert_key_is_scoped_so_one_bad_slot_cannot_mute_another():
    """`Notifier` deduplicates on the key, and `_GATES` splits on the first colon — so the prefix must
    stay `strategy_degraded` to reach `notify_strategy_degraded`, and everything identifying WHICH slot
    must live after it."""
    v = judge_slot(SlotCounts(decisions=1, intents=0, landed=0, failures=0))
    key = v.alert_key("QC345-003", "2026-08-21", "open+5m")
    assert key.split(":", 1)[0] == "strategy_degraded"
    assert "QC345-003" in key and "2026-08-21" in key and "open+5m" in key


def test_a_SHADOW_session_is_SILENT_because_not_acting_is_the_whole_point():
    """THE DEFECT THIS PINS, found by the operator asking whether we had a dry-run path (2026-08-22).

    We do, and it predates all of this — `lifecycle.py:10`:

        "SHADOW IS THE DRY-RUN STATE. It runs the full decision path and publishes what it WOULD do
         without acting."

    `State.decides` is True for SHADOW and `may_submit_entries` is False, so a SHADOW session decides
    and forms no orders BY DESIGN. The detector as first written flags exactly that shape as
    DECIDED_NEVER_ATTEMPTED — so it would have cried wolf on every session of the one path an operator
    uses to try a strategy safely, and taught them to ignore it before it ever caught a real failure.

    An alarm that fires on the intended safe path is worse than no alarm.
    """
    shadow = SlotCounts(decisions=1, intents=0, landed=0, failures=0)
    assert judge_slot(shadow, may_submit=False) is None
    # The SAME counts from a lane that WAS allowed to act are still the bug.
    assert judge_slot(shadow, may_submit=True) is not None


def test_a_SHADOW_session_that_ERRORED_is_still_reported():
    """Not acting is by design; failing is not. A SHADOW run that raised is exactly the signal a
    dry-run exists to produce, and silencing the whole state would discard it."""
    v = judge_slot(SlotCounts(decisions=1, intents=0, landed=0, failures=3), may_submit=False)
    assert v is not None and v.verdict == Verdict.ERRORS


def test_a_SHADOW_session_that_somehow_FORMED_orders_is_reported():
    """SHADOW must not submit. Orders appearing under it is a containment failure, and the detector
    must not be the thing that looks away."""
    v = judge_slot(SlotCounts(decisions=1, intents=4, landed=0, failures=0), may_submit=False)
    assert v is not None and v.verdict == Verdict.ATTEMPTED_DID_NOT_LAND


# ==================================================================================================
# FOLDING JOURNAL ROWS INTO PER-SLOT COUNTS (#437). The predicate had no caller; this is the half that
# feeds it, kept pure so it is testable without a database.
#
# It reads `kind` and `detail->>'phase'` ONLY. The decision blob has three incompatible schemas across
# four runners — PgSessionRunner carries bar_coverage/ranking/target_book, QC345 enter/exit/hold/scores,
# QC27 weights/cash_proxy_weight — and a fold built on those silently misses MOMENTUM and BCTROT, which
# is how the first attempt at this went wrong.
# ==================================================================================================
def row(strategy, session, slot, kind, detail=None, symbol=None):
    return {"strategy_id": strategy, "session": session, "slot": slot,
            "kind": kind, "detail": detail or {}, "symbol": symbol}


def test_the_fixture_uses_the_SHAPE_the_journal_actually_writes():
    """Fixture property first. These are the exact detail payloads observed in exec_action_log — an
    intent with no `ok`, a result carrying `ok`, and a bare error. A fold tested against invented
    shapes proves nothing about the rows it will meet."""
    assert row("S", "d", "s", "order", {"phase": "intent"})["detail"] == {"phase": "intent"}
    assert row("S", "d", "s", "order", {"ok": True, "phase": "result"})["detail"]["ok"] is True


def test_counts_are_folded_per_STRATEGY_SESSION_SLOT():
    """The key is all three. BCTROT ran open+150m and close-20m on the same day with different
    outcomes; folding by strategy alone would merge them and hide one."""
    rows = [
        row("BCTROT-004", "2026-08-20", "open+150m", "decision"),
        row("BCTROT-004", "2026-08-20", "close-20m", "decision"),
        row("BCTROT-004", "2026-08-20", "close-20m", "order", {"phase": "intent"}),
        row("BCTROT-004", "2026-08-20", "close-20m", "order", {"ok": True, "phase": "result"}),
    ]
    folded = fold_slot_rows(rows)
    assert folded[("BCTROT-004", "2026-08-20", "open+150m")] == SlotCounts(1, 0, 0, 0)
    assert folded[("BCTROT-004", "2026-08-20", "close-20m")] == SlotCounts(1, 1, 1, 0)


def test_a_FAILED_result_counts_as_a_failure_and_NOT_as_landed():
    """TECHIVOL 2026-08-21: eight intents, eight `{"ok": false, "phase": "result"}`. Counting a failed
    result as landed would report that session as healthy."""
    rows = [row("T", "d", "s", "decision")] + [
        row("T", "d", "s", "order", {"phase": "intent"}) for _ in range(8)] + [
        row("T", "d", "s", "order", {"ok": False, "phase": "result"}) for _ in range(8)]
    assert fold_slot_rows(rows)[("T", "d", "s")] == SlotCounts(1, 8, 0, 8)


def test_a_TERMINAL_phase_is_not_counted_as_a_second_landing():
    """The journal writes intent -> result -> terminal for one order. Counting terminal as another
    landing would make `landed` exceed `intents` and mask a genuine shortfall underneath it."""
    rows = [row("B", "d", "s", "decision"),
            row("B", "d", "s", "order", {"phase": "intent"}),
            row("B", "d", "s", "order", {"ok": True, "phase": "result"}),
            row("B", "d", "s", "order", {"ok": True, "phase": "terminal"})]
    assert fold_slot_rows(rows)[("B", "d", "s")] == SlotCounts(1, 1, 1, 0)


def test_an_error_ROW_counts_even_with_no_decision():
    """QC345 2026-08-20 errored before deciding. A fold keyed on decisions would drop the slot."""
    assert fold_slot_rows([row("Q", "d", "s", "error", {"ok": False})])[("Q", "d", "s")] \
        == SlotCounts(0, 0, 0, 1)


def test_unrelated_kinds_are_IGNORED_rather_than_miscounted():
    """`pool` and `risk` rows are the bulk of the journal — MOMENTUM alone wrote 451 pool rows in a
    week. Folding them into any counter would swamp the signal. Their SLOT-level treatment is pinned
    separately below; here the point is only that no counter moves."""
    rows = [row("M", "d", "s", "pool"), row("M", "d", "s", "risk", {"no_bar": ["WHD"]}),
            row("M", "d", "s", "state"), row("M", "d", "s", "decision")]
    assert fold_slot_rows(rows)[("M", "d", "s")] == SlotCounts(1, 0, 0, 0)


def test_a_result_row_with_NO_ok_field_is_not_credited_as_landed():
    """Added because a mutation flipping `is True` to `is not False` left the suite green — no fixture
    had a result row without `ok`. The journal has written these: a result row that cannot say it
    succeeded is not evidence that it did, and a health check must under-report health, not over-."""
    rows = [row("X", "d", "s", "decision"),
            row("X", "d", "s", "order", {"phase": "intent"}),
            row("X", "d", "s", "order", {"phase": "result"})]  # no `ok` at all
    assert fold_slot_rows(rows)[("X", "d", "s")] == SlotCounts(1, 1, 0, 1)


def test_a_slot_with_ONLY_ignored_kinds_does_not_appear_at_all():
    """`_IGNORED_KINDS` was dead code: pool/risk/state already match no counting branch, so removing
    the guard entirely left every test green. It only earns its place if it also stops such rows from
    CONSTRUCTING a slot — otherwise MOMENTUM's 451 weekly pool rows mint zero-count slots that the
    judge must then consider, and the detector's own output is mostly noise."""
    folded = fold_slot_rows([row("M", "d", "09:35", "pool"), row("M", "d", "09:35", "risk")])
    assert folded == {}, "pool/risk chatter alone must not create a slot"


def test_but_an_ignored_kind_ALONGSIDE_a_decision_still_leaves_the_slot():
    """The fixture's own property: the guard must drop ROWS, not slots that have real rows too."""
    folded = fold_slot_rows([row("M", "d", "09:35", "pool"), row("M", "d", "09:35", "decision")])
    assert folded[("M", "d", "09:35")] == SlotCounts(1, 0, 0, 0)


# ==================================================================================================
# THE QUERY. It cannot be executed without Postgres, so what is pinned here is the thing that
# actually goes wrong: the SQL and the fold drifting apart, and the decision blob creeping back in.
# ==================================================================================================
def test_the_query_and_the_fold_admit_the_SAME_KINDS():
    """Two derivations of one fact. The SQL narrows by `kind` for speed and the fold counts by `kind`
    for meaning; if either list moves alone the detector goes quietly blind to a whole kind. This has
    already bitten leash validation and the percent->bps rounding across the JS/Python seam."""
    sql = str(SLOT_ROWS)
    admitted = {k for k in ("decision", "order", "error") if f"'{k}'" in sql}
    assert admitted == COUNTED_KINDS, f"SQL admits {admitted}, fold counts {COUNTED_KINDS}"
    for ignored in _IGNORED_KINDS:
        assert f"'{ignored}'" not in sql, f"SQL admits {ignored}, which the fold discards"


def test_the_query_NEVER_selects_the_decision_blob():
    """The whole reason this detector works. Four runners write three incompatible decision schemas,
    and the first attempt at this was built on them and silently missed MOMENTUM and BCTROT. Selecting
    `summary` or joining on decision contents would reopen exactly that door."""
    sql = str(SLOT_ROWS).lower()
    select = sql.split("from")[0]
    assert "summary" not in select
    assert "detail->>'phase'" not in select, "phase is read in Python, so the fold stays testable"
    for col in ("strategy_id", "session", "slot", "kind", "detail"):
        assert col in select, f"the fold reads {col} and the query must supply it"


# ==================================================================================================
# THE SEAM (#437). Everything above tests a pure function; NONE of it proves that anything calls them
# in the right order with the right arguments. That gap is the one that broke production five times on
# 2026-08-14 with a green suite, so `scan_slots` is driven here as the real entry point.
# ==================================================================================================
def _run(coro):
    """No pytest-asyncio in this repo, and adding it would mean a new dependency in BOTH pyproject and
    deploy/Dockerfile.backend — a pairing that has crash-looped the container before. One helper is
    cheaper than a dep."""
    return __import__("asyncio").run(coro)


class _Row:
    """Shaped like a SQLAlchemy Row where scan_slots touches it — `_mapping`, not attributes and not a
    plain dict. `dict(row)` on a real Row raises; a dict double would accept it and hide that."""

    def __init__(self, d):
        self._mapping = d


class _Db:
    def __init__(self, rows):
        self._rows, self.params = rows, None

    async def execute(self, _stmt, params):
        self.params = params
        return [_Row(r) for r in self._rows]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def _factory(rows):
    db = _Db(rows)
    return (lambda: db), db


def test_the_double_REJECTS_what_a_real_Row_rejects():
    """Fixture property first. A real SQLAlchemy Row is not a Mapping — `dict(row)` raises — so if the
    production code ever regresses to `dict(r)` this double must fail with it, not sail past."""
    with pytest.raises(TypeError):
        dict(_Row({"a": 1}))


_SHADOW_SLOT = [
    {"strategy_id": "MOMENTUM-002", "session": "2026-08-19", "slot": "open+5m",
     "kind": "decision", "detail": {}},
]


def test_a_SHADOW_strategy_deciding_and_submitting_nothing_is_SILENT():
    """The dry-run path. MOMENTUM in SHADOW decides and deliberately forms no orders; two of the
    twelve slots in the original 63% were exactly this, which is why the real rate is 47%. An alarm
    here would fire on the intended safe path and get the detector switched off."""
    factory, _ = _factory(_SHADOW_SLOT)
    assert _run(scan_slots(factory, may_submit={"MOMENTUM-002": False})) == []


def test_the_SAME_ROWS_alarm_when_the_strategy_may_actually_submit():
    """The discriminating half: identical journal rows, opposite verdicts, and the only difference is
    the lifecycle. Without this pair the test above is satisfied by a detector that never alarms."""
    factory, _ = _factory(_SHADOW_SLOT)
    out = _run(scan_slots(factory, may_submit={"MOMENTUM-002": True}))
    assert [(s, v.verdict) for s, _, _, v in out] == [
        ("MOMENTUM-002", Verdict.DECIDED_NEVER_ATTEMPTED)]


def test_an_UNRESOLVED_strategy_alarms_rather_than_going_quiet():
    """Fail loud. A strategy missing from the lifecycle map is more likely live than shadow, and a
    detector that defaults to silence on the case it does not understand is the failure mode this
    whole ticket exists to remove."""
    factory, _ = _factory(_SHADOW_SLOT)
    assert len(_run(scan_slots(factory, may_submit={}))) == 1


def test_the_window_reaches_the_QUERY_and_is_not_merely_accepted():
    """`hours` was a parameter that nothing forwarded in an earlier draft — accepted, defaulted, and
    dropped. The bind parameter is the only proof it arrives."""
    factory, db = _factory([])
    _run(scan_slots(factory, hours=72))
    assert db.params == {"hours": 72}


# ==================================================================================================
# THE SLOT COLUMN LIES ON ORDER ROWS (found 2026-08-22 by the kumo-cockpit-ibkr session).
#
# `pgjournal.write` defaults `slot=slot or DEFAULT_SLOT`, and pgrunner's ORDER/RISK/ERROR writes omit
# `slot=` entirely — only the DECISION write passes a real one. So every order in a session wears the
# FIRST slot's label, and the two halves of one session contradict each other inside one table:
#
#     13:35  decision  open+5m       13:35  order  open+5m   <- genuinely open+5m
#     15:40  decision  open+130m     15:40  order  open+5m   <- actually open+130m
#     16:27  decision  open+177m     16:27  order  open+5m   <- actually open+177m
#
# This is not academic. MOMENTUM's 16:27 slot SOLD FSM 933 and VCTR 88, both FILLED at the broker
# (11.68 and 119.03). The detector reported it DECIDED, NEVER ATTEMPTED, because its orders had been
# filed under open+5m. Reported failure rate fell from 10/17 to 5/17 once attribution was fixed.
#
# Attribution is therefore BY DECISION TIME: every row belongs to the most recent decision before it.
# ==================================================================================================
def test_the_fixture_reproduces_the_MISLABELLED_slot_column():
    """Fixture property first. If the order rows carried their true slot, folding by the column and
    folding by decision time would agree and the test below could not fail either way — which is
    exactly why this went unnoticed: every earlier fixture was internally consistent."""
    rows = _MISLABELLED_SESSION
    orders = [r for r in rows if r["kind"] == "order"]
    assert {r["slot"] for r in orders} == {"open+5m"}, "orders must all wear the FIRST slot's label"
    assert len({r["slot"] for r in rows if r["kind"] == "decision"}) == 3, "need three distinct slots"


#: MOMENTUM-002, 2026-08-19, copied from the live journal.
_MISLABELLED_SESSION = [
    row("MOMENTUM-002", "2026-08-19", "open+5m", "decision"),
    row("MOMENTUM-002", "2026-08-19", "open+5m", "order", {"phase": "intent"}),
    row("MOMENTUM-002", "2026-08-19", "open+5m", "order", {"ok": True, "phase": "result"}),
    row("MOMENTUM-002", "2026-08-19", "open+130m", "decision"),
    row("MOMENTUM-002", "2026-08-19", "open+5m", "order", {"phase": "intent"}),
    row("MOMENTUM-002", "2026-08-19", "open+5m", "error", {"ok": False}),
    row("MOMENTUM-002", "2026-08-19", "open+177m", "decision"),
    row("MOMENTUM-002", "2026-08-19", "open+5m", "order", {"phase": "intent"}),
    row("MOMENTUM-002", "2026-08-19", "open+5m", "order", {"ok": True, "phase": "result"}),
]


def test_orders_are_attributed_to_the_DECISION_they_followed():
    """The 16:27 slot really traded — FSM 933 and VCTR 88, filled, verified at the broker. Folding by
    the slot COLUMN credits its orders to open+5m and reports it barren."""
    f = fold_slot_rows(_MISLABELLED_SESSION)
    assert f[("MOMENTUM-002", "2026-08-19", "open+177m")] == SlotCounts(1, 1, 1, 0), (
        "the slot that filled two orders must not read as barren"
    )
    assert f[("MOMENTUM-002", "2026-08-19", "open+130m")] == SlotCounts(1, 1, 0, 1)
    assert f[("MOMENTUM-002", "2026-08-19", "open+5m")] == SlotCounts(1, 1, 1, 0)


def test_no_slot_is_credited_with_ANOTHER_slots_orders():
    """The discriminating half: the bug's signature is one slot holding everything. Pinning only the
    barren slot would pass an implementation that dropped the orders entirely."""
    f = fold_slot_rows(_MISLABELLED_SESSION)
    assert sum(c.intents for c in f.values()) == 3
    assert all(c.intents == 1 for c in f.values()), f"orders piled onto one slot: {f}"


def test_rows_BEFORE_any_decision_are_not_invented_into_a_slot():
    """The live journal has a 07:15 pool-refresh error hours before the 13:35 decision. Attributing it
    to the first slot of the day would blame a slot for a failure that preceded it."""
    rows = [row("M", "d", "open+5m", "error", {"ok": False}),
            row("M", "d", "open+5m", "decision")]
    assert fold_slot_rows(rows)[("M", "d", "open+5m")] == SlotCounts(1, 0, 0, 0)


# ==================================================================================================
# A TERMINAL FILL MUST RETRACT AN EARLIER TERMINAL REJECTION (#512).
#
# MEASURED on staging's own journal, BCTROT-004 / 2026-08-24 / open+215m, by running `fold_slot_rows`
# and `judge_slot` over the 119 real rows rather than counting by hand:
#
#     SlotCounts(decisions=1, intents=8, landed=8, failures=8)  ->  ERRORS
#
# All eight orders FILLED. The eight `failures` are Nautilus's own synthesized
# `ORDER_NOT_FOUND_AT_VENUE` verdicts, written at 17:05:41 when the venue's report came back short
# because tzdata was missing from the image (#527) — and retracted by the venue itself at 17:17:51,
# when the same eight symbols wrote `terminal ... filled` rows. The journal holds both. The fold read
# only the first.
#
# THE DEFECT IS AN ASYMMETRY, NOT A MISCOUNT. `fold_slot_rows` deliberately does not count a terminal
# FILL (it would make `landed` exceed `intents`) — but its `kind == "error"` branch was unconditional
# on phase, so it counted terminal REJECTIONS. One rule applied to one half of a pair: the exact shape
# of "two derivations of one fact drift", with the reader told a slot errored on a session that traded.
# ==================================================================================================
def _bctrot_open215m_rows():
    """The real slot, reduced to its shape: eight orders that submitted, were declared rejected, and
    then filled. Ordering is the journal's own — rejections at 17:05, fills twelve minutes later."""
    syms = ["AEM", "AMGN", "CGAU", "GLD", "HALO", "LH", "SSRM", "WPM"]
    rows = [row("BCTROT-004", "2026-08-24", "open+215m", "decision")]
    rows += [row("BCTROT-004", "2026-08-24", "open+215m", "order", {"phase": "intent"}, symbol=s)
             for s in syms]
    rows += [row("BCTROT-004", "2026-08-24", "open+215m", "order",
                 {"ok": True, "phase": "result"}, symbol=s) for s in syms]
    rows += [row("BCTROT-004", "2026-08-24", "open+215m", "error",
                 {"ok": False, "phase": "terminal"}, symbol=s) for s in syms]
    rows += [row("BCTROT-004", "2026-08-24", "open+215m", "order",
                 {"ok": True, "phase": "terminal"}, symbol=s) for s in syms]
    return rows


def test_the_fixture_can_EXPRESS_the_contradiction_it_is_meant_to_resolve():
    """Fixture property first. A fold that cannot see the symbol cannot pair a fill with the rejection
    it retracts, so the fixture is worthless unless its terminal rows CARRY one and the two terminals
    for a symbol genuinely disagree."""
    terminals = [r for r in _bctrot_open215m_rows() if r["detail"].get("phase") == "terminal"]
    assert len(terminals) == 16
    assert all(r["symbol"] for r in terminals), "terminal rows must carry a symbol to be pairable"
    aem = [r["detail"]["ok"] for r in terminals if r["symbol"] == "AEM"]
    assert aem == [False, True], f"AEM must be rejected THEN filled, got {aem}"


def test_a_terminal_FILL_RETRACTS_the_terminal_rejection_for_the_same_symbol():
    """The slot traded. It must not read ERRORS.

    This is the whole ticket: `decided (submitted 8)`, eight rejections, eight fills, and every
    surface an operator reads reporting a failed session."""
    counts = fold_slot_rows(_bctrot_open215m_rows())[("BCTROT-004", "2026-08-24", "open+215m")]
    assert counts == SlotCounts(1, 8, 8, 0), f"eight fills must retract eight rejections, got {counts}"
    assert judge_slot(counts) is None, "a slot where every order filled must be SILENT"


def test_an_UNRETRACTED_terminal_rejection_still_counts_as_a_failure():
    """The fix must not be "stop counting rejections". A venue rejection that no fill ever answers is
    the signal this detector exists for — TECHIVOL 2026-08-21 was exactly that."""
    rows = [r for r in _bctrot_open215m_rows()
            if not (r["detail"].get("phase") == "terminal" and r["detail"].get("ok") is True)]
    counts = fold_slot_rows(rows)[("BCTROT-004", "2026-08-24", "open+215m")]
    assert counts == SlotCounts(1, 8, 8, 8), f"nothing retracted these eight, got {counts}"
    assert judge_slot(counts).verdict == Verdict.ERRORS


def test_a_terminal_rejection_AFTER_a_fill_is_NOT_retracted_by_it():
    """Order matters and the last terminal wins. A fill followed by a rejection is a real late refusal
    (a bust, a cancel-after-fill); reading the pair as a set would silence it."""
    rows = [row("B", "d", "s", "decision"),
            row("B", "d", "s", "order", {"phase": "intent"}, symbol="AEM"),
            row("B", "d", "s", "order", {"ok": True, "phase": "result"}, symbol="AEM"),
            row("B", "d", "s", "order", {"ok": True, "phase": "terminal"}, symbol="AEM"),
            row("B", "d", "s", "error", {"ok": False, "phase": "terminal"}, symbol="AEM")]
    assert fold_slot_rows(rows)[("B", "d", "s")] == SlotCounts(1, 1, 1, 1)


def test_repeated_terminal_rows_for_ONE_symbol_count_ONCE():
    """CGAU wrote EIGHTEEN terminal fill rows in that slot and the others six each — one per partial
    execution. A per-ROW count would report eighteen outcomes for one order."""
    rows = [row("B", "d", "s", "decision"),
            row("B", "d", "s", "order", {"phase": "intent"}, symbol="CGAU"),
            row("B", "d", "s", "order", {"ok": True, "phase": "result"}, symbol="CGAU")]
    rows += [row("B", "d", "s", "error", {"ok": False, "phase": "terminal"}, symbol="CGAU")
             for _ in range(3)]
    assert fold_slot_rows(rows)[("B", "d", "s")] == SlotCounts(1, 1, 1, 1)


def test_a_terminal_rejection_with_NO_SYMBOL_still_counts_because_nothing_can_retract_it():
    """THE DECLARED LIMIT. Retraction is keyed on the symbol, so a terminal row without one cannot be
    paired with anything. It keeps the pre-fix behaviour — counted — because under-reporting health is
    the safe direction here, and silently dropping it would make an unpairable rejection invisible."""
    rows = [row("B", "d", "s", "decision"),
            row("B", "d", "s", "error", {"ok": False, "phase": "terminal"})]
    assert fold_slot_rows(rows)[("B", "d", "s")] == SlotCounts(1, 0, 0, 1)


def test_a_NON_terminal_error_is_untouched_by_the_retraction_rule():
    """Pool-refresh failures, risk refusals and raised exceptions are `kind='error'` with no terminal
    phase. They have no fill that could ever answer them and must keep counting immediately."""
    rows = [row("B", "d", "s", "decision"),
            row("B", "d", "s", "error", {}),
            row("B", "d", "s", "order", {"ok": True, "phase": "terminal"}, symbol="AEM")]
    assert fold_slot_rows(rows)[("B", "d", "s")] == SlotCounts(1, 0, 0, 1)


def test_the_fold_reads_the_symbol_the_QUERY_actually_selects():
    """Two derivations of one fact. The retraction rule is keyed on `symbol`; if `SLOT_ROWS` stops
    selecting it, every terminal row arrives unpairable and the fix silently reverts to the defect —
    green the whole way, which is how #512 stayed invisible for five days."""
    select_list = re.search(r"select\s+(.*?)\s+from", str(SLOT_ROWS), re.S | re.I)
    assert select_list, "SLOT_ROWS must be a SELECT ... FROM"
    columns = {c.strip().lower() for c in select_list.group(1).split(",")}
    assert "symbol" in columns, (
        f"the fold pairs terminals on `symbol`; the SELECT list is {sorted(columns)}. Checking the "
        f"whole statement would pass on the word appearing in a comment, a WHERE clause, or an alias")


def test_a_terminal_is_the_FIRST_row_of_its_slot_without_crashing_the_fold():
    """The retraction is resolved AFTER the loop with `acc[key][3] += ...`, which assumes `acc[key]`
    exists for every key in `terminals`. It does — `acc.setdefault` runs before the branch — but that
    is an ordering invariant living two edits apart from its use, and a KeyError here takes down
    `/slots` and the whole alert sweep for every strategy, not only this one.

    A restart between submit and the venue's answer produces exactly this: the terminal arrives with
    no intent or result row beside it in the window."""
    rows = [row("B", "d", "s", "decision"),
            row("B", "d", "s", "error", {"ok": False, "phase": "terminal"}, symbol="AEM")]
    assert fold_slot_rows(rows)[("B", "d", "s")] == SlotCounts(1, 0, 0, 1)


def test_a_terminal_BEFORE_the_session_first_decision_is_dropped_like_any_other_row():
    """DECLARED LIMIT, pre-existing and unchanged by #512: a row that precedes the session's first
    decision has no slot to belong to and is skipped, because attributing it to the day's first slot
    would blame that slot for something which preceded it. A terminal for an order submitted in an
    EARLIER slot that has already left the window therefore vanishes rather than landing on the wrong
    slot — the safe direction, but it means the retraction cannot reach across that boundary either.

    Pinned so the next reader learns it from a test instead of from a missing alarm."""
    rows = [row("B", "d", "s", "error", {"ok": False, "phase": "terminal"}, symbol="AEM"),
            row("B", "d", "s", "decision")]
    assert fold_slot_rows(rows)[("B", "d", "s")] == SlotCounts(1, 0, 0, 0)


def test_a_terminal_row_alone_never_INVENTS_a_slot_that_alarms():
    """A lone terminal fill must not conjure an all-zero slot the judge then weighs. It creates the
    accumulator either way; what matters is that the verdict stays silent."""
    counts = fold_slot_rows([row("B", "d", "s", "order",
                                 {"ok": True, "phase": "terminal"}, symbol="AEM")])[("B", "d", "s")]
    assert counts == SlotCounts(0, 0, 0, 0)
    assert judge_slot(counts) is None
