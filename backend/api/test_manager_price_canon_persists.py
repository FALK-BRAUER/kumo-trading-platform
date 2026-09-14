"""The canonicalised manager price must reach the DATABASE, not just the hash (#401).

WHAT WAS SEEN, live, 2026-08-21:

    GET /managers
      A.XNYS     stop_reenter_watch   ARMED    floor_price 131.44614999999993
      APA.XNAS   stop_reenter_watch   ARMED    floor_price  33.98330000000002
      MNDY.XNAS  stop_reenter_rearm   FAILED   "price 81.155 is not a valid tick for MNDY.XNAS"

Fourteen decimal places on a penny-ticked instrument. MNDY had already died of it — attached cleanly,
sat ARMED through the stop-out, and only met the tick check when the rearm tried to build the
re-entry's protective stop, FAILED and terminal with `rearm_count` still 0 and nothing on screen. A and
APA were armed 22 hours AFTER the fix for that (#375) shipped, and were armed into the same failure.

WHY THE FIX DID NOT FIX IT. `_reserve_attach_command` calls `_canon_manager_params`, which runs the
instrument's own `make_price`, and assigns the result to a LOCAL:

    params = self._canon_manager_params(handler, instrument_id, params)   # local
    ...
    return "proceed", "", ledger_cid                                       # params NOT returned

The caller then passed ITS OWN dict — the off-tick one — to `_commit_attach_command`, which is what
`mg.attach()` persists. So `make_price` ran on every attach, its output was hashed for idempotency, and
was then discarded. Every stop-and-reenter on the book carries an off-tick floor.

This is the DEAD-MECHANISM half of CLAUDE.md's verification-by-disagreement rule — *identical when it
should differ* — and the same shape as the four `PortfolioConfig` flags in kumo-strategies that were
computed, discarded, and returned byte-identical results under every setting.

WHY NO TEST CAUGHT IT. #375's suite tested `_canon_manager_params` and got a correct answer from it.
The unit was right; the WIRING dropped it on the floor. Precisely the class CLAUDE.md warns about:
"a passing test on a helper says nothing about whether anything calls it correctly." These tests assert
what reaches the PERSIST call, and nothing about the helper.
"""

from __future__ import annotations

import asyncio
import inspect

from api.engine_node import UiFeedStrategy


class _Instrument:
    """Alpaca equities are penny-ticked — `providers.py` builds every instrument with
    `price_precision=2`. This double quantises the same way, and is the reason these tests can tell a
    real canonicalisation from a no-op."""

    def make_price(self, v: float):
        return round(float(v) + 1e-12, 2)


class _Handler:
    PRICE_PARAMS = ("reclaim_price", "base_price", "floor_price")


class _Ledger:
    """Returns the REAL `ReserveResult`, because the caller reads `.outcome` off it.

    A double returning a bare tuple raised `AttributeError` inside the method under test — failing for
    the wrong reason and proving nothing. Build the double from what production actually emits.
    """

    async def reserve(self, *a, **k):
        from api.command_ledger import Reserve, ReserveResult

        return ReserveResult(outcome=Reserve.RESERVED)

    async def mark(self, *a, **k):
        return None


def test_the_canon_ITSELF_rounds_to_the_instrument_tick():
    """EXECUTED, not scanned.

    A first version of this asserted `"make_price" in inspect.getsource(...)` and passed with the call
    replaced by `round(asked, 6)` — because the function's own DOCSTRING says "make_price". A source
    scan cannot tell code from prose, and the mutation harness is what said so.
    """

    class _Fake:
        _instrument_or_raise = staticmethod(lambda _iid: _Instrument())
        _canon_manager_params = UiFeedStrategy._canon_manager_params

    out = _Fake()._canon_manager_params(
        _Handler(),
        "MNDY.XNAS",
        {"floor_price": 81.155, "base_price": 131.44614999999993, "reclaim_price": None, "qty": 112},
    )

    assert out["floor_price"] == 81.16, "MNDY's actual failing value must land on the penny"
    assert out["base_price"] == 131.45, "A's actual armed value, fourteen decimals, must be corrected"
    assert out["reclaim_price"] is None, "an absent price is not zero and must not be invented"
    assert out["qty"] == 112, "non-price params must pass through untouched"


