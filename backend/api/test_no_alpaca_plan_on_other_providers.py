"""An Alpaca pricing plan must not ration a non-Alpaca provider (#619).

THE DEFECT, measured on staging-ibkr 2026-08-28 02:45 SGT, from the instance's OWN log, 87 times:

    realtime symbol budget (7.0, feed=iex) full — APA.XNAS gets bars/history only, no live
    trade/quote until a slot frees up or the market-data plan is upgraded (free-tier symbol cap)

`feed=iex` on an IBKR instance. The wire:

    engine_node.py:803   self._alpaca_feed: str = "iex"                      # class default
    engine_node.py:983   self._alpaca_feed = provider_config.get("feed", …)  # ONLY under
                                                                            # data_provider == "alpaca"
    engine_node.py:1187  budget = realtime_symbol_budget(self._alpaca_feed)  # EVERY provider
    engine_node.py:318   _MAX_REALTIME_SYMBOLS_IEX = 7

On any non-Alpaca provider line 983 never runs, the class default survives, and the node rations
itself to SEVEN live symbols against a competitor's free-tier cap. the operator's standing rule is that
staging has no business with Alpaca, ever.

The blast radius closes exactly: 21 held positions bypass the budget, +7 slots = 28 symbols x
(trade+quote) = the 56 tick subscriptions measured in #618, which are what hit IBKR's own
tick-by-tick limits (#578).

WHY THE EXISTING TESTS DID NOT CATCH IT. `test_engine_node.py` covers the budget properly and
deliberately — it PINS `strat._alpaca_feed = "iex"` with the comment "the CAPPED plan is what this
test is about — pin it, do not inherit". Correct for Alpaca. But every one of those tests constructs
with `ClientId("ALPACA")`, so no test has ever driven this seam on another provider. The unit was
right; nothing asked whether it should run at all.

THREE DEFECTS CODEX FOUND IN THE FIRST VERSION OF THIS FILE, all of which would have shipped green:

  1. THE FIXTURE WAS NOT ACTUALLY NON-ALPACA. It swapped the ClientId to INTERACTIVE_BROKERS but
     called the real `load_feed_config()`, and `feed.toml:11` still declares `[data].provider =
     "alpaca"` (conftest sets only KUMO_ENGINE, never KUMO_DATA). So the double could not represent
     the deployment it claimed to be — the exact "a double that cannot represent production is the
     bug" pattern, for the seventh time in this repo.
  2. IT SKIPPED THE HALF OF THE GATE THAT MATTERS MOST. `_granularities = []` meant the native BAR
     gate at :1233-1235 was never driven — and `allow_realtime` gates that too. Live trade/quote
     could be fixed while 1m/1d bars stayed capped, and every assertion here would still pass.
  3. IT COULD BE SATISFIED BY THE WRONG FIX. Counting stubbed `subscribe_*` lambdas proves symbols
     got subscribed; it does not prove the budget stopped being an Alpaca constant.
"""

from __future__ import annotations

import dataclasses
import inspect

from nautilus_trader.model.identifiers import ClientId, InstrumentId

from api.engine_node import UiFeedStrategy
from api.feed_config import load_feed_config
from api.providers.alpaca.data_client import _MAX_REALTIME_SYMBOLS_IEX, realtime_symbol_budget

_N = _MAX_REALTIME_SYMBOLS_IEX * 3          # comfortably past the cap, so the cap CAN bind
_NATIVE = ("1m", "1d")                      # the granularities `allow_realtime` also gates (:1233)


def _provider_budget(provider: str) -> float:
    """The budget the PROVIDER'S OWN `build_data` declares — the number production would use."""
    if provider == "alpaca":
        return realtime_symbol_budget("iex")
    from api.providers.ibkr import build_data

    return build_data({}).realtime_symbol_budget


def _provider_tick_planes(provider: str) -> tuple[bool, bool]:
    """`(streams_trade_ticks, streams_quote_ticks)` as the PROVIDER declares them (#812)."""
    if provider == "alpaca":
        import api.providers.alpaca.data_client as mod
        src = inspect.getsource(mod)
        return ("streams_trade_ticks=True" in src, "streams_quote_ticks=True" in src)
    from api.providers.ibkr import build_data

    spec = build_data({})
    return (spec.streams_trade_ticks, spec.streams_quote_ticks)


