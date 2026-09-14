"""The contract a strategy must satisfy to be registered by the platform (#318/#320).

Operator: "maybe you can even write an interface / protocol that a strategy needs to implement to register?"

This is that Protocol. It is deliberately SMALL, and what it leaves out matters as much as what it
requires.

WHAT IT ASKS FOR, AND WHY EACH ONE
-----------------------------------
Every member exists because the platform cannot function without it and cannot derive it:

  `external_id`        the key cockpit maps to its own internal id. The strategy's own name for
                       itself, stable upstream. Cockpit assigns the internal `NAME-tag` that keys
                       positions and never takes it from here — an adapter that hardcodes a tag is
                       naming something it does not own.

  `label`              what an operator reads on the settings and activation screens. A strategy
                       someone cannot identify is one they cannot safely switch on.

  `id`                 the cycle-attribution key. The NETTING position id is
                       {instrument}-{strategy_id}, so this is not a label — it decides which sleeve
                       owns a position and which book a P&L lands in.

                       NAMED `id` BECAUSE NAUTILUS ALREADY PROVIDES IT. The first version of this
                       contract asked for `strategy_id`, which every `Strategy` subclass would have
                       had to add as an alias for something it already had. A contract that demands a
                       second name for an existing member is a contract nobody can satisfy without
                       writing pointless code, and it made a conforming adapter look non-conforming.

  `claimed_instruments` what this strategy owns unattributed broker activity for. Nautilus'
                       `external_order_claims` are EXCLUSIVE ACROSS THE NODE and raise
                       InvalidConfiguration during `Trader.add_strategy`, so a node with two strategies
                       claiming one symbol does not degrade — it fails to boot. Only the platform can
                       see the overlap, because only it knows about all of them.

  `warmup_bars`        how much history before it may decide. QC345 needs 254 sessions; MOMENTUM needs
                       far fewer. The platform surfaces "not ready yet" instead of the operator
                       wondering why a funded strategy is holding nothing.

  `is_entry`           REMOVED 2026-08-22. It used to be required, on the reasoning that the platform
                       "must not guess" whether an order increases exposure. The platform does not
                       guess: `exec_client.py:599` sums a STRATEGY- AND INSTRUMENT-SCOPED net and
                       `budget_gate.is_entry` delegates to the pure `is_entry_order(net, side, qty)`,
                       which settles it completely — flips included. Requiring a per-strategy answer
                       was a second derivation of a fact already derived, called by nothing.

WHAT IT DOES NOT ASK FOR
------------------------
It does NOT ask the strategy to check its own budget, and that is the central decision. A strategy that
polices its own allocation does so correctly until the day it has a bug, and then holds more than it
was granted — silently, because nothing else was watching. Enforcement is `budget_gate.may_submit`, on
the path every order takes.

So the contract is: the strategy DECLARES, the platform DECIDES. A strategy is never asked to be
trustworthy about a limit; it is asked to be honest about what it is doing.

DENIAL IS NORMAL
----------------
A strategy WILL be refused entries while its sleeve is over target. That is not an error and must not
be treated as one. A strategy that logs an error, retries, or halts itself on refusal turns an ordinary
wind-down into an incident. See `docs/strategy-contract.md`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RegistrableStrategy(Protocol):
    """What the platform requires of anything it will register and fund.

    `runtime_checkable` so registration can assert conformance at startup rather than discovering a
    missing member when the strategy first tries to trade. Note that a runtime Protocol check verifies
    member PRESENCE, not signatures — `conforms()` below exists because that distinction has bitten
    this codebase before, where a member existed with the wrong shape and every check passed.
    """

    @property
    def id(self):
        """The Nautilus `StrategyId`, e.g. `QC345-003`. Stable forever: it keys positions, cycles and
        sleeves. Provided by `Strategy` itself — nothing needs to implement this."""
        ...

    @property
    def external_id(self) -> str:
        """The strategy's own stable name for itself, e.g. `QC345`. Cockpit maps it to an internal id.

        NOT the internal id, and not a tag. Whether upstream calls itself `QC345` or `QC345-003` is its
        business; cockpit owns `NAME-tag` because only cockpit sees every strategy in one trader.
        """
        ...

    @property
    def label(self) -> str:
        """Human-readable name for the operator surface."""
        ...

    @property
    def claimed_instruments(self) -> list[str]:
        """Instrument ids this strategy owns unattributed activity for. May be empty.

        Claiming too LITTLE is also a defect, not a safe default: without a claim, the synthetic
        flatting order reconciliation generates is booked under `EXTERNAL` and — under NETTING — OPENS
        a phantom position rather than closing the real one.
        """
        ...

    @property
    def warmup_bars(self) -> int:
        """Bars per symbol required before any decision is trustworthy."""
        ...


#: Members the platform genuinely cannot work without.
#: Members the platform genuinely cannot work without.
#:
#: `is_entry` WAS here and was removed 2026-08-22. Its rationale said the platform "must not guess"
#: entry-ness — but `exec_client.py:599` reads a STRATEGY- AND INSTRUMENT-SCOPED net and hands it to
#: `budget_gate.is_entry`, which delegates to kumo-strategies' pure `is_entry_order`. Net plus side
#: plus quantity determines it completely, flips included. Requiring a per-strategy answer made every
#: lane implement a SECOND DERIVATION of a fact already derived correctly, that nothing calls, to
#: satisfy a rule whose stated reason is untrue.
REQUIRED = ("external_id", "label", "claimed_instruments", "warmup_bars")


def conforms(candidate: object) -> tuple[bool, list[str]]:
    """Does `candidate` satisfy the contract? Returns (ok, missing members).

    Separate from `isinstance(x, RegistrableStrategy)` because a runtime Protocol check answers a
    WEAKER question than it appears to: it verifies that members exist, not that they have the right
    shape. This reports WHICH are missing, so a registration failure names the gap instead of saying
    "does not conform" and leaving someone to diff two files.
    """
    missing = [name for name in REQUIRED if not hasattr(candidate, name)]
    return (not missing, missing)
