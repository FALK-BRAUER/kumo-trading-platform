"""#616: a '1d' bar means different things on IBKR and Alpaca — extended hours on one, RTH on the other.

The divergence, verified from the sources rather than from memory:

  IBKR     RTH for DAY bars since #875 (was EXTENDED under #616): `api/providers/ibkr.py` sets
           `use_regular_trading_hours=False` for the live plane, and the
           installed adapter forwards that flag into EVERY historical bar request — daily included
           (`nautilus_trader/adapters/interactive_brokers/data.py:625  use_rth=self._use_regular_
           trading_hours`, and the config docstring: "If True, will request data for Regular
           Trading Hours only. Only applies to bar data").
  Alpaca   RTH. `/v2/stocks/{sym}/bars` has no session parameter, and Alpaca's own aggregation
           table (Market Data FAQ, "How are bars aggregated?") marks conditions `T` (Extended
           Hours Trade) and `U` (Extended Trading Hours) RED for a DAILY bar's open/close AND
           high/low — extended-hours trades update daily VOLUME only, so daily OHLC is the
           regular session by construction.

So a premarket gap-reversal lands inside the IBKR daily bar and not the Alpaca one, two tenants
charting the same symbol legitimately disagree, and NEITHER looks wrong. The platform answer
(#608 shape): the venue difference is a DECLARED DIMENSION on the provider spec — never inferred
from a provider name above api/providers/ — and it is REQUIRED, because a default is what made
#574 invisible: a spec that says nothing must refuse to construct, not inherit a guess.
"""

from __future__ import annotations

import dataclasses

import pytest


def test_the_spec_REFUSES_construction_without_a_daily_bar_declaration():
    """Fixture-property first: the field must EXIST and carry NO default — then the refusal.

    Both halves matter. Asserting only the TypeError would go green the day someone adds the field
    WITH a default and passes it everywhere in production but one place — the #574 shape, where the
    default quietly answers for the provider that never declared.
    """
    from api.providers.base import DataClientSpec

    f = DataClientSpec.__dataclass_fields__.get("daily_bars_cover")
    assert f is not None, (
        "DataClientSpec has no `daily_bars_cover` field — the platform cannot say what session "
        "span a provider's 1-DAY bars aggregate over (#616)"
    )
    assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING, (
        "`daily_bars_cover` carries a default — an undeclared provider would silently inherit it, "
        "which is exactly how #574's dead knob stayed invisible"
    )
    with pytest.raises(TypeError):
        DataClientSpec(client_id="X", config=None, factory=None)


def test_a_garbage_declaration_is_refused_not_recorded():
    """'RTH', 'regular', 'yes' — a typo recorded as data is worse than a refusal, because every
    consumer comparing `== "rth"` would silently read it as the OTHER value."""
    from api.providers.base import DataClientSpec

    with pytest.raises(ValueError):
        # Both tick declarations are supplied so the refusal under test is about the GARBAGE
        # session value, not about a missing argument — a TypeError here would pass
        # `pytest.raises(ValueError)`? No: it would not, and that is the point. The double
        # must satisfy every OTHER requirement production satisfies (#612, #812).
        DataClientSpec(
            client_id="X", config=None, factory=None,
            daily_bars_cover="RTH", streams_trade_ticks=True, streams_quote_ticks=True,
            price_adjustments=frozenset({"raw"}),  # required since #1124, orthogonal here
        )


