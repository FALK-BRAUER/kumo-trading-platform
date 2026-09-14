"""A repair that runs twice is a second mint, not a wasted effort (#744).

`plan_contra_closes` decides what to repair; `prepare_execution` re-checks it against the live book.
Neither remembers. This is the record, and its rules are the ones that keep the repair safe:

  - booking the same pair twice books offsetting legs against a book the first run already corrected,
    which RE-OPENS the short it closed;
  - process memory is not a record, because the engine is recreated on every deploy;
  - what must be recorded is what was AIMED at, since `contra_execute` is explicit that every failure
    this repair can cause is a failure of aim.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.contra_ledger import BOOKED, REFUSED, ContraLedger, Entry


@dataclass(frozen=True)
class _Leg:
    strategy_id: str = "MOMENTUM-002"
    position_id: str = "WHD.XNYS-MOMENTUM-002"
    side: str = "BUY"
    quantity: float = 28.0
    price: float = 70.57


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: a fresh ledger must report the pair as NOT booked, or the idempotency test
    below passes against a ledger that says no to everything."""
    assert not ContraLedger().already_booked("fp-1")


def test_a_BOOKED_pair_is_never_booked_again():
    """The property the whole module exists for."""
    led = ContraLedger()
    led.record("fp-1", "WHD.XNYS", BOOKED, legs=[_Leg()])
    assert led.already_booked("fp-1")


def test_a_REFUSAL_does_not_mark_a_pair_as_booked():
    """"We decided not to" and "we did it" must not collapse, or a refused pair blocks its own repair
    forever."""
    led = ContraLedger()
    led.record("fp-1", "WHD.XNYS", REFUSED, detail="moved since planning")
    assert not led.already_booked("fp-1")


def test_a_LATER_REFUSAL_CANNOT_UNDO_A_BOOKING():
    """THE ONE THAT WOULD CAUSE A SECOND MINT. After a successful repair the pair no longer matches
    the contra shape — that is what a repaired book LOOKS like — so the next tick refuses it. Letting
    that refusal overwrite the booking would make the repair look un-run and invite a second one.
    """
    led = ContraLedger()
    led.record("fp-1", "WHD.XNYS", BOOKED, legs=[_Leg()])
    led.record("fp-1", "WHD.XNYS", REFUSED, detail="pair no longer matches")
    assert led.already_booked("fp-1"), "a refusal downgraded a completed repair"


def test_the_LEGS_ARE_RECORDED_AS_AIMED():
    """Every failure this repair can cause is a failure of aim, so the position_id each leg targeted
    is the evidence. A summary that cannot answer 'what did we aim at' is not a record."""
    led = ContraLedger()
    e = led.record("fp-1", "WHD.XNYS", BOOKED,
                   legs=[_Leg(position_id="WHD.XNYS-MOMENTUM-002"),
                         _Leg(strategy_id="BCTROT-004", position_id="WHD.XNYS-BCTROT-004", side="SELL")])
    assert len(e.legs) == 2
    assert e.legs[0][1] == "WHD.XNYS-MOMENTUM-002"
    assert e.legs[1][1] == "WHD.XNYS-BCTROT-004"
    assert e.legs[1][2] == "SELL"


def test_it_SURVIVES_A_RESTART_via_load():
    """Process memory is not a record: the engine is recreated on every deploy, and a repair that
    'already ran' in a dead process is a repair that will run again."""
    led = ContraLedger()
    led.load([Entry(fingerprint="fp-1", instrument_id="WHD.XNYS", state=BOOKED)])
    assert led.already_booked("fp-1")


def test_RELOADING_the_same_entries_is_idempotent():
    led = ContraLedger()
    rows = [Entry(fingerprint="fp-1", instrument_id="WHD.XNYS", state=BOOKED)]
    led.load(rows); led.load(rows)
    assert led.summary()["booked"] == 1


def test_the_SUMMARY_names_what_was_repaired():
    """A count cannot be looked into — the whole subject of tonight's observability work."""
    led = ContraLedger()
    led.record("fp-1", "WHD.XNYS", BOOKED, legs=[_Leg(), _Leg()])
    led.record("fp-2", "CGAU.XNYS", REFUSED)
    s = led.summary()
    assert s == {"booked": 1, "refused": 1, "instruments_repaired": ["WHD.XNYS"], "legs_booked": 2}


@pytest.mark.parametrize("state", [BOOKED, REFUSED])
def test_the_two_states_are_distinct(state):
    assert BOOKED != REFUSED and isinstance(state, str) and state