def _strategy(provider: str, client: str, *, granularities=("1m", "1d"), budget=None):
    """A UiFeedStrategy wired the way `provider` deploys it.

    `data_provider` is REPLACED on the config, not just the ClientId. Codex caught the first version
    passing `load_feed_config()` straight through, which declares `provider = "alpaca"` in
    `feed.toml` — so a test claiming to drive IBKR was driving an Alpaca config with an IBKR label.

    No `_alpaca_feed` assignment on the non-Alpaca path: production does not set it there either, and
    pinning it would reproduce the blind spot the existing suite already has.
    """

    class _PosCache:
        def positions_open(self):
            return []                        # nothing held, so nothing BYPASSES the budget

    class _Strat(UiFeedStrategy):
        @property
        def cache(self):                      # type: ignore[override]
            return self._pos_cache

        @property
        def clock(self):                      # type: ignore[override]
            # PRODUCTION ALWAYS HAS ONE — a Strategy gets its clock when the kernel registers it, and
            # these doubles are built outside a kernel. A double that cannot answer a question
            # production always answers is the bug, not the code that asks it (#618).
            import types as _t
            return _t.SimpleNamespace(timestamp_ns=lambda: 1_700_000_000_000_000_000)

    cfg = dataclasses.replace(load_feed_config(), data_provider=provider)
    # THROUGH THE CONSTRUCTOR, exactly as `build_node` passes `spec.realtime_symbol_budget` (#619).
    # Codex caught the first version driving the Alpaca cases by mutating `_alpaca_feed`: once the
    # budget is spec-owned that mutation controls nothing, so those tests would have gone VACUOUS
    # while staying green — the "a test that cannot fail carries no information" rule, caught before
    # it shipped rather than after.
    if budget is None:
        # FROM THE PROVIDER ITSELF, not a hand-passed default. Passing `inf` here is what made the
        # first mutation bite fail to bite: with the defect fully reintroduced the suite stayed 8/8
        # green, because injecting the answer proves only that `_after_definition` respects a budget
        # it is HANDED — never that the right one is chosen. Unit green, seam dead, which is this
        # repo's signature failure mode.
        budget = _provider_budget(provider)
    # THE PROVIDER'S OWN TICK DECLARATIONS, forwarded exactly as `build_node` forwards them (#812).
    #
    # Omitting them left both `None`, and the subscription gate reads `None` as "never declared,
    # keep the old behaviour" — so this fixture claimed to drive IBKR while driving a provider that
    # had declared nothing, and `test_EVERY_symbol_gets_live_TICKS_on_a_non_alpaca_provider` stayed
    # green through the change that stopped IBKR subscribing ticks at all. That is the same
    # double-cannot-represent-production shape this file's own docstring records twice.
    #
    # Read from the provider rather than hand-passed, for the reason the budget comment above gives:
    # injecting the answer proves only that `_after_definition` respects what it is HANDED.
    ticks = _provider_tick_planes(provider)
    strat = _Strat(
        cfg, ClientId(client), "", exec_client_id=ClientId(client), realtime_symbol_budget=budget,
        streams_trade_ticks=ticks[0], streams_quote_ticks=ticks[1],
    )
    strat._pos_cache = _PosCache()
    strat._granularities = list(granularities)
    return strat


def _drive(strat, n=_N):
    """Drive `_after_definition` — THE REAL ENTRY POINT — and record BOTH gated planes.

    Both, because `allow_realtime` gates ticks at :1196-1199 AND native bars at :1233-1235. Recording
    only the tick planes is how the first version of this file let half the defect through.
    """
    ticks: list[str] = []
    bars: list[str] = []
    strat.subscribe_trade_ticks = lambda iid, **k: ticks.append(str(iid))
    strat.subscribe_quote_ticks = lambda iid, **k: ticks.append(str(iid))
    strat.subscribe_bars = lambda b: bars.append(str(b))
    # `request_bars` fires the subscribe through its callback, exactly as the live path does.
    strat.request_bars = lambda bt, callback=None, **k: callback(None) if callback else None
    strat.request_instrument = lambda *a, **k: None
    for i in range(n):
        strat._after_definition(InstrumentId.from_str(f"SYM{i}.XNAS"))
    return ticks, bars