def test_reserve_RETURNS_the_canonicalised_params():
    """EXECUTED, driving the real `_reserve_attach_command` to the point where it returns.

    The earlier version scanned for the `return ... params` line and for the string
    `_canon_manager_params`, and passed with the assignment stripped
    (`self._canon_manager_params(...)` instead of `params = self._canon_manager_params(...)`) — the
    canon still ran, its result still went nowhere, and the shipped bug was back.
    """
    import api.managers as mg

    class _Fake:
        _cmd_ledger = _Ledger()
        id = "MANUAL-001"
        # Production carries this; a double missing it fails on the attribute rather than on the
        # behaviour under test.
        _exec_client_id = "ALPACA-0000c0de"
        _instrument_or_raise = staticmethod(lambda _iid: _Instrument())
        _canon_manager_params = UiFeedStrategy._canon_manager_params
        _reserve_attach_command = UiFeedStrategy._reserve_attach_command

    original = mg.handler_for
    mg.handler_for = lambda kind: _Handler() if kind == "stop_reenter_watch" else original(kind)
    _Handler.validate_params = staticmethod(lambda _p: None)
    try:
        outcome, _detail, _ledger, params = asyncio.run(
            _Fake()._reserve_attach_command(
                "cid-1", "entry-1",
                kind="stop_reenter_watch", account_id="acct", instrument_id="MNDY.XNAS",
                strategy_id="MANUAL-001", cycle_id="cyc", leash="AUTO",
                params={"floor_price": 81.155, "expected_side": "LONG"},
            )
        )
    finally:
        mg.handler_for = original

    assert outcome == "proceed"
    assert params["floor_price"] == 81.16, (
        "the reserve returned an OFF-TICK floor — the canon ran and its result was discarded, which is #401"
    )


def test_EVERY_caller_rebinds_params_from_the_reserve():
    """Aim at the CLASS, not the instance.

    Three call sites reserve then commit. A future fourth that unpacks three values would be a
    TypeError and is caught by the runtime; one that unpacks four and then ignores the fourth would be
    silent, and is exactly how this bug survived. Pinned by counting the rebinding, not the arity.
    """
    src = inspect.getsource(UiFeedStrategy)
    reserves = src.count("await self._reserve_attach_command(")
    rebinds = src.count("ledger_cid, params = await self._reserve_attach_command(")
    assert reserves >= 3, "call sites disappeared — re-derive this test"
    assert rebinds == reserves, (
        f"{reserves} reserve call sites but only {rebinds} rebind `params` — a caller is persisting its "
        "own off-tick dict again"
    )


def test_the_OFF_TICK_price_that_reaches_the_persist_call_is_CORRECTED():
    """THE SEAM. Drives the real reserve->commit chain and inspects what `mg.attach` is handed.

    The double for the reserve deliberately CHANGES the params — echoing them back unchanged could not
    distinguish "the caller used the returned dict" from "the caller kept its own", which is the entire
    defect. It stands in for `make_price` rounding 81.155 to 81.16.
    """
    captured: dict = {}

    async def fake_reserve(cid, entry_id, **kwargs):
        canon = dict(kwargs["params"])
        canon["floor_price"] = 81.16  # what make_price would return for MNDY
        return "proceed", "", "ledger-1", canon

    async def fake_commit(cid, ledger_cid, **kwargs):
        captured.update(kwargs["params"])
        return "ok", "attached"

    class _Fake:
        _reserve_attach_command = staticmethod(fake_reserve)
        _commit_attach_command = staticmethod(fake_commit)
        _handle_attach_manager = UiFeedStrategy._handle_attach_manager

    outcome, _ = asyncio.run(
        _Fake()._handle_attach_manager(
            "cid-1",
            "entry-1",
            kind="stop_reenter_watch",
            account_id="acct",
            instrument_id="MNDY.XNAS",
            strategy_id="MANUAL-001",
            cycle_id="cyc",
            leash="AUTO",
            params={"floor_price": 81.155, "expected_side": "LONG"},
        )
    )

    assert outcome == "ok"
    assert captured["floor_price"] == 81.16, (
        f"persisted {captured['floor_price']!r} — the caller passed its own off-tick dict, which is #401"
    )
    assert captured["expected_side"] == "LONG", "the rest of the params must survive the rebind"


def test_the_canon_covers_the_params_the_LIVE_failures_carried():
    """`floor_price` is the field that killed MNDY and is armed wrong on A and APA right now.

    Pinned by name on both phases of stop-and-reenter, because the params travel the whole chain and a
    canon that covered only the watch would leave the rearm to fail exactly as it did.
    """
    src = inspect.getsource(UiFeedStrategy._canon_manager_params)
    assert "make_price" in src, "round with the instrument's own canonicalisation, not a local round()"

    from api import engine_node

    for cls_name in ("_StopReenterWatch", "_StopReenterRearm"):
        cls = getattr(engine_node, cls_name)
        assert "floor_price" in cls.PRICE_PARAMS, f"{cls_name} does not canonicalise floor_price"
        assert "reclaim_price" in cls.PRICE_PARAMS
        assert "base_price" in cls.PRICE_PARAMS
