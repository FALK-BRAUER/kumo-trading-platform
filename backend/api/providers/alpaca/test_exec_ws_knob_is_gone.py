"""The exec config's `ws_base_url` was a dead knob (#652 item 8b).

`AlpacaExecClientConfig.ws_base_url` was accepted, documented ("Account trade-updates WebSocket
base"), forwarded by `build()` — and read by NOTHING: the exec path has no WebSocket (module
docstring: "Real-time fills arrive via reconciliation for now"). A knob that exists but connects to
nothing is the agreement-is-not-connection hazard — an operator pointing it at a different stream
would see every surface agree while the setting did nothing. (The DATA config's `ws_base_url` is
live — `data_client.py` builds its stream URL from it — and stays.)

Removed until the trade-updates increment actually lands; a feed.toml that still carries the key
gets a LOUD ignore, not a silent one.
"""

from __future__ import annotations

import logging

import pytest

from api.providers.alpaca import exec_client as ec
from api.providers.alpaca.config import AlpacaDataClientConfig, AlpacaExecClientConfig


def test_the_exec_config_no_longer_declares_the_dead_knob():
    assert "ws_base_url" not in AlpacaExecClientConfig.__annotations__, (
        "ws_base_url is declared on the EXEC config but no exec code reads it — a dead knob reads "
        "as configuration while being none"
    )


def test_the_DATA_ws_knob_is_untouched():
    """The sibling that IS wired must survive — enumerate the siblings, don't just fix the one."""
    assert "ws_base_url" in AlpacaDataClientConfig.__annotations__


def test_build_ignores_a_leftover_ws_key_LOUDLY(monkeypatch, caplog):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    with caplog.at_level(logging.WARNING):
        spec = ec.build({"ws_base_url": "wss://paper-api.alpaca.markets/stream"})
    assert not hasattr(spec.config, "ws_base_url") or "ws_base_url" not in AlpacaExecClientConfig.__annotations__
    assert any("ws_base_url" in r.getMessage() for r in caplog.records), (
        "a config key that is accepted and does nothing must be named as ignored — silent is how "
        "KUMO_DATA sat dead for months (#574)"
    )


def test_build_without_the_key_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    with caplog.at_level(logging.WARNING):
        ec.build({})
    assert not any("ws_base_url" in r.getMessage() for r in caplog.records)
