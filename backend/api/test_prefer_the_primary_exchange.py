"""When IB reports a symbol twice, keep the identity that matches its own primary exchange (#625).

MEASURED on an IBKR paper instance 2026-08-29, after #622 made `load_contracts` declare the full universe:

    64 of 209 cached instruments carry TWO identities

    id            primaryExchange   venue
    SPY.ARCX      ARCA              ARCX     <- venue matches
    SPY.XNAS      ARCA              XNAS     <- does not
    RVTY.XNYS     NYSE              XNYS     <- matches
    RVTY.XNAS     NYSE              XNAS     <- does not

`providers/ibkr.py` declares `exchange="SMART"`, IB returns contract details for multiple listings,
and Nautilus builds an instrument per contract. IB is CORRECT to do that. The defect is what we did
next: a dict comprehension kept whichever the cache iterated last, so BCTROT asked for `SPY.XNAS` —
which matches an instrument that never has data — and starved silently:

    BCTROT: Received <Bar[]> data with no bar for SPY.XNAS-1-DAY-LAST-EXTERNAL

THE SPURIOUS TWIN IS SELF-IDENTIFYING: it carries the REAL primary exchange while wearing the wrong
venue. So no external comparison and no tie-break is needed.

WHY THIS LIVES IN COCKPIT AND NOT IN THE RESOLVER. `exchange_to_mic_venue` needs `ibapi`, which
kumo-trading-strategies does not install — and "ARCA" is not "ARCX", so a naive `primaryExchange == venue`
compare matches NOTHING and silently falls through on every symbol while looking like a working
rule. Teaching `symbol_resolution.py` IB's exchange names would also recreate #622 inside the
resolver written to fix it: the layer that knows the RULE is not the layer that knows the VENDOR.

I FILED #625 AS "NOT LIVE TODAY — no symbol appears twice". That was measured on the pre-#622 cache,
before the change that creates the duplicates, and never re-measured after shipping it.
"""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.identifiers import InstrumentId

from api.venue_preference import prefer_primary_exchange


def _cache(rows: dict[str, str]):
    """`{"SPY.ARCX": "ARCA", ...}` -> a Cache stand-in whose instruments carry IB's contract dict."""
    inst = {k: SimpleNamespace(info={"contract": {"primaryExchange": v}}) for k, v in rows.items()}
    return SimpleNamespace(instrument=lambda iid: inst.get(str(iid)))


_REAL = {"SPY.ARCX": "ARCA", "SPY.XNAS": "ARCA", "RVTY.XNYS": "NYSE", "RVTY.XNAS": "NYSE"}


def test_the_fixture_matches_what_the_VENUE_actually_returned():
    """FIXTURE PROPERTY FIRST, and it is the crux of the whole ticket: BOTH twins report the SAME
    primaryExchange. If the fixture gave the XNAS twin `NASDAQ`, the rule under test would be a
    different rule and would not work on the real data."""
    assert _REAL["SPY.ARCX"] == _REAL["SPY.XNAS"] == "ARCA"
    assert _REAL["RVTY.XNYS"] == _REAL["RVTY.XNAS"] == "NYSE"


def test_it_keeps_the_venue_that_matches_its_own_primary_exchange():
    cache = _cache(_REAL)
    cands = [InstrumentId.from_str("SPY.XNAS"), InstrumentId.from_str("SPY.ARCX")]
    assert str(prefer_primary_exchange(cache, "SPY", cands)) == "SPY.ARCX"


def test_RVTY_is_here_because_PREFERENCE_and_FALLBACK_DISAGREE():
    """`sorted()[0]` gives ARCX for SPY by LUCK. On RVTY it gives XNAS, which is wrong. Without a
    symbol where the two disagree, the preference could be ignored entirely and every other
    assertion would still pass — the peer added this and it is the better fixture."""
    cache = _cache(_REAL)
    cands = [InstrumentId.from_str("RVTY.XNAS"), InstrumentId.from_str("RVTY.XNYS")]
    assert sorted(str(c) for c in cands)[0] == "RVTY.XNAS", "the fallback no longer disagrees here"
    assert str(prefer_primary_exchange(cache, "RVTY", cands)) == "RVTY.XNYS"


