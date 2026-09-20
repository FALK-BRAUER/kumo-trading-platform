"""Serving the per-lane window base to the tile (#699 read path).

THE LAST SEAM. The tile already computes each lane's standing unrealized NOW; the observation table
holds what each lane held at a window's START. This serves the second half so the tile can subtract.

WHY ITS OWN ENDPOINT rather than a field on `/trades`. `/trades` is pushed on every engine frame, and
this needs a DATABASE READ — one per frame would put a Postgres round trip on the hot path for an
answer that changes once a day. The tile fetches this when the period changes.

ALL PERIODS IN ONE ANSWER, so switching the selector does not refetch and cannot show one window's
base against another's standing for a frame.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from api.pnl_base import unrealized_base_by_period


class _Store:
    """Rows per session date, with the REAL at-or-before lookback semantics.

    The double implements `base_rows_on_or_before` because that is what production calls, and it
    WALKS BACK the same bounded number of days rather than pretending the exact date always has a
    row. A double that answered only exact dates could not express the case the resolution exists
    for — a base landing on a weekend — which is the case that made this change necessary.

    It records WHICH DATES WERE ASKED FOR and which ANSWERED, because asking for the wrong day
    yields a plausible number from the wrong week.
    """

    def __init__(self, by_date=None, lookback=4, captured=None):
        self._by_date = by_date or {}
        self._lookback = lookback
        #: Days the MANIFEST says were captured. Defaults to the days that have rows, because that
        #: is the ordinary case — but it is settable SEPARATELY, because the whole point is that a
        #: captured day with NO rows is a different fact from an uncaptured one, and a double that
        #: could not express that could not test the bug this resolution exists for.
        self._captured = captured if captured is not None else set(self._by_date)
        self.asked: list[str] = []

    async def base_rows(self, session_date: str):
        return self._by_date.get(session_date, [])

    async def base_rows_on_or_before(self, session_date: str, lookback_days: int | None = None):
        from datetime import date, timedelta

        self.asked.append(session_date)
        d = date.fromisoformat(session_date)
        for back in range(0, (lookback_days or self._lookback) + 1):
            day = (d - timedelta(days=back)).isoformat()
            # RESOLVED ON THE MANIFEST, not on the presence of rows — the distinction production
            # makes, so the double can express a captured-but-flat day.
            if day in self._captured:
                return day, self._by_date.get(day, [])
        return None, []


def _row(lane, inst, day, qty, basis, mark, version="v1"):
    return {"session_date": day, "strategy_id": lane, "instrument_id": inst,
            "qty": qty, "avg_px_engine": basis, "mark_px": mark,
            "method_version": version, "capture_kind": "close"}


TODAY = date(2026, 8, 30)


def test_it_serves_a_lane_total_for_each_period_that_HAS_a_base():
    rows = {"2026-08-23": [_row("MOMENTUM-002", "AEM.XNYS", "2026-08-23", 10.0, 100.0, 110.0)]}
    out = asyncio.run(unrealized_base_by_period(_Store(rows), today=TODAY)).by_period
    assert out["1W"]["MOMENTUM-002"] == 100.0


def test_the_dates_asked_for_come_from_the_ONE_window_function():
    """Not recomputed here. A second derivation of "where does 1W start" is how the realized and
    unrealized halves of one cell end up describing different weeks."""
    from api.eod_window import window_base_date

    store = _Store()
    asyncio.run(unrealized_base_by_period(store, today=TODAY))
    expected = {window_base_date(p, today=TODAY) for p in ("1D", "1W", "1M", "3M")}
    assert set(store.asked) == expected


def test_ALL_is_present_and_NULL_rather_than_absent_from_the_answer():
    """`all` has no base date — it is unbounded. It must appear in the response as an explicit null,
    because a KEY MISSING from a payload is indistinguishable from a serialisation bug, while an
    explicit null is a statement. The tile renders an em dash for it either way, but only one of
    those two is something an operator can trust."""
    out = asyncio.run(unrealized_base_by_period(_Store(), today=TODAY)).by_period
    assert "all" in out
    assert out["all"] is None


def test_a_base_day_NOBODY_CAPTURED_is_NULL_and_a_CAPTURED_FLAT_day_is_ZERO():
    """THE DISTINCTION THE MANIFEST EXISTS FOR, and the one my own lookback fix broke.

    Resolving on ROWS conflated them: a day where capture SUCCEEDED with every lane flat writes zero
    rows, looks identical to a day never captured, and the lookback then served an OLDER day's
    unrealized as this window's base — `now − 500` where the truth is `now − 0`. A fabrication in
    place of the honest refusal it replaced.

    Resolving on the MANIFEST separates them, and both answers are now available: null for
    never-captured, an empty map for captured-and-flat, which the tile reads as base zero."""
    # Nothing captured at all -> UNKNOWN.
    assert asyncio.run(unrealized_base_by_period(_Store(), today=TODAY)).by_period["1W"] is None

    # Captured, and every lane flat -> ZERO, not unknown.
    flat = _Store(captured={"2026-08-23"})
    out = asyncio.run(unrealized_base_by_period(flat, today=TODAY)).by_period
    assert out["1W"] == {}, "a captured day with no positions is a KNOWN zero base"


def test_only_the_periods_that_HAVE_a_base_are_read_at_all():
    """MY FIRST VERSION OF THIS TEST COULD NOT FAIL. It asserted `len(asked) == len(set(asked))` to
    pin de-duplication — but with the real vocabulary (1D=1, 1W=7, 1M=30, 3M=90 days back) two
    periods can never land on the same date, so the assertion held for every possible implementation.
    A test whose fixture cannot violate the property is an assertion about nothing.

    The de-duplication stays, as a defensive `set()`, and is documented as unreachable rather than
    left looking tested.

    What IS testable and does matter: `all` has no base, so it must produce NO read. Four reads for
    five periods."""
    store = _Store()
    asyncio.run(unrealized_base_by_period(store, today=TODAY))
    assert len(store.asked) == 4, f"expected one read per based period; got {store.asked}"
    assert all(d for d in store.asked), "no read may be issued for a period with no base date"


def test_a_STORE_FAILURE_is_reported_as_UNKNOWN_and_never_as_a_clean_zero():
    """A database that will not answer must not render as "every lane started flat". The whole
    payload is null for that period, and the endpoint still answers for the others — one broken
    read must not take the rest with it, which is the contract `/health` already states."""
    class _Broken(_Store):
        # OVERRIDES THE METHOD PRODUCTION CALLS. The first version overrode `base_rows`, which the
        # assembler no longer calls directly — so the double raised on a path nothing took and the
        # "failure" test passed against a perfectly healthy read. A double that cannot produce the
        # condition it is named for tests nothing.
        async def base_rows_on_or_before(self, session_date: str, lookback_days: int | None = None):
            raise ConnectionError("postgres went away")

    res = asyncio.run(unrealized_base_by_period(_Broken(), today=TODAY))
    assert res.by_period["1W"] is None and res.by_period["1M"] is None
    # AND THE OUTAGE IS DISTINGUISHABLE FROM A GAP. Returning only the map made a failed read
    # identical to an uncaptured day — a mutant swapping one for the other was behaviourally
    # EQUIVALENT, which is what proved the endpoint could not tell them apart. An uncaptured day is
    # something the backfill fills; a database that will not answer is something a person must act
    # on, and reporting it as "no data for that window" means nobody ever does.
    assert res.unreadable, "a failed read must be reported as its own condition, not as absence"
    assert "2026-08-23" in res.unreadable


def test_the_version_is_selected_ONCE_PER_DAY_from_that_day_s_own_rows():
    """A corrected re-derivation lands beside the original under a new `method_version`, and days can
    differ — a backfill re-run may have corrected last month and not last week. Choosing per day is
    right; choosing once globally would read a version that does not exist on some days."""
    rows = {
        "2026-08-23": [_row("A", "X.XNYS", "2026-08-23", 1.0, 0.0, 10.0, version="v1")],
        "2026-07-31": [_row("A", "X.XNYS", "2026-07-31", 1.0, 0.0, 20.0, version="v1"),
                       _row("A", "X.XNYS", "2026-07-31", 1.0, 0.0, 25.0, version="v2")],
    }
    out = asyncio.run(unrealized_base_by_period(_Store(rows), today=TODAY)).by_period
    assert out["1W"]["A"] == 10.0
    assert out["1M"]["A"] == 25.0, "the newer derivation wins on the day that has one"


def test_a_base_landing_on_a_WEEKEND_resolves_to_the_last_captured_day_before_it():
    """THE FINDING I HAD TALKED MYSELF INTO, and it was measured rather than argued: over 28 days,
    1M's base lands on a WEEKEND on 8 of 20 trading weekdays — every Monday and Tuesday — with 1D
    every Monday and 3M every Friday. The flagship cell would lose its delta 40% of the week and
    flicker between two compositions under one label, BY WEEKDAY.

    My argument for refusing was that reaching back makes "1W" span different lengths depending on
    where holidays fell. That conflates window LENGTH with mark AVAILABILITY: marks do not move over
    a weekend, so the unrealized prevailing at a Saturday boundary IS Friday's close. Resolving
    at-or-before changes which row answers, not which window is being asked.
    """
    from datetime import date

    friday = "2026-08-28"
    rows = {friday: [_row("A", "X.XNYS", friday, 1.0, 0.0, 42.0)]}
    # Monday viewer: 1D's base is Sunday 08-30, which has no close.
    out = asyncio.run(unrealized_base_by_period(_Store(rows), today=date(2026, 8, 31))).by_period

    # FIXTURE PROPERTY FIRST: the requested base really is a non-trading day, or this proves nothing.
    from api.eod_window import window_base_date

    assert date.fromisoformat(window_base_date("1D", today=date(2026, 8, 31))).weekday() >= 5
    assert out["1D"]["A"] == 42.0, "Friday's close is the base a Sunday boundary inherits"


def test_the_lookback_is_BOUNDED_so_a_real_capture_GAP_still_reads_unknown():
    """Unbounded, this would silently reach past a genuine outage and report a stale base as the
    window's start — a number that looks right and describes a different week. Four days covers a
    long weekend; beyond it the day truly was not captured."""
    from datetime import date

    rows = {"2026-08-20": [_row("A", "X.XNYS", "2026-08-20", 1.0, 0.0, 99.0)]}
    out = asyncio.run(unrealized_base_by_period(_Store(rows), today=date(2026, 8, 31))).by_period
    assert out["1D"] is None, "a base 10 days stale must not answer for yesterday"


def test_ONE_unreadable_version_does_not_null_EVERY_period():
    """PER-PERIOD ISOLATION. An unorderable `method_version` on one day used to raise out of the
    assembler, and the endpoint's outer except then nulled the whole payload — one bad day taking
    every window with it, contradicting the contract the failure test above asserts."""
    from datetime import date

    good = "2026-08-28"
    bad = "2026-07-31"
    rows = {
        good: [_row("A", "X.XNYS", good, 1.0, 0.0, 10.0)],
        bad: [_row("A", "X.XNYS", bad, 1.0, 0.0, 20.0),
              _row("A", "X.XNYS", bad, 1.0, 0.0, 25.0, version="experimental")],
    }
    res = asyncio.run(unrealized_base_by_period(_Store(rows), today=date(2026, 8, 31)))
    assert res.by_period["1M"] is None, "the bad day is unknown"
    assert res.by_period["1D"]["A"] == 10.0, "and the others still answer"
    assert bad in res.unreadable
