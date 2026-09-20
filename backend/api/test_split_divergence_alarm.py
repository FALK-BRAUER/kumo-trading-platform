"""The cache's per-lane split and the claims ledger's must AGREE — and disagreement must page (#692).

Measured at the 15:40 ET close-20m slot, 2026-08-28: BDX totalled 55 both ways while the SPLITS
disagreed — cache MOMENTUM 55 / BCTROT flat, claims MOMENTUM 45 / BCTROT 10. BCTROT's runner sized
an exit off the 10-share claim; Nautilus holds it flat; only MOMENTUM's resting trailing stop
(cross-lane, unreleasable) stopped a cross-strategy sell. Two of four lanes carry no resting stop,
so the same divergence there reaches the venue. Totals-based checks (over_claimed, reconcile_drift)
are structurally blind to it: the mirrors sum to the truth.

Fires from the SCHEDULED sweep, not the order path — by sizing time a decision has already been
made, and the guard that fired for BDX was an accident of a resting order.
"""

from __future__ import annotations

from api.split_divergence import split_divergence


def _cache_rows():
    # what ui:state:positions actually carries (built from live paper, 2026-08-28)
    return [
        {"instrument_id": "BDX.XNYS", "side": "LONG", "quantity": 55.0, "strategy_id": "MOMENTUM-002"},
        {"instrument_id": "BDX.XNYS", "side": "FLAT", "quantity": 0.0, "strategy_id": "BCTROT-004"},
        {"instrument_id": "AEM.XNYS", "side": "LONG", "quantity": 46.0, "strategy_id": "BCTROT-004"},
        {"instrument_id": "TRT.AMEX", "side": "LONG", "quantity": 25.0, "strategy_id": "EXTERNAL"},
    ]


def _claims():
    return {"MOMENTUM-002": {"BDX": 45.0}, "BCTROT-004": {"BDX": 10.0, "AEM": 46.0}}


def test_the_fixture_reproduces_the_BDX_totals_agreement():
    """FIXTURE PROPERTY: totals AGREE (55 == 45+10) — the condition under which every existing
    detector stays green. If this stops holding, the tests below assert against a different defect."""
    cache_total = sum(r["quantity"] for r in _cache_rows() if r["instrument_id"].startswith("BDX"))
    claims_total = sum(c.get("BDX", 0) for c in _claims().values())
    assert cache_total == claims_total == 55.0


def test_the_BDX_split_disagreement_is_detected_per_lane():
    out = split_divergence(_cache_rows(), _claims())
    assert "BDX" in out, "the split disagreement is invisible — totals agreed and nothing looked deeper"
    assert out["BDX"]["BCTROT-004"] == {"claim": 10.0, "cache": 0.0}
    assert out["BDX"]["MOMENTUM-002"] == {"claim": 45.0, "cache": 55.0}


def test_an_agreeing_symbol_is_silent():
    out = split_divergence(_cache_rows(), _claims())
    assert "AEM" not in out, "an agreeing split alarms — the wallpaper direction"


def test_a_cache_position_with_no_claim_is_NOT_divergence_here():
    """MANUAL and reconciled-in positions legitimately carry no claim; the adoption path owns that
    case. This detector fires only where a CLAIM exists and the cache contradicts it."""
    rows = _cache_rows() + [{"instrument_id": "GLD.ARCX", "side": "LONG", "quantity": 5.0,
                             "strategy_id": "MANUAL-001"}]
    out = split_divergence(rows, _claims())
    assert "GLD" not in out


def test_a_SHORT_cache_row_counts_signed():
    """The #635 mirrors are SHORT rows beside longs; a claim compared against an unsigned quantity
    would read a mirrored lane as holding plenty."""
    rows = [
        {"instrument_id": "WHD.XNYS", "side": "LONG", "quantity": 28.0, "strategy_id": "MOMENTUM-002"},
        {"instrument_id": "WHD.XNYS", "side": "SHORT", "quantity": 28.0, "strategy_id": "MOMENTUM-002"},
    ]
    out = split_divergence(rows, {"MOMENTUM-002": {"WHD": 28.0}})
    assert out["WHD"]["MOMENTUM-002"] == {"claim": 28.0, "cache": 0.0}


