"""Order-action registry (#50). Import for the side effect of registering the standard actions."""

from api.order_actions.registry import (
    OrderActionDescriptor,
    OrderBuildContext,
    action_ids,
    get_action,
    register_action,
)
from api.order_actions.standard import register_standard_actions

register_standard_actions()

__all__ = [
    "OrderActionDescriptor",
    "OrderBuildContext",
    "action_ids",
    "get_action",
    "register_action",
    "register_standard_actions",
]
