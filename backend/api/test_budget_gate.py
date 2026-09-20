"""Tests for platform-level budget enforcement (#320).

The rule: over budget refuses ENTRIES and always permits EXITS. The asymmetry is the point — a
strategy over its budget needs to sell, and blocking that would trap it above target permanently.
"""

from __future__ import annotations

import asyncio

import pytest

from api.budget import Sleeve
from api.budget_gate import may_submit, over_budget_strategies
from api.strategy_contract import REQUIRED, RegistrableStrategy, conforms


def _sleeve(target: float, actual: float) -> Sleeve:
    return Sleeve("MOMENTUM-002", target, actual)


def test_over_budget_refuses_an_entry():
    """The live shape: MOMENTUM at 70,363 against a 40,000 target."""
    d = may_submit(_sleeve(40_000, 70_363), is_entry=True, notional=5_000, currently_deployed=70_363)
    assert not d
    assert "over its budget by 30,363" in d.reason
    assert "exits are unaffected" in d.reason, "the reason must say what IS still allowed"


def test_over_budget_still_allows_an_EXIT():
    """The asymmetry. Blocking sells would trap the strategy above target forever — the opposite of
    what the operator asked for when they lowered the number."""
    assert may_submit(_sleeve(40_000, 70_363), is_entry=False, notional=5_000,
                      currently_deployed=70_363)


def test_within_budget_allows_an_entry_that_fits():
    assert may_submit(_sleeve(40_000, 40_000), is_entry=True, notional=5_000,
                      currently_deployed=30_000)


def test_an_entry_larger_than_the_remaining_room_is_refused():
    """Not over budget YET, but this order would take it there. The gate is about the resulting
    position, not the current one."""
    d = may_submit(_sleeve(40_000, 40_000), is_entry=True, notional=15_000, currently_deployed=30_000)
    assert not d
    assert "10,000 of budget left" in d.reason and "15,000" in d.reason


def test_an_entry_that_exactly_fills_the_room_is_allowed():
    """The boundary. Refusing an order that fits exactly would leave the last slice permanently
    unusable and make a fully-funded strategy quietly under-deploy."""
    assert may_submit(_sleeve(40_000, 40_000), is_entry=True, notional=10_000,
                      currently_deployed=30_000)


def test_a_strategy_with_NO_sleeve_is_allowed():
    """A gate whose default is "deny everything" gets switched off wholesale the first time it fires
    wrongly, and then protects nothing.

    This enforces an ALLOCATION. A strategy nobody has allocated to has not breached one, so arriving
    with this feature must not silently stop every strategy that was not yet configured.
    """
    assert may_submit(None, is_entry=True, notional=1_000_000, currently_deployed=0)


def test_deployable_is_bounded_by_actual_so_unfunded_headroom_cannot_be_spent():
    """The transition property, seen through the gate.

    QC345 with a 40,000 target and nothing transferred yet has intent but no capital. Allowing it to
    open against its TARGET would let both sides of a hand-over deploy the same dollars.
    """
    d = may_submit(Sleeve("QC345-003", 40_000, 0.0), is_entry=True, notional=5_000,
                   currently_deployed=0)
    assert not d, "spent capital the donor had not yet freed"


def test_the_operator_surface_names_which_strategies_are_sell_only():
    """A gate that denies silently is a gate nobody can debug."""
    sleeves = {
        "MANUAL-001": Sleeve("MANUAL-001", 0, 8_056),
        "MOMENTUM-002": Sleeve("MOMENTUM-002", 40_000, 70_363),
        "QC345-003": Sleeve("QC345-003", 40_000, 0),
    }
    assert over_budget_strategies(sleeves) == ["MANUAL-001", "MOMENTUM-002"]


# -- the registration contract ---------------------------------------------------------------------


class _Good:
    external_id = "QC345"
    label = "QC345 — monthly top-down rotation"
    id = "QC345-003"
    claimed_instruments: list[str] = []
    warmup_bars = 254

    def is_entry(self, instrument_id, side, quantity):
        return side == "BUY"