def test_the_fixture_can_express_the_defect():
    """FIXTURE PROPERTY FIRST, and it is three separate claims — each one of which, if false, makes
    every assertion below pass against a node with the bug intact.

    This is the kumo-strategies truncation-invariance failure one level out: a test that asserts an
    invariance on a fixture that cannot violate it teaches nothing.
    """
    assert _MAX_REALTIME_SYMBOLS_IEX < _N, "the fixture does not drive past the cap — nothing can bind"
    assert realtime_symbol_budget("iex") == float(_MAX_REALTIME_SYMBOLS_IEX)
    assert realtime_symbol_budget("sip") == float("inf"), "the paid feed is no longer unlimited"
    # ...and the IBKR fixture must really be non-Alpaca, which is what codex caught it not being.
    assert _strategy("ibkr", "INTERACTIVE_BROKERS")._cfg.data_provider == "ibkr", (
        "the fixture still reports data_provider=alpaca — it cannot express a non-Alpaca deployment"
    )


def test_a_non_alpaca_provider_has_no_alpaca_feed_default():
    """THE ROOT CAUSE. A default argument is what makes a missing argument invisible: the field is
    never unset, so no code path can notice it was never configured.

    Asserted on the CONSTRUCTED strategy, not the class attribute, so moving the default into
    `__init__` cannot make this pass while the defect survives.
    """
    feed = getattr(_strategy("ibkr", "INTERACTIVE_BROKERS"), "_alpaca_feed", None)
    assert feed is None, (
        f"a non-Alpaca provider carries _alpaca_feed={feed!r} — an Alpaca plan string on a node that "
        f"holds no Alpaca credential (#619)"
    )


def test_ALPACAS_CAP_IS_NOT_WHAT_DECIDES_a_non_alpaca_providers_ticks():
    """THE MEASURED FAILURE. 87 symbols on staging-ibkr were denied live trade/quote by Alpaca's
    free-tier cap. IBKR's own subscription limits are a different mechanism (#578/#617) and are the
    adapter's business, not a constant named after a competitor's pricing tier.

    THIS PINNED A COUNT (`len(ticks) == _N * 2`) UNTIL #812, AND THE COUNT WAS THE WRONG INVARIANT.
    IBKR declares it serves neither tick plane — measured, 224 unservable subscriptions and 0 ticks
    per boot — so the right number is now ZERO, and a test demanding `_N * 2` would have forced the
    subscription storm back. #619 and #812 do not conflict: what #619 forbids is Alpaca's PRICING
    TIER deciding, and what decides now is the venue's own declaration.

    So the assertion is on the CAUSE. The budget must not bind, and the outcome must not move when
    the Alpaca feed string is set to its most restrictive value — the shape
    `test_MUTATING_alpaca_feed_no_longer_moves_the_gate` already uses for the Alpaca side.
    """
    strat = _strategy("ibkr", "INTERACTIVE_BROKERS")
    assert strat._realtime_budget() == float("inf"), (
        "a non-Alpaca provider is capped — Alpaca's free-tier constant is rationing it (#619)"
    )
    baseline, bars = _drive(strat)

    capped = _strategy("ibkr", "INTERACTIVE_BROKERS")
    capped._alpaca_feed = "iex"                      # the old lever, at its most restrictive value
    after, bars_after = _drive(capped)

    assert len(after) == len(baseline), (
        f"setting _alpaca_feed changed a non-Alpaca provider's tick subscriptions "
        f"({len(baseline)} -> {len(after)}) — a competitor's pricing tier is deciding again (#619)"
    )
    assert len(bars_after) == len(bars) == _N * len(_NATIVE), (
        "the Alpaca cap is gating the native bars on a non-Alpaca provider — the half the first "
        "version of this file missed"
    )


