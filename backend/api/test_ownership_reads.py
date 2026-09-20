"""Ownership reads are strategy-scoped (#437 rule 1), enforced by AST.

Four defects in two days had one shape: code reached for the ACCOUNT-level view where the
STRATEGY-level view was meant, and nothing made that a mistake. TECHIVOL's `_held_symbols` listed
eight foreign exits and its SELL sizing would have been sized to every share the account held of a
shared symbol. It submitted nothing — luck, not design.

WHY THIS IS NOT A METHOD-NAME BAN. `Cache.positions_open()` takes a NATIVE `strategy_id` filter, so
the method is not the problem and banning it would be bypassed by aliasing. It also has legitimate
account-level callers here — reconciliation, subscription bookkeeping, the account snapshot.

WHY IT IS NOT AN ARGUMENT RULE EITHER, which is where the first draft went wrong. Two correct readers
scope in PYTHON rather than with the kwarg:

    for pos in self.cache.positions_open():
        if str(pos.strategy_id) == strategy_id and ...

`_opposite_side_qty` and `consumer.strategy_position` both do this and both are right. A rule demanding
`strategy_id=` flags them, and a guard that cries wolf gets switched off — which is how kumo-trading-strategies'
first module-wide attempt died.

THE PREDICATE THAT SURVIVES BOTH: a function that reads positions unscoped AND never mentions
`strategy_id` anywhere is BLIND to ownership. It cannot be making a per-strategy decision, because it
has no way to tell one strategy from another. Every such function today is legitimately account-level
and is named below; anything new must justify itself by being added here.
"""

from __future__ import annotations

import ast
import pathlib

_FILES = ("engine_node.py", "external_activity.py", "consumer.py", "realized.py")

#: ACCOUNT-LEVEL BY DESIGN. Each of these reads the whole book and does not distinguish owners,
#: because the question it answers is genuinely about the account:
#:
#:   on_start / _after_definition   subscription bookkeeping — what must the node subscribe to
#:   _reconcile_symbols             reconciliation — the broker's net is the anchor, ownership is not
#:   _account_id                    lifts the account id off any position; owner is irrelevant
#:   _mark_px                       marking prices, per instrument
#:   _realized_windows              realized per window across the account (#846) — the per-strategy
#:                                  split is its OUTPUT (legs carry strategy_id), not its input
#:   _publish_account               the account snapshot
#:
#: Adding a name here is a claim that the function answers an ACCOUNT question, not a "what do I own"
#: question. That claim is cheap to make and should be made deliberately — which is the point of a
#: default-deny list rather than a heuristic.
ACCOUNT_LEVEL = frozenset({
    # "can we price what we HOLD" is an account question by construction (#757). A price is a
    # property of the INSTRUMENT, not of who owns it — scoping this per lane would report the same
    # unpriceable symbol once per holder and, worse, would hide an unpriceable position held only by
    # EXTERNAL, which is exactly the book that most needs it visible.
    "_unpriced_positions",
    # The cache-vs-venue comparison is an ACCOUNT question by construction (#758): broker net is the
    # only anchor the venue can verify (ADR 0001), and the venue has no opinion whatsoever on the
    # per-lane split. Scoping this read per lane would make the one number the broker CAN adjudicate
    # unrepresentable. The split is not discarded — `position_truth` groups by strategy_id downstream
    # and reports over-claiming separately, precisely because it must never be compared to the venue.
    "_compute_book_truth",
    # PLANNING the ownership repair is an ACCOUNT question by construction (#771): a contra pair is
    # ONE LANE'S LONG against ANOTHER LANE'S SHORT, so scoping the read to a single strategy makes
    # the pair -- the entire subject -- unrepresentable. Ownership is not ignored here, it is the
    # OUTPUT: `plan_contra_closes` groups by instrument and reads `strategy_id` off both legs to
    # decide which lane gives back what.
    "build_book_repair",
    # Cancelling stops that can never protect is an ACCOUNT-WIDE sweep by construction (#748): it
    # compares EVERY resting protective order against EVERY lane's holdings, so scoping the read to
    # one lane would make the orphan — a stop whose lane holds nothing — unrepresentable. Ownership
    # is not discarded: `reconcile_protection` groups by strategy_id and refuses outright on an
    # unreadable book.
    "_cancel_unprotectable_stops",
    # The repair pass reads the WHOLE book — open and closed, every lane — because a repair pair
    # spans two lanes by definition and a leg aimed at a closed position must be caught rather than
    # silently opening a new one at that id. Ownership is not discarded: `verify_and_book` checks
    # each leg's lane, direction and size against the position it names, and refuses the pair whole.
    "run_book_repair",
    "on_start", "_after_definition", "_reconcile_symbols", "_account_id", "_mark_px",
    "_realized_windows", "_publish_account",
    # The ACCOUNT's standing unrealized (#596) — the Δ half of NET for the "All" window, published on
    # the account frame beside equity and cash. Ownership is deliberately not consulted: the panel's
    # per-strategy cells sum to this number, and scoping it to one strategy would silently drop the
    # unclaimed book, which is the same loss #310 produced for DEPLOYED.
    "_standing_unrealized_total",
    # NET position for the share-availability derivation (#641): |net| − Σ resting reduces replaces
    # Alpaca's `qty_available`, and the venue reserves per ACCOUNT per symbol — it knows nothing about
    # our StrategyId, so a strategy-scoped net cannot answer an account-scoped constraint. Same
    # reasoning `_venue_reducing_orders` documents for the order half of the same arithmetic. The cache
    # read here is the LAST fallback (typed report first, then REST), and it deliberately sums every
    # strategy's open position on the instrument.
    "_venue_net_position",
    # Resolving a VENUE ticker to one of several loaded listings (#862) is an ACCOUNT question: the
    # broker's order and position reports name the account's contract, never our StrategyId, so the
    # tiebreak "which listing does the account hold" must see every lane's position. It reads only
    # `instrument_id` off each position; ownership is neither consulted nor discarded, and the
    # callers (drift, protected set, stop prices) apply their own lane scoping to the resolved id.
    "_resolve_instrument_id",
    # The flows/qty walk for the window identity is an ACCOUNT-WIDE read by construction (#1072 b):
    # it reads EVERY lane's orders and open positions in ONE pass and groups by `strategy_id` on the
    # way out — the same shape as `_compute_book_truth`. Ownership is the OUTPUT (`net_qty`,
    # `qty_now`, `touched` are all keyed by lane); a per-lane read would need N passes over one
    # cache and put the qty invariant's two terms on different moments, which is the skew it removes.
    "_lane_flows",
})


