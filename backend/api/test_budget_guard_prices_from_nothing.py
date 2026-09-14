"""#990 — the budget gate prices an order at 0.0 when the cache has no tick, and therefore cannot refuse.

Measured on staging2 (IBKR, cockpit aeed851), 2026-09-11: "budget gate INSTALLED on
InteractiveBrokersExecutionClient" at every one of four boots, then ZERO allow/refuse lines and ZERO
journal rows carrying its facts since 09-09; TECHIVOL-005 bought 39 GLD and 61 NVDA at 09-10 16:00Z
with every `strategy_sleeve.actual` at 0. Why: `budget_guard.budget_allows` takes the order's own
price, else `cache.quote_tick(...) or cache.trade_tick(...)`, else `px = 0.0` — and that tenant has NO
quote/trade subscription for any name it trades (#983: the login's 100 streaming lines are consumed
by a shadow lane's universe; the held names are priced from 1-MINUTE bars). A market order therefore
checks at notional 0, `0 > room` is never true, and the gate allows everything, silently.

THE RULES THESE TESTS PIN (coordinator, 11:08Z):
  * a price of zero is not a price, it is the ABSENCE of one: `px = 0.0` REFUSES and names the
    missing input — never "infinitely affordable";
  * the gate prices from the SAME SOURCE the lane sizes from (the node's own last-price resolver,
    which on IBKR is bars) — two derivations of one notional drift;
  * every decision JOURNALS its facts (room, needs, deployed, allow/refuse) — a gate that decides
    silently is indistinguishable from one that is not installed;
  * FAILS OPEN stays for an UNREADABLE BOOK (that is the documented policy) — a missing PRICE is a
    different condition from a missing book and gets the opposite answer.

Fixture property first: with a tick in the cache the same order IS refused, so the refusal below is
the gate's doing, not an order that would have been refused anyway.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from api.budget_guard import budget_allows
from api.budget_store import Sleeve


def _run(coro):
    """No pytest-asyncio in this repo (memory: needs_services tests cannot run); coroutines are driven
    with asyncio.run, as api/test_every_exec_client_is_gated.py does."""
    return asyncio.run(coro)


class _Log:
    def __init__(self):
        self.lines: list[str] = []

    def _rec(self, msg, *a):
        self.lines.append(str(msg))

    warning = info = error = debug = exception = _rec


class _MarketOrder:
    """What a lane's market order looks like at the gate: no price, no trigger."""
    def __init__(self, qty=10):
        self.strategy_id = "TECHIVOL-005"
        self.instrument_id = "GLD.ARCX"
        self.client_order_id = "coid-990"
        self.quantity = qty
        self.side = types.SimpleNamespace(name="BUY")
        self.price = None
        self.trigger_price = None


class _Cache:
    def __init__(self, quote=None, trade=None):
        self._quote, self._trade = quote, trade

    def positions_open(self, strategy_id=None, instrument_id=None):
        return []

    def quote_tick(self, iid):
        return self._quote

    def trade_tick(self, iid):
        return self._trade


class _Book:
    def __init__(self, sleeve: Sleeve):
        self.sleeves = {sleeve.strategy_id: sleeve}


def _book(target=1_000.0):
    sleeve = Sleeve("TECHIVOL-005", target, target)   # funded to its target, nothing deployed
    b = _Book(sleeve)

    async def loader():
        return b
    return loader


def test_FIXTURE_with_a_tick_in_the_cache_the_same_order_IS_refused():
    """10 shares at a 200.00 bid against a 1,000 sleeve = 2,000 needed: refused, by the gate, today."""
    tick = types.SimpleNamespace(price=200.0, bid_price=200.0)
    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_Cache(quote=tick),
                                              book_loader=_book(1_000.0), log=_Log()))
    assert allowed is False, (why, facts)
    assert facts["needs"] == 2_000.0 and facts["room"] == 1_000.0