def test_the_IBKR_tick_planes_are_refused_BY_THE_VENUES_OWN_DECLARATION_not_by_a_cap():
    """The other half of the split above: zero ticks, and for the RIGHT reason.

    An assertion that IBKR gets no ticks would pass under the #619 defect too — Alpaca's cap of 7
    against 21 symbols also yields "not all of them". What separates them is that the budget is
    infinite here, so nothing was rationed: the venue was simply never asked.
    """
    from api.providers.ibkr import build_data

    spec = build_data({})
    assert (spec.streams_trade_ticks, spec.streams_quote_ticks) == (False, False), (
        "IBKR no longer declares that it serves neither tick plane — re-anchor this test (#812)"
    )
    strat = _strategy("ibkr", "INTERACTIVE_BROKERS")
    assert strat._realtime_budget() == float("inf"), "the budget must not be what refuses them"
    ticks, bars = _drive(strat)
    assert ticks == [], (
        f"{len(ticks)} tick subscriptions went to a venue that declares it serves none — on staging2 "
        f"that was 224 requests answered with 10189/10190 and zero ticks, burning IB's shared "
        f"tick-by-tick concurrency limit (#812)"
    )
    assert len(bars) == _N * len(_NATIVE), (
        "refusing the tick planes also stopped the native bars — 1m/1d are what this instance "
        "actually trades on, and taking them out would take the instance dark"
    )


def test_EVERY_symbol_gets_live_NATIVE_BARS_on_a_non_alpaca_provider():
    """THE HALF THE FIRST VERSION MISSED (codex, test review). `allow_realtime` also gates the live
    bar subscribe for 1m/1d at :1233-1235 — the two granularities every consumer depends on.

    Fixing only the tick planes would leave a non-Alpaca node loading bar HISTORY and then never
    subscribing to the live follow-on: a chart frozen at boot, which looks exactly like a slow feed.
    This is the sibling path, and it is the one that would have shipped green.
    """
    _, bars = _drive(_strategy("ibkr", "INTERACTIVE_BROKERS"))
    assert len(bars) == _N * len(_NATIVE), (
        f"only {len(bars)} of {_N * len(_NATIVE)} native bar subscriptions survived on a non-Alpaca "
        f"provider — the Alpaca cap gates 1m/1d too (#619)"
    )


def test_the_budget_IS_DECLARED_BY_THE_PROVIDER_not_by_a_hardcoded_plan():
    """AIMED AT THE CLASS, and at the seam codex named.

    Fixing `_after_definition` alone leaves the next Alpaca-derived field free to reach another
    provider. `_alpaca_feed` already flows to `get_stock_snapshots(feed=…)` at :1363, inert today
    ONLY because `self._http` happens to be None on IBKR — "inert because another field happens to be
    None" is not a boundary.

    The budget is a CAPABILITY OF THE DATA PROVIDER. `DataClientSpec` (providers/base.py) is the
    provider-owned contract, and `build_node` already holds the spec at :6698 and throws its
    semantics away before constructing the strategy at :6779. Declaring the budget there makes the
    whole class unrepresentable instead of fixing one instance — a provider that declares no cap
    cannot be capped by one.
    """
    from api.providers.base import DataClientSpec

    assert "realtime_symbol_budget" in DataClientSpec.__dataclass_fields__, (
        "DataClientSpec does not declare a realtime subscription budget, so the cap is still a "
        "hardcoded Alpaca constant reachable from every provider (#619)"
    )
    assert _strategy("ibkr", "INTERACTIVE_BROKERS")._realtime_budget() == float("inf"), (
        "a non-Alpaca provider is still capped — the budget is not being read from the provider"
    )


def test_ALPACA_is_still_capped_and_this_change_does_not_widen_it():
    """The original behaviour must survive INTACT. Over-subscribing 405s Alpaca's ENTIRE stream, so
    breaking this to fix IBKR would trade a display gap on one tenant for a total feed outage on the
    other — the tenant that is actually trading.

    Both planes asserted, for the same reason the IBKR side asserts both.
    """
    strat = _strategy("alpaca", "ALPACA", budget=realtime_symbol_budget("iex"))
    ticks, bars = _drive(strat)
    assert len(ticks) == _MAX_REALTIME_SYMBOLS_IEX * 2, (
        "the Alpaca free-tier cap stopped binding on ticks — over-subscribing 405s the whole stream"
    )
    assert len(bars) == _MAX_REALTIME_SYMBOLS_IEX * len(_NATIVE), (
        "the Alpaca free-tier cap stopped binding on native bars — 1m/1d share the same slot budget"
    )


