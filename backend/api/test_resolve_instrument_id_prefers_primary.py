"""#862 — a broker ticker resolves to the instrument the position is actually on.

MEASURED, staging2, 2026-09-10 14:02Z: `/trades` read `broker_protected: False` for CGAU.XNYS and
WPM.XNYS while their trailing stops were ACCEPTED at the venue; AYA/NSIT/SSRM (XNAS) read True. The
Redis cache held TWO ids for each of the two names — `CGAU.XNAS` and `CGAU.XNYS`, `WPM.XNAS` and
`WPM.XNYS` — because IB's SMART contract details return every listing (#625). `_resolve_instrument_id`
returned whichever id `cache.instrument_ids()` iterated first, so the venue's "CGAU" stop was keyed
`(CGAU.XNAS, SELL)` while the position DTO asked for `(CGAU.XNYS, SELL)`. Two derivations of one
instrument, disagreeing by iteration order. The same resolver feeds broker-vs-cache drift and the
stop-price cache, so all three planes carried the same hole.

The lanes already answer this question with `prefer_primary_exchange` (#625); the resolver now asks
it too, and where no listing is its own primary exchange it prefers the id an OPEN POSITION is on —
the venue is describing an order on the held instrument — before the deterministic sorted pick.
"""
from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.identifiers import InstrumentId

from api.engine_node import UiFeedStrategy

XNAS = InstrumentId.from_str("CGAU.XNAS")
XNYS = InstrumentId.from_str("CGAU.XNYS")


class _Cache:
    def __init__(self, ids, primary: dict, positions=()):
        self._ids = list(ids)
        self._primary = primary
        self._positions = list(positions)

    def instrument_ids(self):
        return list(self._ids)

    def instrument(self, iid):
        primary = self._primary.get(iid)
        return SimpleNamespace(info={"contract": {"primaryExchange": primary}} if primary else {})

    def positions_open(self):
        return list(self._positions)


def _feed(cache):
    f = SimpleNamespace(cache=cache)
    f._resolve_instrument_id = UiFeedStrategy._resolve_instrument_id.__get__(f)
    return f


def test_the_fixture_has_TWO_ids_for_one_ticker_and_iteration_order_picks_the_wrong_one():
    """Fixture property: the cache iterates XNAS first, the position lives on XNYS."""
    cache = _Cache([XNAS, XNYS], {XNYS: "NYSE"})
    assert [str(i).rpartition(".")[0] for i in cache.instrument_ids()] == ["CGAU", "CGAU"]
    assert cache.instrument_ids()[0] == XNAS


def test_a_ticker_with_two_listings_resolves_to_its_PRIMARY_exchange():
    """The live CGAU/WPM shape. Bitten by: returning the first match (XNAS)."""
    f = _feed(_Cache([XNAS, XNYS], {XNYS: "NYSE"}))
    assert f._resolve_instrument_id("CGAU") == "CGAU.XNYS"


def test_no_primary_match_prefers_the_id_an_OPEN_POSITION_is_on():
    """The venue's stop describes an order on the HELD instrument — that is the tiebreak when no
    listing is its own primary exchange (contract details not loaded yet)."""
    held = SimpleNamespace(instrument_id=XNYS)
    f = _feed(_Cache([XNAS, XNYS], {}, positions=[held]))
    assert f._resolve_instrument_id("CGAU") == "CGAU.XNYS"


def test_a_single_listing_resolves_as_before_and_an_unknown_ticker_is_None():
    f = _feed(_Cache([InstrumentId.from_str("AYA.XNAS")], {}))
    assert f._resolve_instrument_id("AYA") == "AYA.XNAS"
    assert f._resolve_instrument_id("ZZZZ") is None


def test_no_primary_and_no_position_is_DETERMINISTIC_not_iteration_order():
    """Neither venue is primary and nothing is held: the sorted first, every time — never whatever
    the cache iterated last (#625's trap)."""
    f = _feed(_Cache([XNYS, XNAS], {}))
    g = _feed(_Cache([XNAS, XNYS], {}))
    assert f._resolve_instrument_id("CGAU") == g._resolve_instrument_id("CGAU") == "CGAU.XNAS"
