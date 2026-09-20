"""A claim on a symbol SEVERAL lanes hold is a guess, not a claim (#749).

WHAT CLAIMS ARE FOR. Reconciliation generates flatting orders, and with no claimant Nautilus books
them under EXTERNAL — NETTING's `{instrument}-{strategy}` id then makes a PHANTOM position instead of
closing the real one. HSBC sat as a -93 short the broker had never heard of. So claiming is
protective and must not simply be switched off.

WHAT WENT WRONG. `get_external_order_claim` returns THE claiming strategy, and a lane claims every
symbol it held at build. When two lanes hold one symbol — measured: 31 of 59 on the live book — the
claimant is whoever registered, regardless of whose shares actually moved. Every correction caused by
BCTROT's shares landed on MOMENTUM, because MOMENTUM claims broadly and BCTROT deliberately claims
nothing (claims are node-exclusive and both lanes share one pool).

That is not a bad guess, it is a guess presented as a fact — and it is why ~$2,400 of real realized
belongs to no lane while $94,000 of fabricated proceeds sit on MOMENTUM and EXTERNAL.

THE CUT: claim where the claim is UNAMBIGUOUS, refuse where it is not. A symbol one lane holds keeps
its claimant and keeps HSBC's protection. A symbol several lanes hold is refused, so its corrections
land on EXTERNAL — visibly unattributed, which breaks the per-lane sum in a way an operator can chase
rather than silently crediting a lane that did not trade.
"""

from __future__ import annotations

import pytest

from strategies.momentum import _resolve_claims


class _Iid:
    def __init__(self, text):
        self._t = text
        self.symbol = type("S", (), {"value": text.rsplit(".", 1)[0]})()

    def __str__(self):
        return self._t

    def __eq__(self, other):
        return str(self) == str(other)

    def __hash__(self):
        return hash(str(self))


class _Pos:
    def __init__(self, instrument_id, strategy_id):
        self.instrument_id = _Iid(instrument_id)
        self.strategy_id = strategy_id


class _Cache:
    def __init__(self, instruments, positions=()):
        self._i = [_Iid(i) for i in instruments]
        self._p = list(positions)

    def instrument_ids(self):
        return list(self._i)

    def positions_open(self):
        return list(self._p)


def test_a_symbol_only_THIS_lane_holds_is_still_claimed():
    """HSBC'S PROTECTION MUST SURVIVE. An unclaimed instrument lets a reconciliation flatting order
    book under EXTERNAL as a phantom, which is the incident claims exist for. Narrowing must not
    become switching off."""
    cache = _Cache(["AEM.XNYS"], [_Pos("AEM.XNYS", "MOMENTUM-002")])
    got = _resolve_claims(["AEM"], cache, "MOMENTUM-002")
    assert [str(i) for i in got] == ["AEM.XNYS"]


def test_a_symbol_ANOTHER_LANE_ALSO_HOLDS_is_REFUSED_and_said_out_loud():
    """THE FIX. Claiming here makes this lane the destination for corrections caused by the OTHER
    lane's shares — a guess presented as a fact, and the mechanism behind every phantom short on the
    live book. Refusing sends them to EXTERNAL instead: visibly unattributed, which an operator can
    chase, rather than silently credited to a lane that did not trade."""
    said = []
    cache = _Cache(["AEM.XNYS"],
                   [_Pos("AEM.XNYS", "MOMENTUM-002"), _Pos("AEM.XNYS", "BCTROT-004")])
    got = _resolve_claims(["AEM"], cache, "MOMENTUM-002", _sink=said.append)

    assert got == [], "an ambiguous claim is a guess"
    assert said and "more than one" in said[0].lower(), said
    assert "AEM" in said[0]


def test_the_UNAMBIGUOUS_ones_survive_alongside_the_refused_one():
    """A partial refusal must not throw the rest away — that would be the HSBC exposure again, for
    symbols whose ownership was never in doubt."""
    said = []
    cache = _Cache(["AEM.XNYS", "WPM.XNYS"],
                   [_Pos("AEM.XNYS", "MOMENTUM-002"), _Pos("AEM.XNYS", "BCTROT-004"),
                    _Pos("WPM.XNYS", "MOMENTUM-002")])
    got = _resolve_claims(["AEM", "WPM"], cache, "MOMENTUM-002", _sink=said.append)
    assert [str(i) for i in got] == ["WPM.XNYS"]


