"""#525: `load_contracts` resolution is SERIAL upstream, and the two cache knobs are DEAD.

WHY THIS FILE EXISTS. The #511 fix (db9b1e2) handed the IBKR exec client a `load_contracts` set built
from the deployment's 98 declared symbols. Five of them do not resolve at IB. The node then took
~62s per unresolvable name before finishing connect, and came up RUNNING with ZERO strategies. I
caused that, and the mitigation was a hand-maintained symbol exclusion.

THE ROOT CAUSE IS UPSTREAM AND CANNOT BE CONFIGURED AWAY.
`InteractiveBrokersInstrumentProvider.load_ids_with_return_async` is:

    for instrument_id in instrument_ids:
        loaded_ids = await self.load_with_return_async(instrument_id, filters)

One `await` per contract, in order. There is no gather, no concurrency limit, no per-contract
timeout, and no "skip on failure" — a name that does not resolve costs IB's full
`reqContractDetails` timeout and every contract behind it waits. So connect time is
`(unresolvable count) x (IB timeout)`, and `load_contracts` size is a connect-time budget, not a
free list.

THE TRAP THIS FILE EXISTS TO SPRING.
`InteractiveBrokersInstrumentProviderConfig` advertises `cache_validity_days` and `pickle_path`.
Both read as the obvious fix — cache the resolutions, stop paying on every boot. In our pinned
version BOTH ARE DEAD:

    cache_validity_days   assigned at providers.py:87, never read again. The line below it is
                          literally `# TODO: If cache_validity_days > 0 and Catalog is provided`.
    pickle_path           appears ONLY in config.py — its docstring, its id tuple, its declaration.
                          No consumer anywhere in the adapter.

Settable, documented, sweepable, and byte-identical either way. That is the `PortfolioConfig` shape
from CLAUDE.md — flags computed and discarded — except here it is in NautilusTrader itself. Setting
`cache_validity_days=7` would have "fixed" #525 while changing nothing, and it would have been
reported as fixed. Agreement is not connection.

WHAT THIS DOES NOT CLAIM. This is not a test of our code and it does not fix #525. It is a
conformance pin on the INSTALLED package, so that the wrong fix fails loudly at the moment someone
reaches for it, and so a version bump that finally implements either field shows up as a red test
inviting the real fix rather than passing silently.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from nautilus_trader.adapters.interactive_brokers import providers as ib_providers
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersInstrumentProviderConfig,
)


def _provider_src() -> str:
    return pathlib.Path(inspect.getfile(ib_providers)).read_text()


def test_the_fixture_can_see_the_installed_provider():
    """THE FIXTURE'S OWN PROPERTY FIRST. Every assertion below is a source read of the installed
    package; if the loader were renamed or the file unreadable they would all pass vacuously."""
    src = _provider_src()
    assert "class InteractiveBrokersInstrumentProvider" in src
    assert "async def load_ids_with_return_async" in src, (
        "the loader was renamed upstream — every assertion in this file is now blind"
    )


def test_contract_resolution_is_SERIAL_so_load_contracts_size_is_a_connect_time_budget():
    """THE #525 MECHANISM. One await per contract, in order, with no failure skip.

    If this ever goes red because upstream added concurrency, #525's cost model is obsolete and the
    symbol exclusion should be revisited — that is the point of pinning it.
    """
    tree = ast.parse(_provider_src())
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_ids_with_return_async"),
        None,
    )
    assert fn is not None, "loader not found — covered by the fixture test above"

    loops = [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.AsyncFor))]
    assert loops, "the loader no longer iterates — re-read it, the cost model has changed"

    awaits_in_loop = [n for loop in loops for n in ast.walk(loop) if isinstance(n, ast.Await)]
    assert awaits_in_loop, (
        "no await inside the resolution loop — upstream may have batched this; #525's "
        "(unresolvable count) x (IB timeout) cost model needs re-measuring"
    )

    concurrent = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) in {"gather", "wait", "as_completed", "TaskGroup"}
    ]
    assert not concurrent, (
        "upstream now resolves contracts concurrently. #525 was filed against a strictly serial "
        "loop where one unresolvable name blocks every contract behind it — re-measure before "
        "keeping the symbol exclusion"
    )


@pytest.mark.parametrize("field", ["cache_validity_days", "pickle_path"])
def test_the_caching_knobs_are_ACCEPTED_AND_DISCARDED_so_they_cannot_fix_525(field):
    """DO NOT REACH FOR THESE. Both are settable, documented, and have no consumer.

    They are the obvious-looking fix for #525 — cache the resolutions, stop paying per boot — and
    they would change NOTHING while looking like a fix that shipped. A flag that is computed and
    discarded returns byte-identical results, which is precisely when a severed wire is invisible.

    RED MEANS GOOD NEWS: upstream implemented it, and #525 may now have a native fix.
    """
    assert field in InteractiveBrokersInstrumentProviderConfig().dict(), (
        f"{field} is no longer a config field — this pin is stale, re-read the config object"
    )

    src = _provider_src()
    private = f"self._{field}"
    reads = [
        line for line in src.splitlines()
        if private in line and not line.strip().startswith(f"{private} =")
    ]
    assert not reads, (
        f"`{field}` now has a consumer in the instrument provider:\n"
        + "\n".join(f"    {r.strip()}" for r in reads)
        + "\n\nUpstream implemented it. #525 (serial contract resolution blocking connect) may have "
        "a native fix now — measure it, and drop the hand-maintained symbol exclusion if it holds."
    )
