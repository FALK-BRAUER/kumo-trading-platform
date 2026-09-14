"""The broker's cost basis wins when it contradicts the engine's (#370).

WHY THIS FILE EXISTS
--------------------
WHD reported **+$263.84 unrealized while Alpaca said +$9.52** — $254.32 of gain that did not exist.
Measured 2026-08-19 16:17 UTC:

    ENGINE  avg_px_open 71.78   qty 136   MOMENTUM-002   unrealized +263.84
    VENUE   avg_entry   73.68   qty 136                  unrealized   +9.52

Reconciliation DETECTED it and left it:

    WARN ExecEngine: Position avg_px mismatch for WHD.XNYS after reconciliation:
      internal=71.78, venue=73.68, diff=1.90 (2.5787%).
      This indicates incomplete reconciliation data from the venue.

The venue was correct and the engine was wrong, so the message pointed every reader at Alpaca. The cause
was attribution: an operator flatten submitted under MANUAL-001 opened a phantom SHORT rather than
closing MOMENTUM-002's LONG, so MOMENTUM's position was never touched and kept its original entry while
the venue's basis reset on the round trip.

That cause is fixed — the flatten now routes to the owning strategy — but a basis already diverged stays
diverged until the position closes, and nothing corrected it. CLAUDE.md is unambiguous about which side
wins: the broker is the only hard reconciliation anchor.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.engine_node import UiFeedStrategy

WHD = "WHD.XNYS"
ENGINE_BASIS = 71.78
VENUE_BASIS = 73.68
QTY = 136
MARK = 73.75  # the close that produced the two numbers above


def _strat(broker: dict | None):
    """The double CARRIES the warn latch, because production initialises it in `__init__` (#386).

    A double missing it would raise `AttributeError` from inside the function under test — failing for
    the wrong reason, and in a way that says nothing about whether the rate limiting works.
    """
    warnings: list[str] = []
    s = SimpleNamespace(
        _broker_avg_entry=broker,
        BASIS_DIVERGENCE_TOL=UiFeedStrategy.BASIS_DIVERGENCE_TOL,
        _basis_divergence_warned=set(),
        log=SimpleNamespace(warning=warnings.append),
    )
    s.warnings = warnings
    s._marking_basis = lambda iid, avg: UiFeedStrategy._marking_basis(s, iid, avg)
    return s


def test_the_fixture_reproduces_the_divergence_that_was_measured():
    """THE FIXTURE MUST BE ABLE TO SHOW THE BUG, or everything below asserts into a vacuum.

    A test that pins "the number is right" against inputs where both bases agree cannot fail. So the gap
    is asserted FIRST, in the same units the incident was reported in: marking 136 shares against the
    engine's basis rather than the broker's manufactures $254.32 that does not exist.
    """
    phantom = (MARK - ENGINE_BASIS) * QTY - (MARK - VENUE_BASIS) * QTY
    assert round(phantom, 2) == 258.40, "the fixture no longer reproduces a material divergence"
    assert abs(VENUE_BASIS - ENGINE_BASIS) / VENUE_BASIS > UiFeedStrategy.BASIS_DIVERGENCE_TOL, (
        "the fixture's gap is inside the tolerance — the contested branch is unreachable and every "
        "assertion below would pass on a build that never checks the broker at all"
    )


def test_the_broker_wins_when_the_two_disagree():
    basis, contested = _strat({WHD: VENUE_BASIS})._marking_basis(WHD, ENGINE_BASIS)
    assert basis == VENUE_BASIS, (
        "the engine kept marking against its own basis — this is the $254.32 of phantom gain, exactly "
        "as WHD reported it"
    )
    assert contested is True


def test_agreement_is_not_reported_as_a_dispute():
    """A flag that fires on the last decimal is a flag operators learn to scroll past."""
    basis, contested = _strat({WHD: ENGINE_BASIS + 0.0001})._marking_basis(WHD, ENGINE_BASIS)
    assert basis == ENGINE_BASIS
    assert contested is False


def test_an_unasked_broker_is_not_agreement():
    """Three-state, the same as `broker_protected`.

    `False` here would claim the bases were compared and matched. Nothing was compared. That distinction
    is the whole reason the WHD number looked trustworthy for a full session.
    """
    basis, contested = _strat(None)._marking_basis(WHD, ENGINE_BASIS)
    assert basis == ENGINE_BASIS
    assert contested is None, "an unchecked basis must not render as a verified one"


def test_a_broker_that_holds_no_basis_for_the_symbol_is_not_a_dispute():
    basis, contested = _strat({})._marking_basis(WHD, ENGINE_BASIS)
    assert basis == ENGINE_BASIS
    assert contested is False


def test_both_marking_paths_ask_the_same_question():
    """TWO DERIVATIONS OF ONE FACT.

    Unrealized P&L is computed in two places — the cycle DTOs the managed book reads and the POSITION
    rows — and each reached for `avg_px_open` independently before this. A fix applied to one would have
    left the other publishing the number it was meant to remove: the same shape that has already bitten
    leash validation and manager-armed derivation.

    Asserted on the source because the property is WHICH FUNCTION each path calls, which is precisely the
    wiring a test of `_marking_basis` alone cannot see.
    """
    import ast
    import inspect
    import textwrap

    for name in ("_mark_cycle_financials", "_enrich_position_financials"):
        src = textwrap.dedent(inspect.getsource(getattr(UiFeedStrategy, name)))
        called = {
            (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
        }
        assert "_marking_basis" in called, (
            f"{name} derives a cost basis without going through _marking_basis — the two marking paths "
            f"can now disagree about the same position"
        )


def test_the_contested_flag_survives_a_position_with_no_price():
    """The flag is not a fact about price, and must not wait for one.

    FOUND BY QUERYING THE RUNNING ENGINE, NOT BY A TEST. The first cut set `basis_contested` after the
    "no mark available, skip this row" continue, so with the market closed and no bars streaming every
    position published `basis_contested: null` — "nobody has checked" — for a basis that had been checked
    and found wrong. The unit was correct; the placement was not, which is the shape CLAUDE.md calls
    testing the seam rather than the unit.
    """
    from api.engine_node import UiFeedStrategy

    class _DTO:
        instrument_id = WHD
        avg_px_open = ENGINE_BASIS
        quantity = float(QTY)
        side = "LONG"
        is_capital_deployed = True
        last_px = None
        market_value = None
        unrealized_pl = None
        unrealized_plpc = None
        venue_avg_px = None
        basis_contested = None

    strat = _strat({WHD: VENUE_BASIS})
    strat._last_close = {}  # exactly what the engine holds outside market hours
    dto = _DTO()
    UiFeedStrategy._mark_cycle_financials(strat, [dto])

    assert dto.basis_contested is True, (
        "an unpriced position reported its basis as unchecked while the broker was contradicting it"
    )
    assert dto.venue_avg_px == VENUE_BASIS
    assert dto.unrealized_pl is None, "there is no mark, so there is no P&L to publish"


# ==================================================================================================
# #386 — the WARN must not repeat once per feed tick, and must not go silent either.
#
# `_marking_basis` runs once per position per tick (~2s). WHD had been contested since 2026-08-19, so
# it emitted a line every two seconds indefinitely and drowned the log being used to diagnose #385 —
# the useful lines were one each against ~30 of these per minute.
#
# This file's own codebase fixed exactly this once before: `_venue_has_account` became 1200 of the last
# 1287 log lines (93%), "drowning the only signal that tells a working strategy from a stuck one". And
# `_equity_estimate_warned`, TWO FUNCTIONS AWAY in the same class, already had the right shape for #382.
# Reintroducing it next door is why these tests aim at the class rather than at WHD.
# ==================================================================================================

def test_a_contested_basis_warns_ONCE_not_once_per_tick():
    s = _strat({WHD: VENUE_BASIS})

    for _ in range(30):  # ~one minute of feed ticks
        basis, contested = s._marking_basis(WHD, ENGINE_BASIS)

    assert len(s.warnings) == 1, f"{len(s.warnings)} warnings for one divergence episode"
    # And the divergence is still REPORTED on every call — only the logging is rate-limited. A
    # contested basis that went quiet everywhere would be the opposite defect, and worse.
    assert contested is True
    assert basis == VENUE_BASIS


def test_TWO_contested_instruments_are_TWO_facts():
    """The latch is per instrument, not global.

    A single bool would silence the second symbol because the first spoke — hiding a divergence
    entirely rather than deduplicating one. That is the failure mode a rate limiter is most likely to
    introduce, so it is pinned by name.
    """
    other = "CVS.XNYS"
    s = _strat({WHD: VENUE_BASIS, other: 200.0})

    for _ in range(10):
        s._marking_basis(WHD, ENGINE_BASIS)
        s._marking_basis(other, 100.0)

    assert len(s.warnings) == 2
    assert any(WHD in w for w in s.warnings)
    assert any(other in w for w in s.warnings)


def test_a_divergence_that_HEALS_and_returns_warns_AGAIN():
    """A latch that never clears reports the second episode as the first still running.

    Agreement is the reset. The second divergence is new information — a basis that healed and diverged
    again is a different event from one that never healed, and the operator must be able to tell them
    apart.
    """
    s = _strat({WHD: VENUE_BASIS})
    s._marking_basis(WHD, ENGINE_BASIS)
    assert len(s.warnings) == 1

    s._broker_avg_entry[WHD] = ENGINE_BASIS          # bases agree — episode over
    _, contested = s._marking_basis(WHD, ENGINE_BASIS)
    assert contested is False
    assert WHD not in s._basis_divergence_warned, "agreement must clear the latch"

    s._broker_avg_entry[WHD] = VENUE_BASIS           # diverges again
    s._marking_basis(WHD, ENGINE_BASIS)
    assert len(s.warnings) == 2, "a NEW episode is new information and must be logged"


def test_the_warn_says_it_will_not_repeat():
    """Silence must be distinguishable from absence.

    An operator who sees one line and then nothing has to know whether the divergence ended or the log
    is deduplicated. The line says which, and points at the badge that does carry it continuously.
    """
    s = _strat({WHD: VENUE_BASIS})
    s._marking_basis(WHD, ENGINE_BASIS)

    assert "ONCE" in s.warnings[0]
    assert "contested" in s.warnings[0]


def test_production_INITIALISES_the_latch():
    """THE SEAM. The rate limiting is correct and would be dead on arrival if `__init__` did not create
    the set — every call would raise `AttributeError` inside the marking path, which runs on every
    position on every tick. A green test against a double that carries it says nothing about that."""
    import inspect

    src = inspect.getsource(UiFeedStrategy.__init__)
    assert "_basis_divergence_warned" in src, "the latch must exist on the real object, not just the double"
