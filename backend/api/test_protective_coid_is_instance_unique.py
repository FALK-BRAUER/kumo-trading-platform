"""Two instances must not mint the same protective client order id (#762).

MEASURED 2026-08-31, on two running stacks against two different brokers:

    paper   (Alpaca)  PROT-SELL-AEM-XNYS-00000001  AEM.XNYS  ACCEPTED
    staging (IBKR)    PROT-SELL-AEM-XNYS-00000001  AEM.XNYS  REJECTED

Byte-identical. And on staging every protective order was rejected — nine of nine, none resting —
with the engine reporting:

    PROTECTION DIVERGENCE on AEM.XNAS: the broker holds 1 resting protective order(s) this engine
    cannot see (PROT-SELL-AEM-XNYS-00000001). ... Exits will be rejected on `available: 0`

Nautilus denies a duplicate client order id LOCALLY, so those nine never reached IBKR. The staging UI
read `22 of 23 unprotected`, which was accurate.

THE DERIVATION WAS ONE TERM SHORT. `sha1(instrument|side|attempt|lane)`. Its own docstring records
instrument, side, attempt and lane each being added after a collision — every one found the same way,
by something going naked. The account is the next term in that series and the only one that spans
instances.

`trader_id` is used rather than a new environment variable: it already differs between the stacks
(PLATFORM-001 vs PLATFORM-STG), is already required to construct a node, and cannot be forgotten on a
new instance the way an optional env var can.
"""

from __future__ import annotations

import pytest

from api.engine_node import _protection_coid

PAPER, STAGING = "PLATFORM-001", "PLATFORM-STG"


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the two trader ids must actually differ."""
    assert PAPER != STAGING


def test_TWO_INSTANCES_mint_DIFFERENT_ids_for_the_same_position():
    """The measured defect, pinned. Same instrument, same side, same attempt, same lane."""
    a = _protection_coid("AEM.XNYS", "SELL", 0, "MOMENTUM-002", PAPER)
    b = _protection_coid("AEM.XNYS", "SELL", 0, "MOMENTUM-002", STAGING)
    assert a != b, (
        f"both instances mint {a} — Nautilus denies a duplicate client order id locally, so the "
        f"second stack's stop never reaches its venue and that position is naked"
    )


def test_THE_SAME_INSTANCE_is_still_deterministic():
    """Idempotency WITHIN an identity is the property #295 added the attempt axis to protect: a
    resubmit of the same intent must reuse the id, or a duplicate order rests at the venue."""
    a = _protection_coid("AEM.XNYS", "SELL", 0, "MOMENTUM-002", PAPER)
    b = _protection_coid("AEM.XNYS", "SELL", 0, "MOMENTUM-002", PAPER)
    assert a == b


@pytest.mark.parametrize("axis,other", [
    ("side", dict(side="BUY")),
    ("attempt", dict(attempt=1)),
    ("lane", dict(lane="BCTROT-004")),
    ("instrument", dict(instrument_id="BDX.XNYS")),
])
def test_EVERY_EXISTING_AXIS_still_discriminates(axis, other):
    """Adding a term must not collapse the ones already there. Each was added after a real collision
    and each is pinned here so the next addition cannot quietly undo them."""
    base = dict(instrument_id="AEM.XNYS", side="SELL", attempt=0,
                lane="MOMENTUM-002", trader_id=PAPER)
    a = _protection_coid(**base)
    b = _protection_coid(**{**base, **other})
    assert a != b, f"the {axis} axis no longer discriminates"


def test_the_id_STILL_FITS_Nautilus_36_character_cap():
    """The trader id is hashed, never appended: the readable head must stay legible in the blotter and
    the whole id must stay inside the identifier cap that produced the original truncation bugs."""
    coid = _protection_coid("AVERYLONGINSTRUMENTNAMEHERE1.XNAS", "SELL", 9,
                            "MOMENTUM-002", "COCKPIT-A-VERY-LONG-TRADER-ID")
    assert len(coid) <= 36, f"{coid} is {len(coid)} chars"
    assert coid.startswith("PROT-SELL-")


def test_it_stays_LEGIBLE_in_the_blotter():
    assert _protection_coid("AEM.XNYS", "SELL", 0, "", PAPER).startswith("PROT-SELL-AEM-XNYS")


def test_an_ABSENT_trader_id_does_not_silently_collapse_the_axis():
    """A caller that forgets the argument must not land back on the defective behaviour. Empty is
    still a value here, but it must differ from a real one — otherwise the one caller that omits it
    reintroduces the collision for every instance at once."""
    assert (_protection_coid("AEM.XNYS", "SELL", 0, "MOMENTUM-002", "")
            != _protection_coid("AEM.XNYS", "SELL", 0, "MOMENTUM-002", PAPER))


def test_the_ACCESSOR_actually_returns_the_nodes_trader_id():
    """The seam, and it was measurably open.

    Making `_trader_id_str` return "" unconditionally left every test above GREEN: the hash function
    discriminates correctly on an argument nothing was checking the value of, so both instances would
    have collided again through a correct-looking derivation. Verified by mutation, not assumed.
    """
    from nautilus_trader.model.identifiers import TraderId

    from api.engine_node import UiFeedStrategy

    # THE REAL TYPE. A `SimpleNamespace` carrying a `__str__` attribute does NOT override `str()` —
    # dunder lookup goes to the type — so a double built that way returns "namespace(...)" and would
    # have passed a laxer assertion while production returned something else entirely.
    class _Probe(UiFeedStrategy):
        @property
        def trader_id(self):
            return TraderId("PLATFORM-STG-001")

    s = _Probe.__new__(_Probe)
    assert s._trader_id_str() == "PLATFORM-STG-001"


def test_the_ACCESSOR_NEVER_RAISES_into_the_protection_path():
    """It rides the order-submission path. A node that cannot name itself must degrade to "" rather
    than stop a protective stop being placed — the observation-must-not-break-its-subject rule."""
    from api.engine_node import UiFeedStrategy

    class _Broken(UiFeedStrategy):
        @property
        def trader_id(self):
            raise RuntimeError("no trader id on this node")

    assert _Broken.__new__(_Broken)._trader_id_str() == ""
