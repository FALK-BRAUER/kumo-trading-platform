"""The four questions that could not be answered on 2026-08-31 must each be one query (#758).

Every test here is aimed at a specific wrong claim made that evening, and at the surface that invited
it. The point is not that the module computes correctly — it is that a reader CANNOT make the same
mistake twice.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.book_truth import (
    ABANDONED,
    InferredFills,
    position_truth,
    truth_summary,
)


def _pos(iid, lane, signed, *, is_open=True):
    return SimpleNamespace(instrument_id=iid, strategy_id=lane, signed_qty=signed, is_open=is_open)


def _report(iid, signed):
    return SimpleNamespace(instrument_id=iid, signed_decimal_qty=signed)


# ==================================================================================================
# "cache and broker agree in aggregate" — the claim that came from reading the cache twice
# ==================================================================================================
def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the GMAB fixture must actually disagree, or every assertion below passes by
    having nothing to violate it."""
    rows = position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], [_report("GMAB.XNAS", 0)])
    assert rows[0].cache_qty != rows[0].venue_qty


def test_GMAB_the_measured_case_reports_the_net_as_DISAGREEING():
    """cache 59 / venue 0, as measured. This is the row that took an hour of greps to establish."""
    rows = position_truth(
        [_pos("GMAB.XNAS", "BCTROT-004", 59), _pos("GMAB.XNAS", "EXTERNAL", -59),
         _pos("GMAB.XNAS", "MOMENTUM-002", 59)],
        [_report("GMAB.XNAS", 0)],
    )
    row = rows[0]
    assert row.cache_qty == 59
    assert row.venue_qty == 0
    assert row.net_disagrees is True
    assert row.by_lane == {"BCTROT-004": 59.0, "EXTERNAL": -59.0, "MOMENTUM-002": 59.0}


def test_an_UNREADABLE_VENUE_is_NOT_agreement_and_NOT_zero():
    """THE CENTRAL RULE. `None` reports as unknown; it must never look like a matching venue, and it
    must never look like a flat one. Passing an empty LIST is a real answer and stays distinct."""
    unreadable = position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], None)[0]
    assert unreadable.venue_qty is None
    assert unreadable.net_disagrees is None, "unreadable rendered as a verdict"

    read_and_flat = position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], [])[0]
    assert read_and_flat.venue_qty == 0
    assert read_and_flat.net_disagrees is True, "a venue that was read and holds nothing must disagree"


def test_an_instrument_MISSING_from_a_readable_venue_read_is_zero_not_unknown():
    """The venue answered and did not mention it — that is a held-nothing answer about that symbol."""
    row = position_truth([_pos("CGAU.XNYS", "BCTROT-004", 80)], [_report("OTHER.XNAS", 5)])
    row = [r for r in row if r.instrument_id == "CGAU.XNYS"][0]
    assert row.venue_qty == 0 and row.net_disagrees is True


def test_a_matching_book_reports_NO_disagreement():
    """A surface that cries wolf on a healthy book is one that gets ignored."""
    row = position_truth([_pos("AAPL.XNAS", "BCTROT-004", 10)], [_report("AAPL.XNAS", 10)])[0]
    assert row.net_disagrees is False and not row.split_disagrees


# ==================================================================================================
# The SPLIT — which the broker cannot adjudicate
# ==================================================================================================
def test_TWO_LANES_CLAIMING_THE_SAME_SHARES_is_reported_as_a_split_problem():
    """GMAB again: BCTROT 59 and MOMENTUM 59 both long, EXTERNAL -59 balancing. The net can be
    anything; the split is over-claimed either way."""
    row = position_truth(
        [_pos("GMAB.XNAS", "BCTROT-004", 59), _pos("GMAB.XNAS", "MOMENTUM-002", 59),
         _pos("GMAB.XNAS", "EXTERNAL", -59)],
        [_report("GMAB.XNAS", 59)],
    )[0]
    assert row.net_disagrees is False, "the net matches here — that is the point"
    assert row.split_disagrees is True, (
        "two lanes each claiming the whole position went unreported while the net looked clean"
    )


def test_a_CLEAN_SPLIT_is_not_flagged():
    row = position_truth(
        [_pos("AAPL.XNAS", "BCTROT-004", 6), _pos("AAPL.XNAS", "MOMENTUM-002", 4)],
        [_report("AAPL.XNAS", 10)],
    )[0]
    assert row.split_disagrees is False


# ==================================================================================================
# Abandonment — the line that scrolls past
# ==================================================================================================
def test_ABANDONMENT_IS_STANDING_STATE_not_a_line():
    """`no further reconciliation attempts will be made` is the engine saying it has PERMANENTLY
    stopped repairing a position it knows is wrong. It appeared once, in one ERROR line."""
    rows = position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], [_report("GMAB.XNAS", 0)],
                          abandoned=["GMAB.XNAS"])
    assert rows[0].state == ABANDONED
    assert truth_summary(rows)["abandoned_names"] == ["GMAB.XNAS"]


