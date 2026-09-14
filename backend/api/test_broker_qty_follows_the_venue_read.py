"""`broker_qty` is derived from the venue read that ALREADY runs on every provider, not from an
Alpaca-only stream (#834).

MEASURED on staging2 (IBKR), 2026-09-09. All 7 unclaimed positions rendered

    manual at broker — broker unconfirmed, cannot be moved yet

because `/external-activity` carried `venue_qty: null` on every POSITION row. `_publish_external`
stamps `venue_qty` from `self._broker_qty`; `self._broker_qty` is set in one place, the subscriber on
`_RECONCILE_TOPIC`; and the ONLY publisher of that topic is `providers/alpaca/exec_client.py:550`.
On an IBKR instance nobody ever publishes it, so the operator can neither claim nor close any
external position — while `/health.book_truth` read the venue perfectly well through
`_venue_position_reports()` (`instruments: 17, venue_unreadable: 0`).

Two derivations of "what does the broker hold", and only the Alpaca one fed the UI. CLAUDE.md,
2026-08-27: anything reading positions is an EXECUTION question and must follow KUMO_EXEC.

THREE STATES SURVIVE. `None` reports (unreadable) -> `_broker_qty` is None, never `{}`: an empty dict
says the broker answered and holds nothing, which turns every row into a "phantom" the UI refuses to
move. And a symbol the venue does not mention resolves to 0.0 at lookup — that IS the phantom case,
and it is `_publish_external`'s `.get(sym, 0.0)`, unchanged.
"""

from __future__ import annotations

import types
from decimal import Decimal

from nautilus_trader.model.enums import PositionSide

from api.engine_node import UiFeedStrategy


def _report(iid: str, side: str, qty: str):
    """The shape `_venue_net_position` already reads: `instrument_id`, `position_side`, `quantity`."""
    return types.SimpleNamespace(
        instrument_id=iid,
        position_side=PositionSide.LONG if side == "LONG" else PositionSide.SHORT,
        quantity=Decimal(qty),
        signed_decimal_qty=Decimal(qty) * (1 if side == "LONG" else -1),
    )


def _probe():
    class _Probe(UiFeedStrategy):
        @property
        def cache(self):
            return types.SimpleNamespace(positions_open=lambda: [])

    s = _Probe.__new__(_Probe)
    from api.book_truth import DisagreementStreaks  # the collaborator _compute_book_truth already needs

    s._disagreements = DisagreementStreaks()
    s._safe_now = lambda: 1_700_000_000_000_000_000
    s._book_truth = None
    s._broker_qty = None
    return s


def test_the_fixture_reports_carry_a_short_and_two_venues_for_one_symbol():
    """Fixture property: a SHORT (sign matters) and CBL on two venues (the key is the BARE symbol, so
    they must sum). Without both, a wrong sign or a wrong key would pass."""
    rs = [_report("CBL.XNYS", "LONG", "50"), _report("CBL.XNAS", "LONG", "10"),
          _report("NTCT.XNAS", "SHORT", "56")]
    assert {r.position_side for r in rs} == {PositionSide.LONG, PositionSide.SHORT}
    assert len({r.instrument_id.split(".")[0] for r in rs}) == 2


def test_the_book_truth_pass_SETS_broker_qty_from_the_venue_reports():
    """THE DEFECT. This pass runs on every provider and succeeds on IBKR; the external plane must read
    what it read, not wait for a stream that only Alpaca publishes."""
    s = _probe()
    s._compute_book_truth([_report("CBL.XNYS", "LONG", "50"), _report("CBL.XNAS", "LONG", "10"),
                           _report("NTCT.XNAS", "SHORT", "56")])
    assert s._broker_qty == {"CBL": 60.0, "NTCT": -56.0}, s._broker_qty


def test_an_UNREADABLE_venue_leaves_broker_qty_NONE_not_empty():
    """`{}` means "the broker answered and holds nothing" and would render every position as a
    phantom that cannot be moved. Unreadable is the third state and must stay None."""
    s = _probe()
    s._broker_qty = {"CBL": 50.0}          # a previous good read
    s._compute_book_truth(None)
    assert s._broker_qty is None, "an unreadable read must return the cache to UNKNOWN (#649)"


def test_an_EMPTY_but_readable_venue_is_an_empty_dict():
    """The venue answered and holds nothing — a real answer, distinct from unreadable."""
    s = _probe()
    s._compute_book_truth([])
    assert s._broker_qty == {}


def test_the_lookup_key_is_the_one_publish_external_uses():
    """Two derivations of one key drift: `_publish_external` looks up `instrument_id.rsplit('.', 1)[0]`.
    A symbol with a dot in it (BRK.B.XNYS) must fold to the same key on both sides."""
    s = _probe()
    s._compute_book_truth([_report("BRK.B.XNYS", "LONG", "3")])
    assert "BRK.B" in s._broker_qty, s._broker_qty
    assert "BRK.B.XNYS".rsplit(".", 1)[0] in s._broker_qty
