"""The repair books nothing unless explicitly armed, and never books a pair twice (#744).

`verify_and_book` decides; this is the pass that acts on the decision. Everything dangerous about the
repair lives here: it writes into the position book, and a leg aimed at an id that is not the real
one OPENS a position rather than closing one — re-minting the exact defect being repaired.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field
from types import SimpleNamespace

import api.engine_node as mod
from api.contra_ledger import BOOKED, ContraLedger


@dataclass(frozen=True)
class _Leg:
    instrument_id: str = "WHD.XNYS"
    strategy_id: str = "MOMENTUM-002"
    position_id: str = "WHD.XNYS-MOMENTUM-002"
    side: str = "BUY"
    quantity: float = 28.0
    price: float = 70.57


@dataclass(frozen=True)
class _Order:
    instrument_id: str = "WHD.XNYS"
    fingerprint: str = "fp-1"
    legs: tuple = ()


@dataclass(frozen=True)
class _Plan:
    orders: tuple = field(default_factory=tuple)


def _pair():
    return _Order(legs=(
        _Leg(side="BUY", strategy_id="MOMENTUM-002", position_id="WHD.XNYS-MOMENTUM-002"),
        _Leg(side="SELL", strategy_id="BCTROT-004", position_id="WHD.XNYS-BCTROT-004"),
    ))


def _probe(booked_calls):
    """A UiFeedStrategy with only what the pass touches, and the REAL method bound."""
    class _P(mod.UiFeedStrategy):
        @property
        def cache(self):
            return SimpleNamespace(
                positions_open=lambda: [
                    SimpleNamespace(id="WHD.XNYS-MOMENTUM-002", strategy_id="MOMENTUM-002", signed_qty=-28.0),
                    SimpleNamespace(id="WHD.XNYS-BCTROT-004", strategy_id="BCTROT-004", signed_qty=28.0),
                ],
                positions_closed=lambda: [],
            )

        @property
        def clock(self):
            return SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000)

        @property
        def log(self):
            return SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None,
                                   exception=lambda *a, **k: None)

    s = _P.__new__(_P)
    s._contra_ledger = ContraLedger()
    s._book_repair_leg = lambda leg, tag: booked_calls.append((leg.position_id, leg.side, tag))
    return s


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the pair must be verifiable against this book, or 'nothing booked' below is the
    fixture failing rather than the arm gate holding."""
    calls = []
    out = _probe(calls).run_book_repair(_Plan((_pair(),)), arm=True)
    assert out["ready"] == 1 and out["booked"] == 1


def test_UNARMED_BOOKS_NOTHING():
    """THE GATE. Every new automation gate in this repo is opt-in, and this one writes into the
    position book."""
    calls = []
    out = _probe(calls).run_book_repair(_Plan((_pair(),)), arm=False)
    assert out["ready"] == 1, "the pair should still be verified and reported"
    assert out["booked"] == 0 and calls == [], "an unarmed repair booked legs"


def test_the_DEFAULT_is_unarmed():
    """Called without the keyword, it must not act — a default that arms is the whole class of
    three-overnight-bug this repo has a rule about."""
    calls = []
    out = _probe(calls).run_book_repair(_Plan((_pair(),)))
    assert out["armed"] is False and calls == []


def test_ARMED_books_EVERY_leg_of_the_pair_at_its_VERIFIED_id():
    calls = []
    _probe(calls).run_book_repair(_Plan((_pair(),)), arm=True)
    assert [c[0] for c in calls] == ["WHD.XNYS-MOMENTUM-002", "WHD.XNYS-BCTROT-004"]
    assert [c[1] for c in calls] == ["BUY", "SELL"]


def test_a_pair_ALREADY_BOOKED_is_not_booked_again():
    """Running a repair twice books offsetting legs against a book the first run corrected, which
    RE-OPENS the short it closed. This is the guard that makes the repair safe to run on a timer."""
    calls = []
    s = _probe(calls)
    s._contra_ledger.record("fp-1", "WHD.XNYS", BOOKED, legs=[])
    out = s.run_book_repair(_Plan((_pair(),)), arm=True)
    assert calls == [] and out["already_done"] == 1 and out["booked"] == 0


def test_BOOKING_IS_RECORDED_so_the_next_tick_skips_it():
    """The ledger write must happen as part of the pass, not be left to a caller — otherwise the very
    next tick re-books the pair."""
    calls = []
    s = _probe(calls)
    s.run_book_repair(_Plan((_pair(),)), arm=True)
    assert s._contra_ledger.already_booked("fp-1")


def test_a_BOOKING_THAT_RAISES_is_recorded_as_REFUSED_not_left_untouched():
    """A pair that raised part-way through is NOT untouched. Leaving it unrecorded lets the next tick
    treat a half-applied pair as fresh, which is the second mint by another route."""
    class _Boom(Exception):
        pass

    s = _probe([])
    s._book_repair_leg = lambda leg, tag: (_ for _ in ()).throw(_Boom("venue said no"))
    out = s.run_book_repair(_Plan((_pair(),)), arm=True)
    assert out["booked"] == 0
    assert not s._contra_ledger.already_booked("fp-1")
    rows = s._contra_ledger.as_rows()
    assert rows and rows[0]["state"] == "refused" and "raised" in rows[0]["detail"]


def test_the_LEG_BOOKING_USES_THE_VERIFIED_ID_not_a_reconstruction():
    """`_apply_leg` reconstructs `PositionId(f"{instrument}-{strategy}")`. For transfers that is
    sound; for a repair it is the defect's re-entry route, and three module docstrings say so. The
    repair primitive must read `leg.position_id`."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.UiFeedStrategy._book_repair_leg)))
    fn = tree.body[0]
    # STRIP THE DOCSTRING FIRST. The prose says "the position id comes from `leg.position_id`", and
    # a substring check over `ast.unparse` matched THAT — so replacing the code with a
    # reconstruction left this test green. It was reading the comment, not the behaviour.
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    code = "\n".join(ast.unparse(n) for n in body)

    assigns = [n for n in ast.walk(ast.parse(code))
               if isinstance(n, ast.Assign) and any(
                   getattr(t, "id", None) == "pid" for t in n.targets)]
    assert assigns, "no `pid` assignment found — this test is blind"
    value = ast.unparse(assigns[0].value)
    assert "leg.position_id" in value, (
        f"the repair leg's target is built as {value!r} instead of the id verified against the live "
        f"book — that is the defect's re-entry route: a fill aimed at a reconstructed id OPENS a "
        f"position rather than closing one"
    )
    # And the fill must use that same value, not a second derivation of it.
    fills = [n for n in ast.walk(ast.parse(code))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "OrderFilled"]
    assert fills, "no OrderFilled construction found"
    aimed = {k.arg: ast.unparse(k.value) for k in fills[0].keywords}
    assert aimed.get("position_id") == "pid", (
        f"the fill aims at {aimed.get('position_id')!r} while the order was cached against `pid` — "
        f"order and fill booked against different positions is the split defect wearing the repair's "
        f"clothes"
    )


def test_CLOSED_POSITIONS_are_included_in_the_aim_check():
    """A leg aimed at a position that has since CLOSED must be caught by the aim check rather than
    silently opening a new one at that id — so the pass must pass closed positions in too."""
    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(mod.UiFeedStrategy.run_book_repair))))
    assert "positions_closed" in src
