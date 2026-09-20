"""External order claims must be resolved at BUILD, not at on_start (#622 follow-up).

THE CRASH, ibkr-paper-retired 2026-08-29 02:45 SGT:

    TypeError: MomentumRotationStrategy.__init__() got an unexpected keyword argument 'claimed_symbols'

My #622 interface note proposed two renames — `instrument_ids -> symbols` AND
`external_order_claims -> claimed_symbols`. The first shipped. The second never existed, and cockpit
was changed as though it had.

THE TWO HALVES ARE NOT SYMMETRIC, WHICH IS THE REAL LESSON. Measured in the installed
nautilus_trader, `system/kernel.py`:

    1022  _connect_clients()
    1024  await _await_engines_connected()
    1028  await _await_execution_reconciliation()    <- RECONCILIATION
    1039  _trader.start()                            <- on_start

**`symbols` can be lazy because SUBSCRIPTION happens after connect. Claims cannot, because
RECONCILIATION happens before `on_start`.** Claims exist to catch reconciliation-generated flatting
orders; resolving them at `on_start` registers a claimant AFTER the event it protects against, and
would look like it worked.

WHAT IS AT STAKE. With no claimant, Nautilus books a reconciliation flatting order under EXTERNAL,
and NETTING's `{instrument}-{strategy}` position id turns that into a PHANTOM position instead of
closing the real one. HSBC sat as a -93 short the broker had never heard of (#197 B8). So "pass None
and boot" is not available: it boots and silently reintroduces a defect already paid for.

THE COLD-START GAP IS REAL AND MUST BE REPORTED. Claims resolve from the durable cache, which a
genuinely cold node does not have. That is survivable where it was not for symbols — every boot needs
symbols, only a node with existing positions needs claims — but "usually" is not "always": staging
had 22 non-flat positions against a fresh container. So a claim that could not resolve is NAMED.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _code(fn) -> str:
    """Source with the docstring stripped — a docstring naming the kwarg must not satisfy these."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
        node.body = node.body[1:]
    return ast.unparse(node)


def test_the_constructor_really_does_NOT_accept_claimed_symbols():
    """FIXTURE PROPERTY FIRST. If kumo-trading-strategies had since added `claimed_symbols`, this whole file
    would be guarding against a problem that no longer exists — and passing while doing so."""
    from kumo_strategies.runtime.nautilus.momentum_rotation import MomentumRotationStrategy

    params = inspect.signature(MomentumRotationStrategy.__init__).parameters
    assert "external_order_claims" in params, "the claims parameter was renamed; update this file"
    assert "claimed_symbols" not in params, (
        "kumo-trading-strategies now accepts claimed_symbols — reconsider whether claims can be lazy, and "
        "check the reconciliation-before-on_start ordering before concluding they can"
    )


def test_cockpit_passes_the_keyword_the_constructor_ACTUALLY_ACCEPTS():
    """THE CRASH. Aimed at the class: any keyword cockpit invents is a build-time TypeError that
    crash-loops the node, and it is the third such gap shipped green in one day."""
    import strategies.momentum as mod

    code = _code(mod._build_rotation)
    assert "claimed_symbols=" not in code, (
        "cockpit passes `claimed_symbols`, which MomentumRotationStrategy does not accept — "
        "TypeError at build, crash loop (#622)"
    )
    assert "external_order_claims=" in code


def test_claims_are_RESOLVED_TO_IDS_not_passed_as_bare_symbols():
    """`external_order_claims` is typed `list[InstrumentId]` and goes straight into StrategyConfig.
    Bare symbols would register a claim on nothing, silently — the failure this exists to prevent,
    arriving through the fix for it."""
    import strategies.momentum as mod

    code = _code(mod._build_rotation)
    assert "_resolve_claims(" in code, (
        "claims are not resolved to instrument ids before registration (#622/#197 B8)"
    )


def test_an_UNRESOLVABLE_claim_is_NAMED_not_silently_dropped(caplog):
    """THE COLD-START GAP, reported rather than hidden.

    Claims resolve from the durable cache, which a cold node lacks. Every boot needs symbols; only a
    node with existing positions needs claims — so this is survivable where the same dependency was
    not for symbols. But staging had 22 non-flat positions against a fresh container, so "usually
    empty" is not "always empty", and a claim that silently failed is invisible.
    """
    from types import SimpleNamespace

    from nautilus_trader.model.identifiers import InstrumentId

    import strategies.momentum as mod

    # `positions_open` IS PART OF THE CACHE PRODUCTION HANDS THIS. A double lacking it made
    # `_resolve_claims` raise AttributeError the moment it began checking whether a symbol is held
    # by more than one lane (#749) — and the tempting fix, a `getattr(cache, "positions_open", ...)`
    # fallback, would have SILENTLY SKIPPED the ambiguity check wherever the attribute was absent.
    # Absence must not read as permission; fix the double.
    cache = SimpleNamespace(
        instrument_ids=lambda: [InstrumentId.from_str("AAPL.XNAS")],
        positions_open=list,
    )
    with caplog.at_level("ERROR"):
        got = mod._resolve_claims(["AAPL", "GONE"], cache, "MOMENTUM-002")
    assert [str(i) for i in got] == ["AAPL.XNAS"]
    assert "GONE" in caplog.text, "a claim that could not be resolved was dropped in silence"


def test_resolving_NO_claims_at_all_is_reported_LOUDLY():
    """0 of 3 is the case that matters and the one an empty cache produces. It must not read the same
    as "this lane claims nothing", which is a deliberate configuration (BCTROT passes claims_from
    None). Two different facts, and conflating them is how #197 B8 stayed invisible."""
    from types import SimpleNamespace

    import strategies.momentum as mod

    cache = SimpleNamespace(instrument_ids=list, positions_open=list)
    logged: list[str] = []
    with pytest.raises(Exception) if False else __import__("contextlib").nullcontext():
        got = mod._resolve_claims(["AAPL", "MSFT"], cache, "MOMENTUM-002", _sink=logged.append)
    assert got == []
    assert any("0 of 2" in m for m in logged), (
        "a lane that claims 2 symbols and resolved none did not say so — indistinguishable from a "
        "lane that deliberately claims nothing"
    )
