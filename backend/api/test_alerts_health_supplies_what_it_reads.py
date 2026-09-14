"""Every key the alert conditions READ must be one `_health()` SUPPLIES (#643, found 2026-08-31).

THE FOURTH FIELD LIST ON THE SAME PAYLOAD. The engine publishes a health dict; `consumer.health()`
copies keys out of the bus frame; `/health` names them again building the response; and
`AlertsService._health()` builds a THIRD, much smaller dict for the alert conditions. A key missing
from any of them is absent forever while every other hop is correct.

MEASURED: `health_conditions` reads `unreconciled_orders` and `_health()` supplies two keys, neither
of them that one. `health.get("unreconciled_orders", []) or []` iterates an empty list on every poll,
so the alert for orders reconciliation had to SKIP — shares reserved and protection provided that the
engine is not counting — has never fired and could not have. An empty result read as "nothing wrong",
which is the exact failure the detector exists to end.

DERIVED, NOT LISTED. Naming the keys is what failed three times already; this reads them off the
function so a condition added tomorrow is covered without editing this file.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import api.alerts as al


def _keys_read_by_conditions() -> set[str]:
    reads: set[str] = set()
    tree = ast.parse(textwrap.dedent(inspect.getsource(al.health_conditions)))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get":
            recv = getattr(n.func.value, "id", None)
            if recv == "health" and n.args and isinstance(n.args[0], ast.Constant):
                reads.add(n.args[0].value)
        if isinstance(n, ast.Subscript) and getattr(n.value, "id", None) == "health":
            if isinstance(n.slice, ast.Constant):
                reads.add(n.slice.value)
    return reads


def _keys_supplied_by_health() -> set[str]:
    out: set[str] = set()
    tree = ast.parse(textwrap.dedent(inspect.getsource(al.AlertsService._health)))
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            out |= {k.value for k in n.keys if isinstance(k, ast.Constant)}
    return out


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the conditions must actually read something off the health dict, or this file
    asserts nothing — which is how a detector comes to recognise its subject by a property the defect
    destroys."""
    assert _keys_read_by_conditions(), "health_conditions reads no health keys — nothing to check"


def test_every_key_the_conditions_read_is_SUPPLIED():
    missing = sorted(_keys_read_by_conditions() - _keys_supplied_by_health())
    assert not missing, (
        f"{missing} are read by `health_conditions` and never supplied by `_health()`, so those "
        f"alerts cannot fire — the poll succeeds, the list is empty, and silence reads as clean. "
        f"Do not fix this by adding just these names; that is what left this one behind."
    )


def test_the_UNRECONCILED_alert_fires_on_a_row_that_reaches_it():
    """The behavioural half. The check above is structural — it proves the key travels, not that the
    condition still works when it does."""
    alerts = al.health_conditions({
        "subsystems": [{"name": "engine", "ok": True, "detail": ""}],
        "reconcile_drift": [],
        "unreconciled_orders": [{"venue_order_id": "V-1", "symbol": "PENG", "reason": "qty is null"}],
    })
    keys = [k for k in alerts if "unreconciled" in k or "V-1" in k]
    assert keys, (
        f"a skipped-reconciliation row produced no alert: {sorted(alerts)} — the shares it reserves "
        f"and the protection it provides are invisible to the engine, and nothing says so"
    )


def test_an_EMPTY_list_raises_nothing():
    """The other direction. A detector that fires on the clean state is one an operator switches off,
    and then the real signal is invisible too."""
    alerts = al.health_conditions({
        "subsystems": [{"name": "engine", "ok": True, "detail": ""}],
        "reconcile_drift": [],
        "unreconciled_orders": [],
    })
    assert not [k for k in alerts if "unreconciled" in k]


def test_the_CONSUMER_hop_also_carries_what_the_conditions_read():
    """The hop before `_health()`, and it was measurably open.

    `AlertsService._health()` reads `self._node.health()`, which in the split deployment is
    `RedisConsumer.health()` — a field list of its own. Supplying `unreconciled_orders` in alerts.py
    while the consumer drops it just moves the empty list one function later, and every test in this
    file stayed green when that was the state. Verified by mutation, not assumed.

    So the requirement is the CHAIN: every key the conditions read must survive both hops.
    """
    import ast
    import inspect
    import textwrap

    import api.consumer as consumer_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(consumer_mod.RedisConsumer.health)))
    forwarded: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            forwarded |= {k.value for k in n.keys if isinstance(k, ast.Constant)}

    # `subsystems` is probed by the api process itself and never comes off the bus.
    needed = _keys_read_by_conditions() - {"subsystems"}
    missing = sorted(needed - forwarded)
    assert not missing, (
        f"{missing} are read by the alert conditions and never leave `RedisConsumer.health()`, so "
        f"they arrive empty however correct the engine and alerts.py are — the same field-list trap, "
        f"one hop earlier"
    )
