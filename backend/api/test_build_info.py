"""#329 — the image must be able to name its own commit.

The defect these pin: on 2026-08-18 the running engine was 59 lines behind `main`, and NOTHING in the
process could say so. Two diagnoses were built on the assumption that container == repo. So the tests here
care about one property above all: **an unstamped or blank build must say `unknown`, never something that
looks like an answer.** A wrong stamp is worse than no stamp, because a wrong stamp gets believed.
"""

from __future__ import annotations

import pytest

from api.build_info import UNKNOWN, build_stamp, build_stamp_line, is_stamped


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Every test states its own environment. Without this the SUITE's own result would depend on whether
    the developer's shell happened to export KUMO_GIT_SHA — a test whose outcome depends on ambient state
    is not pinning anything."""
    monkeypatch.delenv("KUMO_GIT_SHA", raising=False)
    monkeypatch.delenv("KUMO_STRATEGIES_SHA", raising=False)


def test_an_unstamped_image_says_unknown_rather_than_guessing():
    assert build_stamp() == {"git_sha": UNKNOWN, "strategies_sha": UNKNOWN}
    assert is_stamped() is False


def test_the_stamp_is_reported_when_the_build_set_it(monkeypatch):
    monkeypatch.setenv("KUMO_GIT_SHA", "f75934d")
    monkeypatch.setenv("KUMO_STRATEGIES_SHA", "3b042ce")
    assert build_stamp() == {"git_sha": "f75934d", "strategies_sha": "3b042ce"}
    assert is_stamped() is True


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t"])
def test_a_blank_stamp_is_unknown_not_an_empty_string(monkeypatch, blank):
    """`docker run -e KUMO_GIT_SHA=` sets the var to EMPTY, it does not unset it.

    An empty string rendered in the UI is an empty box, which a human reads as "this build has no info"
    — indistinguishable from "the field is broken". Both must collapse to the same explicit `unknown`.
    """
    monkeypatch.setenv("KUMO_GIT_SHA", blank)
    assert build_stamp()["git_sha"] == UNKNOWN
    assert is_stamped() is False


def test_the_two_shas_are_independent(monkeypatch):
    """The cockpit tree and the kumo-strategies checkout are separate build contexts and move separately.

    Reporting one for the other would recreate exactly the ambiguity #329 exists to remove: during the
    2026-08-18 incident "which strategies code is in there" was as unanswerable as the cockpit commit.
    """
    monkeypatch.setenv("KUMO_GIT_SHA", "aaaaaaa")
    stamp = build_stamp()
    assert stamp["git_sha"] == "aaaaaaa"
    assert stamp["strategies_sha"] == UNKNOWN, "a missing strategies sha must not inherit the cockpit's"


def test_a_dirty_tree_is_carried_through_verbatim(monkeypatch):
    """`make build-paper` appends `-dirty` when the working tree has uncommitted changes.

    That suffix is the whole point: an image built from edited-but-uncommitted source is NOT the commit it
    names, and must not be reportable as clean. Pinned here so nobody "tidies" the stamp by stripping
    anything that is not a bare hex sha.
    """
    monkeypatch.setenv("KUMO_GIT_SHA", "f75934d-dirty")
    assert build_stamp()["git_sha"] == "f75934d-dirty"
    assert is_stamped() is True


def test_the_log_line_names_both_trees(monkeypatch):
    """The startup line is the first thing read when a container misbehaves — it must carry both shas.

    Asserting the VALUES appear, not the exact prose, so rewording the line does not fail the test while
    dropping a sha still does.
    """
    monkeypatch.setenv("KUMO_GIT_SHA", "f75934d")
    monkeypatch.setenv("KUMO_STRATEGIES_SHA", "3b042ce")
    line = build_stamp_line()
    assert "f75934d" in line
    assert "3b042ce" in line


def test_the_unstamped_log_line_still_renders():
    """A crash-looping unstamped container must still produce a readable line — this is exactly when the
    question is asked, so the reporter must not itself depend on the stamp existing."""
    assert UNKNOWN in build_stamp_line()
