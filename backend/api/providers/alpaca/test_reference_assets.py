"""The reference-asset ledger is a PROVIDER capability, served from ITS OWN base URL (#647).

Nautilus does not model delisting status — no `Instrument.info` shape carries "this name stopped
existing" — so the `status` column genuinely needs a reference source. That source is DECLARED on
`ExecClientSpec.reference_assets` (a zero-arg callable, or None on a venue with no ledger), never
reached for by a lane:

  * Alpaca serves it from the CONFIGURED `trading_base_url` — the hardcoded
    `https://paper-api.alpaca.markets` in strategies/qc345.py:1044 is what sent live keys to the
    paper endpoint on any future live deployment, because it did not follow the account.
  * IBKR declares None — "no reference ledger on this venue" — and the consumer (qc345) reports
    delisting detection OFF loudly. Three states, not a quiet empty list.

Red evidence (2026-08-29, pre-fix): `ExecClientSpec` had no `reference_assets` field —
TypeError: __init__() got an unexpected keyword argument / AttributeError on the spec.
"""

from __future__ import annotations

import io
import json
import urllib.request

import pytest


def _alpaca_spec(monkeypatch, **table):
    monkeypatch.setenv("APCA_API_KEY_ID", "PKTESTKEY")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "testsecret")
    from api.providers.alpaca.exec_client import build

    return build(table)


def test_the_alpaca_exec_spec_DECLARES_the_ledger(monkeypatch):
    spec = _alpaca_spec(monkeypatch)
    assert callable(spec.reference_assets), (
        "the Alpaca provider no longer declares a reference-asset ledger — qc345's delisting "
        "detection is OFF on every Alpaca tenant"
    )


def test_the_ledger_follows_the_CONFIGURED_base_url_not_a_hardcoded_paper_host(monkeypatch):
    """Tested with a value the default could not produce (agreement is not connection): the paper
    URL is both the hardcoded constant AND the config default, so only a foreign base can prove the
    config is the wire."""
    base = "https://live.example.test:12345"
    spec = _alpaca_spec(monkeypatch, trading_base_url=base)

    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        return io.BytesIO(json.dumps([{"symbol": "AAPL", "status": "active",
                                       "name": "Apple Inc. Common Stock"}]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    rows = spec.reference_assets()

    assert seen["url"].startswith(f"{base}/v2/assets"), (
        f"the ledger fetched {seen.get('url')} — not the configured account's endpoint"
    )
    assert rows and rows[0]["symbol"] == "AAPL"
    # The credentials come from the provider's own env contract, not from a lane.
    lowered = {k.lower(): v for k, v in seen["headers"].items()}
    assert lowered.get("apca-api-key-id") == "PKTESTKEY"


def test_the_ledger_does_NOT_filter_status_or_tradable(monkeypatch):
    """A delisted name is exactly the one the strategy needs told about while it still HOLDS it;
    filtering it out of the reference data is how it disappears quietly instead of being flagged.
    (`terminal_buckets` keys on status == 'inactive' — a fetch of only active rows is a delisting
    detector that can never fire.)"""
    spec = _alpaca_spec(monkeypatch)
    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return io.BytesIO(b"[]")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    spec.reference_assets()
    assert "status=" not in seen["url"] and "tradable" not in seen["url"], (
        f"the ledger filters its own subject away: {seen['url']}"
    )


def test_IBKR_declares_NO_ledger_and_that_is_a_state_not_an_accident():
    """The spec default is None and IBKR's build must not grow one by copy-paste: an IBKR node has
    no Alpaca credential and no Alpaca table (#573 — a credential that is present WILL be used)."""
    from api.providers.base import ExecClientSpec

    spec = ExecClientSpec(client_id="X", config=None, factory=object,
                          reserves_shares_against_resting_stop=False)
    assert spec.reference_assets is None

    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "ibkr.py").read_text()
    assert "reference_assets" not in src, (
        "api/providers/ibkr.py now names reference_assets — if IBKR really grew a delisting "
        "ledger, point it at IBKR's own data and update this pin; it must never be Alpaca's"
    )
    ast.parse(src)  # the pin above is textual; at least prove we pinned a parseable module


def test_build_node_STAMPS_the_capability_and_qc345_CONSUMES_it():
    """The #574 shape is a field that is declared, documented and consulted by nothing. Source-level
    on both sides of the seam: engine_node forwards the spec's answer onto the feed, and the lane
    reads it back by the same name — one wire, both ends visible."""
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[3]
    engine = (backend / "api" / "engine_node.py").read_text()
    lane = (backend / "strategies" / "qc345.py").read_text()
    assert "feed._reference_assets = exec_spec.reference_assets" in engine, (
        "build_node no longer stamps the reference-asset ledger onto the feed — the spec field is "
        "declared and dead (#574 shape)"
    )
    assert 'getattr(feed, "_reference_assets", None)' in lane, (
        "qc345 no longer consumes the stamped capability — delisting detection is OFF everywhere "
        "and nothing says so"
    )
