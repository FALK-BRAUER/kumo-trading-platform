"""The derived (no-broker-publisher) account frame must satisfy the DTO's own invariant (#591).

Measured on ibkr-paper-retired: equity 1,000,000.54, cash 973,807.42, long_market_value 0.00 — while the
book held AAMI/AEM and more. `AccountDTO` documents `cash + long_market_value == equity`; the frame
was off by 25,393.12, and BookTile rendered a zero market value over a held book. The literal 0.0
was written for the flat-book branch and survived the branch growing a held-book path.
"""

from __future__ import annotations

from api.engine_node import derived_account_frame


def test_the_fixture_is_the_staging_shape():
    """FIXTURE PROPERTY: a real equity, a lower cash, i.e. a held book — the branch the literal 0.0
    was never written for."""
    frame = derived_account_frame(equity=1_000_000.54, cash=974_607.42, cash_ccy="SGD",
                                  standing=None, ts=1)
    assert frame["equity"] > frame["cash"]


def test_the_dto_invariant_holds_on_a_held_book():
    frame = derived_account_frame(equity=1_000_000.54, cash=974_607.42, cash_ccy="SGD",
                                  standing=None, ts=1)
    lmv = frame["long_market_value"]
    assert lmv is not None and abs(frame["cash"] + lmv - frame["equity"]) < 0.01, (
        f"cash + long_market_value != equity (lmv={lmv}) — the #591 zero literal"
    )
    assert abs(lmv - 25_393.12) < 0.01


def test_flat_book_still_publishes_a_true_zero():
    frame = derived_account_frame(equity=100_000.0, cash=100_000.0, cash_ccy="USD",
                                  standing=None, ts=1)
    assert frame["long_market_value"] == 0.0


def test_the_publisher_actually_uses_the_builder():
    """THE WIRING (the #644 shape): a correct builder bypassed at the publish site ships the same
    defect with a green suite. Both the msgbus snapshot and the UI frame must come from it."""
    import ast
    import inspect
    import textwrap

    from api import engine_node as mod

    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(mod.UiFeedStrategy._publish_account))))
    assert src.count("derived_account_frame(") >= 1, "publish site does not use the builder"
    assert "'long_market_value': 0.0" not in src and '"long_market_value": 0.0' not in src, (
        "a literal zero market value survives at the publish site"
    )
