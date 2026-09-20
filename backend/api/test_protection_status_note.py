"""A naked-position warning must be true, or an operator learns to scroll past it.

WHY THIS FILE EXISTS
--------------------
On 2026-08-19 a PEAK arm on LFXX failed and the UI said, in red:

    "the venue did not confirm the cancel within the timeout, so nothing was placed — 2 resting exit
     order(s) were already cancelled, so THIS POSITION MAY NOW BE UNPROTECTED. Check the broker before
     retrying."

A `PROT-SELL-LFXX` covering the full 400 shares was resting at the broker the entire time. The message
was emitted whenever any cancel had gone out, without ever asking whether anything was still resting —
and it goes stale within a minute anyway, because the protection reconciler re-arms on its next tick.

Two costs. It asks the operator to do a check the engine already does on every exit path. And it is the
message that must be BELIEVED on the day a position really is naked; one that cries wolf is one nobody
reads. Same reasoning as #269's divergence detector: a false alarm on the healthy case is not a lesser
problem than a missing alarm.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal


class _Host:
    """Carries only what the note reads. A fourth attribute raises rather than passing quietly."""

    def __init__(self, covered):
        self._covered = covered

    async def _reducing_qty_for_exit(self, instrument_id, strategy_id, side):
        if isinstance(self._covered, Exception):
            raise self._covered
        return self._covered


def _note(covered, *, cleared_count=2, reason="the venue did not confirm the cancel"):
    from api.engine_node import UiFeedStrategy

    host = _Host(covered)
    return asyncio.run(
        UiFeedStrategy._protection_status_note(
            host, reason, cleared_count, "LFXX.XNAS", "MANUAL-001", "LONG"
        )
    )


def test_a_still_protected_position_is_not_called_unprotected():
    """The LFXX case, verbatim. 400 shares were covered while the operator was told to check the broker."""
    note = _note(Decimal(429))
    assert "UNPROTECTED" not in note.upper(), f"still claims the position may be naked: {note}"
    assert "429" in note, "the note does not say how much is actually covered"
    assert "protected" in note.lower()


def test_a_genuinely_naked_position_says_so_plainly():
    """The fixture must be able to go the other way, or the test above only proves the string changed."""
    note = _note(Decimal(0))
    assert "UNPROTECTED" in note, f"a truly naked position was not flagged: {note}"
    assert "re-arms automatically" in note, (
        "the operator is told it is naked but not that the engine restores it — which is the difference "
        "between 'act now' and 'watch it'"
    )


def test_an_unreadable_broker_keeps_the_conservative_warning():
    """Fail safe. Not knowing is not the same as knowing it is covered."""
    note = _note(RuntimeError("alpaca unreachable"))
    assert "MAY NOW BE UNPROTECTED" in note
    assert "could not be read" in note, "the note hides WHY it is uncertain"


def test_nothing_cancelled_means_no_warning_at_all():
    """A failure that removed nothing must not imply protection was touched."""
    note = _note(Decimal(429), cleared_count=0)
    assert note == "the venue did not confirm the cancel"


def test_the_note_never_raises_over_the_failure_it_reports():
    """It runs inside an error path. A throw here replaces a useful message with a traceback."""
    assert _note(RuntimeError("boom"))
