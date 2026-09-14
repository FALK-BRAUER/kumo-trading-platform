"""#855 — the cycle-marking seam drops a SHORT whose quantity carries its own sign.

`_mark_cycle_financials` opens with a guard that reads as a sanity check:

    if not d.is_capital_deployed or d.avg_px_open is None or d.quantity <= 0:
        continue

Under the unsigned wire contract that guard means "skip a flat cycle". Under a signed one it means
"skip every short", and it skips it BEFORE any of the four marking fields is written. The cycle is
then published with `last_px`, `market_value`, `unrealized_pl` and `unrealized_plpc` all None — and
None on those fields is not an error anywhere downstream, it is the ordinary "no price yet" state the
UI renders as an em-dash. A short would show as held, engaged, correctly sized, and permanently
unmarked, on a plane whose whole job is to say what it is worth.

`market_value` on this path already shipped wrong once. The comment three lines below the guard says
so: WHD held long 28 under BCTROT-004 and short 28 under MOMENTUM-002, genuinely flat, and an unsigned
market value summed the legs to ~$3,849 of exposure that did not exist. That fix signed the ARITHMETIC.
This is the same defect one line earlier, in the REACHABILITY.

There is a second, quieter one behind it. Even with the guard passed, the percentage is computed as

    cost = basis * d.quantity
    if cost > 0: d.unrealized_plpc = d.unrealized_pl / cost

`basis * -28` is negative, so `cost > 0` is False and the percentage is silently withheld for a signed
short — while the dollars beside it are published. The magnitude is what a cost basis is; the sign
belongs to the position, not to the money it consumed. `abs(quantity)` is the fix, and it is asserted
separately here so removing only the `<= 0` guard does not look like a complete one.

THE SEAM, NOT THE UNIT. These drive `_publish_trades`, which is what production calls, and read the
numbers back off the PUBLISHED FRAME rather than off the DTO objects. A test that called
`_mark_cycle_financials` directly would pass over a `_publish_trades` that had stopped calling it —
the shape this repo has paid for repeatedly. The real methods are bound to a plain double the way
`test_realized_legs.py::_engine` does it, and the binding loop raises if one is renamed, so a missing
seam fails loudly instead of quietly stubbing itself out.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from api.engine_node import UiFeedStrategy
from api.models import TradeDTO

TRADER = "COCKPIT-TEST"
NOW_NS = 1_786_818_600 * 1_000_000_000
INST = "WHD.XNYS"
STRAT = "MOMENTUM-002"

#: The numbers, and why they are these numbers. Entry 100, mark 110: a short is DOWN $10 a share.
ENTRY = 100.0
LAST = 110.0
QTY = 28.0
#: 110 x -28. Negative because a short position is a liability, which is what `market_value` means
#: everywhere else in this file's neighbourhood (`engine_node.py` marks `last * signed`).
WANT_MARKET_VALUE = -3080.0
#: (110 - 100) x -28.
WANT_UNREALIZED = -280.0
#: -280 / (100 x |−28|) = -0.1. The basis is a MAGNITUDE — $2,800 of capital was committed whichever
#: way the position faces — so the sign of the ratio comes from the P&L alone.
WANT_PLPC = -0.1

#: Bound from the class, not stubbed. `_marking_basis` is included because it is the single function
#: two marking paths share, and a double that reimplemented it would be a third derivation of the
#: exact thing it exists to prevent.
REAL = ("_publish_trades", "_mark_cycle_financials", "_marking_basis")


class _Projection:
    """Stands in for a cycle projection. Returns the DTOs it was given — the point under test is what
    MARKING does to a projected short, not how the projection built it."""

    def __init__(self, dtos: list[TradeDTO]) -> None:
        self._dtos = dtos

    def project(self, cache, now_ns):  # noqa: ARG002 - signature must match production's caller
        return self._dtos


def _cycle(quantity: float, side: str = "SHORT") -> TradeDTO:
    """A HELD SHORT cycle as the projection hands it over, `quantity` spelled either way."""
    return TradeDTO(
        account_id="DU1",
        client_id="IB",
        instrument_id=INST,
        strategy_id=STRAT,
        cycle_id=f"DU1:IB:{INST}:{STRAT}:0",
        state="HELD",
        side=side,
        quantity=quantity,
        is_capital_deployed=True,
        is_engaged=True,
        avg_px_open=ENTRY,
        realized_pnl="0.00 USD",
        leg_count=1,
        opened_ts=0,
        last_event_ts=0,
    )


def _engine(dtos: list[TradeDTO]) -> SimpleNamespace:
    """A `UiFeedStrategy` double carrying only what the seam reads, with the methods under test bound
    live off the class.

    `_cycle_store`/`_loop` are None so the envelope-persistence block is skipped; `_broker_avg_entry`
    is absent so `_marking_basis` returns the engine's own basis with `contested=None` (the broker has
    not been asked — which must not read as agreement)."""
    eng = SimpleNamespace(
        cache=None,
        clock=SimpleNamespace(timestamp_ns=lambda: NOW_NS),
        log=SimpleNamespace(error=lambda m: None, warning=lambda m: None, info=lambda m: None),
        trader_id=TRADER,
        _projection_lock=threading.RLock(),
        _trade_cycles={STRAT: _Projection(dtos)},
        _cycles_seeded=True,
        _cycle_store=None,
        _loop=None,
        _last_good_trades=[],
        _last_close={INST: LAST},
        _realized_periods=None,
        published=[],
    )
    eng._publish = lambda key, payload: eng.published.append((key, payload))
    # Not under test, and each would drag an unrelated plane into the fixture: broker stop prices need
    # an HTTP client, session realized needs a Cache full of closed positions.
    eng._mark_broker_stop_prices = lambda d: None
    eng._session_realized = lambda: {}
    # The #846 legs plane (`_realized_windows` / `_realized_legs_status`) is not under test either;
    # the merged publisher reads both, and a double that lacks them fails the frame ("seeding path").
    from api.realized import empty_windows

    eng._realized_windows = lambda: empty_windows(None)
    eng._lane_flows = lambda: {"by_day": {}, "earliest": None, "horizon_days": None, "error": None}  # nor the #699 flows plane
    eng._realized_legs_status = lambda: {"state": "never_ran", "restored": 0, "held": 0, "live": 0,
                                         "seed_stopped_at": None, "complete": False}
    for name in REAL:
        real = getattr(UiFeedStrategy, name, None)
        # A renamed seam must fail HERE, loudly, rather than leaving the double with a stub that
        # publishes a clean frame forever.
        assert real is not None, f"UiFeedStrategy has no {name} — the seam moved"
        setattr(eng, name, real.__get__(eng))
    return eng


def _publish(quantity: float, side: str = "SHORT") -> dict:
    """Drive the real publisher once and return the single published cycle row."""
    eng = _engine([_cycle(quantity, side)])
    eng._publish_trades()
    frames = [p for k, p in eng.published if k == "trades"]
    assert frames, "nothing was published on the trades key"
    rows = frames[-1]["trades"]
    assert len(rows) == 1, f"expected one cycle row, got {len(rows)}"
    return rows[0]


# -- the fixture must be able to express the bug -------------------------------------------------


def test_the_fixture_expresses_the_bug__a_signed_short_is_a_constructible_cycle():
    """If `TradeDTO` refused a negative quantity there would be no defect to find, and every
    assertion below would be about an object production can never build."""
    d = _cycle(-QTY)
    assert d.quantity == -QTY
    assert d.quantity < 0
    assert abs(d.quantity) == QTY
    assert d.side == "SHORT"
    # It arrives UNMARKED. Marking is what is under test, so a fixture that already carried the
    # numbers would pass every assertion without the seam running at all.
    assert d.last_px is None and d.market_value is None and d.unrealized_pl is None


def test_the_fixture_expresses_the_bug__the_publisher_reaches_marking_at_all():
    """Three conditions in `_publish_trades` return early with a `seeding` frame and never mark
    anything: no cycles, not seeded, or a projection that yields nothing. Any of them and the
    None-valued assertions below would pass against a row marking never touched."""
    eng = _engine([_cycle(QTY)])
    eng._publish_trades()
    frame = [p for k, p in eng.published if k == "trades"][-1]
    assert frame["status"] == "ok", "the publisher took the seeding path — marking never ran"
    assert frame["trades"][0]["last_px"] == LAST, "marking ran but produced no price"


# -- the seam -------------------------------------------------------------------------------------


def test_a_signed_short_is_MARKED_not_skipped():
    """`quantity <= 0` reads as "skip a flat cycle" and means "skip every short"."""
    row = _publish(-QTY)
    assert row["last_px"] == LAST
    assert row["market_value"] == WANT_MARKET_VALUE
    assert row["unrealized_pl"] == WANT_UNREALIZED


def test_a_signed_short_gets_a_PERCENTAGE_too():
    """The second defect, behind the first: `cost = basis * quantity` is negative for a signed short,
    so `if cost > 0` withholds the percentage while publishing the dollars beside it. A basis is the
    capital committed — a magnitude — so the ratio's sign must come from the P&L alone."""
    row = _publish(-QTY)
    assert row["unrealized_plpc"] == pytest.approx(WANT_PLPC)


