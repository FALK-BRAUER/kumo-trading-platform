"""The feed's print type travels engine -> bus -> api -> UI beside its last-tick stamp (#834).

WHY THE STAMP ALONE IS NOT ENOUGH. Measured on ibkr-paper, 2026-09-09: under IB DELAYED market data a
perfectly healthy feed presents as ~900 seconds old, which every freshness threshold in the system
calls "down"; and the same 900 seconds under REALTIME IS down. The number cannot be interpreted
without the type, and an entry price rendered as current is a quarter of an hour old.

THREE STATES. "REALTIME" and "DELAYED" are declarations the IB client was built with. None is "the
provider has no such notion, or the bridge is stale" — and None must never be rendered as REALTIME,
which is the one claim nobody made.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import api.consumer as consumer_mod
import api.engine_node as engine_node
from api.models import HealthResponse


def test_the_helper_maps_the_adapters_own_values_to_names():
    from nautilus_trader.adapters.interactive_brokers.config import IBMarketDataTypeEnum as E

    assert engine_node.market_data_type_name(E.REALTIME) == "REALTIME"
    assert engine_node.market_data_type_name(E.DELAYED) == "DELAYED"
    assert engine_node.market_data_type_name(int(E.DELAYED)) == "DELAYED", "an int from a config dict"


def test_NONE_stays_NONE_and_is_not_promoted_to_REALTIME():
    """Alpaca and Databento build no `market_data_type`; the frame must say nothing, not REALTIME."""
    assert engine_node.market_data_type_name(None) is None


def test_a_value_the_adapter_does_not_know_RAISES_rather_than_becoming_undeclared():
    with pytest.raises(ValueError, match="market_data_type"):
        engine_node.market_data_type_name(99)


def test_the_ENGINE_publishes_it_on_the_health_frame_from_the_stored_field():
    """Source assertion: the frame is a literal dict, and a key absent from it is a field that never
    leaves the engine — the #546 class, one hop earlier than the DTO."""
    src = inspect.getsource(engine_node.UiFeedStrategy)
    assert '"market_data_type": self._market_data_type' in src, (
        "the engine health frame does not carry market_data_type, so the api can only ever forward None"
    )


def test_BUILD_NODE_reads_it_off_the_data_clients_OWN_config():
    """The value the gateway was ASKED for is the value the UI is told about. Reading it from anywhere
    else (settings, env, a literal) is a second derivation of one fact, and those drift."""
    src = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(engine_node.build_node))))
    assert "market_data_type=market_data_type_name(getattr(spec.config, 'market_data_type', None))" in src, (
        "build_node does not derive market_data_type from spec.config — the frame can disagree with the gateway"
    )


def test_the_CONSUMER_forwards_it_and_GATES_it_on_the_bridge():
    """A stale frame must not keep asserting DELAYED — or REALTIME — after the engine has gone."""
    src = inspect.getsource(consumer_mod.RedisConsumer.health)
    assert '"market_data_type": self._health.get("market_data_type") if bridge_ok else None' in src, (
        "the consumer either drops market_data_type or carries it forward from a dead frame"
    )


def test_the_DTO_declares_it_optional_with_no_default_other_than_None():
    f = HealthResponse.model_fields["market_data_type"]
    assert f.default is None, "an absent type must read as 'not told', never as a real value"
