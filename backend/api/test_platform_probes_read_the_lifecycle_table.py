"""The platform probe resolver must read the lifecycle a DISABLED row actually lives in (#638 seam).

THE WIRE WAS CUT AND MY OWN FIX'S TEST DID NOT DRIVE IT. #638's short-circuit keys on
platform["lifecycle"] == "DISABLED" — and the resolver computed that field as

    (last_decisions(...).get(sid) or {}).get("lifecycle") or "TRADING"

where `last_decisions` returns {"session", "slot"} and has NEVER carried a "lifecycle" key. Always
None, always `or "TRADING"`: every lane read TRADING regardless of exec_strategy_state, the
DISABLED short-circuit was unreachable from production, and staging kept paging
`PREFLIGHT DEGRADED MOMENTUM-002: budget=0.0` after the fix deployed (measured 17:19-17:23 UTC,
2026-08-28). The #638 test built its platform dict BY HAND — unit green, seam dead.

Also here: the budget line was the THIRD copy of #648's falsy predicate
(`min(actual, target) if target else actual`) — a wound-down lane's platform budget probe read its
full actual.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import api.engine_node as mod


class _Sleeve:
    def __init__(self, actual, target):
        self.actual, self.target = actual, target


def _drive(monkeypatch, *, states, sleeves, siblings):
    import api.budget_store as bs
    import api.db.engine as eng

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(eng, "session_factory", lambda: _Session(), raising=False)

    async def _decisions(_s):
        # WHAT PRODUCTION ACTUALLY RETURNS — the double must not grow a lifecycle key the real
        # function has never had, or this test recreates the defect it exists to catch.
        return {"BCTROT-004": {"session": "2026-08-27", "slot": "open+215m"}}

    async def _states(_s):
        return dict(states)

    async def _book(_s):
        return SimpleNamespace(sleeves=dict(sleeves))

    monkeypatch.setattr(bs, "last_decisions", _decisions, raising=False)
    monkeypatch.setattr(bs, "load_book", _book, raising=False)
    monkeypatch.setattr(bs, "lifecycle_states", _states, raising=False)

    host = SimpleNamespace(
        _sibling_strategies=siblings,
        _platform_probes=None,
        log=SimpleNamespace(warning=lambda *a, **k: (_ for _ in ()).throw(AssertionError(a))),
    )
    asyncio.run(mod.UiFeedStrategy._refresh_platform_probes(host))
    return host._platform_probes


def test_a_DISABLED_row_reaches_the_lifecycle_probe(monkeypatch):
    probes = _drive(monkeypatch,
                    states={"MOMENTUM-002": "DISABLED"},
                    sleeves={"MOMENTUM-002": _Sleeve(0.0, 0.0)},
                    siblings=["MOMENTUM-002"])
    assert probes["MOMENTUM-002"]["lifecycle"] == "DISABLED", (
        "the DISABLED row never reaches the probe — the #638 short-circuit is unreachable from "
        "production and the budget=0 ERROR spam continues"
    )


def test_an_absent_row_is_still_TRADING(monkeypatch):
    """the operator 2026-08-19: an absent row means TRADING. The wire fix must not invert this back."""
    probes = _drive(monkeypatch, states={}, sleeves={"BCTROT-004": _Sleeve(1.0, 1.0)},
                    siblings=["BCTROT-004"])
    assert probes["BCTROT-004"]["lifecycle"] == "TRADING"


def test_the_budget_probe_uses_the_one_sizing_predicate(monkeypatch):
    """#648's third copy: target=0 (wound down) must probe 0, not the full actual — tested with a
    value the default could not produce."""
    probes = _drive(monkeypatch,
                    states={},
                    sleeves={"QC345-003": _Sleeve(50_000.0, 0.0)},
                    siblings=["QC345-003"])
    assert probes["QC345-003"]["budget"] == 0.0, (
        "the platform budget probe still carries the falsy-target escape (#648)"
    )