def test_a_FLAT_sibling_position_does_not_make_a_symbol_ambiguous():
    """THE CASE THAT WOULD MAKE THIS REFUSE EVERYTHING. The cache retains CLOSED positions, and 31 of
    59 live symbols show a sibling lane at quantity ZERO. A lane holding none of it cannot be the
    owner of a correction, so it must not block the claim — otherwise the narrowing swallows nearly
    the whole book and reintroduces the phantom it was built to prevent."""
    said = []
    flat = _Pos("AEM.XNYS", "BCTROT-004")
    flat.quantity = 0
    held = _Pos("AEM.XNYS", "MOMENTUM-002")
    held.quantity = 9
    cache = _Cache(["AEM.XNYS"], [held, flat])
    got = _resolve_claims(["AEM"], cache, "MOMENTUM-002", _sink=said.append)
    assert [str(i) for i in got] == ["AEM.XNYS"], said


def test_a_symbol_NOBODY_holds_is_still_claimed():
    """A lane claiming a symbol it will trade but does not yet hold is unambiguous by definition —
    nobody else holds it either. Refusing here would remove protection before the first fill."""
    cache = _Cache(["AEM.XNYS"], [])
    assert [str(i) for i in _resolve_claims(["AEM"], cache, "MOMENTUM-002")] == ["AEM.XNYS"]


def test_a_PHANTOM_under_EXTERNAL_does_not_make_a_symbol_ambiguous():
    """EXTERNAL is not a lane. Counting it as one turns the damage into a RATCHET.

    THE LOOP. A symbol held by one real lane plus one open phantom leg looks like two holders, so the
    claim is refused. Refusing removes the protection, so the NEXT reconciliation flatting order for
    that symbol books under EXTERNAL as a NEW phantom — which keeps the symbol ambiguous on every
    subsequent boot. The bucket for unattributed money would decide, permanently, that the already
    damaged symbols must stay unprotected. Exactly the symbols that most need the protection back.

    EXTERNAL IS A REAL `StrategyId` in the installed package, so this is not hypothetical typing: it
    is the id Nautilus assigns when nobody claims, and phantom legs sit under it on today's live
    book (#742's mirrored shorts).

    "More than one LANE holds each" is what the refusal says out loud. This makes the code mean it.
    """
    cache = _Cache(
        ["AAPL.XNAS"],
        positions=[
            _Pos("AAPL.XNAS", "MOMENTUM-001"),   # the one real holder
            _Pos("AAPL.XNAS", "EXTERNAL"),       # a phantom leg, not a lane
        ],
    )
    said: list[str] = []

    # FIXTURE PROPERTY FIRST: the fixture must actually contain the thing under test, or the
    # assertion below passes against a rule that does nothing. Exactly one real lane, and EXTERNAL
    # present alongside it.
    holders = {str(p.strategy_id) for p in cache.positions_open()}
    assert "EXTERNAL" in holders, "fixture cannot express the bug: no EXTERNAL holder"
    assert len(holders - {"EXTERNAL"}) == 1, "fixture must have exactly ONE real lane"

    ids = _resolve_claims(["AAPL"], cache, "MOMENTUM-001", _sink=said.append)

    assert [str(i) for i in ids] == ["AAPL.XNAS"], (
        "a symbol one real lane holds must keep its claim and keep its #197 B8 protection, even "
        "when a phantom leg sits beside it under EXTERNAL"
    )
    assert not any("REFUSING" in m for m in said), f"refused on a phantom: {said}"


def test_TWO_REAL_LANES_are_still_refused_when_a_phantom_is_also_present():
    """The EXTERNAL exclusion must not become a way to stop refusing.

    Aim at the class: the risk of the fix above is that it is implemented as "ignore EXTERNAL" in a
    way that also swallows a genuine two-lane collision whenever a phantom happens to be open on the
    same symbol. The refusal still has to fire on the case it was built for.
    """
    cache = _Cache(
        ["AAPL.XNAS"],
        positions=[
            _Pos("AAPL.XNAS", "MOMENTUM-001"),
            _Pos("AAPL.XNAS", "BCTROT-001"),
            _Pos("AAPL.XNAS", "EXTERNAL"),
        ],
    )
    said: list[str] = []
    ids = _resolve_claims(["AAPL"], cache, "MOMENTUM-001", _sink=said.append)

    assert ids == [], "two real lanes hold it — that is the collision #749 refuses"
    assert any("REFUSING" in m and "AAPL" in m for m in said), f"refusal not said: {said}"