class _MissingWarmup:
    """Was `_MissingIsEntry` until 2026-08-22. `is_entry` is no longer required, so a fixture omitting
    it now CONFORMS — and a test asserting non-conformance would have started failing for a reason
    unrelated to what it checks. It checks that a missing member is NAMED, so it needs to omit one that
    is still required."""

    external_id = "X"
    label = "X"
    id = "X-001"
    claimed_instruments: list[str] = []


def test_a_conforming_strategy_passes_and_names_nothing():
    ok, missing = conforms(_Good())
    assert ok and missing == []
    assert isinstance(_Good(), RegistrableStrategy)


def test_a_non_conforming_strategy_NAMES_what_is_missing():
    """A registration failure that says "does not conform" leaves someone diffing two files."""
    ok, missing = conforms(_MissingWarmup())
    assert not ok
    assert missing == ["warmup_bars"]


def test_the_contract_requires_exactly_what_the_platform_cannot_derive():
    """Pinned so the contract does not quietly grow.

    Every member here is something the platform needs and cannot compute for itself. Adding one that it
    COULD derive makes every future strategy implement it — and the temptation is to add
    "check_your_own_budget", which is precisely the thing enforcement exists to avoid depending on.
    """
    # `is_entry` WAS in this set and was removed 2026-08-22 — by this test's own criterion. The
    # platform CAN derive it: exec_client.py:599 sums a strategy- and instrument-scoped net and
    # budget_gate.is_entry delegates to the pure `is_entry_order(net, side, qty)`, which settles it
    # completely including flips. Requiring a per-strategy answer made every lane implement a second
    # derivation of a fact already derived, that nothing called.
    assert set(REQUIRED) == {
        "external_id", "label", "claimed_instruments", "warmup_bars",
    }
    assert "is_entry" not in REQUIRED, (
        "the platform derives entry-ness from its own scoped net; requiring it of a strategy is the "
        "second derivation this test exists to prevent"
    )
    assert "id" not in REQUIRED, (
        "the contract asks for the INTERNAL id — cockpit owns that and must not take it from a strategy"
    )
    assert "budget" not in " ".join(REQUIRED), "the contract asks a strategy to police its own budget"


# -- the entry rule is SHARED, not re-derived (issue 39) --------------------------------


def test_cockpit_and_kumo_strategies_agree_on_what_an_ENTRY_is():
    """Two derivations of one fact disagree — so there is only one, and this proves cockpit uses it.

    The gate refuses entries and permits exits while over budget; the adapters decide what they are
    submitting. A cockpit-local notion of "entry" would drift from theirs and the first symptom would
    be a wind-down silently blocked, or an over-budget strategy quietly opening.
    """
    from kumo_strategies.runtime.nautilus.contract import is_entry_order

    from api.budget_gate import is_entry

    cases = [(0, "BUY", 10), (10, "SELL", 5), (10, "SELL", 10), (10, "SELL", 1000),
             (10, "SELL", 11), (-10, "BUY", 5), (-10, "BUY", 100), (0, "SELL", 7)]
    for net, side, qty in cases:
        assert is_entry(net, side, qty) == is_entry_order(net, side, qty), (
            f"cockpit and kumo-trading-strategies disagree on net={net} {side} {qty}"
        )


def test_a_wind_down_can_never_be_blocked_whatever_the_side():
    """The guarantee the whole asymmetry exists for.

    Reducing or flattening is an EXIT regardless of side — including a BUY closing a short. If any of
    these read as an entry, an over-budget strategy would be trapped above its target permanently,
    which is the opposite of what lowering its number means.
    """
    from api.budget_gate import is_entry

    for net, side, qty in [(10, "SELL", 5), (10, "SELL", 10), (-10, "BUY", 5), (-10, "BUY", 10)]:
        assert not is_entry(net, side, qty), f"a reduction read as an entry: net={net} {side} {qty}"


def test_a_flip_that_GROWS_exposure_is_an_entry():
    """Otherwise "sell 1000 against a 10 long" is an unbounded short dressed as a wind-down."""
    from api.budget_gate import is_entry

    assert is_entry(10, "SELL", 1000)
    assert is_entry(-10, "BUY", 1000)