def test_the_PAID_alpaca_feed_is_still_uncapped():
    """The other end of the Alpaca behaviour. If the fix made the budget provider-declared but lost
    the sip/iex distinction, this is what would catch it — and it is the configuration the trading
    tenant actually runs on."""
    strat = _strategy("alpaca", "ALPACA", budget=realtime_symbol_budget("sip"))
    ticks, _ = _drive(strat)
    assert len(ticks) == _N * 2, "the paid SIP feed became capped"


def test_MUTATING_alpaca_feed_no_longer_moves_the_gate():
    """THE ANTI-VACUITY GUARD, and the reason codex reviews the test before the fix exists.

    Three existing tests in `test_engine_node.py` (:711, :752, :801) drive budget behaviour by setting
    `strat._alpaca_feed`, and so did the first version of this file. Once the budget is spec-owned that
    lever controls nothing — those tests keep passing while measuring NOTHING, which is precisely the
    shape that has shipped green here before.

    So pin it explicitly: the field may still exist for the today-range snapshot, but it must NOT be
    able to ration anything. If someone reconnects the budget to it, this fails.
    """
    strat = _strategy("alpaca", "ALPACA", budget=float("inf"))
    strat._alpaca_feed = "iex"                       # the old lever, at its most restrictive value
    ticks, _ = _drive(strat)
    assert len(ticks) == _N * 2, (
        "setting _alpaca_feed re-capped a node whose provider declared an unlimited budget — the "
        "budget is wired back to the feed string instead of to the provider (#619)"
    )


#: The minimal provider table each data provider needs to BUILD. Without these, `builder({})` raises
#: (databento: KeyError 'api_key_env'; alpaca: RuntimeError on missing creds) — and an `except:
#: continue` around that is what let codex's Databento mutant survive: the loop skipped the very
#: provider it was meant to check and reported green.
_MINIMAL_CONFIG = {
    "databento": {"api_key_env": "DATABENTO_API_KEY"},
    "ibkr": {},
    "alpaca": {},
}


def _build_every_provider(monkeypatch):
    """Build EVERY registered data provider, and refuse to skip any of them."""
    monkeypatch.setenv("DATABENTO_API_KEY", "k")
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    from api.providers import _DATA_REGISTRY

    built = {}
    for name, builder in _DATA_REGISTRY.items():
        built[name] = builder(_MINIMAL_CONFIG.get(name, {}))
    return built


def test_EVERY_registered_provider_can_actually_be_built(monkeypatch):
    """FIXTURE PROPERTY for the test below, and the exact hole codex found.

    The first version wrapped the build in `except Exception: continue`, so Databento — which raises
    without `api_key_env` — was silently skipped, and codex's mutant (`realtime_symbol_budget=7` on
    Databento's spec) passed the whole file. A test that skips its subject is not a lenient test; it
    is a test of nothing.

    Assert the coverage itself, so a provider that stops building here fails LOUDLY instead of
    quietly dropping out of the check below.
    """
    from api.providers import _DATA_REGISTRY

    built = _build_every_provider(monkeypatch)
    assert set(built) == set(_DATA_REGISTRY), (
        f"not every registered provider was built: missing {set(_DATA_REGISTRY) - set(built)} — the "
        f"cap check below would silently skip them"
    )
    assert set(_DATA_REGISTRY) - {"alpaca"}, "no non-Alpaca data provider is registered to check"


def test_NO_REGISTERED_NON_ALPACA_PROVIDER_DECLARES_A_CAP(monkeypatch):
    """THE WIRING, HALF ONE — aimed at the CLASS, not at IBKR.

    Pinning IBKR alone left a live mutant: codex showed that adding `realtime_symbol_budget=7` to
    DATABENTO's spec passed every other assertion here. The question is never "is IBKR right" but
    "what would have caught this AND its siblings".

    Walking the registry means a provider added later inherits this without anyone remembering to.
    """
    capped = {
        name: spec.realtime_symbol_budget
        for name, spec in _build_every_provider(monkeypatch).items()
        if name != "alpaca" and spec.realtime_symbol_budget != float("inf")
    }
    assert not capped, (
        f"these non-Alpaca data providers declare a finite realtime budget: {capped}. They have no "
        f"Alpaca plan to be rationed by, and a venue's real limits belong to its adapter (IBKR: "
        f"#578/#617), not to a slot count (#619)"
    )