def test_the_unsigned_spelling_is_marked_the_same_way():
    """The sibling that passes today. It is here so the failures above cannot be read as "marking is
    broken for shorts" — the arithmetic is right and the reachability is not, and a fix aimed at the
    arithmetic would break this one."""
    row = _publish(QTY)
    assert row["last_px"] == LAST
    assert row["market_value"] == WANT_MARKET_VALUE
    assert row["unrealized_pl"] == WANT_UNREALIZED
    assert row["unrealized_plpc"] == pytest.approx(WANT_PLPC)


def test_the_two_spellings_of_one_position_publish_the_same_financials():
    """Verification by disagreement, stated as the invariant rather than as two number lists: 28 short
    and -28 short are the same position, so every marked field must match. This is the assertion that
    survives a change to ENTRY/LAST, and the one that catches a fix which makes the signed path
    reachable but arrives at a different answer."""
    signed = _publish(-QTY)
    unsigned = _publish(QTY)
    for field in ("last_px", "market_value", "unrealized_pl", "unrealized_plpc"):
        assert signed[field] == unsigned[field], f"{field} disagrees between the two spellings"


def test_a_signed_LONG_is_not_turned_into_a_short():
    """The other direction. A fix that keys the sign off `quantity` instead of off `side` would mark a
    stray-signed long as a short — trading one wrong row for another."""
    d = _cycle(-QTY)
    d.side = "LONG"
    eng = _engine([d])
    eng._publish_trades()
    row = [p for k, p in eng.published if k == "trades"][-1]["trades"][0]
    assert row["market_value"] == LAST * QTY
    assert row["unrealized_pl"] == (LAST - ENTRY) * QTY


def test_a_genuinely_flat_cycle_is_still_skipped():
    """The guard the `<= 0` was reaching for is real: a qty-0 ARMED-then-flat cycle has nothing to
    mark. A fix must not mark it 0 and publish `market_value: 0.0`, which reads as "worth nothing"
    rather than "holds nothing"."""
    row = _publish(0.0)
    assert row["market_value"] is None
    assert row["unrealized_pl"] is None


def test_a_FLAT_row_carrying_a_quantity_is_SKIPPED_not_marked_at_zero():
    """The skip predicate and the sign predicate must be ONE predicate (impl review, #855): a FLAT
    row with a nonzero quantity must not be marked — `market_value: 0.0` on a summed field is
    absence rendered as a confident zero. Not producible by `trade_cycle.py:337` today (FLAT only
    with 0.0), which is exactly why it is pinned here rather than found later."""
    row = _publish(QTY, side="FLAT")
    assert row["market_value"] is None and row["unrealized_pl"] is None and row["last_px"] is None