def test_a_flip_that_SHRINKS_exposure_is_an_exit():
    """Their judgement call, and the right one: +10 SELL 11 leaves -1, strictly smaller than it
    started. Bounded by construction, and calling it an entry would block an order that reduces risk.
    Pinned so the convention is a decision on the record rather than an accident."""
    from api.budget_gate import is_entry

    assert not is_entry(10, "SELL", 11)


# -- the SEAM: the exec client actually refuses (#320) ---------------------------------------------


def _client_with(sleeve, *, net=0.0, deployed=0.0):
    """A double shaped like the exec client's dependencies, bound to the REAL `_budget_allows`."""
    from types import SimpleNamespace

    from api.budget import Book
    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    class _Pos:
        def __init__(self, qty, px):
            self.signed_qty = qty
            self.avg_px_open = px

    class _Cache:
        def positions_open(self, strategy_id=None, instrument_id=None):
            if instrument_id is not None:
                return [_Pos(net, 100.0)] if net else []
            return [_Pos(deployed / 100.0, 100.0)] if deployed else []

        def quote_tick(self, iid):
            return SimpleNamespace(bid_price=100.0)

        def trade_tick(self, iid):
            return None

    class _Fake:
        _cache = _Cache()
        _clock = SimpleNamespace(timestamp_ns=lambda: 0)
        _log = SimpleNamespace(warning=lambda m: None)
        _budget_cache = Book({sleeve.strategy_id: sleeve}) if sleeve else Book({})
        _budget_cache_ns = 0
        _BUDGET_TTL_NS = AlpacaExecutionClient._BUDGET_TTL_NS
        _budget_allows = AlpacaExecutionClient._budget_allows
        _budget_book = AlpacaExecutionClient._budget_book

    return _Fake()


def _order(side="BUY", qty=100, price=100.0, strategy_id="MOMENTUM-002"):
    from types import SimpleNamespace

    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import InstrumentId

    return SimpleNamespace(
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        quantity=qty, price=price, trigger_price=None,
        strategy_id=strategy_id,
        instrument_id=InstrumentId.from_str("AAA.XNAS"),
    )


def test_an_over_budget_ENTRY_is_refused_at_the_submit_path():
    """The live shape: MOMENTUM at 70,363 against a 20,000 target after the BCTROT split."""
    client = _client_with(Sleeve("MOMENTUM-002", 20_000, 70_363), net=0.0, deployed=70_363)
    allowed, why, facts = asyncio.run(client._budget_allows(_order("BUY", 100, 100.0)))
    assert not allowed
    assert "over its budget" in why


def test_an_EXIT_is_allowed_while_over_budget():
    """The asymmetry, at the seam rather than in the pure layer. A wind-down must never be blocked —
    it is the mechanism by which the strategy gets back under its target."""
    client = _client_with(Sleeve("MOMENTUM-002", 20_000, 70_363), net=100.0, deployed=70_363)
    allowed, _, _facts = asyncio.run(client._budget_allows(_order("SELL", 100, 100.0)))
    assert allowed, "a sell that reduces a long was refused during a wind-down"


def test_a_strategy_within_budget_is_untouched():
    client = _client_with(Sleeve("QC345-003", 40_000, 10_000), net=0.0, deployed=5_000)
    allowed, _, _facts = asyncio.run(client._budget_allows(_order("BUY", 10, 100.0, strategy_id="QC345-003")))
    assert allowed


def test_an_unbudgeted_strategy_is_untouched():
    """A gate whose default is deny gets switched off wholesale the first time it fires wrongly."""
    client = _client_with(None)
    allowed, _, _facts = asyncio.run(client._budget_allows(_order("BUY", 1_000_000, 100.0)))
    assert allowed