def test_a_market_order_with_NO_tick_is_REFUSED_naming_the_missing_price_not_allowed_at_notional_zero():
    """THE DEFECT. Same order, same sleeve, no tick anywhere: today (True, '', {... needs: 0.0}) —
    the gate says yes at notional zero. It must say no, and say which input was missing."""
    log = _Log()
    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_Cache(), book_loader=_book(1_000.0), log=log))
    assert allowed is False, f"allowed at notional zero: why={why!r} facts={facts!r}"
    assert "price" in why.lower() and ("no " in why.lower() or "missing" in why.lower() or "unknown" in why.lower()), why
    assert facts.get("needs") != 0.0, "a zero notional must never be the number the gate reasoned from"
    assert facts.get("price") in (None, "missing", "absent"), facts


def test_the_gate_prices_from_the_SAME_source_the_lane_sizes_from():
    """On IBKR the lanes size from bars (the node's last-price resolver), not from ticks the login
    cannot subscribe. The gate takes that resolver and uses it: no tick, resolver says 200 → 2,000
    needed → refused against 1,000. Two derivations of one notional must be one derivation."""
    seen = []

    def price_of(iid):
        seen.append(str(iid)); return 200.0

    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_Cache(), book_loader=_book(1_000.0),
                                              log=_Log(), price_of=price_of))
    assert seen == ["GLD.ARCX"], "the resolver was not consulted for the order's instrument"
    assert allowed is False and facts["needs"] == 2_000.0, (why, facts)


def test_a_resolver_that_has_no_price_either_REFUSES_by_name_never_falls_back_to_zero():
    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_Cache(), book_loader=_book(1_000.0),
                                              log=_Log(), price_of=lambda iid: None))
    assert allowed is False and "price" in why.lower(), (why, facts)


def test_EVERY_decision_is_journaled_with_its_facts_allow_and_refuse_alike():
    """A gate that decides silently is indistinguishable from one that is not installed — which is
    how this sat since 09-09 beside 'budget gate INSTALLED' at every boot. One line per decision on
    the gate's own log, carrying room, needs, deployed and the verdict."""
    tick = types.SimpleNamespace(price=50.0, bid_price=50.0)
    log = _Log()
    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_Cache(quote=tick),
                                              book_loader=_book(1_000.0), log=log))
    assert allowed is True and facts["needs"] == 500.0
    lines = [ln for ln in log.lines if "budget" in ln.lower()]
    assert lines, "an ALLOWED order left no trace on the log"
    assert any("room" in ln and "needs" in ln and "deployed" in ln and ("allow" in ln.lower()) for ln in lines), lines
    log2 = _Log()
    allowed2, why2, _ = _run(budget_allows(_MarketOrder(), cache=_Cache(), book_loader=_book(1_000.0), log=log2))
    assert allowed2 is False
    assert any("refus" in ln.lower() and "price" in ln.lower() for ln in log2.lines), log2.lines


def test_an_UNREADABLE_book_still_fails_OPEN_and_says_so_the_documented_policy_is_unchanged():
    """The two conditions must not be conflated: no BOOK → allow and report (allocation policy, not
    an interlock); no PRICE → refuse. Fixing one by breaking the other would be the over-correction."""
    async def broken():
        raise RuntimeError("postgres away")
    log = _Log()
    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_Cache(), book_loader=broken, log=log))
    assert allowed is True
    assert any("skipped" in ln.lower() or "unreadable" in ln.lower() for ln in log.lines), log.lines


# -- through the INSTALLED gate, the way the IB client runs it ---------------------------------------------

class _Clock:
    def timestamp_ns(self):
        return 1_000


class _VendorClient:
    def __init__(self, cache):
        self._cache = cache
        self._log = _Log()
        self._clock = _Clock()
        self.reached_venue = 0
        self.denied = []

    async def _submit_order(self, command):
        self.reached_venue += 1

    def generate_order_denied(self, **kw):
        self.denied.append(kw)


