"""The raw and typed status vocabularies must agree about what is RESTING (#387, #430).

`is_resting` is the single definition of "still live at the venue" and its docstring records that this
predicate once existed twice and disagreed in BOTH directions, each a live defect. It is fed from two
places now:

    RAW    Alpaca's own lowercase name           is_resting("calculated")
    TYPED  Nautilus's OrderStatus.name, via      is_resting(report.order_status.name)
           _ALPACA_TO_ORDER_STATUS

So the mapping is a SECOND derivation of the same fact, one layer up, and it is free to disagree with
the set. It does: `calculated` maps to ACCEPTED, and ACCEPTED is in `_OPEN_STATUSES`.

    raw:    is_resting("calculated")   -> False   correct
    typed:  is_resting("ACCEPTED")     -> True    a completed stop reads as live

`calculated` is Alpaca POST-completion — settlement bookkeeping on a finished order, the sibling of
`done_for_day`, which correctly maps to CANCELED. Counting it as protection is the SILENCING direction:
the reconciler reads the position as covered by a stop that can never fire again and does not arm one.
The raw path was fixed for exactly this in #387 and the docstring says so. The typed path, which #430
moves protection onto, brings it straight back.
"""

from __future__ import annotations

import pytest

from api.protection import is_resting
from api.providers.alpaca.exec_client import _ALPACA_TO_ORDER_STATUS

#: Alpaca states that are POST-COMPLETION: the order will not execute again. Naming them here rather
#: than deriving them is deliberate — the whole point is to compare two independent derivations, and a
#: list computed FROM either one of them could not disagree with it.
_TERMINAL_AT_ALPACA = ("filled", "canceled", "expired", "rejected", "done_for_day", "calculated",
                       "replaced")


def test_the_fixture_names_states_the_mapping_ACTUALLY_contains():
    """Fixture property first: a name absent from the map would make its row vacuous, and this test
    would then pass while proving nothing about the status it claims to cover."""
    missing = [s for s in _TERMINAL_AT_ALPACA if s not in _ALPACA_TO_ORDER_STATUS]
    assert missing == [], f"{missing} are not in the mapping — this test is not covering them"


@pytest.mark.parametrize("alpaca_status", _TERMINAL_AT_ALPACA)
def test_a_completed_order_is_not_RESTING_in_either_vocabulary(alpaca_status):
    """THE DISAGREEMENT IS THE BUG. Both derivations answer one question — is this order still holding
    shares — and an order that has completed for the day holds none in either.

    `calculated` is the one that failed: raw False, typed True."""
    raw = is_resting(alpaca_status)
    typed = is_resting(_ALPACA_TO_ORDER_STATUS[alpaca_status].name)
    assert raw is False, f"the raw path counts {alpaca_status!r} as resting"
    assert typed is False, (
        f"{alpaca_status!r} maps to {_ALPACA_TO_ORDER_STATUS[alpaca_status].name} which IS resting — "
        f"a completed order reads as live protection, so the reconciler will not arm a stop"
    )


def test_a_genuinely_LIVE_order_is_resting_in_both():
    """The discriminating half. Without it, mapping every status to something terminal passes the test
    above and reports an entire book naked — the opposite defect, and the one that arms duplicate stops
    on every position."""
    for live in ("new", "accepted", "held", "partially_filled", "pending_cancel"):
        assert is_resting(live) is True, f"{live} is no longer resting in the raw vocabulary"
        assert is_resting(_ALPACA_TO_ORDER_STATUS[live].name) is True, f"{live} lost its typed liveness"


#: KNOWN DIVERGENCES, named with the reason rather than forced into agreement by a guess.
#:
#: Unlike `calculated`, neither of these is post-completion, so "not resting" is not obviously right —
#: and neither is "resting". Both directions cost something and the two costs land on different paths:
#:
#:   stopped    Alpaca: "a trade is guaranteed for the order... but has not yet occurred". That reads
#:              LIVE, and the raw set omitting it looks like the error. But nobody here has seen one on
#:              this account, so changing the raw set would be guessing at venue semantics — the thing
#:              that produced #245.
#:   suspended  "not eligible for trading" and not cancelled. Whether the venue still RESERVES the
#:              shares decides the answer, and that is unverifiable by inspection.
#:
#: Counting a dead order as protection is the silencing direction (no stop is armed). Counting a live
#: one as dead arms a duplicate and reserves shares twice. Neither is safe by default, so this is a
#: MEASUREMENT, not a decision: probe the venue with one of each and record the number, the way
#: `scripts/probe_trailing_replace.py` settled Alpaca's replace semantics.
KNOWN_DIVERGENT = frozenset({"stopped", "suspended"})


def test_the_divergence_list_is_EXACTLY_what_diverges():
    """A stale exemption is worse than none — it waves through the next real disagreement. This fails
    both if a listed status stops diverging (delete it) and if a new one starts (decide it)."""
    actual = {
        s for s in _ALPACA_TO_ORDER_STATUS
        if is_resting(s) is not is_resting(_ALPACA_TO_ORDER_STATUS[s].name)
    }
    assert actual == KNOWN_DIVERGENT, (
        f"divergence set changed: now {sorted(actual)}, listed {sorted(KNOWN_DIVERGENT)}"
    )


def test_EVERY_mapped_status_agrees_across_the_two_vocabularies():
    """Aimed at the CLASS rather than at `calculated`. Any future status whose mapping disagrees with
    the raw set fails here, in either direction — which is what the single-definition rule in
    `is_resting`'s docstring is actually asking for, one layer up."""
    disagree = {
        s: (is_resting(s), _ALPACA_TO_ORDER_STATUS[s].name)
        for s in _ALPACA_TO_ORDER_STATUS
        if s not in KNOWN_DIVERGENT
        and is_resting(s) is not is_resting(_ALPACA_TO_ORDER_STATUS[s].name)
    }
    assert disagree == {}, (
        f"raw and typed vocabularies disagree about: {disagree}. Either fix the mapping, or add the "
        f"status to KNOWN_DIVERGENT with the reason it cannot be decided by inspection"
    )
