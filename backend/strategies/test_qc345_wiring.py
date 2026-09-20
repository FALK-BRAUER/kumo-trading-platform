"""QC345 registers ONLY through the safe path (#324).

The hazard this file exists for is not hypothetical and does not announce itself. `QC345RotationStrategy`
has a `session_runner` parameter that defaults to `None`, and `_try_decide` branches on it:

    if self._runner is not None:  self._run_session(...)   # cockpit's gateway: lifecycle, journal, budget
    else:                         self._decide_for(...)    # submits live orders, right there

Both branches produce a strategy that registers cleanly, logs that it is watching, and looks healthy.
They differ only on the next monthly rebalance, when one of them places orders nobody authorised.

So the tests below aim at the CLASS of mistake rather than at one instance of it: no construction of
this adapter anywhere in cockpit may omit the runner, and node registration may not name the adapter
directly. Both are asserted across the source, the way `mobileWidth.test.ts` pins "no unscoped sticky
offset ANYWHERE" rather than "OrdersTile is fixed".

Run with: PYTHONPATH=~/projects/kumo-trading-strategies/src:.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from strategies import qc345

_BACKEND = Path(__file__).resolve().parents[1]


def _code_of(fn) -> str:
    """A function's executable body — docstring and comments removed, via `ast.unparse`.

    Every check below asks what the code DOES. These modules deliberately explain the unsafe
    alternatives in prose so the next reader is warned, which means a check over raw source is
    SATISFIED by an explanation and BROKEN by rewording one — it would be measuring the comments.
    Unparsing from the AST is the only version of this that cannot be fooled either way.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _adapter_calls(path: Path) -> list[str]:
    """Every real `QC345RotationStrategy(...)` CALL in a file, as `file:line`. AST, never text."""
    import ast

    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "QC345RotationStrategy":
                out.append(f"{path.name}:{node.lineno}")
    return out


def _adapter_calls_missing_runner() -> list[str]:
    """Adapter constructions anywhere in the backend that do NOT pass `session_runner`."""
    import ast

    offenders = []
    for path in _BACKEND.rglob("*.py"):
        if path.name.startswith("test_") or ".venv" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "QC345RotationStrategy":
                continue
            if not any(kw.arg == "session_runner" for kw in node.keywords):
                offenders.append(f"{path.relative_to(_BACKEND)}:{node.lineno}")
    return offenders


# --------------------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------------------


def test_qc345_is_OFF_unless_the_gate_is_explicitly_set(monkeypatch):
    monkeypatch.setattr(qc345, "_enabled", lambda: False)
    assert qc345.build_qc345_strategy() is None, "an unset gate must build nothing at all"


def test_the_gate_comes_from_SETTINGS_not_from_the_ENVIRONMENT():
    """Whether a strategy registers is an OPERATOR decision, not a deployment.

    As an env var, flipping it meant editing compose and restarting the stack — for a choice with a
    form field two clicks away. Environment is for what must be known BEFORE settings can load: the
    database URL, Redis, the settings directory. This is not that, and `build_qc345_strategy`
    already resolves the `qc345` domain, so settings are reachable at exactly this point.
    """
    body = _code_of(qc345._enabled)
    assert "settings.resolve('strategies')" in body
    assert "environ" not in body, (
        "the gate still reads the environment — a flag readable from two places is two derivations "
        "of one fact, and the one that loses is the one somebody edited"
    )


def test_the_gate_DEFAULTS_OFF_in_the_schema():
    """CLAUDE.md: all new automation gates default False."""
    import json

    schema = json.loads((_BACKEND / "config/settings/strategies.schema.json").read_text())
    for key in ("QC345_ENABLED", "QC345_UNIVERSE_REFRESH"):
        assert schema["properties"][key]["type"] == "boolean"
        assert schema["properties"][key]["default"] is False, f"{key} defaults ON"


def test_enabling_WITHOUT_a_universe_fails_LOUDLY(monkeypatch):
    """The acceptance criterion. An operator who sets the flag and gets a clean-booting node that
    never trades has been told nothing; the flag is set, the log is quiet, and the strategy is a
    ghost. QC345 ranks cross-sectionally, so an empty universe cannot even produce a hold."""
    monkeypatch.setattr(qc345, "_enabled", lambda: True)
    monkeypatch.setattr(qc345, "_universe_symbols", list)

    with pytest.raises(RuntimeError) as err:
        qc345.build_qc345_strategy()
    msg = str(err.value)
    assert "QC345_UNIVERSE" in msg, "the error does not name the setting an operator must fix"
    assert "refusing" in msg.lower()


def test_the_universe_comes_from_SETTINGS_and_is_not_the_whole_market():
    """`TradableUniverse().symbols()` is every tradable US equity — thousands. Subscribing a
    TradingNode to that is not a universe, it is an outage. The derivation is genuinely unbuilt, so
    the operator supplies the list and the code says so rather than substituting something close."""
    body = _code_of(qc345._universe_symbols)
    assert "settings.resolve('strategies')" in body and "QC345_UNIVERSE" in body
    # The DOCSTRING explains why TradableUniverse is wrong here, so the check must read the CODE.
    # Asserting over the whole source would be satisfied — and tripped — by prose.
    assert "TradableUniverse" not in body, (
        "the universe is being derived from the tradable-asset list — that is the whole market, and "
        "the pre-selection bars that would narrow it are not fetched"
    )


def test_the_universe_setting_EXISTS_in_the_schema_an_operator_edits():
    """A builder reading a key no schema declares renders no field: the strategy can be enabled and
    can never be given a universe. That is the #324 failure mode one layer up."""
    import json

    schema = json.loads((_BACKEND / "config/settings/strategies.schema.json").read_text())
    assert "QC345_UNIVERSE" in schema["properties"], "nothing renders a field for the universe"
    assert schema["properties"]["QC345_UNIVERSE"]["default"] == []


# --------------------------------------------------------------------------------------------------
# The safe path — the reason this module exists
# --------------------------------------------------------------------------------------------------


def test_NO_construction_of_the_adapter_anywhere_omits_the_session_runner():
    """Aimed at the class, not the instance.

    `session_runner=None` is the adapter's DEFAULT, so omitting it is silent — no error, no warning,
    a strategy that registers and looks identical right up until it submits an unsupervised order.
    Asserting only that `build_qc345_strategy` passes it would leave the next call site free.
    """
    # AST, not a regex over the text. Both this module and `engine_node` DESCRIBE the unsafe
    # construction in prose in order to warn about it, and a textual scan cannot tell an explanation
    # apart from the thing it explains — it would report the warning as the violation and, worse,
    # could be silenced by rewording a comment.
    offenders = _adapter_calls_missing_runner()
    assert not offenders, (
        f"QC345RotationStrategy constructed without session_runner at {offenders} — that strategy "
        "decides and submits inside _decide_for, bypassing lifecycle, journal, risk and budget"
    )


def test_the_builder_passes_the_COCKPIT_gateway_not_some_other_runner():
    src = inspect.getsource(qc345.build_qc345_strategy)
    assert "session_runner=gateway" in src
    assert "QC345SessionGateway(" in src
    assert "PgSessionRunner" not in src, (
        "the momentum runner takes a MomentumRotationConfig and resolves the BCT symbol pool — it "
        "would not fail on QC345's panel, it would rank the wrong universe with the wrong config"
    )


def test_the_node_registers_qc345_ONLY_through_the_builder():
    src = (_BACKEND / "api/engine_node.py").read_text()
    assert "build_qc345_strategy" in src, "QC345 is never registered"
    assert not _adapter_calls(_BACKEND / "api/engine_node.py"), (
        "engine_node CONSTRUCTS the adapter directly — the one construction that bypasses the gateway"
    )
    # Wrapped by `build_optional_strategy` (#377) so a transport failure resolving QC345's universe
    # costs QC345 and not the whole node — it took MANUAL/MOMENTUM/BCTROT down with it on 2026-08-19.
    # Anchored on the assignment, which survives the wrapper, rather than on the bare call.
    block = src[src.index("qc345 = build_optional_strategy("):]
    assert "node.trader.add_strategy(qc345)" in block
    assert "feed.register_strategy(qc345.id, qc345)" in block, (
        "unregistered with the feed, QC345's positions project as UNCLAIMED broker activity rather "
        "than as its own managed cycles — and without the INSTANCE (#374) an operator flatten of a "
        "QC345 position has nothing to route the close to and is refused"
    )


def test_MOMENTUM_and_BCTROT_registration_is_UNCHANGED():
    """QC345 is additive. Its own block must not reorder, gate or wrap the existing two."""
    src = (_BACKEND / "api/engine_node.py").read_text()
    for expected in (
        # `feed=feed` added by #358: the exit path needs UiFeedStrategy's release, and `broker.strategy`
        # is the ROTATION strategy, so the feed is threaded in explicitly at construction. Updated rather
        # than loosened — this test's job is that MOMENTUM/BCTROT registration is not disturbed by QC345
        # work, and pinning the literal is how it does that.
        #
        # The second argument added by #374: registering the ID gives a strategy its cycle projection,
        # registering the INSTANCE is what lets an operator flatten it. Under NETTING only the owning
        # strategy may close its own position, so without the object the flatten has nowhere to route and
        # falls back to refusing — which is what left the operator unable to exit any MOMENTUM position.
        "bctrot = build_bctrot_strategy(feed=feed)",
        "momentum = build_momentum_strategy(feed=feed)",
        "node.trader.add_strategy(momentum)",
        "node.trader.add_strategy(bctrot)",
        "feed.register_strategy(bctrot.id, bctrot)",
        "feed.register_strategy(momentum.id, momentum)",
    ):
        assert expected in src, f"existing registration changed: {expected!r} is gone"
    # And QC345 comes AFTER them: a failure in the new strategy must not leave the proven two
    # unregistered on a node that otherwise booted.
    #
    # Anchored on the CALL, not the name. `build_qc345_strategy` is also mentioned in a comment
    # elsewhere in the file explaining where the universe refresh is armed and why — and a substring
    # index found that comment first and reported the order as wrong. Third time this file has had to
    # stop measuring prose.
    assert src.index("qc345 = build_optional_strategy(") > src.index("feed.register_strategy(momentum.id, momentum)")