def test_through_the_installed_gate_a_priceless_market_order_is_DENIED_and_never_reaches_the_venue(monkeypatch):
    from api.providers.gated_exec import install_budget_gate

    client = install_budget_gate(_VendorClient(_Cache()))
    monkeypatch.setattr("api.budget_guard.load_book", _book(1_000.0))
    command = types.SimpleNamespace(order=_MarketOrder())
    _run(client._submit_order(command))
    assert client.reached_venue == 0, "a priceless market order reached the venue"
    assert client.denied and "price" in str(client.denied[0]).lower(), client.denied


# -- the three rulings from the coordinator's coverage pass (11:17Z) ------------------------------------------

def test_the_REAL_installer_hands_the_gate_the_LANES_OWN_sequence_and_a_swap_fails_here():
    """(a) The `price_of` seam is only a seam. With NO resolver passed, the installed gate must price
    through `api.budget_guard.DEFAULT_PRICE_OF` — the lanes' own sequence — and the IB factory must
    not override it. Pinned by behaviour AND by identity: a swap for any other source fails here."""
    import unittest.mock as um

    import api.budget_guard as bg
    from api.last_price import last_price
    from api.providers.gated_exec import install_budget_gate

    assert bg.DEFAULT_PRICE_OF is last_price or getattr(bg.DEFAULT_PRICE_OF, "__name__", "") == "last_price_from_cache"
    seen = {}

    async def spy(order, *, cache, book_loader, log, price_of=None, journal=None, **kw):
        seen["price_of"] = price_of; return (True, "", {})
    client = install_budget_gate(_VendorClient(_Cache()))
    with um.patch.object(bg, "budget_allows", spy):
        _run(client._submit_order(types.SimpleNamespace(order=_MarketOrder())))
    assert seen.get("price_of") is None, "the installer must not hand the gate a resolver of its own — None means the default"
    # the IB factory: source-pinned not to pass price_of= (it passes journal=), so the default is what runs there
    import inspect
    from api.providers import ibkr
    src = inspect.getsource(ibkr.BudgetGatedIBExecClientFactory.create)
    assert "price_of=" not in src and "journal=journal_row" in src


def test_the_mirror_matches_the_installed_lanes_sequence_source_read_until_upstream_exposes_it():
    """The lanes' sequence lives inline in `NautilusBroker.last_price` (or, once l21's extraction
    lands, in `last_price_from_cache`). The cockpit mirror's SEQUENCE tuple must equal the tuple in
    the installed source, read off the AST — the ruled interim guard: the two cannot diverge unnoticed
    while the extraction (FOLLOW_UP) is pending. When upstream also reports source+age the mirror and
    this test are deleted together."""
    import ast
    import inspect
    import textwrap

    import kumo_strategies.runtime.nautilus.broker as upstream
    from api.last_price import FOLLOW_UP, SEQUENCE

    assert "990" in FOLLOW_UP
    # BOTH SHAPES MUST PARSE: on a pin that predates the extraction the sequence lives in the METHOD
    # `NautilusBroker.last_price`, whose source is INDENTED — `ast.parse` on it raises SyntaxError at
    # "<unknown>, line 1". The gate's PIN RUN caught exactly that (1 failed against 06f3055 while the
    # editable tree, which has the free function, passed), which is the difference the pin run exists
    # to find. `dedent` handles the method; a free function is unaffected.
    fn = getattr(upstream, "last_price_from_cache", None) or upstream.NautilusBroker.last_price
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    tuples = [tuple(e.value for e in n.elts) for n in ast.walk(tree) if isinstance(n, ast.Tuple)
              and n.elts and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in n.elts)]
    assert SEQUENCE in tuples, f"upstream's sequence tuples {tuples} do not contain the mirror's {SEQUENCE}"
    assert "cache.bar(" in inspect.getsource(fn), "upstream lost its bar fallback — the mirror's would then be a different sequence"


