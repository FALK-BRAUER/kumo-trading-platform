"""#1102 (d) — the Alpaca borrow plane: `feed.shortable_provider` from `/v2/assets`, three states.

MEASURED 2026-09-18 02:10Z (paper key, `GET /v2/assets/{sym}`): the rows below are verbatim. Alpaca
carries `shortable` + `easy_to_borrow` and NO fee. Across CRSISHORT's 130 names: 68 shortable, 62
not, 0 shortable-but-hard-to-borrow. Account `shorting_enabled: true`.

THE PROTOCOL IS THE LANE'S, NOT OURS: `borrow_rates: symbols -> {symbol: LOCATABLE | None}`
(kumo-trading-strategies crsi_short, `LOCATABLE` identity = available, fee unknown; None = no locate). The
IB plane speaks it (`ibkr_shortable.py`); this one must answer identically for the same fact and
NEVER a number — 0.0 would typecheck, pass the fee ceiling and read as free borrow.

THREE STATES PER NAME, never two: LOCATABLE only when the row is fresh AND tradable AND shortable
AND easy_to_borrow. Everything else is None BY NAME — not shortable (HUBC), shortable-but-HTB (fee
unknown; the lane refuses rather than submits blind), unknown to the table, stale table, table
never loaded. `health()` carries the same words the IB plane does so `/health.shortable` reads
one vocabulary on both instances.

The engine's verbs (engine_node.py `attach_shortable` :2106, `_subscribe_short_lanes` :2151,
health :9521) are what a plane must answer; the seam test drives `_attach_shortable_plane` with the
REAL spec factory so a renamed verb cannot pass here and fail on the node.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from nautilus_trader.model.identifiers import InstrumentId

NS = 1_000_000_000

# Verbatim `/v2/assets/{symbol}` rows, 2026-09-18 02:10Z, reduced to the fields the plane reads.
NVDA = {"symbol": "NVDA", "status": "active", "tradable": True, "shortable": True, "easy_to_borrow": True}
HUBC = {"symbol": "HUBC", "status": "active", "tradable": True, "shortable": False, "easy_to_borrow": False}
AEM = {"symbol": "AEM", "status": "active", "tradable": True, "shortable": True, "easy_to_borrow": True}
# Synthetic siblings the measured set did not contain (0 of 130 were HTB today) — the fee-unknown
# and the not-tradable shapes, both of which Alpaca's schema allows.
HTB = {"symbol": "HTBX", "status": "active", "tradable": True, "shortable": True, "easy_to_borrow": False}
HALTED = {"symbol": "HALT", "status": "active", "tradable": False, "shortable": True, "easy_to_borrow": True}


class _Http:
    def __init__(self, rows, *, fail: Exception | None = None) -> None:
        self.rows, self.fail, self.calls = rows, fail, 0

    async def list_assets(self, status="active", asset_class="us_equity"):
        self.calls += 1
        if self.fail:
            raise self.fail
        return list(self.rows)


def _plane(rows, *, now=100 * NS, fail=None):
    from api.providers.alpaca.shortable import AlpacaShortablePlane

    clock = {"now": now}
    http = _Http(rows, fail=fail)
    return AlpacaShortablePlane(http, now_ns=lambda: clock["now"]), http, clock


def _iid(sym: str) -> InstrumentId:
    return InstrumentId.from_str(f"{sym}.XNAS")


def _subscribe(plane, *syms):
    for s in syms:
        asyncio.run(plane.subscribe(types.SimpleNamespace(id=_iid(s))))


def test_FIXTURE_the_rows_are_the_measured_shapes_and_the_contract_is_not_a_number():
    from kumo_strategies.strategies.crsi_short import LOCATABLE

    assert NVDA["easy_to_borrow"] and not HUBC["shortable"]
    assert not isinstance(LOCATABLE, (int, float)), "LOCATABLE became a number upstream — 0.0 passes a fee gate"


def test_THREE_STATES_per_name_from_the_measured_rows():
    from kumo_strategies.strategies.crsi_short import LOCATABLE

    plane, http, _ = _plane([NVDA, HUBC, AEM, HTB, HALTED])
    _subscribe(plane, "NVDA", "HUBC", "HTBX", "HALT", "ZZZZ")
    got = plane.borrow_rates()(["NVDA", "HUBC", "HTBX", "HALT", "ZZZZ", "AEM"])
    assert got["NVDA"] is LOCATABLE
    assert got["AEM"] is LOCATABLE, "answered from the table even when nobody subscribed the name"
    assert got["HUBC"] is None, "not shortable"
    assert got["HTBX"] is None, "shortable but hard-to-borrow: fee unknown → refused by name, never submitted blind"
    assert got["HALT"] is None, "not tradable"
    assert got["ZZZZ"] is None, "unknown to the table"
    assert not any(isinstance(v, (int, float)) for v in got.values()), "never a number"


def test_the_table_is_loaded_ONCE_and_reloaded_only_when_stale():
    plane, http, clock = _plane([NVDA])
    _subscribe(plane, "NVDA", "HUBC", "AEM")
    assert http.calls == 1, "one request for the whole universe, not one per name (130 names, 200/min)"
    plane.borrow_rates()(["NVDA"])
    assert http.calls == 1
    clock["now"] += 27 * 3600 * NS                       # a daily fact, older than max_age (26 h)
    _subscribe(plane, "NVDA")
    assert http.calls == 2


def test_a_STALE_table_answers_None_for_every_name_and_says_stale():
    from kumo_strategies.strategies.crsi_short import LOCATABLE

    plane, http, clock = _plane([NVDA])
    _subscribe(plane, "NVDA")
    assert plane.borrow_rates()(["NVDA"])["NVDA"] is LOCATABLE
    clock["now"] += 27 * 3600 * NS
    assert plane.borrow_rates()(["NVDA"])["NVDA"] is None, "a stale yes is not a yes"
    assert plane.health()["state"] == "stale"


def test_a_table_that_NEVER_loaded_answers_None_and_health_says_so():
    plane, http, _ = _plane([NVDA], fail=ConnectionError("assets down"))
    with pytest.raises(ConnectionError):
        _subscribe(plane, "NVDA")
    assert plane.borrow_rates()(["NVDA"]) == {"NVDA": None}
    h = plane.health()
    assert h["state"] == "never_loaded" and h["never"] == 1 and h["answered"] == 0
    assert h["loaded_at_ns"] is None and h["assets"] == 0


def test_health_carries_the_IB_planes_vocabulary():
    plane, _, _ = _plane([NVDA, HUBC])
    assert plane.health()["state"] == "never_subscribed"
    _subscribe(plane, "NVDA", "HUBC", "ZZZZ")
    h = plane.health()
    assert set(h) >= {"contract", "subscribed", "answered", "stale", "never", "state", "loaded_at_ns", "assets"}
    assert (h["subscribed"], h["answered"], h["never"], h["stale"]) == (3, 2, 1, 0)
    assert h["state"] == "partial", "ZZZZ was asked and the table has no row — partial, not complete"
    assert h["contract"] == "present"
    # A readback says "table of N at HH:MM" (lead, scope 4): the load time and the row count ride
    # on the row; a never-loaded table carries None and 0, never a stale number.
    assert h["loaded_at_ns"] == 100 * NS and h["assets"] == 2


def test_the_ALPACA_spec_DECLARES_the_plane_like_IB_does(monkeypatch):
    monkeypatch.setenv("X_KEY", "k")
    monkeypatch.setenv("X_SECRET", "s")
    from api.providers.alpaca.data_client import build_data
    from api.providers.alpaca.shortable import AlpacaShortablePlane

    spec = build_data({"key_env": "X_KEY", "secret_env": "X_SECRET"})
    assert spec.shortable_plane is not None, "Alpaca declared no plane — the CRSISHORT builder refuses for want of a provider"
    plane = spec.shortable_plane(lambda *_: None, lambda: 0)
    assert isinstance(plane, AlpacaShortablePlane)


def test_SEAM_the_engine_attaches_it_and_the_feed_answers_in_the_lanes_protocol(monkeypatch):
    """Through `_attach_shortable_plane` (engine_node.py:10436) with the REAL spec factory and a feed
    double carrying only the verbs the engine uses — then `feed.shortable_provider`, which is what
    the CRSISHORT builder reads (`crsi_short.py` borrow gate)."""
    from kumo_strategies.strategies.crsi_short import LOCATABLE

    from api import engine_node
    from api.providers.alpaca import shortable as mod
    from api.providers.alpaca.data_client import build_data

    monkeypatch.setenv("X_KEY", "k")
    monkeypatch.setenv("X_SECRET", "s")
    monkeypatch.setattr(mod.AlpacaHttpClient, "list_assets", lambda self, **kw: _Http([NVDA, HUBC]).list_assets())
    spec = build_data({"key_env": "X_KEY", "secret_env": "X_SECRET"})

    attached = {}

    class _Feed:
        clock = types.SimpleNamespace(timestamp_ns=lambda: 100 * NS)

        def publish_data(self, *a): ...

        def attach_shortable(self, plane):
            attached["plane"] = plane
            self.shortable_provider = plane.borrow_rates(now_ns=lambda: 100 * NS)

    feed = _Feed()
    engine_node._attach_shortable_plane(feed, spec)
    assert "plane" in attached
    _subscribe(attached["plane"], "NVDA", "HUBC")
    assert feed.shortable_provider(["NVDA", "HUBC"]) == {"NVDA": LOCATABLE, "HUBC": None}


def test_the_CRSISHORT_builder_keeps_the_fee_gate_ON_with_this_provider(monkeypatch):
    """No `CRSI_BORROW_GATE_OFF` on paper: with a provider on the feed the ceiling stays armed."""
    from strategies import crsi_short
    from strategies.test_crsi_short_builder import _Feed, _settings, _stub_store_and_calendar

    _stub_store_and_calendar(monkeypatch)
    _settings(monkeypatch)
    plane, _, _ = _plane([NVDA, HUBC])
    strategy = crsi_short.build_crsi_short_strategy(
        feed=_Feed("alpaca", shortable_provider=plane.borrow_rates()))
    assert strategy is not None
    assert strategy._cfg.max_borrow_fee_annual is not None, "the gate was switched off"


# --- the reload the catch-up pass must drive (ks review of #1129) --------------------------------

def test_SEAM_the_catchup_pass_RELOADS_a_stale_table_and_the_plane_recovers():
    """The asset table loaded only from `subscribe()`, and `_shortable_catchup` (engine_node.py)
    skips instruments it already asked for — so after the boot pass nothing ever reloaded: at hour
    27 `_fresh()` flipped, every name read None, ks refused every entry "no locate", `/health`
    read `stale`, and only a restart recovered it (ks review). The catch-up pass now asks the plane
    to refresh when stale — getattr-guarded, the IB plane has no table to refresh."""
    from api.test_shortable_wiring import _Cache, _ShortLane, _feed

    lane = _ShortLane(_iid("NVDA"), _iid("HUBC"))
    feed, _ = _feed(lane, cache=_Cache(_iid("NVDA"), _iid("HUBC")), attach=False)
    plane, http, clock = _plane([NVDA, HUBC])
    feed.clock.timestamp_ns = lambda: clock["now"]
    feed.attach_shortable(plane)

    feed._shortable_catchup()                              # boot pass: subscribes, loads once
    assert http.calls == 1 and plane.health()["state"] == "complete"
    feed._shortable_catchup()                              # nothing new to ask → no reload
    assert http.calls == 1

    clock["now"] += 27 * 3600 * NS
    assert plane.health()["state"] == "stale", "fixture: the table IS stale before the pass"
    feed._shortable_catchup()                              # the pass drives the refresh
    assert http.calls == 2, "the catch-up pass did not reload a stale table"
    assert plane.health()["state"] == "complete"
    from kumo_strategies.strategies.crsi_short import LOCATABLE

    assert feed.shortable_provider(["NVDA"]) == {"NVDA": LOCATABLE}


def test_the_IB_plane_without_a_table_is_untouched_by_the_refresh_hook():
    from api.test_shortable_wiring import _Cache, _ShortLane, _feed

    feed, client = _feed(_ShortLane(_iid("NVDA")), cache=_Cache(_iid("NVDA")))   # attaches the IB plane
    feed._shortable_catchup()
    feed.clock.timestamp_ns = lambda: 99 * 3600 * NS
    feed._shortable_catchup()                              # must not raise on a plane lacking refresh_if_stale


def test_a_FAILED_refresh_is_counted_and_retried_and_the_table_stays_as_it_was():
    """The refresh rides the same pending map as the subscriptions; its failure must be COUNTED
    (`_shortable_subscribe_failures`), must not discard any instrument from `_shortable_subscribed`,
    and must be asked again next pass. Bitten by: the sentinel compared with `==` against a Cython
    InstrumentId raised TypeError inside the observation wrapper — every failure then read as 0."""
    from api.test_shortable_wiring import _Cache, _ShortLane, _feed

    lane = _ShortLane(_iid("NVDA"))
    feed, _ = _feed(lane, cache=_Cache(_iid("NVDA")), attach=False)
    plane, http, clock = _plane([NVDA])
    feed.clock.timestamp_ns = lambda: clock["now"]
    feed.attach_shortable(plane)
    feed._shortable_catchup()
    assert http.calls == 1
    clock["now"] += 27 * 3600 * NS
    http.fail = ConnectionError("assets down")
    feed._shortable_catchup()                                  # refresh scheduled, raises
    feed._shortable_catchup()                                  # settled: counted, re-asked
    assert feed._shortable_subscribe_failures >= 1
    assert _iid("NVDA") in feed._shortable_subscribed, "a failed REFRESH must not un-subscribe a name"
    assert plane.health()["state"] == "stale" and plane.health()["assets"] == 1
    http.fail = None
    feed._shortable_catchup()
    feed._shortable_catchup()
    assert plane.health()["state"] == "complete" and http.calls >= 3