def test_qc345_carries_COCKPITS_identity_and_tag():
    assert qc345.STRATEGY_ID == "QC345-003"
    assert qc345.ORDER_ID_TAG == "003"
    src = inspect.getsource(qc345.build_qc345_strategy)
    assert "order_id_tag=ORDER_ID_TAG" in src, (
        "the tag is left to the adapter's default — it happens to be 003 today, and a default that "
        "silently matches is not an allocation. BCTROT's 003 -> 004 -> 003 churn came from exactly "
        "this, reading a tag off code cockpit does not own"
    )


def test_qc345_claims_NO_instruments():
    """`external_order_claims` are EXCLUSIVE node-wide and raise InvalidConfiguration at
    `Trader.add_strategy`. QC345 shares its universe with MOMENTUM and BCTROT by construction, so any
    overlap does not degrade — the node does not boot, taking the UI feed down with it."""
    assert "external_order_claims=None" in inspect.getsource(qc345.build_qc345_strategy)


# --------------------------------------------------------------------------------------------------
# Lifecycle semantics, exercised through the REAL gateway method
# --------------------------------------------------------------------------------------------------


class _EmptyLedger:
    """The claims ledger as production's PgJournal exposes it — `journal.sessionmaker()` yielding a
    session whose SELECT returns rows. Empty here: this lane claims nothing yet, which is exactly
    the state the #540 back-fill exists for. A journal double WITHOUT this made every session log a
    back-fill failure — the double failing to represent production, not a defect."""

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        def begin(self):
            # `store.write_claim` opens `async with session.begin()`. Without this the back-fill's
            # every write raised AttributeError — swallowed and logged until #848 named it — so
            # every session here silently adopted nothing while the tests read clean.
            return self

        async def execute(self, *_a, **_k):
            class _R:
                @staticmethod
                def all(): return []
            return _R()

    def __call__(self):
        return self._S()


class _Journal:
    def __init__(self, already=False):
        self.already = already
        self.writes: list[tuple] = []
        self.sessionmaker = _EmptyLedger()

    async def decided_this_session(self, session, slot=None):
        return self.already

    async def write(self, kind, summary, *, session, detail=None, symbol=None, correlation=None,
                    slot=None):
        self.writes.append((kind, session, slot, detail))
        return 1


class _Broker:
    def __init__(self, positions=None, price=10.0):
        self._pos = positions or {}
        self._price = price
        # ONE ORDERED STREAM, PLUS ROUTING TAGS. `submitted` stays "everything that reached the
        # venue, in order", because that is what `test_TRADING_submits_and_EXITS_GO_FIRST` reads to
        # pin sell-before-buy -- splitting exits into a second list would have made a property that
        # still holds unobservable, which is a worse outcome than the bug. `via_submit`/`exited`
        # record WHICH call carried each one, which is the #459 question.
        self.submitted: list = []
        self.via_submit: list = []
        self.exited: list = []

    def positions(self):
        """THE ACCOUNT BOOK. Production's NautilusBroker has this and the claims back-fill (#540)
        anchors on it — a double without it makes every session journal a back-fill failure, which
        is the double failing to represent production rather than a defect."""
        return dict(self._pos)

    def strategy_positions(self):
        return dict(self._pos)

    def position_entries(self):
        return {s: self._price for s in self._pos}

    def last_price(self, symbol):
        return self._price

    def equity(self):
        return 100_000.0

    async def exit(self, req):
        """THE REAL BROKER HAS THIS AND THIS DOUBLE DID NOT (#459).

        `NautilusBroker.exit(req)` calls `feed.release_for_exit(...)` to cancel the resting protective
        stop BEFORE selling. `submit()` releases nothing, so a SELL routed through it reaches Alpaca
        while the stop still holds every share -- `qty_available = 0` on every held symbol -- and comes
        back `403 insufficient qty available (available: 0)`.

        IT IS ALSO A COROUTINE WHILE `submit` IS NOT -- production is asymmetric here, and a
        synchronous `exit` on this double is what let a missing `await` pass every test in this file.
        The call returned a coroutine object, `getattr(res, "ok", False)` was False, and QC345 would
        have reported an ordinary refusal while releasing nothing and sending nothing. Codex caught it;
        the suite could not, because the double disagreed with production about the one thing that
        mattered. `test_the_double_matches_the_real_brokers_async_contract` now pins it.

        A double carrying only `submit` cannot tell those two paths apart, which is how QC345's exit
        path stayed broken under a green suite. The dryrun harness recorded the same lesson in the same
        week: "no exit() method -> AttributeError the moment real holdings produced a real exit -- the
        ROTATION path, the one that matters most."
        """
        self.submitted.append(req)
        self.exited.append(req)
        return SimpleNamespace(ok=True, reason=None)

    def submit(self, req):
        self.submitted.append(req)
        self.via_submit.append(req)
        return SimpleNamespace(ok=True, reason=None)


def _gateway(state, *, journal=None, broker=None, decision=None, terminal=None,
             monkeypatch=None):
    """The REAL `QC345SessionGateway`, with only the two things it cannot have in a unit test
    replaced: the Postgres lifecycle read and the pure decision. Everything between them — the order
    of refusals, the SHADOW branch, the last-look recheck, the submit loop — is production code."""
    from kumo_strategies.runtime.executor.lifecycle import Lifecycle

    gw = qc345.QC345SessionGateway(
        sm=None, journal=journal or _Journal(),
        cfg=SimpleNamespace(portfolio_size=5),
        broker=broker or _Broker(), limits=SimpleNamespace(
            max_position_notional=20_000.0, max_deployed_frac=0.80),
    )
    gw._lifecycle = lambda: _async(Lifecycle(state))
    if decision is not None:
        # THREE values since terminal handling landed. The double follows production rather than
        # production being loosened to fit it — a two-tuple here would make every caller pass while
        # the real `_decide` returned something the gateway could not unpack.
        gw._decide = lambda panel, held: (decision, 50, dict(terminal or {}))
    gw._budget_allows = lambda symbol, notional: _async((True, ""))
    return gw


async def _async(value):
    return value


def _store(row):
    """A sessionmaker double for `_lifecycle`, which is the only thing under test here.

    It must be able to represent BOTH outcomes production has — a row and no row — because the whole
    point is which one yields which state. A double that could only answer "no row" would make the
    absent-row assertion pass and say nothing about whether an explicit row still wins.
    """
    class _Result:
        def scalars(self):
            return self

        def first(self):
            return row

    class _Session:
        async def execute(self, _stmt):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return lambda: _Session()


def _decision(enter=(), exit_=(), hold=()):
    return SimpleNamespace(enter=tuple(enter), exit=tuple(exit_), hold=tuple(hold), scores={})


def _run(gw, panel=None, session="2026-09-01"):
    return asyncio.run(gw.run(panel, session))


def test_an_ABSENT_lifecycle_row_is_TRADING(monkeypatch):
    """An absent row means TRADING (2026-08-19). INVERTED from the original rule; read why.

    The old rule was "an absent row is DISABLED — never a default that submits", on the reasoning
    that a strategy nobody has enabled has not been trusted. the operator's objection, and it is correct:
    STRATEGIES DO NOT APPEAR RANDOMLY IN THE DB. One exists only because somebody wrote it, wired it
    into `engine_node.build_node`, and shipped a deploy. The lifecycle row was a second gate on an act
    that was already deliberate, and its practical effect was that every genuinely-wanted strategy sat
    inert until someone hand-wrote a row in psql — which is how BCTROT-004 and QC345-003 spent days
    registered, RUNNING, and submitting nothing.

    What the original comment worried about is still covered, by the row rather than by the fallback:
    a restart cannot silently resume TRADING, because a strategy that has ever been set to HALTED or
    DISABLED HAS a row, and an explicit row always wins over this default.

    What this DOES accept, stated plainly so nobody has to rediscover it: a brand-new strategy trades
    on its first session, and a lifecycle table that is empty — a fresh database, a restore that lost
    rows — reads as "everything trades" rather than "nothing trades".
    """
    from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State

    # The absent-row branch is `Lifecycle(State.TRADING, ...)`, NOT the bare `Lifecycle()`, whose own
    # default stays DISABLED — that constructor is kumo-trading-strategies' and is not ours to redefine.
    assert Lifecycle().state is State.DISABLED, "kumo-trading-strategies' own default changed under us"

    # Drive the REAL `_lifecycle` on BOTH gateways. `_gateway()` above stubs `_lifecycle` wholesale, so
    # it cannot reach this branch at all — an assertion built on it would have passed against the old
    # DISABLED default and pinned nothing.
    from strategies.momentum import SessionGateway

    for cls in (SessionGateway, qc345.QC345SessionGateway):
        gw = cls.__new__(cls)
        gw._strategy_id = "NEW-999"

        # ABSENT ROW -> TRADING.
        gw._sm = _store(None)
        assert asyncio.run(cls._lifecycle(gw)).state is State.TRADING, (
            f"{cls.__name__}: a strategy with no lifecycle row must trade")

        # A PRESENT ROW STILL WINS. This is the half the operator's argument rests on: the restart worry is
        # covered by the row, not by the fallback, so anything ever halted stays halted. If this
        # regressed, the inversion would have turned HALT into a suggestion.
        gw._sm = _store(SimpleNamespace(state="HALTED", reason="operator: stand down"))
        life = asyncio.run(cls._lifecycle(gw))
        assert life.state is State.HALTED, f"{cls.__name__}: an explicit row must beat the default"
        assert life.reason == "operator: stand down"

    journal = _Journal()
    gw = _gateway(State.DISABLED, journal=journal, decision=_decision(enter=["AAA"]))
    res = _run(gw)
    assert res.decided is False and "DISABLED" in res.blocked
    assert journal.writes == [], "a DISABLED strategy journalled a decision it must not have made"


