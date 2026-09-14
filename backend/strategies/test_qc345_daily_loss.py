"""QC345 must be able to halt on a daily loss, and say so when it cannot (#784, #548).

WHAT WAS MISSING. `qc345.py` had NO risk check at all — its own docstring says that path runs "with
NO lifecycle read, NO journal, NO idempotency key, NO risk check and NO budget gate". Measured on
the live paper database: QC345-003 has 3 decision rows and TECHIVOL-005 has 14, and NOT ONE carries
an `equity` key. So even with `enforce` wired, `baseline()` would return MISSING every session and
the stop would be ARMED-BUT-UNENFORCEABLE forever.

THE ANCHOR IS COCKPIT'S HALF. `daily_loss.anchor()` must go on QC345's OWN decision detail, which is
this repo's code. Wiring the check without it is the build-it-never-fires shape.

SHIPPED UNARMED, DELIBERATELY. `daily_loss_frac` resolves to 0.05 from a bare `RiskLimits()`, the
basis is the SHARED ACCOUNT rather than the sleeve (#517), and QC345's decision rows are 8 days
apart — so "5% daily loss" would mean "5% of the whole account since up to a week ago", which an
ordinary market week trips. Arming is a separate decision with a number the operator gives.
"""

from __future__ import annotations

import inspect


def test_the_INSTALLED_package_offers_what_this_wiring_passes():
    """CONFORMANCE, against the package actually installed — not a double.

    Four defects in this repo shipped green through a double that was more permissive than
    production. This asserts the real `daily_loss` contract, so a strategies bump that changes the
    signature fails HERE rather than at the first live halt.

    (The first version of this test asserted `... or True`, which cannot fail. It was caught before
    shipping; recorded because a vacuous test is worse than no test — it reports coverage it has not
    got.)
    """
    from kumo_strategies.runtime.executor import daily_loss

    enforce = inspect.signature(daily_loss.enforce)
    assert set(enforce.parameters) == {
        "journal", "write", "lifecycle", "equity", "frac", "session", "on_halt"}
    assert enforce.parameters["on_halt"].default is None
    assert set(inspect.signature(daily_loss.anchor).parameters) == {"equity"}
    # `finite` is what makes the guard about the CLASS of non-finite anchors rather than one bug.
    assert callable(daily_loss.finite)


def test_QC345_writes_an_equity_ANCHOR_on_its_decision_row():
    """Without this the stop can never fire: `baseline()` returns MISSING every session.

    Killed by removing the `anchor(...)` spread from the decision detail."""
    from strategies import qc345

    # TWO ASSERTIONS, because one level away is not the property. The first version looked for
    # "daily_loss.anchor" in `run` and failed once the call moved into a helper — a detector aimed at
    # where the code happened to be rather than at what must be true.
    run_src = inspect.getsource(qc345.QC345SessionGateway.run)
    detail = run_src[run_src.index("detail = {"):]
    assert "_daily_loss_anchor()" in detail, (
        "QC345's decision detail carries no equity anchor, so a wired daily-loss stop would be "
        "ARMED-BUT-UNENFORCEABLE forever — baseline() returns MISSING every session"
    )
    helper = inspect.getsource(qc345.QC345SessionGateway._daily_loss_anchor)
    assert ".anchor(" in helper and "self._broker.equity" in helper, (
        "the anchor helper does not call the module's anchor() on the broker's equity"
    )


def test_the_check_runs_BEFORE_anything_is_decided_or_sized():
    """A risk gate after the orders are built is not a gate. `enforce` must precede `_decide`."""
    from strategies import qc345

    src = inspect.getsource(qc345.QC345SessionGateway.run)
    assert "daily_loss.enforce" in src or "_enforce_daily_loss" in src
    call = min(i for i in (src.find("daily_loss.enforce"), src.find("_enforce_daily_loss")) if i > 0)
    decide = src.find("self._decide(")
    assert 0 < call < decide, "the daily-loss check must run before the decision is computed"


