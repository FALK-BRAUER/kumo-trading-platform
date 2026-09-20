"""Display backfill must not starve trading warmup after a boot (#618 step 2).

Measured 2026-08-28: a 12:54 ET restart put ~200 display requests into the paced queue while the
lanes' ~200 daily-bar warmup requests fought the same IB ~60/10min venue budget. At the 13:05 slot
warmup stood at 78% — below the 80% floor — and the first IBKR decision correctly refused. The
display plane can wait; the lanes cannot.

KUMO_DISPLAY_BACKFILL_HOLD_SECS holds the drain for N seconds after the feed starts. Since #836 the
queue also carries the lanes' warmups and the live subscribes (it was display-only when this was
written); the hold parks the DISPLAY tier (priority 3) only — a held position's planes, its ATR
history and the lanes' warmups are not display and leave during the hold. Default 0 = off: every
new behaviour gate defaults off (CLAUDE.md), and an unmetered venue skips the queue entirely anyway.
"""

from __future__ import annotations

from types import SimpleNamespace

import api.engine_node as mod
from api.observation import Observations


def _host(*, hold, now_ns, started_ns, queue):
    """`queue` is a list of (bar_type, kw); it is laid out the way `_enqueue_paced` lays it out (#836):
    a heap of (priority, seq, bound fn, args, kw, key), de-duplicated on `key`."""
    calls = []
    request_bars = lambda bt, **kw: calls.append(bt)  # noqa: E731
    heap = [(mod._DISPLAY_HISTORY_PRIORITY, i, request_bars, (bt,), kw, ("request", "feed", str(bt)))
            for i, (bt, kw) in enumerate(queue)]
    host = SimpleNamespace(
        _hist_rate=6.0,
        _observations=Observations(),
        _bar_request_q=heap,
        _bar_request_seq=len(heap),
        _bar_request_seen={k for *_, k in heap},
        _display_hold_secs=hold,
        _feed_started_ns=started_ns,
        clock=SimpleNamespace(timestamp_ns=lambda: now_ns),
        request_bars=request_bars,
    )
    return host, calls


def test_the_fixture_drains_when_no_hold_is_set():
    """FIXTURE PROPERTY + the default: hold 0 drains immediately — the gate ships OFF."""
    host, calls = _host(hold=0.0, now_ns=1_000, started_ns=0, queue=[("BT1", {})])
    mod.UiFeedStrategy._drain_bar_requests(host)
    assert calls == ["BT1"], "the paced drain does not drain at all — fixture broken"


def test_display_requests_WAIT_while_the_hold_stands():
    ns = 1_000_000_000
    host, calls = _host(hold=900.0, now_ns=100 * ns, started_ns=0, queue=[("BT1", {})])
    mod.UiFeedStrategy._drain_bar_requests(host)
    assert calls == [], (
        "display backfill drained inside the hold window — it competes with lane warmup for the "
        "venue's shared budget, which is the 78%-at-the-slot starvation (#618)"
    )
    assert list(host._bar_request_q), "the queue was dropped rather than held"


def test_the_hold_EXPIRES_and_display_drains_afterwards():
    ns = 1_000_000_000
    host, calls = _host(hold=900.0, now_ns=901 * ns, started_ns=0, queue=[("BT1", {})])
    mod.UiFeedStrategy._drain_bar_requests(host)
    assert calls == ["BT1"], "the hold never lifts — display would be dark forever, not degraded"
