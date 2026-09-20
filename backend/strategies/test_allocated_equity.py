"""A strategy must size against its own allocation, and must re-read it every session.

WHY THIS FILE EXISTS
--------------------
`RiskLimits.allocated_equity` defaults to `None`, and both gateways construct `RiskLimits()` with no
arguments (`momentum.py:372`, `qc345.py:476`). The runner's sizing line is:

    equity = self.limits.allocated_equity or self.broker.equity()      # pgrunner.py:801

so it has *always* taken the second branch. MOMENTUM-002 has been sizing ~$10k a name off the whole
~$100k account against a $20k target built to hold eight names — which is the cause of the 67k sleeve
overflow, and therefore of `is_reducing` being true and `budget_gate` refusing every entry.

kumo-trading-strategies' own docstring predicted this exact outcome before it happened: *"a live book of 2 names
sized off the whole account is a different portfolio with ~4x the single-name concentration — not the one
that was tested."*

WHY THE OBVIOUS FIX IS WRONG, AND THIS TEST PINS THE DIFFERENCE
---------------------------------------------------------------
The one-line fix is to read the sleeve target at gateway construction and pass it in. That produces a
**stale copy** of a number the operator can edit at runtime — Law 1's second writer, arriving in the fix
for a Law 1 violation. QC345's target was cut 40k -> 20k on 2026-08-15 while a strategy was running;
under a construction-time capture it would have kept sizing off the old number and nothing would have
flagged it.

`SessionGateway` already documents why it builds a FRESH runner per session — *"an operator can arm,
pause or halt the strategy from the UI while the node keeps running"*. Allocation is the same kind of
fact. So the requirement is not "pass it once", it is **"re-read it per session"**, and the second test
below is the one that survives the naive fix.

Cockpit #334, build-spec item 2.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

_STRATEGIES = pathlib.Path(__file__).parent


def _Limits():
    """The REAL `RiskLimits`, not a stand-in.

    The first version built its own local dataclass carrying `allocated_equity`, and its docstring
    claimed `dataclasses.replace` "behaves identically". It does not: the installed `RiskLimits` may not
    have the field at all, so the double passed while the production path raised `TypeError`. That is the
    fourth double tonight that could not represent production, and this one hid a CRITICAL.
    """
    from kumo_strategies.runtime.executor.runner import RiskLimits

    return RiskLimits()


def test_the_installed_risklimits_actually_has_the_field():
    """The conformance check the double was hiding — and it is RED in this venv right now.

    The installed `RiskLimits` predates `allocated_equity`, while the running container has a newer one.
    That disagreement is the version skew `pyproject.toml` does not pin, and it is the #346 defect class
    exactly: a seam between two repos with nothing asserting they agree.
    """
    from kumo_strategies.runtime.executor.runner import RiskLimits

    assert "allocated_equity" in RiskLimits.__dataclass_fields__, (
        "the installed RiskLimits has no `allocated_equity` — sizing silently falls back to the whole "
        "account, and pyproject.toml pins kumo-trading-strategies by URL with no revision to enforce otherwise"
    )


def _risklimits_constructions() -> list[tuple[str, int, bool]]:
    """Every `RiskLimits(...)` in production: (file, line, passes allocated_equity)."""
    found: list[tuple[str, int, bool]] = []
    for path in _STRATEGIES.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "RiskLimits":
                continue
            explicit = any(kw.arg == "allocated_equity" for kw in node.keywords)
            found.append((str(path.relative_to(_STRATEGIES)), node.lineno, explicit))
    return found


def test_no_strategy_sizes_off_the_whole_account(monkeypatch):
    """The GUARANTEE, not the mechanism — and this test's first draft demanded the wrong mechanism.

    It originally scanned for an explicit `allocated_equity=` kwarg at every `RiskLimits(...)` call site.
    That is the construction-time capture the file's own docstring rejects: it would have gone green on
    the stale-copy fix and stayed red on the correct one. A test that pushes production toward the defect
    it was written to prevent is worse than no test.

    What matters is that the limits the RUNNER receives carry this strategy's allocation. Bound against
    the real method with settings stubbed, so it fails if the derivation is removed OR if it silently
    stops reaching the runner.
    """
    from strategies.momentum import SessionGateway

    gw = SessionGateway.__new__(SessionGateway)
    gw._strategy_id = "MOMENTUM-002"
    gw._limits = _Limits()

    monkeypatch.setitem(__import__("sys").modules, "api.settings",
                        type("m", (), {"resolve": staticmethod(lambda d: {"MOMENTUM-002": 20000.0})}))

    limits = gw._limits_for_session()
    assert limits.allocated_equity == 20000.0, (
        "the limits handed to the runner carry no allocation, so `pgrunner.py:801` falls through to "
        "`broker.equity()` and a 20k sleeve buys ~10k a name off a ~100k account"
    )
    assert gw._limits.allocated_equity is None, (
        "the constructed limits were MUTATED — the per-session value must be a fresh copy, or the first "
        "session's allocation becomes a captured stale one for every session after it"
    )


def test_an_unreadable_allocation_does_not_silently_change_sizing(monkeypatch):
    """Fail-safe direction: a settings hiccup must not alter how a live session sizes."""
    from strategies.momentum import SessionGateway

    gw = SessionGateway.__new__(SessionGateway)
    gw._strategy_id = "MOMENTUM-002"
    gw._limits = _Limits()

    def _boom(_domain):
        raise RuntimeError("settings unreachable")

    monkeypatch.setitem(__import__("sys").modules, "api.settings",
                        type("m", (), {"resolve": staticmethod(_boom)}))

    assert gw._limits_for_session() is gw._limits, (
        "an unreadable allocation changed the limits — sizing must fall back to today's behaviour, not "
        "to a partially-derived number"
    )


def test_the_allocation_is_re_read_per_session_not_captured_once():
    """The test that survives the naive fix, and the reason this file exists.

    Passing the target once at construction satisfies the test above and still sizes off a stale number
    the moment an operator edits it — which happened on 2026-08-15 when a target was cut 40k -> 20k.
    `SessionGateway` already rebuilds the runner every session for exactly this class of reason; the
    allocation has to travel on that same path.

    NOTE ON THIS TEST'S OWN HISTORY: it first asserted `"allocated_equity" in src or
    "_limits_for_session" in src`, and stayed GREEN when the wiring was deleted — because the string
    `allocated_equity` appears in the COMMENT explaining the fix. Matching a comment instead of a call is
    a documented defect class in this repo, and this test had it. It now binds the AST.
    """
    from strategies.momentum import SessionGateway

    tree = ast.parse(textwrap.dedent(inspect.getsource(SessionGateway.run)))
    calls = {
        (getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "_limits_for_session" in calls, (
        "`SessionGateway.run` never CALLS the per-session allocation derivation — if the allocation is "
        "captured at construction it is a stale copy of a number the operator can change mid-run"
    )

    runner = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "PgSessionRunner"),
        None,
    )
    assert runner is not None, "PgSessionRunner is no longer constructed in run() — this test is blind"
    limits_kw = next((kw for kw in runner.keywords if kw.arg == "limits"), None)
    assert limits_kw is not None and not (
        isinstance(limits_kw.value, ast.Attribute) and limits_kw.value.attr == "_limits"
    ), (
        "the runner is handed the CONSTRUCTED limits, so the freshly-derived allocation never reaches "
        "sizing — the derivation exists and does nothing"
    )


def test_the_scanner_sees_the_constructions_it_judges():
    """An invariant asserted over an empty set passes for the wrong reason."""
    assert _risklimits_constructions(), (
        "no RiskLimits construction found in strategies/ — the guarantees above would be vacuous"
    )


# THE MARKER IS GONE BECAUSE THE DEFECT IS (2026-08-23). It read:
#
#   "QC345 sizes off the WHOLE ACCOUNT by a different route and is NOT fixed by the momentum change.
#    `_equity_per_position` computes `self._broker.equity() * max_deployed_frac / portfolio_size`
#    directly -- it never consults `allocated_equity` at all ... strict=True so that whoever fixes it
#    must delete this marker and read why."
#
# It worked exactly as designed: the fix turned it XPASS(strict) and the suite refused to go green
# until it was read. It also predicted the consequence precisely -- "surfaces only when QC345-003 is
# funded and buys ~10k a name against a 20k target". QC345-003 was funded to 20,000 on 2026-08-19, and
# on 2026-08-21 it decided to enter five names and placed ZERO orders. 103,466 x 0.80 / 5 = 16,554 a
# name, of which at most one fits the sleeve.
#
# The lane has never placed an order in its life. This is why.
def test_qc345_sizes_against_its_allocation_not_the_account():
    """The gap the PR described inaccurately, pinned accurately.

    Deliberately asserts the BEHAVIOUR (sizing consults the allocation) rather than the construction
    site, because the construction site is not where QC345's defect lives — that was the mistake in how
    the gap was originally written down.
    """
    import inspect

    from strategies import qc345

    src = inspect.getsource(qc345.QC345SessionGateway._equity_per_position)
    assert "allocated_equity" in src, (
        "QC345's per-position sizing never reads `allocated_equity` — it divides the whole account by "
        "portfolio_size, so a 20k-target strategy would buy ~10k a name the moment it is funded"
    )
