"""A pool symbol the venue has never heard of is refused at ingestion, by name (#663, #557).

Measured on paper 2026-08-29, both lanes, unchanged for two days:

    9 unresolved of 117 — BLLLN, BOX, DBX, GEN, GTLAB, IQVIA, JEPO, NSIT, OVVI

Paper is the authoritative reading: its `[universe]` is two symbols and the engine loads 13,392
Alpaca instruments, so a failure there means Alpaca genuinely has no such asset. (Staging's larger
list is NOT comparable — all of its entries are absent from its curated `[universe]`, so it
measures #511's snapshot drift, and four of them are perfectly good tickers.)

Three categories, and only the first is a "typo":

    misread, twin already in the pool   BLLLN/BLLN · IQVIA/IQV · FTNR/FTNT · OVVI/OVV · JEPO/JEPQ
    an operator PIN                     GTLAB — an exec_pool_override row, no feed involved
    no alternate form, venue has none   BOX · DBX · GEN · NSIT

A SHAPE CHECK CATCHES NONE OF THEM — every one is uppercase, alphanumeric and <= 5 characters, and
`IQVIA` parses perfectly; it simply is not a ticker. Validation has to be against a venue asset
catalog, which cockpit already keeps in-process (`instrument_search.py`, TTL-refreshed from
`/v2/assets`).

Refusing SILENTLY would recreate the invisibility #663 just removed, so a refusal is a named,
countable condition — which is #557's subject.
"""

from __future__ import annotations

from api.pool_validation import PoolSymbolVerdict, validate_pool_symbols


class _Catalog:
    """What the in-process catalog answers. `None` = the catalog has never loaded, which is the
    third state and must NOT read as 'every symbol is bad'."""

    def __init__(self, symbols):
        self._symbols = None if symbols is None else {s.upper() for s in symbols}

    def known_symbols(self):
        return self._symbols


def test_the_fixture_reproduces_the_live_shape():
    """FIXTURE PROPERTY: the catalog holds the good twin and not the misread — without that, the
    refusal below could pass against an empty catalog that rejects everything."""
    cat = _Catalog({"BLLN", "IQV", "FTNT", "AAPL"})
    known = cat.known_symbols()
    assert "BLLN" in known and "BLLLN" not in known


def test_a_symbol_the_VENUE_does_not_list_is_refused_and_named():
    verdict = validate_pool_symbols(["AAPL", "BLLLN", "IQVIA"], _Catalog({"AAPL", "BLLN", "IQV"}))
    assert verdict.accepted == ("AAPL",)
    assert verdict.refused == ("BLLLN", "IQVIA"), "the refusal does not name what it dropped"
    assert verdict.status == "ok"


def test_a_MISREAD_is_reported_with_its_likely_twin():
    """The pair signature is what makes these diagnosable without an external lookup: the pool
    carries both spellings and only one resolves. Naming the twin turns a refusal into a fix."""
    verdict = validate_pool_symbols(["BLLLN", "IQVIA", "BOX"], _Catalog({"BLLN", "IQV", "AAPL"}))
    assert verdict.suggestions == {"BLLLN": "BLLN", "IQVIA": "IQV"}, (
        "a refused symbol whose correct form is one edit away was not paired with it"
    )
    assert "BOX" not in verdict.suggestions, "a name with no twin must not get a guessed one"


def test_an_UNLOADED_catalog_accepts_EVERYTHING_rather_than_refusing_it():
    """THREE STATES. 'The catalog has not loaded' is not 'the venue has never heard of this' — and
    refusing a whole pool because a REST call failed would take every lane out of the market over
    an unrelated outage. Unknown accepts and says so."""
    verdict = validate_pool_symbols(["AAPL", "BLLLN"], _Catalog(None))
    assert verdict.accepted == ("AAPL", "BLLLN")
    assert verdict.refused == ()
    assert verdict.status == "catalog_unavailable", "the degraded state is indistinguishable from a clean pass"


def test_an_EMPTY_catalog_is_treated_as_unavailable_not_as_a_venue_with_no_assets():
    """A catalog that loaded zero rows is a broken read, not a venue that lists nothing — the same
    reasoning the venue-calendar sweep uses (#645): absence in an enumeration is only evidence when
    the enumeration itself is trustworthy."""
    verdict = validate_pool_symbols(["AAPL"], _Catalog(set()))
    assert verdict.accepted == ("AAPL",)
    assert verdict.status == "catalog_unavailable"