def test_the_check_FAILS_OPEN_when_the_book_cannot_be_read():
    """A budget is an allocation policy, not a safety interlock — the interlocks are the protective
    stops and the lifecycle. Refusing orders because Postgres hiccuped would stop a strategy trading
    for a reason unrelated to risk, and would do it silently."""
    client = _client_with(Sleeve("MOMENTUM-002", 20_000, 70_363), deployed=70_363)
    client._budget_cache = None
    client._budget_cache_ns = 0
    allowed, _, _facts = asyncio.run(client._budget_allows(_order("BUY", 100, 100.0)))
    assert allowed, "an unreadable sleeve blocked an order"


def test_the_SUBMIT_PATH_consults_the_gate():
    """The call site, not the callee — the fourth time today this gap appeared.

    Every test above calls `_budget_allows` directly, so deleting the two lines that invoke it from
    `_submit_order` left all of them green. A correct gate nothing calls enforces nothing.

    A source assertion because `_submit_order` is an async method on a live client that talks to
    Alpaca, and there is no harness for it here. What is pinned is that the submit path consults the
    gate AND rejects on refusal — a call whose result is discarded would be worse than no call, since
    it would look enforced.
    """
    import inspect

    from api.providers.alpaca.exec_client import AlpacaExecutionClient

    source = inspect.getsource(AlpacaExecutionClient._submit_order)
    assert "_budget_allows" in source, "the submit path never consults the budget gate"
    assert "generate_order_rejected" in source, "the gate is consulted but its refusal is discarded"


def test_the_check_fails_OPEN_when_the_position_lookup_raises():
    """The actual except path, which the earlier fails-open test never reached.

    That one nulled the cache, which returns via the `book is None` branch — a different line. So a
    genuine exception inside the check was untested, and making it fail closed changed nothing. Here
    the cache lookup raises, which is what a real Nautilus hiccup looks like.
    """
    from api.budget import Book

    client = _client_with(Sleeve("MOMENTUM-002", 20_000, 70_363), deployed=70_363)

    class _Exploding:
        def positions_open(self, **kw):
            raise RuntimeError("cache unavailable")

        def quote_tick(self, iid):
            return None

        def trade_tick(self, iid):
            return None

    client._cache = _Exploding()
    client._budget_cache = Book({"MOMENTUM-002": Sleeve("MOMENTUM-002", 20_000, 70_363)})
    allowed, _, _facts = asyncio.run(client._budget_allows(_order("BUY", 100, 100.0)))
    assert allowed, "an exception in the budget check blocked an order"


# ==================================================================================================
# EVERY may_submit CALL SITE MUST MATCH THE SIGNATURE (2026-08-21, found in a live session).
#
# TECHIVOL-005's first ever run logged this ten times, once per intended entry:
#
#     TECHIVOL-005 budget pre-check unavailable
#       (TypeError('may_submit() takes 1 positional argument but 4 were given'))
#       — deferring to the exec client
#
#     strategies/qc27.py:220   may_submit(book, STRATEGY_ID, symbol, notional)   # 4 positional
#     def may_submit(sleeve, *, is_entry, notional, currently_deployed)          # 1 + keyword-only
#
# It passes a BOOK where a SLEEVE is expected, and three positional arguments the signature does not
# accept. The call has never once succeeded, so QC27 has never had a budget pre-check.
#
# It is caught and downgraded to "deferring to the exec client", which is why it blocked nothing and
# why nobody noticed: the gate is enforced again at submit, so the cost is the ATTRIBUTED refusal —
# "over budget", in the session result where an operator reads it — being replaced by a warning nobody
# reads. qc345.py:476 carries a comment about a test that "claimed it fails open ... passing on the
# wrong exception"; this is that shape again, one file over.
#
# Signature-checked rather than text-matched: a scan for the argument names would pass against a call
# that merely spelled them right in the wrong positions.
# ==================================================================================================
def test_the_signature_really_does_reject_the_call_qc27_makes():
    """The fixture's own property first. If `may_submit` ever grew positional parameters this test
    would silently stop describing anything, so prove the rejection is real before asserting nobody
    triggers it."""
    from api.budget_gate import may_submit

    with pytest.raises(TypeError, match="positional"):
        may_submit(None, "STRATEGY", "SYM", 1000.0)          # exactly qc27.py:220's shape


