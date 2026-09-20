"""`failed_requests` promises history and delivers a snapshot (#908).

`FailedRequests.record` accumulates `count`, `first_ts`, `last_ts` — and the protection reconciler calls
`clear_kind("protection")` on EVERY pass, so for that kind the three fields are dead by construction:
`count` is always 1 and `first_ts == last_ts`, whatever the history. At 01:15 SGT on 2026-09-11 the
CRAK/DINO rows carried the only evidence of $4,501 naked (#870) as `count: 1`, timestamped seconds ago
— a refusal that had recurred every 60 s for as long as both lanes held the name, presenting as
brand-new and probably transient.

The per-pass clear STAYS: "what is true NOW" is the right thing for the row to answer. What survives it
is a RECURRENCE record per (kind, subject): `consecutive_passes` (passes it was recorded in without a
gap — resets on a pass that records nothing for it), `total_passes` and `recurring_since_ns` (never
reset while the recorder lives). Two numbers, because a consecutive-only count cannot see an
INTERMITTENT refusal — refused on passes 1–40, clear on 41, refused 42–80 reads `1 or 2` forever while
being refused 98% of the time (coordinator). `consecutive_passes: 1, total_passes: 412` is the flapping
case made visible. NAMED so they cannot be read as `first_ts`/`count` one word apart (review), and so
#907's resetting `streak_started_ns` and this surviving `recurring_since_ns` are never taken for one fact.

A PASS is delimited by `clear_kind(kind)`, and it counts EVALUATED passes, not minutes: a protection
pass that returns before its clear (broker unreadable, outside RTH) neither increments nor resets —
UNKNOWN must not count (#873). A kind that has never been delimited reads `None` on all three — three
states; `1` beside `count: 275` would be indistinguishable from a first occurrence, the exact reading
this ticket exists to kill.
"""
from __future__ import annotations

from api.failed_requests import FailedRequests


def _row(fr: FailedRequests, kind: str, subject: str) -> dict:
    rows = [r for r in fr.as_rows() if r["kind"] == kind and r["subject"] == subject]
    assert len(rows) == 1, rows
    return rows[0]


def test_FIXTURE_the_per_pass_clear_makes_count_and_first_ts_dead():
    """The defect, reproduced on the store itself before anything is added: three passes, a clear
    between each, and the row reads exactly like a first occurrence every time."""
    fr = FailedRequests()
    for ts in (100, 200, 300):
        fr.clear_kind("protection")
        fr.record("protection", "CRAK.ARCX", "reserved_by_other_order", ts=ts)
        row = _row(fr, "protection", "CRAK.ARCX")
        assert row["count"] == 1 and row["first_ts"] == row["last_ts"] == ts


def test_a_refusal_recorded_on_consecutive_passes_carries_its_pass_count_and_first_seen():
    fr = FailedRequests()
    for ts in (100, 200, 300):
        fr.clear_kind("protection")
        fr.record("protection", "CRAK.ARCX", "reserved_by_other_order", ts=ts)
    row = _row(fr, "protection", "CRAK.ARCX")
    assert row["count"] == 1, "the per-pass row still says what is true NOW"
    assert row["consecutive_passes"] == 3 and row["total_passes"] == 3 and row["recurring_since_ns"] == 100


def test_a_pass_that_records_NOTHING_for_the_key_resets_passes_but_not_total_or_first_seen():
    """The flapping case: refused, clear, refused reads `passes 1, total 2` — visible, where a
    consecutive-only count would have shown `1` and a first occurrence."""
    fr = FailedRequests()
    fr.clear_kind("protection"); fr.record("protection", "CRAK.ARCX", "x", ts=100)
    fr.clear_kind("protection")                       # a clean pass: nothing recorded for CRAK
    fr.clear_kind("protection"); fr.record("protection", "CRAK.ARCX", "x", ts=300)
    row = _row(fr, "protection", "CRAK.ARCX")
    assert (row["consecutive_passes"], row["total_passes"], row["recurring_since_ns"]) == (1, 2, 100)


