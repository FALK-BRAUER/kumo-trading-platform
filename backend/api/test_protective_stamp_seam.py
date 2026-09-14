"""The stamp must reach the ORDER, not merely be computable (#748).

A mutation replacing the `stamp_for` call in the protection dispatch with a function returning None
passed the entire suite. The helper had nine tests; nothing drove the dispatch that uses it. That is
the seam-versus-unit failure this repo has paid for five times in one day — a green helper says
nothing about whether anything calls it correctly.

What the payload's `strategy_id` carries decides which position a reduce-only fill resolves to under
NETTING, so this is the field the whole defect lives in.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap


def _dispatch_source() -> str:
    from api import engine_node

    src = pathlib.Path(inspect.getfile(engine_node)).read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reconcile_protection_inner"),
              None)
    assert fn is not None, "_reconcile_protection_inner moved — this test is blind"
    return ast.get_source_segment(src, fn) or ""


def test_the_protective_payload_RESOLVES_A_LANE_rather_than_shipping_a_blank():
    """`intent.strategy_id` is blank in production — the per-lane row sources are built and not wired,
    which `test_THE_LIVE_PROTECTION_PATH_CARRIES_NO_LANE_so_the_748_split_is_INERT_here` pins. So the
    dispatch must resolve the lane itself, or every stop ships stamped with the submitting strategy
    and the mint continues."""
    tree = ast.parse(textwrap.dedent(_dispatch_source()))
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "stamp_for" in calls, (
        "the protection dispatch does not call `stamp_for`, so a protective stop is stamped with the "
        "submitting strategy (MANUAL-001). On an instrument MANUAL-001 does not hold, that fill "
        "resolves to a position which has never existed and the position poll fabricates it — the "
        "phantom mint, which is 8 mirror pairs and 262 fabricated shares on the live paper book"
    )



def test_the_RESOLVED_LANE_reaches_the_BUILT_ORDER(monkeypatch):
    """Behavioural, because the syntactic version measured its neighbour.

    This used to walk the AST for `stamp_for(...)` appearing INLINE as the `strategy_id` value in the
    payload dict. Hoisting it to `owner = intent.strategy_id or stamp_for(...)` — so the dispatch can
    REFUSE when there is no owner — broke the test while the behaviour got strictly better. A
    detector that fails on a refactor it should be indifferent to is aimed one level away from the
    thing it protects, which is the defect class this whole file exists for.
    """
    from api.test_protection_reconciler import (
        _Fake, _Http, _ns, _pos_in_cache, _position, _run, _settings,
    )

    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    fake.cache._positions = [_pos_in_cache("AEM.XNYS", "MOMENTUM-002", 54.0)]
    _run(fake)

    assert fake.built, "no protective order was built for a held, unambiguous position"
    assert fake.built[0]["strategy_id"] == "MOMENTUM-002", (
        f"built stamped {fake.built[0]['strategy_id']!r}. Under NETTING the position id derives from "
        f"the ORDER's strategy_id, so any other value resolves to a position that does not exist, "
        f"the fill is rejected, and the sale never reaches the cache"
    )


def test_a_CLOSED_position_does_not_supply_the_lane(monkeypatch):
    """The stamp must read the OPEN book.

    The durable cache retains CLOSED positions under the same id shape. Reading the full book would
    find stale holders, make almost every instrument ambiguous, and leave the change doing nothing
    while appearing to work. Pinned by BEHAVIOUR: a closed position for another lane must neither
    make the instrument ambiguous nor be chosen as the owner.
    """
    from api.test_protection_reconciler import (
        _Fake, _Http, _ns, _pos_in_cache, _position, _run, _settings,
    )

    _settings(monkeypatch)
    fake = _Fake(ts_ns=_ns(10, 0), http=_Http(positions=[_position()]))
    closed = _pos_in_cache("AEM.XNYS", "BCTROT-004", 99.0)
    closed.is_open = False
    fake.cache._positions = [closed, _pos_in_cache("AEM.XNYS", "MOMENTUM-002", 54.0)]
    _run(fake)

    assert fake.built, "a closed sibling made a single-holder instrument unstampable"
    assert fake.built[0]["strategy_id"] == "MOMENTUM-002", (
        f"stamped {fake.built[0]['strategy_id']!r} — a CLOSED position supplied the lane"
    )