def test_a_CLOSED_position_does_not_contribute_to_the_cache_side():
    rows = position_truth(
        [_pos("AAPL.XNAS", "BCTROT-004", 10, is_open=False), _pos("AAPL.XNAS", "MOMENTUM-002", 4)],
        [_report("AAPL.XNAS", 4)],
    )
    assert rows[0].cache_qty == 4 and rows[0].net_disagrees is False


# ==================================================================================================
# Inferred fills — the mint, currently logging at INFO
# ==================================================================================================
def test_an_INFERRED_FILL_THAT_CANNOT_LAND_names_the_lane():
    """The mint's exact shape: a fabricated fill stamped with a lane that holds nothing there. A
    count alone cannot say which instrument to look at, so the lane is kept."""
    inf = InferredFills()
    inf.record("HALO.XNAS", landed=False, strategy_id="MANUAL-001")
    inf.record("HALO.XNAS", landed=True)
    assert inf.by_instrument["HALO.XNAS"] == 2
    assert inf.unlanded["HALO.XNAS"] == "MANUAL-001"
    assert inf.as_rows()[0]["unlanded_strategy"] == "MANUAL-001"


def test_an_inferred_fill_that_LANDS_is_counted_but_not_flagged():
    """Bookkeeping happens. A surface that flags every reconciliation fill is noise, and noise is
    how the real one stayed invisible."""
    inf = InferredFills()
    inf.record("AAPL.XNAS", landed=True)
    assert inf.total == 1 and inf.as_rows()[0]["unlanded_strategy"] is None


def test_an_UNSTAMPED_inferred_fill_is_named_rather_than_blank():
    """An empty strategy_id is not neutral — per the operating notes it is MANUAL-001 by default, which is
    what preserves the mint. It must not render as an absent value."""
    inf = InferredFills()
    inf.record("HALO.XNAS", landed=False, strategy_id="")
    assert inf.unlanded["HALO.XNAS"] == "<unstamped>"


# ==================================================================================================
# The summary that /health carries
# ==================================================================================================
def test_the_summary_carries_NAMES_not_only_counts():
    """"2 net disagreements" cannot be looked into. The whole cost of that evening was that the
    affected symbols could not be named without grep."""
    rows = position_truth(
        [_pos("GMAB.XNAS", "BCTROT-004", 59), _pos("CGAU.XNYS", "BCTROT-004", 80)],
        [_report("GMAB.XNAS", 0), _report("CGAU.XNYS", 0)],
    )
    s = truth_summary(rows)
    assert s["net_disagrees"] == 2
    assert s["net_disagrees_names"] == ["CGAU.XNYS", "GMAB.XNAS"]


def test_the_summary_separates_UNREADABLE_from_DISAGREEING():
    """Otherwise a venue outage reads as a book-wide fault, and the next real one is ignored."""
    s = truth_summary(position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], None))
    assert s["venue_unreadable"] == 1 and s["net_disagrees"] == 0


# ==================================================================================================
# Stuck instruments — measured from the symptom, because Nautilus emits no event for its give-up
# ==================================================================================================
def test_ONE_disagreeing_read_is_not_yet_stuck():
    """A single read can straddle a fill in flight. Flagging those is the noise that gets a real
    alarm switched off."""
    from api.book_truth import DisagreementStreaks

    st = DisagreementStreaks()
    st.observe(position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], [_report("GMAB.XNAS", 0)]))
    assert st.stuck() == []


def test_TWO_CONSECUTIVE_disagreeing_reads_are_stuck():
    from api.book_truth import DisagreementStreaks

    st = DisagreementStreaks()
    rows = position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], [_report("GMAB.XNAS", 0)])
    st.observe(rows)
    st.observe(rows)
    assert st.stuck() == ["GMAB.XNAS"]


def test_AGREEMENT_CLEARS_the_streak():
    """Otherwise the surface becomes a history of everything that ever disagreed, which is the same
    silence at the other extreme."""
    from api.book_truth import DisagreementStreaks

    st = DisagreementStreaks()
    bad = position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], [_report("GMAB.XNAS", 0)])
    st.observe(bad); st.observe(bad)
    assert st.stuck() == ["GMAB.XNAS"]
    st.observe(position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], [_report("GMAB.XNAS", 59)]))
    assert st.stuck() == []


def test_an_UNREADABLE_VENUE_NEITHER_ADVANCES_NOR_CLEARS():
    """THE ONE THAT MATTERS. A read that did not happen is not evidence of agreement — clearing on it
    would let a venue outage silently reset every alarm — and it is not evidence of disagreement."""
    from api.book_truth import DisagreementStreaks

    st = DisagreementStreaks()
    bad = position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], [_report("GMAB.XNAS", 0)])
    st.observe(bad)
    st.observe(position_truth([_pos("GMAB.XNAS", "BCTROT-004", 59)], None))   # venue unreadable
    assert st.stuck() == [], "an unreadable read advanced the streak"
    st.observe(bad)
    assert st.stuck() == ["GMAB.XNAS"], "an unreadable read wiped a real streak"
