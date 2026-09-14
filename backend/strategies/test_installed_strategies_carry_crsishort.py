"""THE ONE LOUD TEST for a moving dependency (#858).

The cockpit venv resolves `kumo_strategies` from a sibling WORKING TREE (`uv pip install -e`), and
that tree changes branch under this suite — on 2026-09-10 it moved from `feat/one-backtest-runner`
(adapter present) to `fix/133-protective-fill` (adapter absent) between two runs, and every
CRSISHORT test went from green to `ImportError` with no change in this repo.

Every CRSISHORT test therefore SKIPS when the adapter is absent — and this test FAILS, so the suite
stays red by exactly one named line instead of twenty ImportErrors or, worse, a green suite that
measured nothing. RED HERE MEANS: align the venv to a kumo-strategies revision that carries
`runtime/nautilus/crsi_short.py` and `strategies/crsi_short.LOCATABLE` (the branch to be pinned
when kumo-strategies#123 lands on main).
"""
from __future__ import annotations

import importlib.util

import pytest


def crsishort_installed() -> bool:
    return (importlib.util.find_spec("kumo_strategies.runtime.nautilus.crsi_short") is not None
            and importlib.util.find_spec("kumo_strategies.strategies.crsi_short") is not None)


def _installed_is_the_pin() -> bool:
    """A developer venv resolves `kumo_strategies` from a sibling WORKING TREE (`uv pip install -e`),
    which can be on any branch; CI and the image install the PIN. The strict xfail below is a
    statement about the pin, so it only runs where the pin is what is installed."""
    import kumo_strategies

    return "site-packages" in str(getattr(kumo_strategies, "__file__", ""))


@pytest.mark.skipif(not _installed_is_the_pin(),
                    reason="kumo_strategies is an editable working tree here, not the pin — the pin "
                           "statement below is only meaningful against the pin (CI, the image)")
# The strict xfail that sat here FAILED-BY-PASSING on the pin bump to 4d28488, as designed: the pin
# now carries the CRSISHORT adapter, so the mark is deleted and this test asserts for real (#885).
def test_the_installed_kumo_strategies_carries_the_CRSISHORT_adapter():
    import kumo_strategies

    assert crsishort_installed(), (
        f"kumo_strategies at {kumo_strategies.__file__} has no CRSISHORT adapter — the sibling tree "
        f"is on the wrong branch, or the pin predates kumo-strategies#123. Every CRSISHORT test in "
        f"this suite is SKIPPED until it does; nothing about the lane is being measured.")