def _blind_readers_in(source: str) -> list[str]:
    """The scan, over SOURCE TEXT so it can be driven with cases the repo does not contain yet.

    No call site uses the native `strategy_id=` kwarg today, so the branch that excludes it is dead
    against real files — deleting it left the whole suite green. A rule with an untestable branch is a
    rule with an untested branch, and this is how it gets exercised."""
    out = []
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        unscoped = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) in ("positions_open", "positions")
            and not any(kw.arg == "strategy_id" for kw in n.keywords)
        ]
        if not unscoped:
            continue
        mentions = any(
            (isinstance(n, ast.Name) and n.id == "strategy_id")
            or (isinstance(n, ast.Attribute) and n.attr == "strategy_id")
            for n in ast.walk(fn)
        )
        if not mentions:
            out.append(fn.name)
    return out


def _blind_readers(files=_FILES) -> list[str]:
    """Functions that read the position book unscoped AND never mention strategy_id.

    SCOPED PER FUNCTION. A module-wide scan gave kumo-trading-strategies a false positive on its first run:
    one compliant reader anywhere in a file made the whole file look compliant, and one offender made
    the whole file look guilty.
    """
    out = []
    for name in files:
        out.extend(_blind_readers_in((pathlib.Path(__file__).parent / name).read_text()))
    return out


def test_the_scan_actually_finds_the_known_readers():
    """Fixture property first. If the scan found nothing the rule below would pass over any codebase,
    including one where every read is unscoped and blind."""
    found = _blind_readers()
    assert len(found) >= 5, f"scan found only {found} — it is not seeing engine_node.py"


def test_every_blind_reader_is_a_NAMED_account_level_function():
    """Default deny. A new function that reads the book and cannot tell one strategy from another is a
    candidate for TECHIVOL's bug: sizing an exit to every share the account holds. It has to be named
    as account-level, deliberately, or scoped."""
    offenders = sorted(set(_blind_readers()) - ACCOUNT_LEVEL)
    assert offenders == [], (
        f"{offenders} read the position book without any notion of ownership. If the question is "
        f"genuinely about the ACCOUNT, add the name to ACCOUNT_LEVEL with its reason. If it is about "
        f"what a strategy owns, pass strategy_id= or compare pos.strategy_id"
    )


def test_the_allowlist_has_no_STALE_names():
    """An allowlist that outlives its entries stops being a list of decisions and becomes decoration —
    and the next real offender can be waved through by a name someone deleted years ago."""
    stale = sorted(ACCOUNT_LEVEL - set(_blind_readers()))
    assert stale == [], (
        f"{stale} no longer read the book blindly — remove them from ACCOUNT_LEVEL so the list keeps "
        f"meaning what it says"
    )


def test_a_python_side_strategy_filter_COUNTS_as_scoped():
    """The false positive that killed the first draft, pinned so it cannot come back.

    `_opposite_side_qty` reads `positions_open()` with no kwarg and is CORRECT — it compares
    `pos.strategy_id` inside the loop. Flagging it would make this rule wrong about working code, and a
    rule that is wrong about working code gets deleted."""
    assert "_opposite_side_qty" not in _blind_readers()
    assert "strategy_position" not in _blind_readers()


def test_the_NATIVE_kwarg_filter_counts_as_scoped():
    """The branch no file exercises. `Cache.positions_open(strategy_id=...)` is the form #437 asks for,
    and it has no caller in cockpit yet — so removing the exclusion changed nothing and the suite
    stayed green. Driven here on source the repo does not contain, which is the only way this branch
    can fail before someone writes the first such caller."""
    scoped = "def f(self, sid):\n    return self.cache.positions_open(strategy_id=sid)\n"
    assert _blind_readers_in(scoped) == []


def test_the_same_function_WITHOUT_the_kwarg_is_flagged():
    """The discriminating half — otherwise the test above passes against a scan that flags nothing."""
    unscoped = "def f(self, sid):\n    return self.cache.positions_open()\n"
    assert _blind_readers_in(unscoped) == ["f"]