def test_no_call_site_passes_may_submit_more_than_one_positional_argument():
    """AIMED AT THE CLASS. Two of three callers were right and one was wrong for the life of the lane;
    the question is which OTHER caller drifts next."""
    import ast
    import inspect
    import pathlib

    from api import budget_gate

    root = pathlib.Path(inspect.getfile(budget_gate)).parent.parent
    offenders = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name != "may_submit":
                continue
            if len(node.args) > 1:
                offenders.append(f"{path.name}:{node.lineno} passes {len(node.args)} positional args")
    # The fixture's own property: there must BE call sites, or an empty result proves nothing.
    assert any("may_submit" in p.read_text(errors="ignore")
               for p in root.rglob("*.py") if ".venv" not in p.parts), "no call sites found at all"
    assert not offenders, (
        "may_submit takes ONE positional argument and the rest keyword-only; these callers cannot "
        f"succeed and their pre-check is dead: {offenders}"
    )


def test_the_REFUSAL_CARRIES_ITS_DERIVATION_to_the_caller():
    """The seam that was open, measured live at 23:40:01 SGT on 2026-08-31.

    `GateDecision.inputs` was added earlier that evening with seven tests and a mutation pass — and
    was consumed by NOTHING. The exec client logged only `decision.reason`, so the live refusal still
    read:

        Budget refused kumo-f57d0b2539c0d488b649: MOMENTUM-002 has 0 of budget left and this order
        needs 1,824

    and answering "is the lane overtrading, or holding positions the broker does not have?" still
    took a position query, a venue reconstruction and arithmetic by hand — the exact thing the field
    was added to end. A field nobody reads is the anchors-on-something-absent shape, committed inside
    the change meant to close it.
    """
    client = _client_with(Sleeve("MOMENTUM-002", 20_000, 20_000), net=0.0, deployed=19_000)
    allowed, why, facts = asyncio.run(client._budget_allows(_order("BUY", 1_000_000, 100.0)))
    assert not allowed
    assert facts, "the refusal reached the caller with no derivation attached"
    for k in ("target", "deployed", "room", "needs"):
        assert k in facts, f"{k} missing — this is a number the caller would otherwise recompute"


def test_a_FAIL_OPEN_path_returns_the_same_arity():
    """The three fail-open branches return `(True, "", {})`. A branch returning a 2-tuple raises
    `ValueError: too many values to unpack` INSIDE the submit path — turning a defensive
    "let the order through" into a crash on the one path that exists to never block an order."""
    client = _client_with(None)          # no sleeve -> the unbudgeted fail-open branch
    out = asyncio.run(client._budget_allows(_order("BUY", 100, 100.0)))
    assert len(out) == 3 and out[0] is True


def test_the_LOG_LINE_ITSELF_carries_the_derivation():
    """Pinned at the LOG STATEMENT, not just at the gate.

    Measured as a mutation survivor: deleting the `| {facts}` suffix from the warning left every
    other test in this file green, because they all assert on the returned tuple. The live line is
    what an operator reads at 23:40, and it was the only thing standing between "0 of budget left"
    and an hour of reconstructing the book by hand.

    A source assertion deliberately: driving the real submit path needs a venue client, and the claim
    here is narrow — that the facts reach the emitted string.
    """
    import ast
    import inspect
    import textwrap

    from api.providers.alpaca import exec_client as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.AlpacaExecutionClient._submit_order)))
    # ON THE LOG CALL'S ARGUMENT, not on the function source. The first version asserted `"facts" in
    # src` and could not discriminate: `facts` also appears in the tuple unpacking two lines above,
    # so deleting it from the message left the test green. It measured its neighbour.
    logged = [
        ast.unparse(n.args[0])
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "warning"
        and n.args
        and "Budget refused" in ast.unparse(n.args[0])
    ]
    assert logged, "the refusal is no longer logged at all"
    assert "facts" in logged[0], (
        f"the refusal log line dropped its derivation — back to a verdict with no inputs, which is "
        f"the state that cost an evening on 2026-08-31. Emitted: {logged[0][:120]}"
    )
