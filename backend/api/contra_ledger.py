"""A durable record of every repair leg BOOKED, so the repair can be audited and never repeated.

WHY A LEDGER AND NOT A FLAG (#744). This repair writes into the position book. If it runs twice on
one pair it does not merely waste effort — the second run books offsetting legs against a book the
first already corrected, which re-opens the very short it closed. Idempotency here is not hygiene, it
is the difference between a repair and a second mint.

AND IT MUST SURVIVE A RESTART. Process memory is not a record: the engine is recreated on every
deploy, and a repair that "already ran" in a dead process is a repair that will run again. The
fingerprint is the durable key.

WHAT IS RECORDED IS WHAT WAS AIMED AT. `contra_execute`'s docstring is explicit that every failure
this repair can cause is a failure of AIM — a leg pointed at a PositionId that is not the real one
opens a new position instead of closing the short. So the ledger stores the exact `position_id` each
leg was aimed at, not a summary: when something goes wrong the first question is what we aimed at,
and a record that cannot answer it is not evidence.

REFUSALS ARE RECORDED TOO. A pair that was planned and then refused as moved is a fact about the
book, and "we decided not to" must be distinguishable from "we never looked" — the third state this
codebase keeps rediscovering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BOOKED = "booked"
REFUSED = "refused"


@dataclass(frozen=True)
class Entry:
    fingerprint: str
    instrument_id: str
    state: str
    detail: str = ""
    ts_ns: int = 0
    #: Every leg exactly as aimed: (strategy_id, position_id, side, quantity, price). Stored as a
    #: tuple of tuples so an entry cannot be mutated after the fact by whatever built it.
    legs: tuple = ()


@dataclass
class ContraLedger:
    """In-memory index over durable entries. The CALLER owns persistence; this owns the rules."""

    _by_fingerprint: dict[str, Entry] = field(default_factory=dict)

    def load(self, entries) -> None:
        """Rehydrate from durable storage at boot. Later entries win, so a re-read is idempotent."""
        for e in entries or ():
            self._by_fingerprint[str(e.fingerprint)] = e

    def already_booked(self, fingerprint: str) -> bool:
        e = self._by_fingerprint.get(str(fingerprint))
        return e is not None and e.state == BOOKED

    def record(self, fingerprint: str, instrument_id: str, state: str, *,
               legs=(), detail: str = "", ts_ns: int = 0) -> Entry:
        entry = Entry(
            fingerprint=str(fingerprint), instrument_id=str(instrument_id), state=str(state),
            detail=str(detail), ts_ns=int(ts_ns or 0),
            legs=tuple(
                (str(l.strategy_id), str(l.position_id), str(l.side), float(l.quantity), float(l.price))
                for l in legs
            ),
        )
        # A REFUSAL NEVER OVERWRITES A BOOKING. The pair was repaired; a later tick finding it moved
        # is expected — that is what a repaired book looks like — and letting it downgrade the record
        # would make the repair look un-run and invite a second one.
        prior = self._by_fingerprint.get(entry.fingerprint)
        if prior is not None and prior.state == BOOKED and entry.state != BOOKED:
            return prior
        self._by_fingerprint[entry.fingerprint] = entry
        return entry

    @property
    def booked(self) -> tuple[Entry, ...]:
        return tuple(e for e in self._by_fingerprint.values() if e.state == BOOKED)

    def as_rows(self) -> list[dict]:
        return [
            {"fingerprint": e.fingerprint, "instrument_id": e.instrument_id, "state": e.state,
             "detail": e.detail, "ts_ns": e.ts_ns, "legs": len(e.legs)}
            for e in sorted(self._by_fingerprint.values(), key=lambda e: (e.instrument_id, e.fingerprint))
        ]

    def summary(self) -> dict:
        booked = [e for e in self._by_fingerprint.values() if e.state == BOOKED]
        refused = [e for e in self._by_fingerprint.values() if e.state == REFUSED]
        return {
            "booked": len(booked),
            "refused": len(refused),
            "instruments_repaired": sorted({e.instrument_id for e in booked}),
            "legs_booked": sum(len(e.legs) for e in booked),
        }
