"""One unresolvable contract must not cost the whole connect budget (#525).

Measured inside kumo-staging-engine-1 on the deployed image:

    Awaiting engine connections (60.0s timeout)   08:15:47.138
    ExecClient Connected                          08:16:04.726     -> 17.6s used, 41s of headroom

`_connect()` awaits `instrument_provider.initialize()` BEFORE `_set_connected(True)`, and
`kernel.py:1020-1025` returns without `_trader.start()` when the engines do not connect in time —
which is why the node comes up with engines running and ZERO strategies.

Resolution is serial (`providers.py:271-291` is a plain loop with one await per contract), and an
unresolvable name pays a full `request_timeout_secs` even though IB answers instantly: error 200 is
logged at DEBUG and `_end_request(success=False)` cancels the request with a no-op default lambda,
leaving the future unresolved until `asyncio.wait_for` gives up. The default is 60s and cockpit
sets neither client, so ONE bad symbol busts a 60s connect budget with 41s of headroom on its own.

Five bad tickers are in today's live pool (BLLLN, FTNR, IQVIA, JEPO, OVVI — three of them are
transcription errors: BLLN/BLLLN, FTNT/FTNR, IQV/IQVIA). Bounding the per-request wait turns
5 × 60s into 5 × 5s: inside the budget, with no denylist to maintain and no second source of truth.
"""

from __future__ import annotations

import msgspec

from api.providers import ibkr


def _cfg_field(cfg, name):
    for f in msgspec.structs.fields(type(cfg)):
        if f.name == name:
            return getattr(cfg, name)
    raise AssertionError(f"{type(cfg).__name__} has no field {name}")


def test_the_fixture_reproduces_the_default_that_bites():
    """FIXTURE PROPERTY: the adapter's own default really is 60s — if upstream lowers it, this
    ticket's arithmetic changes and the test should be re-read, not silently kept."""
    from nautilus_trader.adapters.interactive_brokers.config import (
        InteractiveBrokersDataClientConfig,
    )

    defaults = {f.name: f.default for f in msgspec.structs.fields(InteractiveBrokersDataClientConfig)}
    assert defaults["request_timeout_secs"] == 60


def test_the_DATA_client_bounds_its_per_request_wait():
    spec = ibkr.build_data({"ibg_host": "h", "ibg_port": 4002})
    assert _cfg_field(spec.config, "request_timeout_secs") <= 10, (
        "one unresolvable contract still costs a full 60s of a 60s connect budget (#525)"
    )


def test_the_EXEC_client_takes_the_SAME_bound(monkeypatch):
    """One number, both clients. I first split this out of a fear that a 5s cap on the exec client's
    `get_open_orders` / `get_positions` would return an EMPTY list on a slow answer and manufacture
    the ORDER_NOT_FOUND_AT_VENUE rejections of #512. Checked the adapter instead of reasoning: both
    return **None** on failure with an explicit "caller must not infer" comment (order.py:161,
    account.py:174), and the callers leave order state unchanged. The dangerous reading is
    prevented upstream, so a second constant would be one more thing to keep in sync for no
    measured benefit."""
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DUTEST001")
    spec = ibkr.build({"ibg_host": "h", "ibg_port": 4002,
                       "account_id_env": "IBKR_ACCOUNT_ID"})
    assert _cfg_field(spec.config, "request_timeout_secs") <= 10


def test_HISTORICAL_BARS_do_not_take_this_bound():
    """`market_data.py` carries its own `timeout: int = 60` and never reads `request_timeout_secs`,
    so the paced backfill (#617, one request per 10s, large daily pulls) is untouched. Pinned
    because a 5s cap there would trade a boot failure for missing history."""
    import pathlib

    import nautilus_trader.adapters.interactive_brokers as ib

    src = (pathlib.Path(ib.__file__).parent / "client" / "market_data.py").read_text()
    assert "request_timeout_secs" not in src, (
        "the adapter now bounds historical bar requests with the same knob — re-read #525 before "
        "keeping this value"
    )


def test_the_bound_respects_the_BAR_distribution_not_qualifications_margin():
    """THE LOWER BOUND IS SET BY HISTORICAL BARS, not by qualification — and the first version of
    this test said otherwise, which is what would have made a later tightening dangerous.

    Measured on the 2026-08-29 staging boot: 236 paired bar request/response cycles, median 0.94s,
    p99 1.70s, MAX 1.97s. Qualification averaged 137ms. So a tightening to 3s — which the earlier
    docstring explicitly permitted on the strength of "a cold gateway" — would leave the BAR path
    1.5x margin while looking like it only touched contract lookups. Same knob, seven paths.

    4s floor: twice the observed bar maximum, stated as a number a future reader can check against
    a fresh measurement rather than against a feeling."""
    spec = ibkr.build_data({"ibg_host": "h", "ibg_port": 4002})
    assert _cfg_field(spec.config, "request_timeout_secs") >= 4