def test_each_data_provider_DECLARES_its_daily_bar_session(monkeypatch):
    """Both production providers declare RTH daily bars — Alpaca by the venue's own aggregation
    rule, IBKR by this repo's factory (#875, superseding #616 under which they DIFFERED). Each is
    pinned against the measured venue catalogue (api/venues/facts.py) because two derivations of one
    fact drift, and IBKR's is pinned to the FACTORY that makes it true, so the declaration cannot
    outlive the mechanism."""
    from api.providers import ibkr
    from api.providers.alpaca.data_client import build_data as alpaca_build_data
    from api.venues.facts import ALPACA, IBKR

    ibkr_spec = ibkr.build_data({})
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    alpaca_spec = alpaca_build_data({})

    # RTH SINCE #875 (coordinator, 2026-09-11): the vendor adapter still forwards
    # `use_regular_trading_hours=False` into every bar request, but the cockpit's data-client factory
    # rewrites `use_rth` for DAY aggregations on the IB client it returns — for the WHOLE node, chart
    # included, superseding #616's extended daily bars. This assertion used to pin "extended" as
    # deliberate and turned red on the gate the moment the decision moved: the declaration is tied
    # to the FACTORY that makes it true (test_ibkr_rth_daily pins the pair), not to the config flag.
    assert ibkr_spec.daily_bars_cover == "rth", (
        "IBKR's DAY bars are requested regular-hours by RthDailyIBDataClientFactory (#875); the "
        "declaration must say so, and must move with that factory, not with the config flag")
    assert ibkr_spec.factory.__name__ == "RthDailyIBDataClientFactory", (
        "a declaration of 'rth' without the factory that makes it true is the comment-that-reads-"
        "as-safety class")
    assert alpaca_spec.daily_bars_cover == "rth", (
        "Alpaca's SIP aggregation rules exclude extended-hours trades from daily OHLC — the spec "
        "must say so"
    )
    assert ibkr_spec.daily_bars_cover == IBKR["daily_bars_cover"]
    assert alpaca_spec.daily_bars_cover == ALPACA["daily_bars_cover"]
    # THE TWO AGREE SINCE #875, AND AGREEMENT IS NOT CONNECTION: they must agree for DIFFERENT
    # reasons, each named in its own provenance — Alpaca by the venue's SIP aggregation rule, IBKR by
    # this repo's factory. Two catalogue entries citing the same mechanism would be the copy-paste
    # declaration #616's original `!=` assertion existed to catch.
    assert ibkr_spec.daily_bars_cover == alpaca_spec.daily_bars_cover == "rth"
    assert "RthDailyIBDataClientFactory" in IBKR.evidence_for("daily_bars_cover").evidence
    assert "SIP" in ALPACA.evidence_for("daily_bars_cover").evidence
    assert "RthDailyIBDataClientFactory" not in ALPACA.evidence_for("daily_bars_cover").evidence


def test_the_databento_spec_declares_too():
    """The field is REQUIRED, so the third (currently inactive) provider must also answer — from
    Databento's documented semantics, not a guess: 'Our ohlcv-1d schema is based on UTC dates. If
    you are interested in daily data based on exchange session hours, you may need to ... aggregate
    the data yourself' (docs/schemas-and-data-formats/ohlcv, read 2026-08-29). A UTC calendar day
    over every trade the feed prints is not the regular session: it is 'extended'."""
    import ast
    import inspect

    from api.providers import databento

    # Source-level, not construction: `build` needs a live API key and the module resolves venue
    # datasets at import. The declaration is what #616 requires, and the REQUIRED field guarantees
    # a construction without it cannot survive to production.
    src = inspect.getsource(databento.build)
    call = next(
        n for n in ast.walk(ast.parse(src.lstrip()))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "DataClientSpec"
    )
    kw = {k.arg: k.value for k in call.keywords}
    assert "daily_bars_cover" in kw, (
        "databento.build constructs DataClientSpec without daily_bars_cover — with the field "
        "required this cannot even construct, so the databento path would die at boot"
    )
    assert getattr(kw["daily_bars_cover"], "value", None) == "extended", (
        "Databento ohlcv-1d is a UTC-date aggregation over all dataset trades (their docs), "
        "not a regular-session bar"
    )


def test_the_builder_hands_the_session_span_to_the_feed():
    """THE SEAM (#574 shape): a declared spec field nobody wires is declared, documented, and
    consulted by nothing. `build_node` must forward the spec's own field into the feed strategy,
    the same wiring the other three data dimensions get."""
    import ast
    import inspect
    import textwrap

    import api.engine_node as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.build_node)))
    fn = tree.body[0]
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    assert "daily_bars_cover=spec.daily_bars_cover" in ast.unparse(fn), (
        "build_node does not forward daily_bars_cover, so the constructor default wins on every "
        "node and the compass payload can never say which session its daily bars cover"
    )


def test_the_compass_payload_SAYS_which_session_its_daily_bars_cover():
    """The one consumer wired now: the market compass grades ADX/Ichimoku over daily bars 'on
    whatever provider this node has', and its own header says the grade is 'fixed so two stacks
    agree'. Two stacks on different providers now grade over DIFFERENT session spans by venue
    construction — so the payload must carry `daily_bars_cover`, making a cross-stack disagreement
    attributable to the declared dimension instead of reading as a broken compass.

    Three states, never two: a feed constructed without the declaration stamps 'undeclared' —
    absence must not be readable as either session."""
    import ast
    import inspect
    import textwrap

    import api.engine_node as mod

    src = textwrap.dedent(inspect.getsource(mod.UiFeedStrategy._refresh_rotation))
    tree = ast.parse(src)
    stamped = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "daily_bars_cover"
            for t in n.targets
        )
    ]
    assert stamped, (
        "_refresh_rotation publishes a graded payload with no daily_bars_cover stamp — two stacks "
        "disagreeing on identical prices would still read as a defect instead of a declared "
        "venue difference"
    )
    assert "undeclared" in src, (
        "the stamp has no third state — a feed whose builder never declared the span would "
        "publish one of the real values, which is absence read as an answer"
    )