def test_several_records_in_ONE_pass_are_one_pass_and_a_count_of_records():
    """`count` counts records within the pass; `passes` counts passes. Two records on one pass is
    `count 2, passes 1` — the two numbers answer different questions."""
    fr = FailedRequests()
    fr.clear_kind("protection")
    fr.record("protection", "CRAK.ARCX", "a", ts=100)
    fr.record("protection", "CRAK.ARCX", "b", ts=101)
    row = _row(fr, "protection", "CRAK.ARCX")
    assert row["count"] == 2 and row["consecutive_passes"] == 1 and row["total_passes"] == 1


def test_a_per_subject_clear_resets_passes_and_keeps_total():
    """`clear(kind, subject)` is a request that started succeeding: the streak ends, the history does
    not — the next failure reads `passes 1, total_passes 2`."""
    fr = FailedRequests()
    fr.clear_kind("protection"); fr.record("protection", "CRAK.ARCX", "x", ts=100)
    fr.clear("protection", "CRAK.ARCX")
    fr.clear_kind("protection"); fr.record("protection", "CRAK.ARCX", "x", ts=300)
    row = _row(fr, "protection", "CRAK.ARCX")
    assert (row["consecutive_passes"], row["total_passes"], row["recurring_since_ns"]) == (1, 2, 100)


def test_subjects_and_kinds_keep_SEPARATE_records():
    """CRAK on three passes, DINO on two (absent on the middle one), both PRESENT on the last pass so
    the attachment of recurrence to rows is per (kind, subject) — not positional, not per kind."""
    fr = FailedRequests()
    fr.clear_kind("aggregation")   # delimit the other kind ONCE, so its numbers are readable
    for ts in (100, 200, 300):
        fr.clear_kind("protection")
        fr.record("protection", "CRAK.ARCX", "x", ts=ts)
        if ts != 200:
            fr.record("protection", "DINO.XNAS", "y", ts=ts)
        # ANOTHER KIND, same subject, recorded on every protection pass: the protection clear must
        # not delimit it (review, HIGH — a `clear_kind` ignoring its kind filter drove this upward).
        fr.record("aggregation", "CRAK.ARCX", "z", ts=ts)
    crak, dino, agg = (_row(fr, "protection", "CRAK.ARCX"), _row(fr, "protection", "DINO.XNAS"),
                       _row(fr, "aggregation", "CRAK.ARCX"))
    assert (crak["consecutive_passes"], crak["total_passes"]) == (3, 3)
    assert (dino["consecutive_passes"], dino["total_passes"]) == (1, 2)
    assert (agg["consecutive_passes"], agg["total_passes"], agg["count"]) == (1, 1, 3)


def test_a_kind_that_was_never_delimited_reads_None_not_one():
    """Three states (review): `consecutive_passes: 1` beside `count: 275` would be indistinguishable
    from a first occurrence. A kind nobody has ever called `clear_kind` for has no pass structure to
    report, and says so with None — `count` keeps accumulating as before."""
    fr = FailedRequests()
    for ts in (100, 200, 300):
        fr.record("aggregation", "AEM.XNYS", "bars unavailable", ts=ts)
    row = _row(fr, "aggregation", "AEM.XNYS")
    assert row["count"] == 3
    assert row["consecutive_passes"] is None and row["total_passes"] is None and row["recurring_since_ns"] is None


def test_rows_are_ordered_by_RECURRENCE_first_so_an_entrenched_refusal_is_on_top():
    """`as_rows` sorted by `-count`, and a per-pass-cleared row is `count 1` forever — so the refusal
    recurring for 412 passes sorted BELOW any twice-recorded row and off the readback's 300-char prefix
    (review). Recurrence first, then count, then recency."""
    fr = FailedRequests()
    fr.clear_kind("aggregation")
    fr.record("aggregation", "AEM.XNYS", "twice", ts=100)
    fr.record("aggregation", "AEM.XNYS", "twice", ts=101)
    for ts in (100, 200, 300):
        fr.clear_kind("protection")
        fr.record("protection", "CRAK.ARCX", "x", ts=ts)
    rows = fr.as_rows()
    assert [(r["kind"], r["count"], r["total_passes"]) for r in rows] == [
        ("protection", 1, 3), ("aggregation", 2, 1)]