def test_ONLY_the_alpaca_module_names_a_realtime_cap_at_all():
    """The same rule as SOURCE, so it holds even for a provider that cannot be built in tests.

    Two derivations of one property, deliberately: the build-based check above proves the values, and
    this proves no OTHER provider module even mentions the knob. If a future adapter is unbuildable
    here — a live socket, a licence file — the build check degrades to skipping it, and this one does
    not. Comments are stripped, because an explanatory comment naming the field would otherwise
    satisfy a raw grep, which has already fooled this repo once tonight.
    """
    import ast
    import pathlib as _pl

    root = _pl.Path(__file__).parent / "providers"
    offenders = []
    for f in root.rglob("*.py"):
        if "alpaca" in f.parts or f.name == "base.py" or f.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.keyword) and node.arg == "realtime_symbol_budget":
                offenders.append(f"{f.relative_to(root)}:{node.value.lineno}")
    assert not offenders, (
        f"non-Alpaca provider modules declare a realtime cap: {offenders}. The cap is an Alpaca plan "
        f"constraint; every other provider must inherit the uncapped default (#619)"
    )



def test_the_ALPACA_PROVIDER_still_declares_its_own_cap(monkeypatch):
    """THE WIRING, HALF TWO. Alpaca's cap must survive the move — over-subscribing 405s its entire
    stream, so losing this trades a display gap on the IBKR tenant for a total outage on the trading
    one. Driven through the real `build_data`, which reads its feed off the provider table."""
    from api.providers.alpaca.data_client import build_data

    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    assert build_data({"feed": "iex"}).realtime_symbol_budget == float(_MAX_REALTIME_SYMBOLS_IEX)
    assert build_data({"feed": "sip"}).realtime_symbol_budget == float("inf")


def test_build_node_PASSES_THE_SPECS_BUDGET_to_the_strategy():
    """THE WIRING, HALF THREE — the hop that the first version of this file left entirely unpinned.

    `build_node` already held the spec and threw its semantics away, constructing the strategy without
    it. That single omission is the whole defect: every provider then fell back to a default. A live
    node cannot be built in a unit test, so this reads the source with COMMENTS AND DOCSTRINGS
    STRIPPED — a raw grep is satisfied by the explanatory comment beside the fix, which has already
    fooled this repo once tonight (`test_feed_freshness_is_venue_neutral`).
    """
    import ast
    import inspect
    import textwrap

    import api.engine_node as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.build_node)))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    src = ast.unparse(fn)
    assert "realtime_symbol_budget=spec.realtime_symbol_budget" in src, (
        "build_node does not pass the data provider's declared budget to UiFeedStrategy, so the "
        "constructor default rations every provider — the defect itself (#619)"
    )


def test_the_settings_FEED_OVERRIDE_still_reaches_the_budget():
    """THE SUBTLE ONE, and the hazard codex named first: if the spec captured the feed BEFORE the UI's
    settings override were applied, an operator downgrading `sip -> iex` would leave the budget
    unlimited while the socket ran capped — reopening the whole-stream 405 that the cap exists to
    prevent, on the tenant that is actually trading.

    `build_node` applies the override to `provider_config` and only then calls
    `build_data_client_spec`, so the budget is derived from the SAME effective config the WS client
    connects on. That ordering is load-bearing and nothing else pins it.

    This is also strictly better than what it replaced: the budget used to come from `_alpaca_feed`,
    set separately in `on_start` off `cfg.provider_config` — two derivations of one fact, which is the
    shape that drifts. There is one now.
    """
    import ast
    import inspect
    import textwrap

    import api.engine_node as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.build_node)))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    src = ast.unparse(fn)
    assert "get_override" in src and "build_data_client_spec" in src, "build_node no longer does both"
    assert src.index("get_override") < src.index("build_data_client_spec"), (
        "the data client spec is built BEFORE the settings feed override is applied, so the realtime "
        "budget is derived from a stale feed — an iex override would budget as unlimited while the "
        "socket runs capped, 405-ing the whole Alpaca stream (#619)"
    )