@pytest.mark.parametrize("state_name", ["DISABLED", "WARMUP", "HALTED"])
def test_states_that_may_not_decide_do_not_decide(state_name):
    from kumo_strategies.runtime.executor.lifecycle import State

    gw = _gateway(State[state_name], decision=_decision(enter=["AAA"]))
    assert _run(gw).decided is False


def test_SHADOW_decides_journals_and_submits_NOTHING():
    """SHADOW is the dry run that earns trust on live data — the full path, no orders. A SHADOW that
    submitted would be indistinguishable from TRADING until the fills arrived."""
    from kumo_strategies.runtime.executor.lifecycle import State

    journal, broker = _Journal(), _Broker(positions={"OLD": 10})
    gw = _gateway(State.SHADOW, journal=journal, broker=broker,
                  decision=_decision(enter=["AAA"], exit_=["OLD"]))
    res = _run(gw)

    assert res.decided is True, "SHADOW must still decide — that is the entire point of it"
    assert res.submitted == 0 and broker.submitted == [], "SHADOW submitted an order"
    # "pool" first: OLD is held and unclaimed, so the #540 back-fill adopts it and says so. Until
    # #848 this row never appeared — the ledger double had no `begin()`, the write raised, and the
    # swallow read as "nothing to adopt". The decision row is the assertion; the pool row is what
    # production does on this book.
    assert [w[0] for w in journal.writes] == ["pool", "decision"], "SHADOW did not journal its decision"
    # The BRANCH's own signature, not just its effect. Deleting the SHADOW return leaves SHADOW still
    # orderless — the last-look recheck catches it on the way to submitting — so asserting only
    # "no orders" passed with the branch removed. Defence in depth is why production is safe; it is
    # also why a test that stops at the outcome cannot tell which guard held.
    assert res.blocked == "shadow", (
        f"SHADOW did not take the shadow branch (blocked={res.blocked!r}) — it reached the submit "
        "path and was stopped further down, which is luck rather than design"
    )


def test_TRADING_submits_and_EXITS_GO_FIRST():
    """Ordering is a risk decision, not style. Selling first frees cash AND Alpaca's share
    reservation before anything asks for them, so a rotation cannot half-apply into a shortfall —
    still holding what it meant to sell and unable to buy what it meant to enter."""
    from kumo_strategies.runtime.executor.lifecycle import State

    broker = _Broker(positions={"OLD": 10})
    gw = _gateway(State.TRADING, broker=broker, decision=_decision(enter=["AAA"], exit_=["OLD"]))
    res = _run(gw)

    sides = [(r.side, r.symbol) for r in broker.submitted]
    assert sides == [("SELL", "OLD"), ("BUY", "AAA")], f"wrong order or missing leg: {sides}"
    assert res.submitted == 2


def test_LIQUIDATING_FLATTENS_EVERYTHING_including_names_the_model_wants_to_KEEP():
    """LIQUIDATING is an instruction, not an input to the model.

    The first version of this test put the held name in `decision.exit` and asserted only that no
    BUY went out. It passed with the bug present: the gateway ran `decide()` and submitted
    `decision.exit`, so a held name the model still wanted sat in `decision.hold`, never reached the
    exit list, and survived the wind-down — while the session reported success. Found by codex on
    conformance against `PgSessionRunner`, which flattens `held_qty` outright (pgrunner.py:510).

    So the fixture is now adversarial: the model wants to HOLD both names and enter a third. A
    wind-down must sell both anyway.
    """
    from kumo_strategies.runtime.executor.lifecycle import State

    broker = _Broker(positions={"KEEP": 10, "ALSO": 5})
    gw = _gateway(State.LIQUIDATING, broker=broker,
                  decision=_decision(enter=["AAA"], exit_=[], hold=["KEEP", "ALSO"]))
    res = _run(gw)

    sides = sorted((r.side, r.symbol) for r in broker.submitted)
    assert sides == [("SELL", "ALSO"), ("SELL", "KEEP")], (
        f"a wind-down did not flatten the book: {sides}. Holding what the model likes is exactly "
        "what LIQUIDATING is not"
    )
    assert res.submitted == 2


def test_an_OPERATOR_HALT_DURING_the_session_stops_the_orders():
    """The state is read at the top and deciding takes real time. Without the last look, an operator
    who hits HALT mid-session watches it submit anyway — the bug that made `_save_if_unchanged`
    necessary on the momentum side, in its other half."""
    from kumo_strategies.runtime.executor.lifecycle import Lifecycle, State

    broker = _Broker(positions={"OLD": 10})
    gw = _gateway(State.TRADING, broker=broker, decision=_decision(enter=["AAA"], exit_=["OLD"]))
    gw._lifecycle = lambda: _async(Lifecycle(State.TRADING))
    gw._current_state = lambda: _async(State.HALTED)     # the operator, mid-session

    res = _run(gw)
    assert broker.submitted == [], "orders went out after an operator halt"
    assert "HALTED" in (res.blocked or "")


def test_a_SLOT_THAT_ALREADY_DECIDED_does_not_decide_again():
    """Idempotency. A restart mid-session must not submit the book twice."""
    from kumo_strategies.runtime.executor.lifecycle import State

    broker = _Broker()
    gw = _gateway(State.TRADING, journal=_Journal(already=True), broker=broker,
                  decision=_decision(enter=["AAA"]))
    res = _run(gw)
    assert res.decided is False and "already decided" in res.blocked
    assert broker.submitted == []


def test_the_DECISION_IS_JOURNALLED_BEFORE_any_order():
    """A crash between the two must leave evidence we intended to trade. The reverse order leaves
    orders nobody can explain, and makes the unique index an audit note rather than a guarantee."""
    src = inspect.getsource(qc345.QC345SessionGateway.run)
    assert src.index('self._journal.write(\n                "decision"') < src.index("self._submit(")


def test_the_BUDGET_check_uses_the_SAME_predicate_as_the_exec_client():
    """Two derivations of one limit disagree. The exec client refuses at submit; this refuses early
    so the reason reaches the operator — but both must be `budget_gate.may_submit`, or a strategy
    can pass one and fail the other and look broken instead of over budget."""
    gate_src = inspect.getsource(qc345.QC345SessionGateway._budget_allows)
    assert "from api.budget_gate import may_submit" in gate_src
    assert "may_submit(" in gate_src

    # THE ANCHOR USED TO BE A SUBSTRING THAT LIED. This asserted `"budget_gate" in exec_src`, and the
    # only such substring in the Alpaca client was a dead `from api.budget_gate import is_entry` inside
    # the inline ownership method — not the budget path at all. #1030 deleted that method and the test
    # went red on a client whose budget enforcement had not moved. The client delegates to
    # `api.budget_guard.budget_allows` (#782); THAT is the backstop, and it must bottom out in
    # `may_submit` — assert the chain, not a word.
    exec_src = (_BACKEND / "api/providers/alpaca/exec_client.py").read_text()
    assert "from api.budget_guard import budget_allows" in exec_src, (
        "the exec client no longer delegates to the shared budget rule (#782)")
    guard_src = (_BACKEND / "api/budget_guard.py").read_text()
    assert "from api.budget_gate import may_submit" in guard_src and "may_submit(" in guard_src, (
        "budget_guard no longer bottoms out in budget_gate.may_submit — the two predicates can drift")


def test_the_budget_pre_check_CALLS_load_book_WITH_A_SESSION():
    """The check that would have caught a dead gate.

    `load_book(session)` takes a DB session (budget_store.py:66). The first version called it bare,
    so every invocation raised TypeError into the fails-open except — the budget was never consulted
    and every order was permitted. The fails-open TEST passed on that TypeError, which is why the
    defect survived: the test and the bug were the same event.

    Asserting the CALL SHAPE is what distinguishes "deferred because the store was unreachable" from
    "deferred because we never asked".
    """
    body = _code_of(qc345.QC345SessionGateway._budget_allows)
    assert "load_book(db)" in body, (
        "load_book is not passed a session — the pre-check raises TypeError every time and silently "
        "permits every order"
    )
    assert "session_factory()" in body, "no session is opened for the budget read"


def test_the_budget_pre_check_FAILS_OPEN_when_the_STORE_is_unreachable(monkeypatch):
    """Matching the exec client. The gate is enforced again at submit, so a failure here costs a
    clearer message, not a control — failing closed would let a Postgres blip silently stop a funded
    strategy for a month.

    Exercised by breaking the STORE, not by breaking the call. A test that fails open because the
    code is malformed proves nothing about what happens when Postgres is down.
    """
    import api.db

    def _boom():
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(api.db, "session_factory", _boom)
    gw = qc345.QC345SessionGateway(sm=None, journal=_Journal(), cfg=SimpleNamespace(portfolio_size=5),
                                   broker=_Broker(), limits=SimpleNamespace(
                                       max_position_notional=20_000.0, max_deployed_frac=0.80))
    allowed, _why = asyncio.run(gw._budget_allows("AAA", 5_000.0))
    assert allowed is True, "an unreachable budget store blocked a trade instead of deferring"


