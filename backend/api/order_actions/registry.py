"""Order-action registry (#50) — pluggable order TYPES (what order to build), the foundation for #10.

Order construction was hardcoded in engine_node._build_order. An action descriptor makes each order type a
registered unit: `build(ctx, params) -> Order | OrderList`, with a JSON-Schema `params_schema` (the same
contract the UI order tile will render). New types (trailing-stop, MOC/LOC, OCO variants) register here with no
core change. Same pattern as the tile + datasource registries.

Descriptors are PURE: they take a build context (the engine's OrderFactory + already-RESOLVED instrument/side/
qty/TIF/coid/tags) + params, and return a Nautilus order object. They never fetch data, never derive prices
(that is #65 prefill — WHERE prices sit, orthogonal to WHAT type), never submit, never own strategy identity.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Quantity


@dataclass(frozen=True)
class OrderBuildContext:
    """Everything an order builder needs that the ENGINE owns (so the descriptor stays pure). The OrderFactory
    carries strategy identity (Nautilus-owned); the rest is resolved from the validated command payload."""

    factory: OrderFactory
    instrument_id: InstrumentId
    side: OrderSide
    quantity: Quantity
    time_in_force: TimeInForce
    client_order_id: ClientOrderId | None
    tags: list[str] | None


@dataclass(frozen=True)
class OrderActionDescriptor:
    id: str
    kind: str  # entry | exit | both
    params_schema: dict  # JSON Schema for the type-specific params (price/trigger/…)
    build: Callable[[OrderBuildContext, dict], object]  # (ctx, params) -> Order | OrderList

    def validate(self, params: dict) -> None:
        """Fail-closed param check against params_schema (numeric strings like "190.00" are accepted — the
        schemas allow string|number so this never changes the existing accepted shapes)."""
        Draft202012Validator(self.params_schema).validate(params)


_REGISTRY: dict[str, OrderActionDescriptor] = {}


def register_action(descriptor: OrderActionDescriptor) -> None:
    Draft202012Validator.check_schema(descriptor.params_schema)  # compile-check at registration
    if descriptor.id in _REGISTRY:
        raise ValueError(f"order action already registered: {descriptor.id}")
    _REGISTRY[descriptor.id] = descriptor


def get_action(action_id: str) -> OrderActionDescriptor:
    descriptor = _REGISTRY.get(action_id)
    if descriptor is None:
        raise ValueError(f"unknown order action {action_id!r}")
    return descriptor


def action_ids() -> list[str]:
    return sorted(_REGISTRY)
