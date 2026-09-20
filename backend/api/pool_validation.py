"""Refuse a pool symbol the venue has never heard of, by name (#663, #557).

Measured on an Alpaca paper instance 2026-08-29: nine of 117 pool symbols never resolve — BLLLN, BOX, DBX, GEN,
GTLAB, IQVIA, JEPO, NSIT, OVVI — unchanged for two days, and the lanes drop them at ERROR on every
boot. The drop is correct; nine permanent entries in that list are what makes the TENTH invisible
when it arrives.

WHY NOT A SHAPE CHECK. Every one of the nine is uppercase, alphanumeric and at most five
characters; `IQVIA` parses perfectly and simply is not a ticker — it is the company name. The
defect is semantic, so the only oracle that settles it is a venue asset catalog. Cockpit already
keeps one in-process (`instrument_search.py`, TTL-refreshed from `/v2/assets`, ~13k US equities),
which is why this validation belongs here rather than in the book parser.

THE PAIR SIGNATURE. The pool carries BOTH spellings of the misreads — BLLN/BLLLN, IQV/IQVIA,
FTNT/FTNR, OVV/OVVI — and only the good one resolves. That makes them diagnosable from inside the
data with no external lookup, and naming the twin turns a refusal into a correction.

THREE STATES, NEVER TWO. A catalog that has not loaded is not a venue with no assets: refusing a
whole pool because a REST call failed would take every lane out of the market over an unrelated
outage. Unknown accepts everything AND SAYS SO, so a degraded pass cannot be read as a clean one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PoolSymbolVerdict:
    #: "ok" when the catalog answered; "catalog_unavailable" when it could not be consulted.
    status: str
    accepted: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    #: refused symbol -> the pool's own correct spelling, where one edit explains it.
    suggestions: dict[str, str] = field(default_factory=dict)


#: Suffix families that make one-edit neighbours everywhere: warrants, units, rights, preferreds
#: and share classes. A suggestion differing only by one of these is a DIFFERENT INSTRUMENT OF THE
#: SAME ISSUER — the most dangerous kind, because it is entirely plausible to an operator about to
#: pin it. Measured against the real 13,392-symbol catalog: these families are nearly the whole
#: residual false-positive rate.
_SUFFIX_FAMILY = ("W", "U", "R")


def _same_issuer_variant(a: str, b: str) -> bool:
    """True when two symbols differ only by a warrant/unit/right suffix or a class/preferred tail."""
    if "." in a or "." in b:
        # AKO.A vs AKO.B, AUB.PRA vs ALB.PRA — share classes and preferreds of one issuer.
        return a.split(".")[0] == b.split(".")[0] or a.split(".")[-1] == b.split(".")[-1]
    if len(a) == len(b):
        # BDCIW vs BDCIU — one stem, two suffix letters. Same issuer, different instrument, and a
        # substitution rather than an insertion, so the length rule below cannot see it.
        return a[:-1] == b[:-1] and a[-1] in _SUFFIX_FAMILY and b[-1] in _SUFFIX_FAMILY
    short, long_ = sorted((a, b), key=len)
    return len(long_) == len(short) + 1 and long_.startswith(short) and long_[-1] in _SUFFIX_FAMILY


def _likely_twin(symbol: str, known: set[str], _pool: set[str]) -> str | None:
    """The correct form of a misread, or None.

    MEASURED AGAINST THE REAL CATALOG (13,392 Alpaca symbols, 2026-08-29), because the first version
    of this function was ranked backwards and nobody could have seen it from a small double:

        OVVI -> WVVI   WRONG — Willamette Valley Vineyards, a real unrelated issuer
        JEPO -> None   MISSED — JEPQ is listed and tradable

    Every misread in this class is an INSERTION (BLLN->BLLLN, OVV->OVVI, IQV->IQVIA, GTLB->GTLAB),
    so the true twin is always SHORTER than the refused symbol — while a same-length substitution
    neighbour is longer and therefore always won a longest-first ranking. The rule was inverted for
    the exact defect class it was written for, and BLLLN/IQVIA/GTLAB only worked because no
    same-length neighbour happened to exist in a 13k namespace. Luck, not design.

    SO THERE IS NO RANKING NOW. Exactly one candidate, or nothing. Measured on 4,000 valid tickers
    removed from the catalog one at a time — which is what a DELISTING looks like to this code — the
    ranked rule produced a false suggestion 17.4% of the time; requiring a unique candidate takes
    that to 4.3%, and excluding same-issuer suffix variants takes the rest.

    A wrong suggestion is worse than none: it invites an operator to pin a symbol nobody checked.
    """
    s = symbol.upper()
    candidates = [
        c for c in known
        if c != s and (_one_edit(s, c) or (s.startswith(c) and len(s) - len(c) <= 2))
    ]
    if len(candidates) != 1:
        # Zero: nothing to say. More than one: we do not know which, and guessing is the failure
        # mode this function exists to avoid.
        return None
    # THE SAME-ISSUER GUARD IS APPLIED HERE, AFTER UNIQUENESS — NEVER INSIDE THE COMPREHENSION.
    #
    # Filtering same-issuer candidates BEFORE the uniqueness check removes COMPETITORS, so
    # uniqueness is reached by deletion and a safely-ambiguous case becomes a confident wrong
    # answer. Measured on a live instance catalog, 4,000 valid tickers removed one at a time — and stated
    # as a BAND WITH ITS SEEDS, because a single figure without one is what produced two different
    # numbers for the same code thirty lines apart in this file's own history:
    #
    #     no guard        4.3-4.4%
    #     guard inside    4.8%      <- worse than none
    #     guard here      2.5-3.2% across seeds 1/7/11/42 (100-127 of 4,000)
    #
    # Eighty-five symbols were PROMOTED from an honest None to a wrong suggestion by the guard
    # meant to suppress them.
    #
    #     AFRIW  raw ['AFRI', 'BFRIW']  -> inside removes AFRI (the right answer), leaving BFRIW,
    #                                      a DIFFERENT issuer's warrant, as the unique survivor
    #     BEATW  raw ['BEAT', 'SEATW']  -> same shape
    #
    # The 4.9% -> 5.2% in my own previous measurement was this, reported and not chased: a filter
    # that can only ever REMOVE suggestions cannot raise a false-positive rate unless it is
    # changing which case counts as unique. The number moving the wrong way was the finding.
    #
    # Here it can only turn a suggestion into None, which is also why the over-broad dotted-tail
    # rule below is harmless in this position and was not in the other.
    if _same_issuer_variant(s, candidates[0]):
        return None
    return candidates[0]


def _one_edit(a: str, b: str) -> bool:
    """True when `a` and `b` differ by at most one insertion, deletion or substitution."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = j = diff = 0
    while i < len(short) and j < len(long_):
        if short[i] != long_[j]:
            diff += 1
            if diff > 1:
                return False
            j += 1
            continue
        i += 1
        j += 1
    return True