def test_a_DECISION_FAILURE_journals_and_submits_nothing():
    from kumo_strategies.runtime.executor.lifecycle import State

    journal, broker = _Journal(), _Broker()
    gw = _gateway(State.TRADING, journal=journal, broker=broker)

    def _boom(panel, held):
        raise ValueError("no rankable rows")

    gw._decide = _boom
    res = _run(gw)
    assert res.decided is False and "ValueError" in res.blocked
    assert broker.submitted == []
    # A "state" row now PRECEDES the error: the daily-loss stop journals that it is NOT ARMED before
    # the decision runs (#548/#784). That row is the point — "had no reason to halt" and "has no
    # ability to halt" must not be the same empty journal. The error still follows; nothing is sent.
    assert [w[0] for w in journal.writes] == ["state", "error"]


def test_both_gateways_honour_the_SAME_lifecycle_flags():
    """Cockpit now has two session gateways. They are separate code paths on purpose — QC345's
    decision is not momentum's — but the lifecycle rule is ONE rule, and two implementations of one
    rule drift. Both must read the same store and branch on the same `State` properties."""
    from strategies.momentum import SessionGateway

    for gw in (SessionGateway, qc345.QC345SessionGateway):
        src = inspect.getsource(gw._lifecycle)
        assert "StrategyState" in src, f"{gw.__name__} does not read the shared lifecycle store"
        # INVERTED 2026-08-19. This asserted `"Lifecycle()" in src` — the bare constructor, i.e. absent
        # row means DISABLED. It is kept as a guard rather than deleted, because the property that
        # matters is unchanged: the two gateways must not disagree about what an absent row means. Only
        # the answer moved.
        assert "State.TRADING" in src, f"{gw.__name__} does not treat an absent row as TRADING"
        assert "else Lifecycle()" not in src, (
            f"{gw.__name__} still falls back to the bare Lifecycle() — that is DISABLED")

    qc_run = inspect.getsource(qc345.QC345SessionGateway.run)
    assert "state.decides" in qc_run
    assert "may_submit_exits" in qc_run
    assert "may_submit_entries" in inspect.getsource(qc345.QC345SessionGateway._submit)


@pytest.mark.parametrize(
    "state_name, wants_flat",
    [("LIQUIDATING", True), ("TRADING", False), ("SHADOW", False)],
)
def test_the_two_gateways_agree_on_what_each_state_MEANS_not_just_on_its_name(state_name, wants_flat):
    """Token presence was not enough, and this is the test that says so.

    The previous version asserted that both gateways mention `State` and treat an absent row as
    DISABLED. Both did — while disagreeing completely about LIQUIDATING: Pg flattens the book, QC345
    submitted only the model's exits. Two implementations of one rule drift, and a check that reads
    identifiers cannot see it.

    So this asserts BEHAVIOUR: in LIQUIDATING the book goes flat regardless of what the model wants.
    """
    from kumo_strategies.runtime.executor.lifecycle import State

    broker = _Broker(positions={"KEEP": 10})
    gw = _gateway(State[state_name], broker=broker,
                  decision=_decision(enter=[], exit_=[], hold=["KEEP"]))
    _run(gw)

    sold = [r.symbol for r in broker.submitted if r.side == "SELL"]
    assert (sold == ["KEEP"]) is wants_flat, (
        f"{state_name}: sold={sold}, expected flat={wants_flat} — the two gateways disagree on what "
        "this state instructs"
    )


# --------------------------------------------------------------------------------------------------
# The ETF/fund exclusion, which a missing column silently disables
# --------------------------------------------------------------------------------------------------


def test_the_assets_frame_carries_NAMES_so_the_fund_filter_can_run():
    """`is_fundamental_like_asset` matches markers in the NAME — " ETF", "SPDR", "ISHARES". With the
    column absent every fund in the universe ranks as a stock, and QC345 stops being a stock-selection
    strategy while still producing a number.

    Since #647 the names come from the CACHE (`Instrument.info` via the venue-neutral `asset_meta`
    accessor), not from an Alpaca HTTP call — the functional coverage lives in
    test_qc345_assets_from_cache.py. This pins the wiring facts that survive: the builder populates
    a real `name` column and no vendor URL is anywhere near it.
    """
    body = _code_of(qc345._assets_frame_from_cache)
    assert "asset_meta" in body, (
        "the assets frame no longer reads the venue-neutral accessor — where do names come from?"
    )
    assert "'name': meta['name']" in body or '"name": meta["name"]' in body, (
        "the assets frame does not populate a name column — the fund filter cannot match anything"
    )
    assert "/v2/assets" not in body and "urllib" not in body, (
        "the cache-derived frame grew an HTTP call back — that is #647 returning"
    )


def test_the_fund_markers_match_what_ALPACA_ACTUALLY_RETURNS():
    """The predicate returns True for KEEP — it answers "is this a real company", not "is this a fund".

    Names here are verbatim from Alpaca's live `/v2/assets` payload on 2026-08-17: the double is built
    from what production emits, not from what the marker list hopes for. Written the other way round
    the first time, and the real strings are what caught it.
    """
    from kumo_strategies.strategies.qc345_rotation.engine import is_fundamental_like_asset

    # Excluded by NAME, on venues the ARCA rule never sees. These are the ones that matter.
    assert is_fundamental_like_asset("Invesco QQQ Trust, Series 1", "NASDAQ") is False
    assert is_fundamental_like_asset("Tradr 2X Long AAOI Daily ETF", "BATS") is False
    # Excluded by VENUE, before the name is even read.
    assert is_fundamental_like_asset("State Street SPDR S&P 500 ETF Trust", "ARCA") is False
    # Kept.
    assert is_fundamental_like_asset("Apple Inc. Common Stock", "NASDAQ") is True
    assert is_fundamental_like_asset("NVIDIA Corporation Common Stock", "NASDAQ") is True


def test_the_name_column_removes_funds_the_VENUE_RULE_CANNOT():
    """Why the name column is worth an HTTP call, as a number rather than an assertion of principle.

    Measured against the full live asset list on 2026-08-17 — 13,371 tradable US equities:

        kept with names       7,227
        kept without names   10,664
        difference            3,437   funds ONLY the name marker catches

    `is_fundamental_like_asset` short-circuits on ARCA, so a naive reading says the venue rule already
    handles ETFs. It does not: every BATS- and NASDAQ-listed fund survives it, and that set includes
    LEVERAGED SINGLE-STOCK products — "Tradr 2X Long AAOI Daily ETF", "Direxion Daily AAPL Bear 1X".
    A momentum ranking that admits a 2x daily-reset AAPL ETF is not selecting stocks; it is buying
    the most convex thing available and calling the result a stock strategy.

    Pinned as a RATIO on real names rather than as the live count, which drifts daily.
    """
    from kumo_strategies.strategies.qc345_rotation.engine import is_fundamental_like_asset

    sample = [
        # (name, exchange) — verbatim Alpaca rows
        ("Invesco QQQ Trust, Series 1", "NASDAQ"),
        ("Tradr 2X Long AAOI Daily ETF", "BATS"),
        ("GraniteShares ETF Trust GraniteShares 2x Long AAPL Daily ETF", "NASDAQ"),
        ("Direxion Shares ETF Trust Direxion Daily AAPL Bear 1X ETF", "NASDAQ"),
        ("Apple Inc. Common Stock", "NASDAQ"),
        ("NVIDIA Corporation Common Stock", "NASDAQ"),
    ]
    with_names = [n for n, x in sample if is_fundamental_like_asset(n, x)]
    without_names = [n for n, x in sample if is_fundamental_like_asset(None, x)]

    # THE FIXTURE'S OWN PROPERTY FIRST: these six must not all agree, or the ratio below is a
    # statement about a sample that could not discriminate.
    assert 0 < len(with_names) < len(sample), (
        "every row in the sample got the same verdict — this fixture cannot show what the name "
        "column does either way")
    assert with_names == ["Apple Inc. Common Stock", "NVIDIA Corporation Common Stock"]
    assert len(sample) - len(with_names) == 4, (
        "the name column rejected a different number of leveraged/fund products than the four this "
        "sample was built from — re-read the sample before trusting the rule")

    # A MISSING NAME EXCLUDES. This asserted `len(without_names) == 6` until 2026-08-25 — the OLD,
    # PERMISSIVE behaviour, where a missing name fell through to the venue rule and kept everything.
    # Upstream closed that: `_name_text(None) is None -> return False`, because coercing a missing
    # name to "" contains none of the rejection markers and falls through to `return True`, ADMITTING
    # an instrument whose name we do not have. Strictly safer, and the direction we would have asked
    # for.
    #
    # It surfaced only because the fixture's own guard refused to assert the ratio once the sample
    # stopped demonstrating it — and that only ran after the backend venv was aligned to the DEPLOYED
    # strategies pin. The local venv had an older revision, so this had been asserting the behaviour
    # of code production does not run.
    assert without_names == [], (
        "a missing name no longer excludes. An empty name contains none of the rejection markers, so "
        "it falls through every check and ADMITS an instrument whose name we do not have — a "
        "leveraged ETF into a fundamental strategy's universe")

    # THE OPERATIONAL EDGE THIS BUYS, stated so nobody re-opens the permissive path to "fix" it: if
    # Alpaca's name column is unavailable for EVERY symbol the universe goes empty and QC345 trades
    # nothing, which is the correct failure. Silence is not the risk here — the missing name is
    # logged as an ERROR, pinned by the test below.


# `test_a_missing_name_does_NOT_stop_the_strategy_but_is_logged_as_an_ERROR` moved: since #647 the
# metadata comes from the cache and the ERROR-level states (unclassifiable symbols named; no-metadata
# degrade to venue-only; delisting detection OFF without a reference ledger) are pinned functionally
# in test_qc345_assets_from_cache.py, driving the real `_decide` seam.