def test_the_recurrence_fields_come_BEFORE_the_note_in_the_row():
    """The readback renders a container as `json.dumps(v)[:300]`; a flip note alone runs ~180 chars.
    Fields after `note` would be past the cut on every row but the first — the #892 allow cannot cite
    what it cannot see (review)."""
    fr = FailedRequests()
    fr.clear_kind("protection")
    fr.record("protection", "CRAK.ARCX", "n" * 250, ts=100)
    keys = list(fr.as_rows()[0].keys())
    assert keys.index("consecutive_passes") < keys.index("note") and keys.index("total_passes") < keys.index("note")


def test_the_frame_carries_as_rows_verbatim():
    """HOP 1: the health dict's value for `failed_requests` is `as_rows()` — the same rows, no copy
    that could drop the new keys (#233/#322/#336 family). Pinned on the frame's AST like `flip_pending`."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).parent / "engine_node.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
            if "engine_ok" in keys and "armed_lanes" in keys:
                value = node.values[keys.index("failed_requests")]
                assert (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                        and value.func.attr == "as_rows"), "the frame does not carry as_rows() verbatim"
                return
    raise AssertionError("health frame not found — this test is blind")


def test_the_engine_pass_carries_the_fields_to_the_frame(monkeypatch):
    """THE SEAM: the protection pass clears and re-records on every tick, and the frame's
    `failed_requests` rows are `as_rows()` verbatim (untyped dicts through the DTO — nothing to drop).
    Three passes of the same refusal at the reconciler must read `passes: 3` on the frame's rows."""
    from api.test_lane_protection_seam import (
        _CachedOrder,
        _floor_settings,
        _trail_on_floor_lane,
        _with_cached,
    )
    from api.test_protection_reconciler import _Fake, _Http, _ns, _position, _run

    _floor_settings(monkeypatch)
    trail = _trail_on_floor_lane()
    cached = _CachedOrder(trail["client_order_id"], "BCTROT-004")
    fake = _with_cached(_Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()], orders=[trail])), cached)
    fake._last_price_for = lambda iid: 90.0   # floor_unplaceable on every pass

    for minute in (0, 1, 2):
        fake.clock._ts = _ns(10, minute)
        _run(fake)

    rows = [r for r in fake._failed_requests.as_rows() if r["subject"] == "AEM.XNYS"]
    assert rows and rows[0]["count"] == 1 and rows[0]["consecutive_passes"] == 3
    assert rows[0]["recurring_since_ns"] == _ns(10, 0)


def test_the_pass_boundary_precedes_EVERY_protection_record_in_the_reconciler():
    """The reconciler's `clear_kind("protection")` sat below `_cancel_unprotectable_stops`, which
    records protection rows — so (a) those rows were wiped a hundred lines later on the same pass and
    never reached the frame, and (b) a key recorded before the clear and again after it read a
    permanent +1 on the recurrence record (impl review: 3 passes → `consecutive 4`). The clear is the
    pass boundary and must precede every record; it must also sit BELOW the RTH gate, so an
    out-of-session tick leaves the last evaluation's rows on the surface rather than "nothing refused"."""
    import inspect

    from api.engine_node import UiFeedStrategy

    src = inspect.getsource(UiFeedStrategy._reconcile_protection_inner)
    gate = src.index("if not _us_market_open(now_ns):")
    clear = src.index('self._failed_requests.clear_kind("protection")')
    first_record = src.index("self._failed_requests.record(")
    cancel_pass = src.index("self._cancel_unprotectable_stops(now_ns)")
    assert gate < clear < cancel_pass, "the clear must follow the RTH gate and precede the cancel pass"
    assert clear < first_record, "a protection row is recorded before the pass boundary"
    assert src.count('self._failed_requests.clear_kind("protection")') == 1, "two pass boundaries in one pass"