def test_the_scheduled_announce_actually_pages_the_BDX_shape():
    """THE WIRING — drive the real _announce_split_divergence with the measured BDX state and a
    capturing notifier; the predicate being right proves nothing about the sweep calling it."""
    import asyncio
    from types import SimpleNamespace

    import api.alerts as al

    sent = []

    class _N:
        async def send(self, key, alert):
            sent.append((key, alert.title))

    class _Row:
        def __init__(self, sid, sym, qty):
            self._mapping = {"strategy_id": sid, "symbol": sym, "qty": qty}

    class _DB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, _q):
            return SimpleNamespace(all=lambda: [_Row("MOMENTUM-002", "BDX", 45.0),
                                                _Row("BCTROT-004", "BDX", 10.0)])

    import api.db.engine as eng
    orig = eng.session_factory
    eng.session_factory = lambda: _DB()
    try:
        host = SimpleNamespace(
            _node=SimpleNamespace(positions=lambda: _cache_rows()),
            _n=_N(),
            _split_alarmed=set(),
            _check_failed=lambda *a, **k: (_ for _ in ()).throw(AssertionError(repr(a))),
            _check_ok=lambda *a, **k: None,
            _split_pending={},
        )
        # TWICE: a page requires the divergence to survive two consecutive polls (the debounce that
        # ended the 22-races-in-one-poll burst); this divergence is durable, so it pages on the 2nd.
        asyncio.run(al.AlertsService._announce_split_divergence(host))
        asyncio.run(al.AlertsService._announce_split_divergence(host))
    finally:
        eng.session_factory = orig
    keys = [k for k, _ in sent]
    assert "split_divergence:BDX:BCTROT-004" in keys, (
        "the sweep never paged the BDX split disagreement — the predicate is tested and unreached"
    )
    assert "split_divergence:BDX:MOMENTUM-002" in keys


def test_the_run_loop_actually_calls_the_announce():
    """Bite evidence: removing the run-loop call left every test green — the announce was tested and
    unreached, the #651 'seven mechanisms driven by nothing' shape. Docstring-stripped source pin,
    same style as the backend's other wiring pins."""
    import ast
    import inspect
    import textwrap

    import api.alerts as al

    src = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(al.AlertsService.run))))
    assert "_announce_split_divergence()" in src, (
        "run() never calls _announce_split_divergence — the detector exists and nothing drives it"
    )


def test_a_divergence_must_SURVIVE_two_polls_before_paging():
    """Read back from live within minutes of deploying: 22 pages, most of them races — claims are
    written asynchronously around fills, so a poll landing mid-update sees a one-tick divergence
    that self-heals. A critical channel that pages on races becomes wallpaper (#638's lesson), so a
    pair pages only when the SAME (claim, cache) reading stands on two consecutive polls; a pair
    that changed or healed in between resets."""
    import asyncio
    from types import SimpleNamespace

    import api.alerts as al

    sent = []

    class _N:
        async def send(self, key, alert):
            sent.append(key)

    class _Row:
        def __init__(self, sid, sym, qty):
            self._mapping = {"strategy_id": sid, "symbol": sym, "qty": qty}

    class _DB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, _q):
            return SimpleNamespace(all=lambda: [_Row("MOMENTUM-002", "BDX", 45.0)])

    import api.db.engine as eng
    orig = eng.session_factory
    eng.session_factory = lambda: _DB()
    try:
        host = SimpleNamespace(
            _node=SimpleNamespace(positions=lambda: [
                {"instrument_id": "BDX.XNYS", "side": "LONG", "quantity": 55.0,
                 "strategy_id": "MOMENTUM-002"}]),
            _n=_N(), _split_alarmed=set(), _split_pending={},
            _check_failed=lambda *a, **k: None, _check_ok=lambda *a, **k: None,
        )
        asyncio.run(al.AlertsService._announce_split_divergence(host))
        assert sent == [], "paged on the FIRST sighting — every fill race becomes a critical page"
        asyncio.run(al.AlertsService._announce_split_divergence(host))
        assert sent == ["split_divergence:BDX:MOMENTUM-002"], "a persisting divergence never paged"
    finally:
        eng.session_factory = orig