# --------------------------------------------------------------------------------------------------
# Interruption between the journal write and the orders (codex, Critical)
# --------------------------------------------------------------------------------------------------


class _ResumeJournal(_Journal):
    """Decided already, and can say what was decided — the state a cancelled session leaves behind."""

    def __init__(self, detail):
        super().__init__(already=True)
        self._detail = detail

    async def explain(self, session):
        return {"session": session, "kind": "decision", "detail": self._detail}


def test_an_INTERRUPTED_session_RESUMES_instead_of_skipping_the_month():
    """`on_stop` cancels the session task (qc345_rotation.py:205), and the window between the journal
    write and the submit loop is real — a pause, a redeploy or a restart lands there.

    Returning on `decided_this_session` made that permanent: the idempotency key blocks the retry, so
    a session that decided and never traded silently skips an entire MONTHLY rebalance. Replaying is
    safe because `client_order_id` is a hash of (strategy, session, symbol, side), so anything that
    did go out is denied as a duplicate rather than doubling the position.
    """
    from kumo_strategies.runtime.executor.lifecycle import State

    broker = _Broker(positions={"OLD": 10})
    journal = _ResumeJournal({"enter": ["AAA"], "exit": ["OLD"], "hold": ["AAA"]})
    gw = _gateway(State.TRADING, journal=journal, broker=broker)

    res = _run(gw)
    sides = [(r.side, r.symbol) for r in broker.submitted]
    assert sides == [("SELL", "OLD"), ("BUY", "AAA")], (
        f"the interrupted session was not finished: {sides} — that book skips a monthly rebalance"
    )
    assert "resumed" in (res.blocked or "")


def test_a_RESUME_replays_the_JOURNALLED_decision_not_a_fresh_one():
    """Recomputing would be a DIFFERENT decision — prices have moved — so the orders going out would
    not be the ones the journal says were authorised. That correspondence is the only thing the
    journal is for."""
    from kumo_strategies.runtime.executor.lifecycle import State

    broker = _Broker(positions={"OLD": 10})
    journal = _ResumeJournal({"enter": ["FROM_JOURNAL"], "exit": ["OLD"], "hold": []})
    gw = _gateway(State.TRADING, journal=journal, broker=broker,
                  decision=_decision(enter=["FRESHLY_COMPUTED"], exit_=[]))

    def _must_not_run(panel, held):
        raise AssertionError("a resume recomputed the decision instead of replaying it")

    gw._decide = _must_not_run
    _run(gw)
    assert [r.symbol for r in broker.submitted if r.side == "BUY"] == ["FROM_JOURNAL"]


def test_a_resume_with_NOTHING_READABLE_says_so_rather_than_reporting_success():
    """Decided, unreplayable. The one case where a human has to look, so it must not be reported as
    a quiet no-op alongside the ordinary "already decided"."""
    from kumo_strategies.runtime.executor.lifecycle import State

    gw = _gateway(State.TRADING, journal=_ResumeJournal({}), broker=_Broker())
    res = _run(gw)
    assert res.decided is False and "cannot be replayed" in res.blocked


def test_a_SWALLOWED_journal_failure_blocks_the_orders():
    """`PgJournal.write` catches non-integrity failures and returns None (pgjournal.py:57), so "no
    exception raised" is not "durably recorded". Submitting on that would place orders with no
    decision row — invisible to `_resume`, to `explain()`, and to anyone asking why they exist."""
    from kumo_strategies.runtime.executor.lifecycle import State

    class _Swallowing(_Journal):
        async def write(self, kind, summary, **kw):
            self.writes.append((kind, kw.get("session"), kw.get("slot"), kw.get("detail")))
            # EXPLICIT, and noqa'd rather than removed: an implicit None is byte-identical but
            # says nothing, and what this double exists to represent is precisely that
            # `PgJournal.write` RETURNS None on failure instead of raising.
            return None  # noqa: RET501, PLR1711

    broker = _Broker(positions={"OLD": 10})
    gw = _gateway(State.TRADING, journal=_Swallowing(), broker=broker,
                  decision=_decision(enter=["AAA"], exit_=["OLD"]))
    res = _run(gw)

    assert broker.submitted == [], "orders went out with no durable decision row"
    assert "not durably journalled" in (res.blocked or "")


def test_a_REFUSED_order_reports_the_brokers_OWN_reason():
    """`OrderResult` is (ok, order_id, detail, request) — broker.py:43. Reading `.reason` returned
    the fallback every time, discarding the one thing that says why: "not a subscribed instrument",
    "no instrument definition cached", the venue's rejection text."""
    from kumo_strategies.runtime.executor.lifecycle import State

    class _Refusing(_Broker):
        # REFUSES ON EVERY ROUTE. A venue that will not take the order does not care which method
        # carried it, and this double overrode only `submit` -- so when exits moved to `exit()` for
        # #459 the refusal silently stopped happening and `res.blocked` came back None. A double that
        # refuses on one path and accepts on another cannot represent any real broker.
        def _refuse(self, req):
            self.submitted.append(req)
            return SimpleNamespace(ok=False, order_id=None,
                                   detail="no instrument definition cached for OLD", request=req)

        def submit(self, req):
            self.via_submit.append(req)
            return self._refuse(req)

        async def exit(self, req):
            self.exited.append(req)
            return self._refuse(req)

    gw = _gateway(State.TRADING, broker=_Refusing(positions={"OLD": 10}),
                  decision=_decision(exit_=["OLD"]))
    res = _run(gw)
    assert "no instrument definition cached" in res.blocked, (
        f"the broker's reason was discarded: {res.blocked!r}"
    )


# --------------------------------------------------------------------------------------------------
# Delisted holdings (codex handback item 5)
# --------------------------------------------------------------------------------------------------


def test_a_TERMINAL_holding_is_REPORTED_and_not_sold_into_a_dead_tape():
    """A name that has stopped printing cannot be exited by a market order — there is nothing to
    hit. Submitting one produces a rejection, and a rejection in the log is indistinguishable from
    an exit refused for an ordinary reason, which is how a stuck position stops being visible.

    So it is surfaced as a risk row and skipped, not attempted."""
    from kumo_strategies.runtime.executor.lifecycle import State

    journal, broker = _Journal(), _Broker(positions={"DEAD": 10, "LIVE": 5})
    gw = _gateway(State.TRADING, journal=journal, broker=broker,
                  decision=_decision(exit_=["DEAD", "LIVE"]),
                  terminal={"DEAD": "bankruptcy"})
    res = _run(gw)

    sold = [r.symbol for r in broker.submitted if r.side == "SELL"]
    assert sold == ["LIVE"], f"a sell was sent into a delisted name: {sold}"
    assert "terminal (bankruptcy)" in (res.blocked or "")
    assert any(w[0] == "risk" for w in journal.writes), (
        "a terminal holding was skipped without a risk row — the position is now invisible"
    )


def test_terminal_state_comes_from_terminal_symbols_NOT_from_scanner_membership():
    """Two different facts, and conflating them breaks both ways.

    A name can leave the monthly universe while trading perfectly well — exiting on that alone turns
    universe churn into forced selling, which upstream fixed deliberately
    (`test_decide_does_not_sell_a_held_name_solely_because_the_source_drops_it`). And a name can
    DELIST while still sitting in a carried-forward universe, so membership says nothing about
    whether it can be sold.
    """
    body = _code_of(qc345.QC345SessionGateway._decide)
    assert "terminal_symbols" in body, "delisting is not read from the strategy's own classifier"
    assert "eligible" in body, "the fixture is wrong — the universe lookup is gone entirely"
    # And the two are separate calls on separate lines: a terminal set DERIVED from `eligible` would
    # be membership inference wearing the right name.
    assert "terminal_symbols(session_date, held)" in body


def test_the_terminal_set_is_JOURNALLED_even_when_EMPTY():
    """"Nothing had delisted" and "we never looked" must not read the same in the record."""
    from kumo_strategies.runtime.executor.lifecycle import State

    journal = _Journal()
    gw = _gateway(State.TRADING, journal=journal, broker=_Broker(),
                  decision=_decision(enter=[]), terminal={})
    _run(gw)
    decisions = [w for w in journal.writes if w[0] == "decision"]
    assert decisions, "no decision row"
    assert "terminal" in (decisions[0][3] or {}), "the decision does not record the terminal set"


def test_the_assets_frame_carries_STATUS_or_delisting_handling_is_DEAD():
    """`terminal_buckets` returns EVERY name as "active" when `status` is missing (engine.py:147).
    A frame without it does not fail — it reports that nothing has ever delisted, so
    `terminal_symbols` answers {} forever and the whole path above is wired, tested and inert.

    Since #647 the status comes from the exec provider's DECLARED reference ledger
    (`_reference_status`), not an Alpaca URL in the lane. The functional pins live in
    test_qc345_assets_from_cache.py ("inactive" travels into the frame; no ledger -> no fabricated
    column + delisting-detection-OFF at ERROR) and api/providers/alpaca/test_reference_assets.py
    (the ledger fetch filters neither status nor tradable). This keeps the wiring fact: the builder
    threads the ledger into the frame at all."""
    body = _code_of(qc345.build_qc345_strategy)
    assert "_reference_status" in body, (
        "the builder no longer reads the reference ledger — the status column is dead and "
        "terminal classification silently returns active"
    )
    assert "status_by_symbol=status_by_symbol" in body, (
        "the ledger's answer is read and then not handed to the frame builder — declared, "
        "documented, consulted by nothing (#574 shape)"
    )


# --------------------------------------------------------------------------------------------------
# The one that crash-looped the engine
# --------------------------------------------------------------------------------------------------


