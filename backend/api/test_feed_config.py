"""Feed-config + provider-registry tests — all offline (no Databento key or network)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from api.feed_config import load_feed_config
from api.providers import build_data_client_spec

_VALID = """
[data]
provider = "databento"
backfill_days = 200
[data.databento]
api_key_env = "DATABENTO_API_KEY"
dataset = "XNAS.ITCH"
[execution]
provider = "ibkr"
[execution.ibkr]
account_id = "DUPTEST02"
ibg_port = 4002
[engine]
kind = "live"
trader_id = "COCKPIT-001"
venue = "XNAS"
[universe]
symbols = ["AAPL", "MSFT"]
[chart.defaults]
"1W" = "1h"
"1M" = "1d"
[chart.lookback_days]
"1m" = 7
"1h" = 45
"1d" = 200
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "feed.toml"
    path.write_text(textwrap.dedent(body))
    return path


def test_load_parses_all_fields(tmp_path: Path) -> None:
    cfg = load_feed_config(_write(tmp_path, _VALID))
    assert cfg.data_provider == "databento"
    assert cfg.provider_config == {"api_key_env": "DATABENTO_API_KEY", "dataset": "XNAS.ITCH"}
    assert cfg.backfill_days == 200
    assert cfg.engine_kind == "live"
    assert cfg.venue == "XNAS"
    assert cfg.symbols == ("AAPL", "MSFT")
    assert cfg.chart_defaults == {"1W": "1h", "1M": "1d"}
    assert cfg.chart_lookback_days == {"1m": 7, "1h": 45, "1d": 200}
    assert cfg.exec_provider == "ibkr"
    assert cfg.exec_config == {"account_id": "DUPTEST02", "ibg_port": 4002}


def test_missing_provider_table_raises(tmp_path: Path) -> None:
    body = _VALID.replace('[data.databento]\napi_key_env = "DATABENTO_API_KEY"\ndataset = "XNAS.ITCH"', "")
    with pytest.raises(ValueError, match="no \\[data.databento\\] table"):
        load_feed_config(_write(tmp_path, body))


def test_empty_universe_raises(tmp_path: Path) -> None:
    body = _VALID.replace('symbols = ["AAPL", "MSFT"]', "symbols = []")
    with pytest.raises(ValueError, match="no universe symbols"):
        load_feed_config(_write(tmp_path, body))


def test_registry_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown data provider 'nope'"):
        build_data_client_spec("nope", {})


def test_repo_feed_toml_loads() -> None:
    """The committed config/feed.toml is valid (catches edits that break the schema)."""
    cfg = load_feed_config()
    assert cfg.data_provider in {"alpaca", "databento", "ibkr"}  # a registered data provider
    assert cfg.symbols


def test_exec_provider_with_no_table_REFUSES_it_does_not_build_a_paper_alpaca_client(tmp_path: Path) -> None:
    """#650. The DATA path raises on exactly this condition; the EXECUTION path returned {} — and for
    alpaca an empty exec config builds a default PAPER client off whatever APCA_* the environment
    carries, on an instance whose file never declared Alpaca execution. The #574/#581 shape, on the
    path whose consequence is ORDERS. Same rule both paths: no table, no provider — refuse and name it.
    """
    # FIXTURE PROPERTY first: the same file WITH its table parses — the refusal below is about the
    # missing table, not a broken fixture.
    assert load_feed_config(_write(tmp_path, _VALID)).exec_provider == "ibkr"
    body = _VALID.replace('[execution.ibkr]\naccount_id = "DUPTEST02"\nibg_port = 4002\n', "")
    with pytest.raises(ValueError, match="no \\[execution.ibkr\\] table"):
        load_feed_config(_write(tmp_path, body))


def test_KUMO_EXEC_naming_an_undeclared_provider_refuses(tmp_path: Path, monkeypatch) -> None:
    """The env override is the louder half: KUMO_EXEC=alpaca on a file declaring only ibkr must
    refuse, never run orders through a provider the instance never configured. Tested with a value
    the file could not produce (agreement-is-not-connection rule)."""
    monkeypatch.setenv("KUMO_EXEC", "alpaca")
    with pytest.raises(ValueError, match="no \\[execution.alpaca\\] table"):
        load_feed_config(_write(tmp_path, _VALID))


def test_exec_provider_none_still_means_no_execution(tmp_path: Path) -> None:
    """The deliberate no-exec case must survive the refusal: provider 'none' has no table by design
    and stays an empty config, not an error."""
    body = _VALID.replace('provider = "ibkr"', 'provider = "none"').replace(
        '[execution.ibkr]\naccount_id = "DUPTEST02"\nibg_port = 4002\n', "")
    cfg = load_feed_config(_write(tmp_path, body))
    assert cfg.exec_provider == "none"
    assert cfg.exec_config == {}
