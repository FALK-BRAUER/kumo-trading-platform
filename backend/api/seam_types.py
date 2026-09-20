"""Structural types for the CROSS-REPO SEAMS, so a wrong object is a type error and not a Tuesday.

WHY THIS FILE EXISTS. Reviewing the defects of 2026-08-25, the most expensive one by a wide margin
was `run_boot_gate(..., broker=self._broker_account)` — the ACCOUNT FRAME, a plain dict, passed where
an object answering `equity()` belonged. It cost three commits, a day of a confidently wrong
explanation written into a docstring, and it was invisible to a green 2000-test suite because the
parameter carried no annotation at all.

    def run_boot_gate(strategies: dict, *, broker, platform: dict, ...)
                                              ^^^^^^ nothing to check against

Measured, not assumed — annotating that one parameter and running mypy produces:

    error: Argument "broker" to "run_boot_gate" has incompatible type
           "dict[Any, Any] | None"; expected "Broker"  [arg-type]

That is the whole defect, caught at write time, for one line of typing.

STRUCTURAL, NOT NOMINAL, AND THAT IS THE POINT. These are `Protocol`s, so kumo-trading-strategies' classes
satisfy them without importing anything from cockpit and without inheriting from us. The seam stays
one-directional — cockpit knows what it needs, the other repo is free to provide it however it likes.
A nominal base class here would be cockpit reaching across the boundary to dictate a hierarchy, which
is the thing the adapter split exists to prevent.

WHAT THESE DO NOT COVER. Only the family of defects where the WRONG OBJECT reaches a boundary. The
other three families this week each need their own check and none of them is typing:

    a name that moved (`broker` vs `_broker`)   -> capability/provenance pins in strategies/
    the environment (image, venv, credential)   -> bin/check-running-image-conforms.sh, a deploy gate
    unread framework behaviour                  -> read the INSTALLED package, per CLAUDE.md
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LaneBroker(Protocol):
    """What a lane's `preflight` actually calls, and what the account dict cannot do.

    `runtime_checkable` so `isinstance` works for the defensive check in `boot_gate._broker_of` —
    though that check deliberately tests `callable(x.equity)` rather than `isinstance`, because a
    Protocol's runtime check only looks at attribute PRESENCE and would accept an object whose
    `equity` is a float. That distinction is itself a defect this session caught by mutation.
    """

    def equity(self) -> float: ...

    def strategy_positions(self) -> dict: ...

    def last_price(self, symbol: str) -> float | None: ...


class Lane(Protocol):
    """A registered strategy, as cockpit reads it across the repo boundary.

    Every member here is something cockpit reaches for with a silent default somewhere
    (`getattr(s, "is_armed", None)`, `getattr(strategy, "preflight", None)`), which is why they are
    also pinned by provenance in `strategies/test_installed_strategies_accept_what_we_pass.py`. The
    Protocol says what we need; that test says the installed classes still have it.
    """

    @property
    def is_armed(self) -> bool | None: ...

    @property
    def is_running(self) -> bool: ...

    def preflight(self, broker: LaneBroker) -> list: ...