def test_it_ships_UNARMED_and_the_flag_defaults_to_False():
    """Every gate in this repo defaults off. Arming here would put a 5% halt on a SHARED account
    against an anchor that can be 8 days old. Killed by defaulting the flag True."""
    from strategies.qc345 import QC345SessionGateway

    sig = inspect.signature(QC345SessionGateway.__init__)
    assert "daily_loss_armed" in sig.parameters, "no armed flag — the stop cannot be left off"
    assert sig.parameters["daily_loss_armed"].default is False


def test_expected_is_captured_BEFORE_enforce_runs():
    """`enforce` halts the lifecycle object before calling `on_halt`, so a compare-and-set that reads
    `life.state` afterwards compares HALTED against HALTED and silently no-ops. momentum.py:228 is
    the pattern. Killed by moving the capture below the enforce call."""
    from strategies import qc345

    src = inspect.getsource(qc345.QC345SessionGateway.run)
    cap = src.find("entered_with")
    call = min(i for i in (src.find("daily_loss.enforce"), src.find("_enforce_daily_loss")) if i > 0)
    assert 0 < cap < call, "entered_with must be captured before enforce mutates the lifecycle"


def test_the_halt_is_PERSISTED_through_the_one_writer():
    """`lifecycle.halt()` alone survives ONE session: pgrunner never persists, and QC345 reads
    lifecycle fresh each session and never writes it back. `lifecycle_state.save_if_unchanged` is the
    single durable writer (momentum.py:117 already routes through it).

    Killed by dropping the on_halt hook."""
    from strategies import qc345

    src = inspect.getsource(qc345)
    assert "save_if_unchanged" in src, (
        "QC345 does not persist a halt, so a risk HALT evaporates after one session — a durable stop "
        "degraded to a one-session decline"
    )


def test_a_stack_without_daily_loss_DEGRADES_LOUDLY_rather_than_crashing_at_import():
    """An engine running pre-ea21f98 strategies has no `daily_loss` module. Importing it at module
    scope would crash-loop the node at boot — a change that anchors on something absent, at import
    time. The import must be local and its absence reported, not fatal."""
    from strategies import qc345

    src = inspect.getsource(qc345)
    head = src.split("class ", 1)[0]
    assert "import daily_loss" not in head and "from kumo_strategies.runtime.executor import daily_loss" not in head, (
        "daily_loss is imported at module scope; a stack without it cannot boot"
    )


# --------------------------------------------------------------------------------------------
# BEHAVIOURAL — the source-grep tests above survived four mutations. These drive the real helper.
# --------------------------------------------------------------------------------------------
#
# Measured: mutating on_halt to None, ignoring the armed flag, and reordering the `expected` capture
# all left every grep-style test green. A test that asserts WHERE code sits cannot see WHAT it does.


class _Life:
    def __init__(self, state):
        self.state = state
        self.halted = False

    def halt(self, *_a, **_k):
        self.halted = True


class _State:
    def __init__(self, name, may=True):
        self.value = name
        self.name = name
        self.may_submit_entries = may


class _Journal:
    """Carries what production's PgJournal carries, not merely what this test happens to call.

    `enforce` -> `baseline()` does `await journal.tail(400, kind=DECISION)`. A double without `tail`
    fails with an AttributeError from inside the vendor, which is a test-harness error wearing the
    costume of a product failure — and four defects in this repo shipped green behind doubles thinner
    than production.
    """

    def __init__(self, rows=None):
        self.writes = []
        self._rows = list(rows or [])

    async def write(self, kind, summary, **kw):
        self.writes.append((kind, summary, kw.get("detail")))
        return 1

    async def tail(self, n, kind=None):
        return self._rows[-n:]


class _Broker:
    def __init__(self, eq=100_000.0):
        self._eq = eq

    def equity(self):
        return self._eq


