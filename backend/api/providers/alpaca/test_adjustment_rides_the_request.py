"""#1124 — split-adjusted daily bars for the ONE lane that declares it, per REQUEST, kept out of the
shared cache.

MEASURED 2026-09-18 (paper key, data API): NVDA 1Day 2024-06-07 reads 1208.88 `raw`, 120.89 `split`
(== IB's TRADES bar to the cent), 120.54 `all`. 29 of CRSISHORT's 130 names split inside the 200-day
warmup window. Nautilus 1.229 `DataEngine._handle_bar` (engine.pyx:2815) writes every historical
response bar into the SHARED cache unless the request carries `disable_historical_cache` — the ks
adapter's half; this file pins the cockpit half:

  1. `_request_bars` forwards `request.params["adjustment"]` to the HTTP door, RAW when absent.
  2. The response params are STAMPED with what was actually sent — the declaration is a fact from
     the component that pulled the bars, and the requester can read back what it got.
  3. The DataClientSpec DECLARES `price_adjustments` — the set a venue can serve — required,
     keyword-only, validated, like `daily_bars_cover`; the two venues declare different sets and the
     venue catalogue agrees (two derivations pinned).

Driven through the REAL `AlpacaDataClient._request_bars` (unbound, duck-typed self — the #354
idiom) on a REAL `RequestBars`, with the HTTP door captured. `_handle_bars` is captured on the same
self so the stamp is asserted where Nautilus would read it.
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timezone

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import RequestBars
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import Venue

from api.providers.alpaca.data_client import AlpacaDataClient
from api.providers.alpaca.http import PRICE_ADJUSTMENTS as PRICE_ADJUSTMENTS_AT_THE_DOOR

_BAR_TYPE = BarType.from_str("NVDA.XNAS-1-DAY-LAST-EXTERNAL")


class _Http:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get_bars(self, **kw):
        self.calls.append(kw)
        return {"bars": [], "next_page_token": None}


class _Instrument:
    price_precision = 2
    size_precision = 0


def _self(http: _Http) -> types.SimpleNamespace:
    handled: list[tuple] = []
    fake = types.SimpleNamespace(
        _instrument_provider=types.SimpleNamespace(find=lambda _iid: _Instrument()),
        _http=http,
        _feed="sip",
        _log=types.SimpleNamespace(error=lambda *a, **k: None, warning=lambda *a, **k: None),
        _parse_bar=AlpacaDataClient._parse_bar,
        _handle_bars=lambda *a: handled.append(a),
        handled=handled,
    )
    return fake


def _request(params: dict | None) -> RequestBars:
    return RequestBars(
        _BAR_TYPE, datetime(2024, 6, 1, tzinfo=timezone.utc), None, 0, None, Venue("XNAS"), None,
        UUID4(), 0, params,
    )


def test_FIXTURE_a_real_RequestBars_carries_params_the_way_the_engine_hands_them_over() -> None:
    """The double must not be looser than production: `params` is a real dict on a real message,
    and Nautilus normalises an absent one to `{}` (measured here — the adapter never sees None).
    The boot log shows `params={'update_catalog': False, 'join_request': False}` on every lane
    request, so the "no adjustment key" shape is the production shape."""
    assert _request({"adjustment": "split"}).params == {"adjustment": "split"}
    assert _request(None).params == {}


def test_a_request_WITHOUT_the_param_is_raw_exactly_as_before() -> None:
    http = _Http()
    asyncio.run(AlpacaDataClient._request_bars(_self(http), _request({"update_catalog": False})))
    assert http.calls and http.calls[0]["adjustment"] == "raw"
    http = _Http()
    asyncio.run(AlpacaDataClient._request_bars(_self(http), _request(None)))
    assert http.calls and http.calls[0]["adjustment"] == "raw"


def test_a_request_that_ASKS_for_split_is_forwarded_to_the_door() -> None:
    http = _Http()
    asyncio.run(AlpacaDataClient._request_bars(_self(http), _request({"adjustment": "split"})))
    assert http.calls and http.calls[0]["adjustment"] == "split"


def test_the_response_params_are_STAMPED_with_what_was_sent() -> None:
    """The requester reads back the fact, never re-derives it from what it asked for."""
    http = _Http()
    fake = _self(http)
    asyncio.run(AlpacaDataClient._request_bars(fake, _request({"adjustment": "split", "x": 1})))
    (bar_type, bars, req_id, start, end, params), = fake.handled
    assert params["adjustment"] == "split" and params["x"] == 1
    fake = _self(_Http())
    asyncio.run(AlpacaDataClient._request_bars(fake, _request(None)))
    assert fake.handled[0][5]["adjustment"] == "raw"


def test_a_request_naming_a_value_the_door_refuses_is_refused_by_NAME_not_sent_raw() -> None:
    """`all` must not silently degrade to raw — a lane that asked for adjusted bars and got raw ones
    is the #854 defect with a green log line."""
    http = _Http()
    fake = _self(http)
    with pytest.raises(ValueError, match="adjustment"):
        asyncio.run(AlpacaDataClient._request_bars(fake, _request({"adjustment": "all"})))
    assert not http.calls, "the door was reached with a value it refuses"