def validate_pool_symbols(symbols, catalog) -> PoolSymbolVerdict:
    """Split a proposed pool into what the venue lists and what it does not."""
    wanted = [str(s).strip().upper() for s in (symbols or ()) if str(s).strip()]
    try:
        # `catalog is None` is the staging case: the search index is Alpaca-backed and an IBKR
        # instance has none. No oracle, so no refusals — same third state as a failed load.
        known = catalog.known_symbols() if catalog is not None else None
    except Exception:  # noqa: BLE001 — an oracle that raises is an oracle we do not have
        known = None
    if not known:
        # Never loaded, or loaded zero rows: a broken read, not a venue that lists nothing. Same
        # reasoning as the venue-calendar sweep (#645) — absence in an enumeration is evidence only
        # when the enumeration itself is trustworthy.
        return PoolSymbolVerdict(status="catalog_unavailable", accepted=tuple(wanted))

    accepted = tuple(s for s in wanted if s in known)
    refused = tuple(s for s in wanted if s not in known)
    pool = set(wanted)
    suggestions = {}
    for s in refused:
        # THE CATALOG is the source of correct spellings, not the pool. The misreads happen to
        # travel WITH their twin (BLLN beside BLLLN), which is what makes them diagnosable by eye —
        # but a correction must be offered even when the good form is absent from the pool, and the
        # pool is not an oracle for what a real ticker is.
        twin = _likely_twin(s, known, pool)
        if twin:
            suggestions[s] = twin
    return PoolSymbolVerdict(status="ok", accepted=accepted, refused=refused,
                             suggestions=suggestions)
