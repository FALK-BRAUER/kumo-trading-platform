"""Data-provider registry — maps a provider name to its Nautilus data client builder.

Add a provider: write a module with `build(provider_config) -> DataClientSpec` and add one line here.
Everything downstream sees only Nautilus Bars, so it never knows which provider is registered.
"""

from __future__ import annotations

from collections.abc import Callable

from api.providers import databento, ibkr
from api.providers.alpaca import build_data as alpaca_build_data
from api.providers.alpaca import build_exec as alpaca_build_exec
from api.providers.base import DataClientSpec, ExecClientSpec

_DATA_REGISTRY: dict[str, Callable[[dict], DataClientSpec]] = {
    "databento": databento.build,
    "ibkr": ibkr.build_data,
    "alpaca": alpaca_build_data,
}
_EXEC_REGISTRY: dict[str, Callable[[dict], ExecClientSpec]] = {
    "ibkr": ibkr.build,
    "alpaca": alpaca_build_exec,
}


def build_data_client_spec(provider: str, provider_config: dict) -> DataClientSpec:
    """Look up the data provider and build its data client spec."""
    try:
        builder = _DATA_REGISTRY[provider]
    except KeyError:
        raise ValueError(
            f"unknown data provider {provider!r} — registered: {sorted(_DATA_REGISTRY)}"
        ) from None
    return builder(provider_config)


def build_exec_client_spec(provider: str, exec_config: dict) -> ExecClientSpec:
    """Look up the execution provider and build its exec client spec."""
    try:
        builder = _EXEC_REGISTRY[provider]
    except KeyError:
        raise ValueError(
            f"unknown exec provider {provider!r} — registered: {sorted(_EXEC_REGISTRY)}"
        ) from None
    return builder(exec_config)