# --- the declaration -------------------------------------------------------------------------------

def test_the_spec_DECLARES_price_adjustments_required_and_validated() -> None:
    from api.providers.base import DataClientSpec

    with pytest.raises(TypeError):
        DataClientSpec(client_id="X", config=None, factory=None, daily_bars_cover="rth",
                       streams_trade_ticks=True, streams_quote_ticks=True)  # missing → refused
    with pytest.raises(ValueError, match="price_adjustments"):
        DataClientSpec(client_id="X", config=None, factory=None, daily_bars_cover="rth",
                       streams_trade_ticks=True, streams_quote_ticks=True,
                       price_adjustments=frozenset({"all"}))
    with pytest.raises(ValueError, match="price_adjustments"):
        DataClientSpec(client_id="X", config=None, factory=None, daily_bars_cover="rth",
                       streams_trade_ticks=True, streams_quote_ticks=True,
                       price_adjustments=frozenset())


def test_the_two_venues_DECLARE_DIFFERENT_sets_and_the_catalogue_agrees(monkeypatch) -> None:
    """Alpaca serves both, per request (measured); IB's TRADES bars are split-adjusted always and
    ignore the parameter. Two derivations pinned: the spec and `api/venues/facts.py`."""
    from api.providers.alpaca.data_client import build_data as alpaca_build
    from api.venues.facts import VENUES

    monkeypatch.setenv("X_KEY", "k")
    monkeypatch.setenv("X_SECRET", "s")

    spec = alpaca_build({"key_env": "X_KEY", "secret_env": "X_SECRET"})
    assert spec.price_adjustments == frozenset({"raw", "split"})
    assert VENUES["ALPACA"]["price_adjustments"] == frozenset({"raw", "split"})
    assert VENUES["INTERACTIVE_BROKERS"]["price_adjustments"] == frozenset({"split"})
    # The IB spec is built by the same factory path `test_ibkr_rth_daily` drives; pin it by SOURCE
    # here (the IB build needs a gateway host to construct its config) — the literal must exist.
    import inspect

    from api.providers import ibkr

    assert 'price_adjustments=frozenset({"split"})' in inspect.getsource(ibkr.build_data)
    # and the HTTP door's own constant is the SAME set the Alpaca spec declares (one door, one fact)
    from api.providers.alpaca.http import PRICE_ADJUSTMENTS

    assert spec.price_adjustments == PRICE_ADJUSTMENTS


# --- the two halves meet ---------------------------------------------------------------------------

def test_the_ks_adapters_history_request_asks_for_EXACTLY_what_the_cockpit_door_serves() -> None:
    """The ks half of #1124 (issue 262, 8bf98ef): `HISTORY_REQUEST_PARAMS` on the CRSISHORT
    adapter is what rides `request.params` into `_request_bars` above. Imported, never retyped — two
    copies of one dict are how the two halves drift. RED until the pin carries ks#262; the gate
    order is ks#262 → pin → this PR (lead, 2026-09-18), so a red here on an older pin is the
    ordering being enforced, not a defect."""
    from kumo_strategies.runtime.nautilus.crsi_short import HISTORY_REQUEST_PARAMS

    assert HISTORY_REQUEST_PARAMS["adjustment"] == "split"
    assert HISTORY_REQUEST_PARAMS["adjustment"] in PRICE_ADJUSTMENTS_AT_THE_DOOR
    # The second key is what keeps the split series OUT of the shared cache (Nautilus 1.229
    # engine.pyx:3047) — without it QC345/TECHIVOL read CRSISHORT's adjusted bars for the 17/18
    # names the universes share (measured 2026-09-18).
    assert HISTORY_REQUEST_PARAMS["disable_historical_cache"] is True
    # And the door forwards exactly that dict, stamped.
    http = _Http()
    fake = _self(http)
    asyncio.run(AlpacaDataClient._request_bars(fake, _request(dict(HISTORY_REQUEST_PARAMS))))
    assert http.calls[0]["adjustment"] == "split"
    assert fake.handled[0][5]["disable_historical_cache"] is True