def test_ARCA_is_not_ARCX_so_a_naive_string_compare_must_not_be_what_this_does():
    """THE OBJECTION THAT SAVED THIS. `primaryExchange` is IB's name, the venue is a MIC. A direct
    compare matches NOTHING on any real pair and would silently return None for every symbol while
    looking like a working preference — the fifth check-that-passes-by-not-applying today."""
    from nautilus_trader.adapters.interactive_brokers.parsing.instruments import (
        exchange_to_mic_venue,
    )

    assert exchange_to_mic_venue("ARCA") == "ARCX" != "ARCA"
    assert exchange_to_mic_venue("NYSE") == "XNYS" != "NYSE"


def test_it_returns_NONE_rather_than_guessing_when_nothing_matches():
    """`None` means "I cannot tell" and the resolver's own deterministic fallback takes over —
    REPORTED. Two fallbacks would disagree, so this must not invent one."""
    cache = _cache({"AAA.XNAS": "SOMETHING_UNMAPPED", "AAA.XNYS": "SOMETHING_UNMAPPED"})
    cands = [InstrumentId.from_str("AAA.XNAS"), InstrumentId.from_str("AAA.XNYS")]
    assert prefer_primary_exchange(cache, "AAA", cands) is None


def test_a_MISSING_instrument_or_info_does_not_raise():
    """The cache can legitimately not hold one of the candidates mid-load. A preference that raises
    is caught and reported by the resolver, but returning None is the honest answer."""
    cache = SimpleNamespace(instrument=lambda iid: None)
    cands = [InstrumentId.from_str("SPY.XNAS"), InstrumentId.from_str("SPY.ARCX")]
    assert prefer_primary_exchange(cache, "SPY", cands) is None


def test_it_only_ever_returns_one_of_the_CANDIDATES():
    """The resolver refuses an id not in the list, so this cannot put an untradeable instrument on
    the order path — but it should not try to."""
    cache = _cache(_REAL)
    cands = [InstrumentId.from_str("SPY.ARCX")]
    assert prefer_primary_exchange(cache, "SPY", cands) in cands


def test_EVERY_lane_installs_the_preference():
    """THE WIRING, and the fourth time today this exact gap has been the defect.

    A preference nothing installs is a preference that never runs, and the lane then keeps whichever
    identity the cache iterated last — the silent starvation this ticket is about. Deleting the line
    from one lane must fail here rather than at the next deploy.
    """
    import ast
    import pathlib as _pl

    root = _pl.Path(__file__).parent.parent / "strategies"
    missing = [f for f in ("momentum.py", "qc27.py", "qc345.py")
               if "_prefer_venue" not in ast.unparse(ast.parse((root / f).read_text()))]
    assert not missing, f"these lanes never install the venue preference: {missing} (#625)"


def test_the_preference_reads_the_cache_LAZILY_not_at_build():
    """`self.cache` is EMPTY when the lane is built and populated by the time `on_start` resolves —
    `TradingNode.build()` only registers clients; instruments load on connect.

    Capturing the cache at build would preserve that emptiness for the process lifetime, which is
    precisely the trap #622 paid for. The lambda must close over the STRATEGY and read `.cache` when
    called, so this asserts the indirection rather than the mere presence of a lambda.
    """
    import ast
    import pathlib as _pl

    root = _pl.Path(__file__).parent.parent / "strategies"
    for f in ("momentum.py", "qc27.py", "qc345.py"):
        src = ast.unparse(ast.parse((root / f).read_text()))
        assert "_s.cache" in src, (
            f"{f} does not read the cache lazily — a cache captured at build is empty forever (#622)"
        )