def test_THE_TWIN_FINDER_AGAINST_A_REALISTIC_NAMESPACE():
    """THE TEST THAT WOULD HAVE CAUGHT IT. The first version of this file certified the twin finder
    against an 8-symbol double, where every misread had exactly one candidate. Run against the real
    13,392-symbol Alpaca catalog it produced:

        OVVI -> WVVI   WRONG — Willamette Valley Vineyards, a real unrelated issuer
        JEPO -> None   MISSED — JEPQ is listed and tradable

    because the ranking was longest-first while every misread in this class is an INSERTION, so the
    true twin is always SHORTER. A double that cannot represent production, hiding the defect in the
    test whose docstring called itself the acceptance criterion.

    This namespace is small but DENSE in the way that matters: competing same-length neighbours and
    the suffix families (warrants W, units U, rights R, share classes .A/.B) that put one-edit
    neighbours everywhere. Measured on the real catalog, those families were nearly the whole
    residual false-positive rate: 18.4% of delisted-like symbols got a suggestion under the ranked
    rule, 4.9% under this one.
    """
    catalog = _Catalog({
        # the correct twins
        "BLLN", "IQV", "GTLB", "OVV", "JEPQ", "FTNT",
        # the competing neighbours that broke the ranked rule
        "WVVI", "CEPO", "JEPI", "JPO",
        # suffix families — same issuer, different instrument
        "FUSE", "FUSEW", "BDCIU", "BDCIW", "AKO.A", "AKO.B", "ALB.PRA", "AUB.PRA",
        # ordinary names
        "AAPL", "MSFT", "BOX", "DBX", "GEN", "NSIT",
    })
    verdict = validate_pool_symbols(
        ["BLLLN", "IQVIA", "GTLAB", "OVVI", "JEPO", "AAPL"], catalog)

    assert verdict.suggestions == {"BLLLN": "BLLN", "IQVIA": "IQV", "GTLAB": "GTLB"}, (
        "the twin finder guessed where it should have stayed silent, or lost a correction it had"
    )
    assert "OVVI" in verdict.refused and "OVVI" not in verdict.suggestions, (
        "OVVI was paired again — WVVI is a real unrelated issuer and this is the measured failure"
    )
    assert "JEPO" in verdict.refused and "JEPO" not in verdict.suggestions


def test_a_SUFFIX_VARIANT_of_the_same_issuer_is_never_suggested():
    """The most dangerous suggestion is a different instrument of the SAME issuer — a warrant for
    its common, a preferred for another preferred — because it is entirely plausible to an operator
    about to pin it."""
    cat = _Catalog({"FUSEW", "BDCIU", "AKO.A"})
    assert validate_pool_symbols(["FUSE"], cat).suggestions == {}
    assert validate_pool_symbols(["BDCIW"], cat).suggestions == {}
    assert validate_pool_symbols(["AKO.B"], cat).suggestions == {}


def test_an_AMBIGUOUS_correction_is_refused_rather_than_guessed():
    """Two equally plausible twins means we do not know. Three states again."""
    verdict = validate_pool_symbols(["ABCD"], _Catalog({"ABCE", "ABCF"}))
    assert verdict.refused == ("ABCD",)
    assert verdict.suggestions == {}


def test_THE_REFRESH_ENDPOINT_DROPS_THE_BAD_AND_REPORTS_IT(monkeypatch):
    """THE WIRING, driven rather than grepped. My first version of this test searched the endpoint's
    source for the call — and the bite that wrapped it in `None if True else ...` left the text in
    place, so the mutation did not bite. A source grep proves a string exists, not that a branch
    runs.
    """
    import asyncio

    import api.app as app_mod

    # Via monkeypatch, not a bare assignment: app.state is process-global and a leaked
    # catalog makes the next test order-dependent.
    monkeypatch.setattr(app_mod.app.state, "search", _Catalog({"AAPL", "BLLN"}), raising=False)

    class _Body:
        symbols = ["AAPL", "BLLLN"]
        detail = None
        create = False
        allow_shrink = False

    written = {}

    class _Pool:
        async def refresh_source(self, name, symbols, **kw):
            written["symbols"] = list(symbols)
            written["detail"] = kw.get("detail")

    monkeypatch.setattr(app_mod, "pool", _Pool(), raising=False)
    monkeypatch.setattr(app_mod, "get_pool", lambda: _noop(), raising=False)
    asyncio.run(app_mod.refresh_pool_source("george_book", _Body()))

    # THE GOOD SYMBOL GOES THROUGH. Refusing the whole push over one bad name would block a
    # 106-symbol refresh on a single typo — and `accepted` was computed and discarded in the first
    # version, which is its own defect class here.
    assert written["symbols"] == ["AAPL"], "the clean symbols were dropped with the bad one"
    assert written["detail"]["refused_symbols"] == ["BLLLN"], (
        "the refusal does not reach the pool surface, only a log nobody opens"
    )
    assert "did you mean BLLN" in written["detail"]["refused_reason"], (
        "the refusal does not carry the correction, so an operator cannot act on it"
    )


