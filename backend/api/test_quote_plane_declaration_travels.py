"""The quote-plane declaration must travel provider -> spec -> build_node -> strategy -> subscribe (#812).

THE HOP THE BEHAVIOURAL TESTS CANNOT REACH, and codex flagged it as the gap in the first draft of
`test_tick_planes_are_not_subscribed_without_a_tape.py`: those tests set `_streams_quote_ticks`
directly on a `__new__`-built probe, because constructing a real `UiFeedStrategy` needs a live
Nautilus kernel. So every one of them would stay GREEN against a fix where

  - `DataClientSpec` never grows the field,
  - the field grows a DEFAULT so a provider that says nothing is assumed to serve quotes,
  - IBKR or Alpaca declares the wrong value,
  - `build_node` never forwards it,
  - or `UiFeedStrategy.__init__` accepts it and drops it on the floor.

That is this repo's signature failure — "a change that anchors on something absent does nothing,
quietly" — and it is why #612's sibling file carries the same pair of source assertions for
`streams_trade_ticks`. This is the quote equivalent, plus the per-provider values.

REQUIRED, NOT DEFAULTED. `streams_trade_ticks` is `field(kw_only=True)` with no default precisely so
every provider must answer; a `bool = False` default would make "never told us" indistinguishable
from "declares no quotes", and a `True` default would put the 224-subscription storm back on any
provider added later that forgets to answer. Three states collapse to two the moment a default
appears, so the absence of one is asserted here.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import api.engine_node as engine_node
from api.providers.base import DataClientSpec


def test_the_SPEC_declares_the_quote_plane_and_REQUIRES_an_answer():
    fields = DataClientSpec.__dataclass_fields__
    assert "streams_quote_ticks" in fields, (
        "DataClientSpec cannot express whether a venue serves an NBBO stream, so `_after_definition` "
        "has nothing to gate the quote subscription on (#812)"
    )
    import dataclasses
    f = fields["streams_quote_ticks"]
    assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING, (
        "streams_quote_ticks has a DEFAULT. A provider that never answers would then be assumed to "
        "serve (or not serve) quotes, which is exactly the absence-read-as-an-answer this field "
        "exists to prevent — match streams_trade_ticks and require it"
    )


def test_a_SPEC_BUILT_WITHOUT_IT_RAISES():
    """The requirement, proven rather than inferred from the dataclass metadata. A `field(kw_only=True)`
    with no default is only load-bearing if omitting it actually fails."""
    with pytest.raises(TypeError):
        DataClientSpec(
            client_id="X", config=None, factory=None,
            daily_bars_cover="rth", streams_trade_ticks=True,
        )


@pytest.mark.parametrize(
    ("module", "builder", "trades", "quotes", "why"),
    [
        (
            "api.providers.ibkr", "build_data", False, False,
            "MEASURED on staging2 2026-09-09: 135 x 10189 (no market data permissions for NYSE / "
            "ISLAND / AMEX STK) and 199 x 10190 (max tick-by-tick requests reached) across 224 "
            "subscriptions, 0 ticks delivered. IB serves market data to ONE session per user and "
            "the operator's live session holds it.",
        ),
    ],
)
def test_each_provider_declares_BOTH_planes_honestly(module, builder, trades, quotes, why):
    """The values themselves, per provider. A field every provider must answer is only worth having
    if the answers are right — and these two are the whole point of the ticket."""
    import importlib
    spec = getattr(importlib.import_module(module), builder)({})
    assert spec.streams_trade_ticks is trades, f"{module} declares the wrong trade tape. {why}"
    assert spec.streams_quote_ticks is quotes, f"{module} declares the wrong quote plane. {why}"


def test_alpaca_still_declares_BOTH_planes_true():
    """The other direction, and the one a careless fix breaks. Alpaca serves trades AND quotes on
    SIP; declaring either False would silently delete a working plane on the paper stack, which is a
    far worse outcome than the log noise this ticket is about.

    Read from the source rather than by building the spec, because `build_data` there wants
    credentials and a catalog this test has no business touching.
    """
    import api.providers.alpaca.data_client as mod
    src = inspect.getsource(mod)
    assert "streams_trade_ticks=True" in src, "Alpaca no longer declares its trade tape"
    assert "streams_quote_ticks=True" in src, (
        "Alpaca does not declare its quote plane — on the paper stack the order ticket's spread/mid "
        "prefill and marketable-limit auto-select would go dark"
    )


def test_the_STRATEGY_takes_the_parameter():
    tree = ast.parse(textwrap.dedent(inspect.getsource(engine_node.UiFeedStrategy.__init__)))
    fn = tree.body[0]
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "streams_quote_ticks" in params, (
        "UiFeedStrategy takes no `streams_quote_ticks`, so the provider's declaration cannot reach "
        "the subscription no matter what build_node passes"
    )


def test_the_STRATEGY_STORES_the_parameter_rather_than_a_constant():
    """A parameter accepted and then ignored is the same defect wearing the signature of a fix.

    Source assertion on purpose: the behavioural tests set `_streams_quote_ticks` themselves, so
    substituting a literal here is invisible to every one of them — the mutation that proved the
    equivalent hop open for `streams_trade_ticks` in #612.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(engine_node.UiFeedStrategy.__init__)))
    assigns = [
        n for n in ast.walk(tree.body[0])
        if isinstance(n, ast.Assign)
        and any(getattr(t, "attr", None) == "_streams_quote_ticks" for t in n.targets)
    ]
    assert assigns, "UiFeedStrategy never stores streams_quote_ticks"
    value = assigns[0].value
    assert isinstance(value, ast.Name) and value.id == "streams_quote_ticks", (
        f"_streams_quote_ticks is set from {ast.dump(value)} rather than from the constructor's own "
        f"parameter — the provider declares and the node ignores it"
    )