def test_the_adapter_source_is_CONSTRUCTIBLE_under_the_PROMOTED_config():
    """This raised in production and restarted the node in a loop the moment QC345 was enabled.

    `_adapter_source` passed `assets=None` on the reasoning that the live path never reads it — the
    gateway computes the universe per session. But `QC345ComputedSource.__init__` calls
    `filter_asset_universe` EAGERLY, which raises `ValueError: assets are required for
    asset_universe_mode='fundamental_like'`. The object could not be constructed at all.

    The test that would have caught it has to use the PROMOTED config, because that is where
    `asset_universe_mode="fundamental_like"` comes from. Every existing test built a config through
    a fixture, and a test that constructs its own config is testing its own config.
    """
    from kumo_strategies.strategies.qc345_rotation import QC345RotationConfig

    # The PROMOTED pair, both fields — `fundamental_like` is what needs assets, and `close` is what
    # we actually run (#319). Asserted rather than assumed, because a fixture that drifts off the
    # promoted config stops testing the thing that broke.
    promoted = QC345RotationConfig(asset_universe_mode="fundamental_like",
                                   momentum_price_field="close", corporate_action_window=252)
    assert promoted.asset_universe_mode == "fundamental_like", "the fixture cannot reach the failure"

    # And the DATACLASS default names a different price column, which the empty frame must also
    # carry — a constructor that works for one setting and not the other is a landmine.
    default_field = QC345RotationConfig().momentum_price_field
    assert default_field != "close", "the two configs no longer differ — this check proves nothing"
    assert qc345._adapter_source(QC345RotationConfig(momentum_price_field=default_field)) is not None

    # No assets — the exact production call at build time when reference data is unavailable.
    src = qc345._adapter_source(promoted)
    assert src is not None

    # And with assets, the normal path.
    import pandas as pd

    assets = pd.DataFrame([{"symbol": "AAPL", "name": "Apple Inc. Common Stock",
                            "exchange": "NASDAQ", "status": "active"}])
    assert qc345._adapter_source(promoted, assets) is not None


def test_the_BUILDER_survives_the_promoted_config_end_to_end(monkeypatch):
    """The SEAM, not the unit. `_adapter_source` being constructible says nothing about whether
    `build_qc345_strategy` completes — and it was the builder that raised inside `build_node`,
    which is what took the node down rather than one strategy."""
    reached = {}

    real = qc345._adapter_source          # bound BEFORE patching, or the spy calls itself

    def _spy(cfg, assets=None):
        reached["cfg_mode"] = cfg.asset_universe_mode
        return real(cfg, assets)

    monkeypatch.setattr(qc345, "_adapter_source", _spy)
    monkeypatch.setattr(qc345, "_enabled", lambda: True)
    monkeypatch.setattr(qc345, "_universe_symbols", lambda: ["AAPL"])
    # No feed is passed, so `_reference_status(None)` logs delisting-detection-OFF and the builder
    # proceeds — the exact no-ledger venue state. Nothing to stub: the build makes no HTTP call
    # for assets any more (#647).
    # The builder opens a cross-loop Postgres engine before it reaches the source. Stubbed, because
    # the property under test is "the source is constructible inside the builder", not "Postgres is
    # up" — and a test that needs a database is a test nobody runs.
    import asyncio as _aio

    # RETURNS A 2-TUPLE, because `_prepare` now returns `(sessionmaker, held_claims)` — the builder
    # reads the symbols it HOLDS so a position outside the universe can still be SOLD
    # (see `_held_claims`). A stub returning a bare object unpacks into a TypeError before the source
    # is ever reached, and this test would then "fail" for a reason unrelated to what it asserts.
    monkeypatch.setattr(_aio, "run", lambda coro: (coro.close(), (object(), set()))[1])
    # Everything past the source that needs a database or a venue.
    # `_lane_symbols` runs BEFORE the source, so stopping there would prove nothing. The sentinel
    # goes on the ADAPTER CONSTRUCTOR — the last step, reached only if the source was built.
    from kumo_strategies.runtime.nautilus import qc345_rotation as A

    import strategies.momentum as M

    monkeypatch.setattr(M, "_lane_symbols", lambda syms: list(syms))

    def _stop(**kw):
        raise _Reached("reached the adapter — the source was built without raising")

    monkeypatch.setattr(A, "QC345RotationStrategy", _stop)

    # The builder is stopped by WHATEVER comes after the source — the adapter sentinel, or the
    # calendar refusing to run holiday-unaware without credentials. Either is fine; the property
    # under test is that the source was CONSTRUCTED, and asserting a specific downstream failure
    # would make this test brittle about steps it does not care about.
    with pytest.raises(Exception):  # noqa: B017 — see above
        qc345.build_qc345_strategy()

    assert reached, (
        "the builder never reached _adapter_source — it raised earlier, so this proves nothing "
        "about the crash-loop"
    )
    assert reached["cfg_mode"] == "fundamental_like", (
        f"the builder used {reached['cfg_mode']!r}, not the promoted config — the failure needs "
        "fundamental_like to be reachable"
    )


class _Reached(RuntimeError):
    """Sentinel: the builder got past the step under test."""


def _iid(symbol: str):
    from nautilus_trader.model.identifiers import InstrumentId

    return InstrumentId.from_str(f"{symbol}.XNAS")


def test_EVERY_gateway_accepts_the_SessionRunner_protocol_signature():
    """AIMED AT THE CLASS. QC345-003 fired exactly on time on 2026-08-24 and died on the call:

        TypeError: QC345SessionGateway.run() got an unexpected keyword argument 'slot'
        File ".../runtime/nautilus/qc345_rotation.py", line 381, in _session_coro

    `qc345_rotation._session_coro` calls `self._runner.run(panel, day, slot=self._slot_name)`.
    momentum's gateway and `QC27SessionRunner` both take `slot`; this one did not, so every session
    since the adapter started passing it has died before deciding. `last_decision` was 2026-08-21.

    kumo-trading-strategies already wrote this contract down after the SAME failure on 2026-08-17
    (`qc27_runner.py:104`): "`jobs` and `slot` are accepted by EVERY runner even where unused — the
    SessionRunner protocol." Second occurrence, different gateway — so the assertion is over EVERY
    gateway cockpit builds, by signature, not over the one that just broke.

    A per-lane test here would have been written for qc345, fixed qc345, and left the next gateway
    free to do it again.
    """
    import inspect

    from kumo_strategies.runtime.executor.qc27_runner import QC27SessionRunner

    from strategies import momentum, qc345

    gateways = [
        ("momentum.SessionGateway", momentum.SessionGateway),
        ("qc345.QC345SessionGateway", qc345.QC345SessionGateway),
        ("qc27_runner.QC27SessionRunner", QC27SessionRunner),
    ]
    missing = []
    for name, cls in gateways:
        params = inspect.signature(cls.run).parameters
        for kw in ("jobs", "slot"):
            if kw not in params:
                missing.append(f"{name}.run() has no `{kw}`")
            elif params[kw].default is inspect.Parameter.empty:
                missing.append(f"{name}.run()'s `{kw}` has no default — callers that omit it break")
    assert not missing, (
        "these cannot be driven by the SessionRunner protocol; an adapter passing the kwarg kills "
        "the session with TypeError BEFORE it decides:\n  " + "\n  ".join(missing))


# -- QC345 must journal what it sends (#513) ---------------------------------------------------------
#
# `_submit` called `self._broker.submit(...)` directly and wrote NO journal row for orders, ever.
# Every other lane journals ORDER intent BEFORE the broker sees it (`qc27_runner._send`, pgrunner).
#
# Live evidence, session 2026-08-21:
#
#     BCTROT-004    decision 1 | order 3
#     MOMENTUM-002  decision 1 | order 6
#     TECHIVOL-005  decision 1 | order 8 | error 8
#     QC345-003     decision 1 | (nothing else)
#
# QC345 had decided `enter ['AMAT','DELL','INTC','LRCX','MRVL']`.
#
# So `/slots` reports `DECIDED, NEVER ATTEMPTED — int 0 land 0 fail 0` for this lane STRUCTURALLY: the
# verdict counts ORDER rows and QC345 writes none. It says "never attempted" no matter what happened,
# and that verdict was quoted as evidence of a trading failure on 2026-08-24 when it was measuring
# nothing. Success and silence are indistinguishable for this lane.
#
# Refusals ARE journalled (qc345.py:314) — that landed after the 08-21 incident. A SUCCESS still wrote
# nothing, which is the half this closes.


def _intent_rows(journal):
    return [w for w in journal.writes if (w[3] or {}).get("phase") == "intent"]


def _trading_gw(journal, broker, **dec):
    from kumo_strategies.runtime.executor.lifecycle import State

    return _gateway(State.TRADING, journal=journal, broker=broker, decision=_decision(**dec))


def test_an_entry_journals_its_INTENT_before_the_broker_sees_it():
    """FAIL CLOSED is the point of ordering it this way: if the intent write fails we have not traded,
    and `_resume` — which decides what was attempted from these records — would otherwise re-send."""
    j, brk = _Journal(), _Broker(price=10.0)
    _run(_trading_gw(j, brk, enter=["AAA"]))
    assert brk.submitted, "the fixture never submitted, so this asserts nothing"
    assert _intent_rows(j), "an order was sent with no journal row — invisible to /slots and to _resume"


def test_an_EXIT_journals_its_intent_too():
    """Exits are the half that strands capital; a sell nobody can see is worse than a buy nobody can
    see."""
    j, brk = _Journal(), _Broker(positions={"BBB": 5}, price=10.0)
    _run(_trading_gw(j, brk, exit_=["BBB"]))
    assert brk.submitted, "the fixture never submitted an exit"
    assert _intent_rows(j), "an exit was sent with no journal row"