def test_THE_REFRESH_ENDPOINT_LETS_A_CLEAN_POOL_THROUGH(monkeypatch):
    """The other direction, and the one that matters most: a pool of listed symbols must reach the
    writer untouched. A validation that refuses everything is worse than none."""
    import asyncio

    import api.app as app_mod

    # Via monkeypatch, not a bare assignment: app.state is process-global and a leaked
    # catalog makes the next test order-dependent.
    monkeypatch.setattr(app_mod.app.state, "search", _Catalog({"AAPL", "MSFT"}), raising=False)
    written = {}

    class _Pool:
        async def refresh_source(self, name, symbols, **kw):
            written["symbols"] = list(symbols)

    class _Body:
        symbols = ["AAPL", "MSFT"]
        detail = None
        create = False
        allow_shrink = False

    monkeypatch.setattr(app_mod, "pool", _Pool(), raising=False)
    monkeypatch.setattr(app_mod, "get_pool", lambda: _noop(), raising=False)
    asyncio.run(app_mod.refresh_pool_source("george_book", _Body()))
    assert written["symbols"] == ["AAPL", "MSFT"], "a clean pool did not reach the writer"


async def _noop():
    return None


def test_A_PIN_WARNS_BUT_IS_NOT_REFUSED(monkeypatch):
    """GTLAB is a pin row from 2026-08-11 that has never resolved, so this door needs the check —
    but a pin is the operator's explicit override, and the case it exists for includes "our catalog
    is the thing that is wrong". So it records and proceeds."""
    import asyncio

    import api.app as app_mod

    # Via monkeypatch, not a bare assignment: app.state is process-global and a leaked
    # catalog makes the next test order-dependent.
    monkeypatch.setattr(app_mod.app.state, "search", _Catalog({"GTLB"}), raising=False)
    pinned = {}

    class _Pool:
        async def set_override(self, symbol, kind, reason):
            pinned["symbol"] = symbol

    class _Body:
        symbol = "GTLAB"
        kind = "pin"
        reason = "operator"

    monkeypatch.setattr(app_mod, "pool", _Pool(), raising=False)
    monkeypatch.setattr(app_mod, "get_pool", lambda: _noop(), raising=False)
    asyncio.run(app_mod.set_pool_override(_Body()))
    assert pinned["symbol"] == "GTLAB", "an operator pin was refused — the escape hatch is gone"


def test_the_same_issuer_guard_runs_AFTER_uniqueness_not_inside_the_candidate_filter():
    """THE PLACEMENT, and it is not a detail — inside the filter the guard is WORSE THAN NONE.

    Removing same-issuer candidates before the uniqueness check removes COMPETITORS, so uniqueness
    is reached by deletion and a safely-ambiguous case becomes a confident wrong answer. Measured on
    the real 13,392-symbol catalog, 4,000 valid tickers removed one at a time, as a band with its
    seeds — this docstring and the module comment carried 3.2% and 2.6% for the SAME code, which is
    what an unseeded figure does: 4.3-4.4% with no guard, 4.8% with it inside,
    2.5-3.2% across seeds 1/7/11/42 (100-127 of 4,000) with it after. Eighty-five symbols were PROMOTED from an honest
    None to a wrong suggestion by the guard meant to suppress them.

    This case is taken verbatim from that measurement rather than invented, because a hand-built one
    does not discriminate: BDCIW against {BDCI, BDCIU} is filtered on BOTH placements and looks
    fine. AFRIW is the real shape — the guard deletes AFRI, the correct same-issuer relative, and
    leaves BFRIW, a DIFFERENT issuer's warrant, as the unique survivor.
    """
    cat = _Catalog({"AFRI", "BFRIW"})
    verdict = validate_pool_symbols(["AFRIW"], cat)
    assert verdict.suggestions == {}, (
        "the guard is filtering candidates instead of vetting the winner — it deleted the right "
        "answer and promoted another issuer's warrant"
    )

    # And the other direction: a genuine unique twin with no same-issuer relationship still pairs.
    assert validate_pool_symbols(["BLLLN"], _Catalog({"BLLN", "AAPL"})).suggestions == {
        "BLLLN": "BLLN"
    }