def test_EVERY_decision_lands_as_a_ROW_in_the_journal_not_only_on_the_log():
    """(b) A log line dies with the container (#955, #974). One durable row per decision — allow and
    refuse — carrying the facts, through the journal writer the gate is installed with."""
    rows = []

    async def journal(**row):
        rows.append(row)
    tick = types.SimpleNamespace(price=50.0, bid_price=50.0)
    allowed, _, facts = _run(budget_allows(_MarketOrder(), cache=_Cache(quote=tick), book_loader=_book(1_000.0),
                                           log=_Log(), journal=journal))
    assert allowed is True
    assert len(rows) == 1 and rows[0]["kind"] == "budget" and rows[0]["code"] == "budget_allow", rows  # kind measured: `risk` is consumed by session_state.py:72 and narrative/check.py regardless of code
    assert rows[0]["strategy_id"] == "TECHIVOL-005" and rows[0]["detail"]["needs"] == 500.0 and "room" in rows[0]["detail"]
    allowed2, _, _ = _run(budget_allows(_MarketOrder(), cache=_Cache(), book_loader=_book(1_000.0),
                                        log=_Log(), journal=journal))
    assert allowed2 is False
    assert len(rows) == 2 and rows[1]["code"] == "budget_refused" and rows[1]["detail"]["price"] == "missing", rows[1]


def test_the_IB_factory_installs_the_gate_WITH_a_journal_writer_so_the_row_exists_in_production():
    """Wired, not merely accepted: `BudgetGatedIBExecClientFactory.create` must hand `install_budget_gate`
    a journal writer that reaches exec_action_log. A seam nobody wires is the #955 shape."""
    import inspect
    from api.providers import ibkr
    src = inspect.getsource(ibkr.BudgetGatedIBExecClientFactory.create)
    assert "journal=" in src, "the IB factory installs the gate without a journal writer"


def test_a_priceless_SHORT_entry_is_REFUSED_and_an_exit_stays_EXEMPT_by_design():
    """(c) `is_entry` decides, not the side: a SELL that OPENS a short with no price is an entry and
    is refused by name; a SELL that CLOSES a long is an exit and is allowed without pricing at all
    (the documented exemption, pinned deliberately rather than left true by construction)."""
    short_open = _MarketOrder(); short_open.side = types.SimpleNamespace(name="SELL")
    allowed, why, facts = _run(budget_allows(short_open, cache=_Cache(), book_loader=_book(1_000.0), log=_Log()))
    assert allowed is False and "price" in why.lower(), (why, facts)

    class _LongHeld(_Cache):
        def positions_open(self, strategy_id=None, instrument_id=None):
            return [types.SimpleNamespace(signed_qty=10.0, avg_px_open=100.0)]
    exit_sell = _MarketOrder(); exit_sell.side = types.SimpleNamespace(name="SELL")
    allowed, why, facts = _run(budget_allows(exit_sell, cache=_LongHeld(), book_loader=_book(1_000.0), log=_Log()))
    assert allowed is True and facts.get("exempt") == "exit", (why, facts)


# -- e70suark's coverage review (11:23Z) ----------------------------------------------------------------

def test_a_STALE_only_price_REFUSES_by_name_when_a_bound_is_set_and_the_row_carries_the_age():
    """(3b) On IB with no ticks the price is bar-backed and can be days old. With `max_age_ns` the gate
    skips a stale source and refuses by name; without a bound the age still travels in the facts."""
    class _Bar:
        def __init__(self, close, ts): self.close, self.ts_event = close, ts
    class _BarCache(_Cache):
        def bar_types(self, instrument_id=None): return ["GLD.ARCX-1-MINUTE-LAST-EXTERNAL"]
        def bar(self, bt): return _Bar(types.SimpleNamespace(as_double=lambda: 200.0), ts=1_000)
    day = 24 * 3600 * 1_000_000_000
    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_BarCache(), book_loader=_book(1_000.0),
                                             log=_Log(), max_age_ns=60 * 1_000_000_000, now_ns=1_000 + 2 * day))
    assert allowed is False and "price" in why.lower() and "stale" in (why + str(facts)).lower(), (why, facts)
    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_BarCache(), book_loader=_book(1_000.0),
                                             log=_Log(), now_ns=1_000 + 2 * day))
    assert allowed is False and facts["needs"] == 2_000.0 and facts["price_source"].startswith("bar:")
    assert facts["price_age_ns"] == 2 * day, facts