def _gateway(armed: bool, equity=100_000.0):
    """A gateway with only the surface `_enforce_daily_loss` and `_daily_loss_anchor` touch."""
    from strategies.qc345 import QC345SessionGateway

    gw = QC345SessionGateway.__new__(QC345SessionGateway)
    gw._journal = _Journal()
    gw._broker = _Broker(equity)
    gw._slot = "close-20m"
    gw._daily_loss_armed = armed
    gw._strategy_id = "QC345-003"
    gw._sm = None

    class _L:
        daily_loss_frac = 0.05
    gw._limits = _L()
    return gw


def test_the_ARMED_FLAG_actually_decides_whether_frac_reaches_enforce():
    """Killed by ignoring the flag. The grep test could not see this at all."""
    import asyncio

    from kumo_strategies.runtime.executor import daily_loss

    seen = {}
    real = daily_loss.enforce

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    daily_loss.enforce = spy
    try:
        gw = _gateway(armed=False)
        asyncio.run(gw._enforce_daily_loss("2026-09-02", _Life(_State("TRADING")), _State("TRADING")))
        assert seen["frac"] is None, "an UNARMED lane still passed a threshold to enforce"

        gw = _gateway(armed=True)
        asyncio.run(gw._enforce_daily_loss("2026-09-02", _Life(_State("TRADING")), _State("TRADING")))
        assert seen["frac"] == 0.05, "an ARMED lane did not pass its threshold"
    finally:
        daily_loss.enforce = real


def test_an_ON_HALT_HOOK_IS_SUPPLIED_so_a_halt_can_be_persisted():
    """Killed by `on_halt=None`. Without a hook the halt survives one session and evaporates — and
    the grep test passed because `_persist_halt` still existed, unused."""
    import asyncio

    from kumo_strategies.runtime.executor import daily_loss

    seen = {}
    real = daily_loss.enforce

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    daily_loss.enforce = spy
    try:
        gw = _gateway(armed=True)
        asyncio.run(gw._enforce_daily_loss("2026-09-02", _Life(_State("TRADING")), _State("TRADING")))
    finally:
        daily_loss.enforce = real
    assert seen.get("on_halt") is not None, (
        "no on_halt hook: a risk HALT would survive exactly one session and then evaporate"
    )
    assert callable(seen["on_halt"])


def test_the_anchor_helper_returns_a_USABLE_key_and_omits_a_NON_FINITE_one():
    """Behaviour, not source. `anchor()` must omit the key when equity is not finite — key present
    <=> value usable — so a poisoned anchor cannot be spelled the same way as a good one."""
    gw = _gateway(armed=False, equity=100_000.0)
    assert gw._daily_loss_anchor() == {"equity": 100_000.0}

    gw = _gateway(armed=False, equity=float("nan"))
    assert "equity" not in gw._daily_loss_anchor(), "a NaN equity was written as an anchor"

    gw = _gateway(armed=False, equity=float("inf"))
    assert "equity" not in gw._daily_loss_anchor(), (
        "an infinite equity was written as an anchor — old code turned that into a PERMANENT "
        "spurious halt, since now < inf*0.95 is always true"
    )


def test_a_stack_without_the_module_SAYS_SO_and_does_not_block_the_session():
    """Three states: armed, not armed, and cannot check. Killed by returning None silently."""
    import asyncio

    from strategies.qc345 import QC345SessionGateway

    gw = _gateway(armed=True)
    gw._daily_loss_module = staticmethod(lambda: None)
    out = asyncio.run(
        QC345SessionGateway._enforce_daily_loss(gw, "2026-09-02", _Life(_State("TRADING")),
                                               _State("TRADING")))
    assert out is None, "an unavailable stop must not block the session"
    kinds = [w[0] for w in gw._journal.writes]
    assert "state" in kinds
    assert any("UNAVAILABLE" in w[1] for w in gw._journal.writes), (
        "the lane cannot halt and did not say so — 'no reason to halt' and 'no ability to halt' "
        "must not read the same"
    )