def test_an_order_is_NOT_sent_when_its_intent_cannot_be_journalled():
    """`PgJournal.write` SWALLOWS non-integrity failures and returns None, so "no exception" is not
    "durably recorded" — the same reason the decision row is checked for `None` at qc345.py:281.
    Sending anyway places an order that no record explains."""
    class _Deaf(_Journal):
        async def write(self, kind, summary, *, session, detail=None, symbol=None,
                        correlation=None, slot=None):
            self.writes.append((kind, session, slot, detail))
            return None if (detail or {}).get("phase") == "intent" else 1

    j, brk = _Deaf(), _Broker(price=10.0)
    _run(_trading_gw(j, brk, enter=["AAA"]))
    assert _intent_rows(j), "the fixture did not even attempt an intent row"
    assert brk.submitted == [], "an order was sent although its intent was not durably journalled"


def test_a_journal_that_RAISES_on_the_refusal_row_does_not_abort_the_session():
    """THE SECOND WRITE MUST BE GUARDED TOO, and it was not.

    `_intent` wraps its intent write, but the FAILURE path then wrote an `error` row unguarded:

        if wrote is None:
            await self._journal.write("error", ...)   # <- raised straight out
            return False

    `_submit` is awaited unguarded at qc345.py:351, so that exception leaves `run()` mid-loop. EXITS
    RUN FIRST — deliberately, so a rotation cannot be half-applied into a cash shortfall — which means
    the sells are already away when the entries are abandoned. A half-executed rotation is the precise
    outcome the ordering exists to prevent, reached through the error handler.

    Reachable in exactly the case that matters: `PgJournal.write` SWALLOWS non-integrity failures and
    returns None, so a genuinely unavailable store gives None on the first write and an exception on
    the second.
    """
    class _Hostile(_Journal):
        async def write(self, kind, summary, *, session, detail=None, symbol=None,
                        correlation=None, slot=None):
            self.writes.append((kind, session, slot, detail))
            if (detail or {}).get("phase") == "intent":
                return None                        # swallowed failure, as PgJournal does
            if kind == "error":
                raise RuntimeError("journal store unavailable")
            return 1

    j, brk = _Hostile(), _Broker(positions={"BBB": 5}, price=10.0)
    gw = _trading_gw(j, brk, enter=["AAA"], exit_=["BBB"])
    result = _run(gw)                              # must not raise
    assert brk.submitted == [], "an order was sent although its intent was not journalled"
    assert result is not None


# ---------------------------------------------------------------------------------------------
# Two holes codex found in the #513 fix (2026-08-25). Both are places where "every order journals
# its intent, and one failure refuses one symbol" was asserted and was not true.
# ---------------------------------------------------------------------------------------------


def test_a_raise_in_the_ENTRIES_phase_does_not_abandon_the_rotation_after_the_SELLS_ARE_AWAY():
    """THE WORST SHAPE IN THIS FILE. `_submit` sends every EXIT before it sends any ENTRY.

    `run()` awaits `_submit` unguarded (qc345.py:351), and inside the entries loop
    `last_price`, `_equity_per_position`, `_budget_allows` and `broker.submit` are all called with
    no per-symbol guard. So a transient failure on ONE entry — a broker hiccup, a missing price
    raising rather than returning None — propagates out of `run()` with the sells already at the
    venue and the buys never attempted.

    The strategy would have LIQUIDATED ITS BOOK and not rebought, from an error that refused nothing
    and reported nothing. Every other failure mode in this loop appends to `refusals` and continues;
    this one does not, and that asymmetry is the defect.

    Same family as the PEAK trim path on 2026-08-12, which stripped protection off five live
    positions because a partial sequence was allowed to half-execute.
    """
    class _HalfBroker(_Broker):
        def last_price(self, symbol):
            if symbol == "BOOM":
                raise RuntimeError("transient quote failure")
            return self._price

    j, brk = _Journal(), _HalfBroker(positions={"OLD": 7}, price=10.0)
    gw = _trading_gw(j, brk, exit_=["OLD"], enter=["AAA", "BOOM", "ZZZ"])

    res = _run(gw)

    sells = [o for o in brk.submitted if getattr(o, "side", None) == "SELL"]
    assert sells, "the fixture never sent the exit, so this test cannot see the hazard it is about"

    buys = [o for o in brk.submitted if getattr(o, "side", None) == "BUY"]
    assert buys, (
        "the sells are away and NOT ONE buy was attempted — a raise inside the entries loop escaped "
        "`_submit`, so a transient failure on one name flattened the book and abandoned the "
        "rotation. One bad symbol must refuse ONE symbol"
    )
    assert {getattr(o, "symbol", None) for o in buys} == {"AAA", "ZZZ"}, (
        "the healthy entries either side of the failing one must still be attempted"
    )
    assert res is not None and any("BOOM" in r for r in (res.blocked or "").split(";")), (
        "the failing symbol was swallowed silently — it must appear in refusals with its reason, "
        "the way every other refusal in this loop does"
    )


def test_the_LIQUIDATING_path_journals_an_ORDER_INTENT_for_every_sell_it_sends():
    """`_liquidate` bypassed `_intent` entirely — the #513 defect, still live on one path.

    `run()` routes LIQUIDATING straight to `_liquidate` (qc345.py:267-273), which writes ONE decision
    row naming the symbols and then submits SELLs directly (qc345.py:537). So the wind-down path —
    the one flattening everything the strategy owns — had no per-order record at all, which is
    exactly what #513 was filed about.

    A decision row naming five symbols is not a substitute: it says what was INTENDED, not what was
    SENT, so a submit that never happened and one that was refused look identical afterwards. That
    is the distinction `/slots` and `_resume` are built on.
    """
    from kumo_strategies.runtime.executor.lifecycle import State

    j, brk = _Journal(), _Broker(positions={"AAA": 3, "BBB": 4}, price=10.0)
    gw = _gateway(State.LIQUIDATING, journal=j, broker=brk, decision=_decision())

    _run(gw)

    assert brk.submitted, "the fixture liquidated nothing, so this asserts nothing"
    assert len(brk.submitted) == 2, "expected one sell per held name"

    rows = _intent_rows(j)
    assert rows, (
        "LIQUIDATING sent sells with no ORDER intent row — /slots and `_resume` are blind on the "
        "wind-down path for exactly the reason #513 was filed"
    )
    assert len(rows) == len(brk.submitted), (
        f"{len(brk.submitted)} orders went out and {len(rows)} intents were journalled — every "
        f"order needs its own record, not one row for the batch"
    )


# ---------------------------------------------------------------------------------------------
# #540 — QC345 must CLAIM what it holds, or the cross-strategy ceiling silently skips.
# ---------------------------------------------------------------------------------------------


def _claim_calls(monkeypatch):
    """Capture what the LANE writes to the claims ledger.

    Patched on `strategies.qc345`'s own import site, not on the store, so this proves THE GATEWAY
    calls it. Patching `record_claim` itself would prove the function works and say nothing about
    whether anything calls it — which is the entire defect (#540) and the same asymmetry as the
    `sid` NameError: the callable being correct says nothing about the call.
    """
    recorded: list = []
    dropped: list = []

    async def _rec(journal, strategy_id, symbol, qty, entry_px):
        recorded.append((strategy_id, symbol, qty, entry_px))

    async def _drop(journal, strategy_id, symbol):
        dropped.append((strategy_id, symbol))

    from kumo_strategies.runtime.executor import store
    monkeypatch.setattr(store, "record_claim", _rec, raising=False)
    monkeypatch.setattr(store, "drop_claim", _drop, raising=False)
    return recorded, dropped


def test_an_accepted_ENTRY_records_a_claim(monkeypatch):
    """THE DEFECT. QC345 has never written a claim row, so `_foreign_claims` returns 0 for its
    symbols and `own_ceiling(acct, mine, 0)` collapses to `mine` — the attribution term vanishes
    exactly when another lane also holds the name. DELL is QC345 7 + TECHIVOL 2 today.
    """
    recorded, _dropped = _claim_calls(monkeypatch)
    j, brk = _Journal(), _Broker(price=10.0)

    _run(_trading_gw(j, brk, enter=["AAA"]))

    assert brk.submitted, "the fixture never submitted, so this asserts nothing"
    assert recorded, "an accepted BUY wrote no claim — the lane still holds shares it does not claim"
    sid, sym, qty, entry = recorded[0]
    assert sym == "AAA"
    assert qty > 0, "a claim of 0 tells _foreign_claims the lane owns it while claiming nothing"
    assert entry == 10.0, f"entry must be the REAL fill price, got {entry!r}"


def test_an_accepted_EXIT_releases_the_claim(monkeypatch):
    """QC345's exits are FULL — `qty = abs(positions.get(symbol))` — so the claim goes, not shrinks.

    A lane still claiming a symbol it has sold narrows every other lane's ceiling forever, which is
    the over-claim direction: safe for us, wrong for them, and invisible.
    """
    _recorded, dropped = _claim_calls(monkeypatch)
    j, brk = _Journal(), _Broker(positions={"BBB": 5}, price=10.0)

    _run(_trading_gw(j, brk, exit_=["BBB"]))

    assert brk.submitted, "the fixture never sent an exit"
    assert ("QC345-003", "BBB") in dropped, f"the exit released no claim: {dropped}"


