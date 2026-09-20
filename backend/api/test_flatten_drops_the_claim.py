"""A cockpit flatten of a LANE position must drop that lane's claim (#923). Red before the fix.

MEASURED 2026-09-11 (map for #922): `_handle_flatten_command` → `owner.close_position(pos, tags=[...])`
closes the position, and neither claim-release trigger in the strategies adapter fires on that fill —
`contract.py:211 is_foreign` matches client-order-id PREFIXES (`PROT-`, `FL-`, `TR-` …) while
`close_position` mints the coid from the owner's own factory and carries `FL-…` only as a TAG, and
`momentum_rotation.py:989 _record_terminal` returns early without a `session:` tag. So after a flatten
the `exec_position_state` row survives at its pre-flatten qty until the lane's next session — and a
stale claim narrows every NEIGHBOUR lane's `own_ceiling` on that symbol (#910's mechanism, the BETA
shape: 79 held, 2 sellable). The deferred flatten (`_DeferredFlatten.apply`) has the same gap.

THE FIX IS ON THE PLATFORM SIDE: the flatten paths drop the claim for (lane, symbol) after the close is
accepted, through the same `_drop_claim` seam `liquidate_lane` uses (#922). MANUAL-001 has no claim
ledger; only lane-owned positions are affected.
"""

from __future__ import annotations

import asyncio

from nautilus_trader.model.enums import TimeInForce

from api.engine_node import UiFeedStrategy
from api.test_flatten_tif_is_one_predicate import _ClearHost, _RecordingOwner
from api.test_lane_flatten_keeps_exit_pending import _Ledger


class _ClaimHost(_ClearHost):
    """The flatten harness plus the claim seam, RECORDED — and refusing a MANUAL drop, which has no ledger."""

    def __init__(self, ledger=None):
        super().__init__(ledger=ledger)
        self.dropped_claims: list[tuple[str, str]] = []

    async def _drop_claim(self, strategy_id, symbol):
        assert strategy_id != str(self.id), "MANUAL-001 has no claim ledger — nothing to drop"
        self.dropped_claims.append((strategy_id, symbol))


def _flatten(host, strategy_id="MOMENTUM-002"):
    return asyncio.run(host._handle_flatten_command("d" * 32, {
        "instrument_id": "AEM.XNYS", "strategy_id": strategy_id,
        "expected_side": "LONG", "expected_qty": 136}, "1-1"))


def test_fixture_property_the_owner_close_goes_out_and_the_claim_seam_is_recorded():
    host = _ClaimHost(ledger=_Ledger()); owner = _RecordingOwner()
    host._sibling_strategies["MOMENTUM-002"] = owner
    status, detail = _flatten(host)
    assert status == "ok", detail
    assert owner.calls and owner.calls[0]["time_in_force"] is TimeInForce.DAY
    assert isinstance(host.dropped_claims, list)


def test_an_owner_routed_flatten_DROPS_the_lanes_claim_for_that_symbol():
    host = _ClaimHost(ledger=_Ledger()); owner = _RecordingOwner()
    host._sibling_strategies["MOMENTUM-002"] = owner
    _flatten(host)
    assert host.dropped_claims == [("MOMENTUM-002", "AEM")], (
        "the position closed at the venue but the claims ledger still says MOMENTUM-002 holds 136 AEM — "
        "every neighbour lane's own_ceiling on AEM is narrowed by a claim for shares nobody holds (#910)")


def test_the_claim_is_dropped_AFTER_the_close_is_accepted_never_before():
    """Order matters: a claim dropped before the close would let a sibling lane's ceiling widen onto shares
    the venue still holds under this lane."""
    host = _ClaimHost(ledger=_Ledger()); owner = _RecordingOwner()
    seq: list[str] = []
    real_close = owner.close_position
    owner.close_position = lambda position, **kw: (seq.append("close"), real_close(position, **kw))[1]
    real_drop = host._drop_claim

    async def drop(sid, sym):
        seq.append("drop"); await real_drop(sid, sym)
    host._drop_claim = drop
    host._sibling_strategies["MOMENTUM-002"] = owner
    _flatten(host)
    assert seq == ["close", "drop"]


def test_a_close_the_owner_refuses_keeps_the_claim():
    class _Refusing(_RecordingOwner):
        def close_position(self, position, **kw):
            raise RuntimeError("venue refused")
    host = _ClaimHost(ledger=_Ledger()); host._sibling_strategies["MOMENTUM-002"] = _Refusing()
    status, detail = _flatten(host)
    assert status == "error" and "venue refused" in detail
    assert host.dropped_claims == [], "no close, no drop — the claim still describes real shares"


def test_the_DEFERRED_flatten_drops_the_claim_too():
    """`_DeferredFlatten.apply` re-runs the same close at the open through the owner; the claim gap is
    identical there. Drive the real apply with the row it expects."""
    from types import SimpleNamespace
    from api.engine_node import _DeferredFlatten
    host = _ClaimHost(ledger=_Ledger()); owner = _RecordingOwner()
    host._sibling_strategies["MOMENTUM-002"] = owner
    row = SimpleNamespace(manager_id="m" * 32, instrument_id="AEM.XNYS", strategy_id="MOMENTUM-002",
                          cycle_id=None, params={"expected_side": "LONG", "expected_qty": 136})
    status, err = asyncio.run(_DeferredFlatten().apply(host, row))
    assert status == "APPLIED", err
    assert host.dropped_claims == [("MOMENTUM-002", "AEM")]


def test_a_claim_that_will_not_drop_is_REPORTED_and_a_redelivery_does_NOT_close_twice():
    """The close is out before the drop runs. If the drop raises, the ack says so — and the deterministic
    coid must already be recorded, or a redelivery of the same command would submit a second close."""
    host = _ClaimHost(ledger=_Ledger()); owner = _RecordingOwner()
    host._sibling_strategies["MOMENTUM-002"] = owner

    async def refusing_drop(sid, sym):
        raise RuntimeError("journal down")
    host._drop_claim = refusing_drop
    status, detail = _flatten(host)
    assert status == "error" and "NOT dropped" in detail and "journal down" in detail
    assert len(owner.calls) == 1, "the close went out once"
    status2, detail2 = _flatten(host)          # the SAME command id, redelivered
    assert len(owner.calls) == 1, f"a redelivery closed again: {status2} {detail2}"