def test_a_LIMIT_or_STOP_order_prices_from_its_OWN_price_deliberately():
    """(3c) A limit far from the market sizes off the limit — the notional the venue can fill. Pinned
    as deliberate, with the source named, so it does not read as unconsidered."""
    limit = _MarketOrder(); limit.price = 300.0
    allowed, why, facts = _run(budget_allows(limit, cache=_Cache(), book_loader=_book(1_000.0), log=_Log()))
    assert allowed is False and facts["needs"] == 3_000.0 and facts["price_source"] == "order.price", (why, facts)
    stop = _MarketOrder(); stop.trigger_price = 150.0
    allowed, why, facts = _run(budget_allows(stop, cache=_Cache(), book_loader=_book(1_000.0), log=_Log()))
    assert allowed is False and facts["needs"] == 1_500.0 and facts["price_source"] == "order.trigger_price"


def test_a_FLIP_that_GROWS_exposure_is_an_ENTRY_and_priceless_is_refused_a_flip_that_SHRINKS_is_exempt():
    """(3d) The predicate is upstream's ONE rule (`budget_gate.is_entry`, kumo-strategies #39, pinned
    by test_budget_gate.py): does ABSOLUTE exposure grow? +5 SELL 8 leaves -3 — smaller — an EXIT,
    exempt, unpriced. +5 SELL 1000 leaves -995 — an ENTRY: priceless → refused by name. The short lane's
    shape (#950, ks#173), pinned on both sides of the rule rather than re-derived here."""
    class _Long5(_Cache):
        def positions_open(self, strategy_id=None, instrument_id=None):
            return [types.SimpleNamespace(signed_qty=5.0, avg_px_open=100.0)]
    shrink = _MarketOrder(qty=8); shrink.side = types.SimpleNamespace(name="SELL")
    allowed, why, facts = _run(budget_allows(shrink, cache=_Long5(), book_loader=_book(1_000.0), log=_Log()))
    assert allowed is True and facts.get("exempt") == "exit", (why, facts)
    grow = _MarketOrder(qty=1_000); grow.side = types.SimpleNamespace(name="SELL")
    allowed, why, facts = _run(budget_allows(grow, cache=_Long5(), book_loader=_book(1_000.0), log=_Log()))
    assert allowed is False and "price" in why.lower() and facts.get("exempt") is None, (why, facts)


def test_the_NUMBER_changes_from_bid_to_the_lanes_sequence_and_the_row_names_the_source():
    """(1) Today's gate took `quote_tick` FIRST and priced a BUY at the BID; the lanes' sequence is
    the same quote-first order (`cache.price(iid)` with one argument raises on Nautilus 1.229 and is
    skipped, on both sides — mirrored, not corrected), so paper's `needs` stays bid-priced, but the
    row now NAMES the source so a reader of the 09-09 refusal and a future line can tell them apart."""
    quote = types.SimpleNamespace(bid_price=49.0, ask_price=51.0, ts_event=5)
    trade = types.SimpleNamespace(price=50.0, ts_event=6)
    class _Both(_Cache):
        def price(self, iid): raise TypeError("price() takes exactly 2 positional arguments")
    allowed, why, facts = _run(budget_allows(_MarketOrder(), cache=_Both(quote=quote, trade=trade),
                                             book_loader=_book(1_000.0), log=_Log(), now_ns=10))
    assert allowed is True and facts["needs"] == 490.0 and facts["price_source"] == "quote_tick", facts
    assert facts["price_age_ns"] == 5
