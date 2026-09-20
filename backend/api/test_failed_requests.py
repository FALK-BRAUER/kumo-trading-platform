"""A failing request must become standing state (#757 follow-up)."""

from __future__ import annotations

from api.failed_requests import FailedRequests


def test_repeated_failures_of_ONE_subject_COALESCE_into_one_row_with_a_count():
    """275 identical refusals are ONE condition, not 275 events. A surface that grew per occurrence
    would be scrolled past — the same silence at the other extreme."""
    f = FailedRequests()
    for _ in range(275):
        f.record("market_data_subscription", "ALKS.XNAS", "requires additional subscription", ts=1)
    assert len(f) == 1
    assert f.as_rows()[0]["count"] == 275
    assert f.total == 275


def test_DIFFERENT_subjects_stay_SEPARATE():
    """132 symbols refused once each is 132 rows — an operator needs to know WHICH."""
    f = FailedRequests()
    for sym in ("ALKS.XNAS", "CGAU.XNYS", "RDN.XNYS"):
        f.record("market_data_subscription", sym, "no permissions", ts=1)
    assert len(f) == 3


def test_the_VENUE_S_OWN_WORDS_are_kept():
    """"request failed" cannot be acted on by anyone. "requires additional subscription for API" can."""
    f = FailedRequests()
    f.record("market_data_subscription", "ALKS.XNAS", "requires additional subscription for API", ts=1)
    assert "additional subscription" in f.as_rows()[0]["note"]


def test_the_LATEST_note_wins():
    """A refusal whose reason changes — a permission error becoming a pacing limit — must report what
    is true now, not what was true first."""
    f = FailedRequests()
    f.record("md", "X", "no permissions", ts=1)
    f.record("md", "X", "pacing violation", ts=2)
    assert f.as_rows()[0]["note"] == "pacing violation"


def test_a_request_that_STARTS_SUCCEEDING_stops_being_a_condition():
    """Otherwise the first failure of the day is reported until restart and the surface becomes a
    history rather than a state — which is how a real condition gets lost among stale ones."""
    f = FailedRequests()
    f.record("md", "X", "no permissions", ts=1)
    f.clear("md", "X")
    assert len(f) == 0 and f.as_rows() == []


def test_rows_are_ordered_WORST_FIRST():
    """An operator reads the top of the list."""
    f = FailedRequests()
    f.record("md", "rare", "n", ts=1)
    for _ in range(9):
        f.record("md", "common", "n", ts=2)
    assert [r["subject"] for r in f.as_rows()] == ["common", "rare"]


def test_an_EMPTY_recorder_is_EMPTY_not_absent():
    """Nothing failing is a real answer and must be distinguishable from never having asked — the
    whole reason this exists."""
    f = FailedRequests()
    assert f.as_rows() == [] and f.total == 0 and len(f) == 0
