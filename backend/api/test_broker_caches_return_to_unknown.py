"""A broker cache with no path back to 'unknown' renders the last snapshot as CURRENT (#649 item 1).

`_reconcile_protection` populates `_broker_stop_prices` / `_broker_avg_entry` / `_broker_protected`
from a venue read. Its failure paths just `return` — so after ONE good tick, a permanent broker
outage leaves SECURED badges and stop prices rendering the dead snapshot as live truth, forever.
The exec client fixed exactly this shape for `_unrealized_totals`; the feed did not.

Downstream already speaks three states: `_mark_broker_stop_prices` renders `broker_protected=None`
as unknown, never as NAKED and never as SECURED. The caches just never said "unknown" again after
their first success.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import api.engine_node as mod


def _host(order_reports):
    """The REAL method bound to a minimal host — seam, not a re-implementation."""
    host = SimpleNamespace(
        _broker_stop_prices={"stale-coid": 12.34},
        _broker_avg_entry={"AAPL.XNAS": 190.0},
        _broker_protected={"AAPL.XNAS"},
        _protection_divergence=[],
        _exit_windows={},
        _naked_positions=[],
        _sweep_exit_windows=lambda: None,
        log=SimpleNamespace(warning=lambda *a, **k: None,
                            exception=lambda *a, **k: None,
                            info=lambda *a, **k: None,
                            debug=lambda *a, **k: None),
        _http=None,
        _venue_position_reports=order_reports["positions"],
        _venue_order_reports=order_reports["orders"],
        _protection_running=False,
        # Production carries this (#907); the pass takes and clears it before any gate, so a double
        # without it fails on the attribute rather than on the failure path under test.
        _flip_pending={},
        _flip_evaluated=False,
        clock=SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000),
    )
    # The REAL reset bound to the host — the method under test, not a stand-in.
    host._broker_state_unknown = lambda: mod.UiFeedStrategy._broker_state_unknown(host)
    return host


def _run(host):
    """Drive the REAL inner reconciler with protection enabled — the seam the ticks call."""
    asyncio.run(mod.UiFeedStrategy._reconcile_protection_inner(
        host, broker_rows=None, plan_protection=None, resolve=lambda domain: {"enabled": True},
        declared=lambda domain: {}))


def test_the_fixture_reaches_the_failure_path():
    """FIXTURE PROPERTY: the read genuinely fails — if this stops raising, the tests below assert
    against a path that no longer exists."""
    async def _boom():
        raise RuntimeError("venue unreachable")
    host = _host({"positions": _boom, "orders": _boom})
    _run(host)  # must not raise


def test_a_failed_broker_read_returns_the_caches_to_UNKNOWN():
    """After one good tick and then an outage, yesterday's stops must not render as today's."""
    async def _boom():
        raise RuntimeError("venue unreachable")
    host = _host({"positions": _boom, "orders": _boom})
    _run(host)
    assert host._broker_protected is None, (
        "a dead broker read left _broker_protected rendering the last snapshot as current (#649)"
    )
    assert host._broker_avg_entry is None
    assert host._broker_stop_prices == {}, (
        "stale stop prices survive a failed read and SECURED renders a dead number"
    )