def test_BUILD_NODE_forwards_the_spec_field_to_the_strategy():
    """The hop between them. A required field nobody forwards is #581's shape: compose declared two
    variables into a container that read neither."""
    src = ast.unparse(ast.parse(inspect.getsource(engine_node.build_node)))
    assert "streams_quote_ticks=spec.streams_quote_ticks" in src, (
        "build_node constructs the strategy without forwarding the provider's quote-plane "
        "declaration, so the constructor default (or None) wins on every node"
    )


def test_the_SUBSCRIPTION_reads_the_stored_field_for_each_plane():
    """The last hop, asserted at the source because the behavioural file supplies both flags itself.

    Pins that the two planes are gated SEPARATELY: `_streams_trade_ticks` guards the trade call and
    `_streams_quote_ticks` guards the quote call. Gating both on one attribute passes every
    behavioural test that only exercises the False/False and True/True corners.
    """
    src = inspect.getsource(engine_node.UiFeedStrategy._after_definition)
    assert "_streams_trade_ticks" in src, "_after_definition does not consult the trade declaration"
    assert "_streams_quote_ticks" in src, "_after_definition does not consult the quote declaration"

    tree = ast.parse(textwrap.dedent(src))
    for call, guard in (("subscribe_trade_ticks", "_streams_trade_ticks"),
                        ("subscribe_quote_ticks", "_streams_quote_ticks")):
        node = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == call),
            None,
        )
        assert node is not None, f"{call} is no longer called at all — re-anchor this test"
        enclosing = [
            ast.unparse(n.test) for n in ast.walk(tree)
            if isinstance(n, ast.If) and any(
                isinstance(c, ast.Call) and getattr(c.func, "attr", None) == call
                for c in ast.walk(n)
            )
        ]
        assert any(guard in t for t in enclosing), (
            f"{call} is not guarded by {guard}; the conditions reaching it are {enclosing}. One flag "
            f"gating both planes would pass the behavioural corners and be wrong."
        )