def test_the_LIQUIDATING_path_releases_its_claims_too(monkeypatch):
    """The wind-down path bypassed `_intent` once already (#513). It must not bypass this."""
    from kumo_strategies.runtime.executor.lifecycle import State

    _recorded, dropped = _claim_calls(monkeypatch)
    j, brk = _Journal(), _Broker(positions={"AAA": 3, "BBB": 4}, price=10.0)

    _run(_gateway(State.LIQUIDATING, journal=j, broker=brk, decision=_decision()))

    assert len(brk.submitted) == 2, "the fixture liquidated nothing"
    assert {s for _sid, s in dropped} == {"AAA", "BBB"}, (
        f"liquidation left claims behind: {dropped}. A flattened lane still claiming its old book "
        f"narrows every other lane's ceiling for as long as the rows survive")


def test_a_REFUSED_order_claims_NOTHING(monkeypatch):
    """Claim on ACCEPT, matching pgrunner's `if r.ok` — but a REFUSAL is not an accept.

    Claiming a rejected order would narrow other lanes against shares nobody holds.
    """
    class _Refusing(_Broker):
        def submit(self, req):
            self.submitted.append(req)
            return SimpleNamespace(ok=False, detail="refused by the venue")

    recorded, dropped = _claim_calls(monkeypatch)
    j, brk = _Journal(), _Refusing(price=10.0)

    _run(_trading_gw(j, brk, enter=["AAA"]))

    assert brk.submitted, "the fixture never reached the broker"
    assert not recorded and not dropped, f"a refused order touched the ledger: {recorded} {dropped}"


def test_a_LEDGER_FAILURE_does_not_stop_the_session(monkeypatch):
    """The ledger is bookkeeping; the order is already at the venue. #377: never take the lane down.

    Losing a claim row is bad — it under-claims, which lets other lanes size into what we hold — but
    raising here would abandon a rotation mid-flight with sells already away, which is worse.
    """
    async def _boom(*a, **k):
        raise RuntimeError("postgres is down")

    from kumo_strategies.runtime.executor import store
    monkeypatch.setattr(store, "record_claim", _boom, raising=False)
    monkeypatch.setattr(store, "drop_claim", _boom, raising=False)

    j, brk = _Journal(), _Broker(price=10.0)
    res = _run(_trading_gw(j, brk, enter=["AAA"]))  # must not raise

    assert brk.submitted, "the order never went out"
    assert res is not None


def test_an_EXIT_goes_through_broker_exit_not_submit():
    """#459. `submit()` releases nothing; `exit()` cancels the resting stop first.

    MEASURED AT THE VENUE: every held symbol has `qty_available = 0` — 100% of shares reserved by
    resting trailing stops (AEM 18/0, CGAU 174/0, BETA 79/0 ...). A SELL that does not first cancel
    the stop is refused with `403 insufficient qty available (available: 0)`.

    `pgrunner` exits through `broker.exit()`; so does `qc27_runner` now. QC345 had ZERO calls to it —
    both its SELL sites went through `submit()`, so every exit it ever attempted was unreleasable.
    """
    j, brk = _Journal(), _Broker(positions={"BBB": 5}, price=10.0)

    _run(_trading_gw(j, brk, exit_=["BBB"]))

    sells = [o for o in brk.via_submit if getattr(o, "side", None) == "SELL"]
    assert not sells, (
        f"the exit went through submit() — {len(sells)} SELL(s) straight to the venue with the stop "
        f"still holding the shares. Alpaca refuses those with available: 0")
    assert [getattr(o, "symbol", None) for o in brk.exited] == ["BBB"], (
        f"the exit did not go through broker.exit(): {brk.exited}")


def test_the_LIQUIDATING_path_also_releases_before_selling():
    """A wind-down is the case where this matters most — it sells the WHOLE book, so every one of
    those symbols has its shares reserved by a stop."""
    from kumo_strategies.runtime.executor.lifecycle import State

    j, brk = _Journal(), _Broker(positions={"AAA": 3, "BBB": 4}, price=10.0)

    _run(_gateway(State.LIQUIDATING, journal=j, broker=brk, decision=_decision()))

    assert not [o for o in brk.via_submit if getattr(o, "side", None) == "SELL"], (
        "liquidation sold through submit() — the wind-down path is the one that flattens everything, "
        "so every symbol in it is stop-reserved")
    assert {getattr(o, "symbol", None) for o in brk.exited} == {"AAA", "BBB"}


def test_ENTRIES_still_go_through_submit():
    """The regression this could introduce. `exit()` releases shares; a BUY has none to release, and
    routing entries through it would ask the feed to cancel a stop that does not exist."""
    j, brk = _Journal(), _Broker(price=10.0)

    _run(_trading_gw(j, brk, enter=["AAA"]))

    assert [getattr(o, "side", None) for o in brk.via_submit] == ["BUY"]
    assert not brk.exited, f"an entry was routed through exit(): {brk.exited}"


def test_the_double_matches_the_real_brokers_async_contract():
    """THE DOUBLE MUST AGREE WITH PRODUCTION ABOUT WHICH CALLS ARE COROUTINES (#459).

    `NautilusBroker` is asymmetric: `submit` is synchronous, `exit` is `async` because it must
    `await feed.release_for_exit(...)` before selling. `_Broker.exit` was written synchronously, so
    `self._broker.exit(...)` WITHOUT `await` returned a coroutine object, `getattr(res, "ok", False)`
    read False, and every exit reported a plain refusal — releasing nothing, sending nothing. All 60
    tests in this file passed over that.

    Bound to the real class rather than restated, so the day `submit` becomes async this fails instead
    of quietly describing a contract that has moved.
    """
    import inspect

    from kumo_strategies.runtime.nautilus.broker import NautilusBroker

    for name in ("submit", "exit"):
        real = inspect.iscoroutinefunction(getattr(NautilusBroker, name))
        mine = inspect.iscoroutinefunction(getattr(_Broker, name))
        assert real == mine, (
            f"_Broker.{name} is {'async' if mine else 'sync'} but NautilusBroker.{name} is "
            f"{'async' if real else 'sync'} — a call site missing (or wrongly given) `await` passes "
            f"every test in this file while doing nothing in production")


def test_the_exit_call_sites_are_AWAITED():
    """The mutation the contract test above cannot see: the double could be correct and the call site
    still drop the `await`. Asserts on the source, because a dropped `await` produces a coroutine that
    fails the `ok` check and looks exactly like an ordinary broker refusal."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(qc345.__file__).read_text())
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "exit"
        and getattr(getattr(n.func, "value", None), "attr", None) == "_broker"
    ]
    assert len(calls) == 2, (
        f"expected 2 broker.exit() call sites (rotation exit + _liquidate), found {len(calls)} — "
        f"if they moved this test proves nothing by finding nothing")
    bare = [n for n in calls if id(n) not in awaited]
    assert not bare, (
        f"{len(bare)} call(s) to broker.exit() are not awaited (line(s) "
        f"{[n.lineno for n in bare]}) — an unawaited coroutine fails the `ok` check and is "
        f"indistinguishable from an ordinary broker refusal, while releasing and sending nothing")


def test_a_back_fill_whose_WRITE_FAILED_journals_a_RISK_row_not_silence(monkeypatch):
    """`store.write_claim` can stall (kumo-trading-strategies f54f54d, `ClaimWriteStalled`) and the sweep
    names it under `failed`. The session's journal gate keyed only on adopted/capped/unknowable/
    refused, so a sweep whose every write failed journalled NOTHING — the position stays unclaimed,
    invisible to every claim-based ceiling, and the session reads as clean (review-848-final).
    Same condition as an unavailable sweep, same row kind: `risk`."""
    from kumo_strategies.runtime.executor.lifecycle import State
    import strategies.claims_backfill as backfill

    adoption = {"status": "ok", "adopted": 0, "capped": [], "unknowable": [], "refused": [],
                "failed": ["OLD(ClaimWriteStalled: QC345-003/OLD: claim write exceeded 30s; rolled back)"]}

    async def _sweep(_broker, _journal, _sid):
        return dict(adoption)

    monkeypatch.setattr(backfill, "backfill_claims", _sweep, raising=True)
    journal, broker = _Journal(), _Broker(positions={"OLD": 10})
    gw = _gateway(State.SHADOW, journal=journal, broker=broker, decision=_decision(hold=["OLD"]))
    _run(gw)

    risk = [w for w in journal.writes if w[0] == "risk"]
    assert risk, f"a failed claim write journalled nothing: {[w[0] for w in journal.writes]}"
    assert risk[0][3] == adoption, risk
    assert not [w for w in journal.writes if w[0] == "pool"], (
        "a sweep with a FAILED write is not a 'pool' housekeeping row")


def test_a_REFUSED_adoption_reaches_the_journal_row_the_session_writes(monkeypatch):
    """The `refused` list is asserted at the back-fill level; nothing pinned that the session's
    journal row carries it — reverting the `qc345.py` gate to its pre-#848 shape left the suite
    403 green (review-848-final). This drives the gate: a sweep that refused one adoption and
    adopted nothing writes a `pool` row whose detail names the refusal."""
    from kumo_strategies.runtime.executor.lifecycle import State
    import strategies.claims_backfill as backfill

    adoption = {"status": "ok", "adopted": 0, "capped": [], "unknowable": [],
                "refused": ["OLD(held 10 at read, none under the lock)"], "failed": []}

    async def _sweep(_broker, _journal, _sid):
        return dict(adoption)

    monkeypatch.setattr(backfill, "backfill_claims", _sweep, raising=True)
    journal, broker = _Journal(), _Broker(positions={"OLD": 10})
    gw = _gateway(State.SHADOW, journal=journal, broker=broker, decision=_decision(hold=["OLD"]))
    _run(gw)

    pool = [w for w in journal.writes if w[0] == "pool"]
    assert pool, f"a refused adoption journalled nothing: {[w[0] for w in journal.writes]}"
    assert pool[0][3]["refused"] == adoption["refused"], pool
    assert not [w for w in journal.writes if w[0] == "risk"], "a refusal is not a failed write"
