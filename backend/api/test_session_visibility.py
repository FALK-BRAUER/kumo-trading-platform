"""A strategy that has stopped deciding must say so where an operator already looks.

WHY THIS FILE EXISTS
--------------------
MOMENTUM-002 stopped deciding on 2026-08-17. It was found on 2026-08-18 **by a human reading container
logs**, after the book had been stale for two sessions and the day was down $1,178.

Everything needed to raise the alarm was already recorded. `exec_action_log` held a `risk` row per failed
session, naming the `TypeError`, on both days. Nothing read them.

WHY THE OBSERVER HOOK IS NOT THE GAP
------------------------------------
`build_session_observer` IS wired — `momentum.py` passes it as `on_result`. It cannot help, for a reason
worth stating precisely: `on_result` fires on a RESULT, and a session that raises never produces one. The
hook covers the path where a session ran and declined; the gap is the path where it never ran at all.
Those are the two states this whole system keeps confusing, and #349 named the wrong mechanism.

WHAT IS ASSERTED HERE
---------------------
`GET /strategies` is the screen an operator already opens before touching a target — the endpoint's own
docstring says it is "one place that answers what strategies exist". A strategy that has not decided since
2026-08-14 must be visible THERE, not only in a log line nobody reads.

Deliberately not "an alert fires": an alert is a second mechanism that can itself be inert, which is the
category this system keeps producing. The API field is the fact; alerting consumes it.

#349, #199, build-spec item 5.
"""

from __future__ import annotations

import ast
import pathlib

_APP = pathlib.Path(__file__).parent / "app.py"


def _strategies_endpoint_source() -> str:
    source = _APP.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "get_strategies":
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("get_strategies no longer exists — this test is blind and must be rewritten")


def test_the_strategies_screen_reports_when_a_strategy_last_decided():
    """Red today. The endpoint reports capital and says nothing about whether the strategy is alive.

    Two sessions died silently while this endpoint cheerfully reported `target 20,000 · actual 67,111`
    — a picture of a healthy, funded strategy that had not made a decision in two days.
    """
    src = _strategies_endpoint_source()
    assert "last_decision" in src or "last_successful_session" in src, (
        "`GET /strategies` reports a strategy's capital but not whether it is still deciding — so a "
        "strategy dead for two sessions renders identically to a healthy one, which is exactly how "
        "2026-08-17 and 08-18 were missed"
    )


def test_it_reports_how_many_scheduled_slots_have_been_missed():
    """A date alone is not enough — an operator has to know a date is WRONG to act on it.

    "last decided 2026-08-14" is only alarming if you know today's date and the schedule. A count of
    missed slots is self-evidently wrong at a glance, which is the property that makes a screen an alarm
    rather than a record.
    """
    src = _strategies_endpoint_source()
    assert "missed" in src or "sessions_since" in src, (
        "no missed-slot count — a bare last-decided date requires the reader to already suspect a "
        "problem, which is precisely what nobody did for three days"
    )


def test_a_strategy_that_has_never_decided_is_distinguishable_from_one_that_has_stopped():
    """The ambiguity this system keeps reproducing, pinned here before it is built.

    BCTROT-004 has never decided; MOMENTUM-002 decided and stopped. Both would render as "no recent
    decision" under a naive implementation, and they demand opposite reactions: one is unfunded and
    unarmed by design, the other is a live strategy that has failed.
    """
    src = _strategies_endpoint_source()
    assert "never" in src.lower() or "None" in src, (
        "nothing distinguishes never-decided from stopped-deciding — an unarmed strategy and a dead one "
        "must not render the same, or the screen trains its reader to ignore it"
    )


def test_the_fixture_can_see_the_endpoint_it_judges():
    """An assertion over an empty string passes for the wrong reason."""
    assert _strategies_endpoint_source().strip(), "the endpoint source came back empty — vacuous"
