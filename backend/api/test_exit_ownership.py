"""An exit must reduce the lane that placed it, never open the opposite side (#748, second shape).

MEASURED on an Alpaca paper instance 2026-08-31, and found only because a reviewer read the residual cache rather than
the logs: WHD carries BCTROT-004 +28 and MOMENTUM-002 −28; CGAU carries MOMENTUM-002 −88. A lane
exit stamped with a lane that did not hold the shares booked as an OPPOSITE-SIDE position on that
lane.

IT HAS NO FINGERPRINT. The account net matches the venue, so reconciliation generates no diff and no
EXTERNAL mirror appears — the mint's usual tell is absent. What is wrong is the SPLIT, and both
lanes' budgets size off it. That is why it survived a week of hunting the phantom.

AND NAUTILUS CANNOT CATCH IT. `_reject_reduce_only_netting_position_open` only fires for reduce-only
orders; that is what makes the protective-stop shape fail loudly at the venue boundary. An exit
without the flag is accepted, fills, and silently opens the other side.
"""

from __future__ import annotations

import pytest

from api.exit_ownership import NO_POSITION, OK, OVERSIZE, WRONG_SIDE, classify_exit


def _c(**kw):
    base = dict(side="SELL", quantity=10.0, lane="MOMENTUM-002", instrument_id="WHD.XNYS",
                lane_signed_qty=10.0)
    return classify_exit(**{**base, **kw})


def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the default fixture must be a LEGITIMATE exit, or every refusal below is the
    fixture failing rather than the rule firing."""
    assert _c().ok and _c().reason == OK


# ==================================================================================================
# The measured shapes
# ==================================================================================================
def test_an_exit_from_a_lane_that_HOLDS_NOTHING_is_refused():
    """WHD: MOMENTUM-002 held nothing and a SELL 28 booked it SHORT 28."""
    v = _c(quantity=28, lane_signed_qty=0.0)
    assert not v.ok and v.reason == NO_POSITION
    # #1030: the verdict names the side the lane is DECLARED to hold, not "a long-only book".
    assert "SHORT" in v.detail and "declared LONG" in v.detail


def test_an_exit_on_the_WRONG_SIDE_is_refused():
    """A SELL against a short adds to it. The order is an entry wearing an exit's clothes."""
    v = _c(side="SELL", lane_signed_qty=-5)
    assert not v.ok and v.reason == WRONG_SIDE


def test_an_OVERSIZE_exit_is_refused_and_names_the_excess():
    """CGAU shape. Selling more than the lane holds does not close a position, it flips it."""
    v = _c(side="SELL", quantity=90, lane_signed_qty=88)
    assert not v.ok and v.reason == OVERSIZE
    assert "excess 2" in v.detail


def test_a_BUY_reduces_a_SHORT():
    """Side alone does not say which way exposure moves — a BUY is the exit on a short."""
    assert _c(side="BUY", quantity=5, lane_signed_qty=-5).ok


def test_an_EXACT_full_exit_is_allowed():
    assert _c(side="SELL", quantity=10, lane_signed_qty=10).ok


def test_a_PARTIAL_exit_is_allowed():
    assert _c(side="SELL", quantity=3, lane_signed_qty=10).ok


# ==================================================================================================
# What it must NOT do
# ==================================================================================================
def test_an_UNREADABLE_book_ALLOWS_the_order_and_says_so():
    """THE FAIL-OPEN, and it is deliberate. Refusing every exit because a cache read failed would
    stop a lane trading for a reason unrelated to ownership — and a guard that halts a healthy book
    is one that gets switched off, after which it protects nothing.

    `None` is not zero, and the distinction is the whole rule: zero means the lane holds nothing and
    is REFUSED; None means we could not look and is ALLOWED, reported as `unreadable`."""
    v = _c(lane_signed_qty=None)
    assert v.ok and v.reason == "unreadable"
    assert v.reason != OK, "an unreadable book must not report as a verified reduction"


def test_ZERO_and_NONE_do_not_collapse():
    """Pinned as a pair, because collapsing them is this repo's most repeated defect."""
    assert not _c(lane_signed_qty=0.0).ok
    assert _c(lane_signed_qty=None).ok


@pytest.mark.parametrize("eps", [1e-12, -1e-12])
def test_venue_ROUNDING_does_not_make_a_full_exit_look_oversize(eps):
    """A full exit sized off a venue-rounded quantity must not be refused for a billionth of a share."""
    assert _c(side="SELL", quantity=10 + eps, lane_signed_qty=10).ok


# ==================================================================================================
# The SEAM — the classifier being right proves nothing about it being consulted
#
# #1030 MOVED THE SEAM. The guard used to be `AlpacaExecutionClient._exit_reduces_its_own_lane`, called
# inline from `_submit_order`, and these tests drove that method on a `_Probe(AlpacaExecutionClient)`.
# It is now `gated_exec.install_ownership_gate`, a wrap installed on BOTH venues' clients — so the
# submit path these tests must drive is the WRAPPED `_submit_order`, on a double that is the venue.
# The properties pinned here are unchanged; what they are pinned AGAINST is the wrap. The full
# side-aware suite is `test_ownership_gate_both_venues.py`; this section keeps #748's own claims
# where #748's measurements are.
# ==================================================================================================
import asyncio as _asyncio
import types as _types


def _order(side="SELL", qty=28.0, lane="MOMENTUM-002", iid="WHD.XNYS", reduce_only=False, coid="c-1"):
    return _types.SimpleNamespace(
        client_order_id=coid, instrument_id=iid, strategy_id=lane,
        side=_types.SimpleNamespace(name=side), quantity=qty, is_reduce_only=reduce_only,
        is_closed=False,
    )


class _Log:
    def __init__(self):
        self.records = []

    def error(self, msg, *a):
        self.records.append(("error", str(msg)))

    def warning(self, msg, *a):
        self.records.append(("warning", str(msg)))

    def info(self, msg, *a):
        self.records.append(("info", str(msg)))


def _client(held):
    """The exec client with only the pieces the ownership check touches, WRAPPED by the real guard.
    `held=None` is an unreadable book; `held=RuntimeError` makes the read raise."""
    from api.providers.gated_exec import install_ownership_gate

    class _Cache:
        def positions_open(self, strategy_id=None, instrument_id=None):
            if held is RuntimeError:
                raise RuntimeError("boom")
            if held is None:
                return None
            return [_types.SimpleNamespace(signed_qty=held)] if held else []

    class _Venue:
        def __init__(self):
            self._cache = _Cache()
            self._log = _Log()
            self._clock = _types.SimpleNamespace(timestamp_ns=lambda: 1)
            self.sent, self.denied = [], []

        async def _submit_order(self, command):
            self.sent.append(command.order.client_order_id)

        def generate_order_denied(self, **kw):
            self.denied.append(kw)

    return install_ownership_gate(_Venue())


def _submit(client, order):
    _asyncio.run(client._submit_order(_types.SimpleNamespace(order=order)))
    return client


def test_the_SUBMIT_PATH_consults_the_rule():
    """Drives the wrapped submit path. `classify_exit` returning correct verdicts is not the claim —
    the claim is that submission asks it."""
    assert _submit(_client(0.0), _order()).denied
    assert _submit(_client(28.0), _order()).sent == ["c-1"]


def test_a_PROTECTIVE_STOP_is_skipped_by_identity__a_reduce_only_lane_exit_is_NOT():
    """#748 skipped every reduce-only order: "Nautilus already refuses to let a reduce-only fill open a
    netting position". #1030 measured that on 1.229.0 that refusal is POST-FILL and the risk engine's
    pre-check needs a `position_id` strategies never pass — while kumo-trading-strategies submits every lane
    exit `reduce_only=True`. So the skip is by IDENTITY (cockpit's own stops, which legitimately rest
    before the position shows up in this cache read), and a reduce-only lane exit IS asked."""
    from api.protection import PROTECTION_COID_PREFIX
    stop = _order(reduce_only=True, coid=f"{PROTECTION_COID_PREFIX}SELL-WHD-XNYS-deadbeef")
    assert _submit(_client(0.0), stop).sent == [stop.client_order_id]
    assert _submit(_client(0.0), _order(reduce_only=True)).denied


def test_an_EXCEPTION_in_the_check_ALLOWS_the_order():
    """A guard that halts a healthy book is one that gets switched off, after which it guards
    nothing. Same fail-open contract as the budget gate."""
    c = _submit(_client(RuntimeError), _order())
    assert c.sent == ["c-1"]
    assert any("boom" in m for l, m in c._log.records if l == "warning")


def test_an_UNREADABLE_book_ALLOWS_the_order_and_SAYS_SO():
    c = _submit(_client(None), _order())
    assert c.sent == ["c-1"]
    assert any(l == "warning" and "could not be read" in m for l, m in c._log.records), c._log.records


def test_the_REFUSAL_IS_LOUD_and_DENIES_the_order():
    """It must reach the ERROR log AND generate an OrderDenied — a refusal that only logs is a
    silently dropped order, which is the same silence this whole family lives in. DENIED, not
    rejected (#1030): the venue was never asked, and `OrderRejected` is the venue's word
    (issue 103 journals it as "rejected by the venue")."""
    c = _submit(_client(0.0), _order())
    assert any(l == "error" and "OWNERSHIP REFUSED" in m for l, m in c._log.records), c._log.records
    assert len(c.denied) == 1 and c.denied[0]["client_order_id"] == "c-1"
    assert c.sent == []


def test_OWNERSHIP_IS_CHECKED_BEFORE_BUDGET():
    """A budget verdict on an order that is secretly an entry answers the wrong question — and would
    report 'insufficient budget' for an order that should never have been sized at all. Pinned at the
    factory: ownership is installed AFTER budget (outermost), and the reverse RAISES."""
    import ast
    import inspect
    import textwrap

    import pytest

    from api.providers import ibkr
    from api.providers.gated_exec import install_budget_gate, install_ownership_gate

    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(ibkr.BudgetGatedIBExecClientFactory.create))))
    assert "install_ownership_gate(install_budget_gate(" in src, src
    with pytest.raises(RuntimeError):
        install_budget_gate(install_ownership_gate(_client(0.0)))


def test_the_REFUSAL_BRANCH_IS_ACTUALLY_CONDITIONAL_on_the_verdict():
    """Measured as a mutation survivor on the inline guard: replacing `if not owns:` with `if False:`
    left every test in this file green. Driven now rather than read: the same wrap must pass a
    permitted order AND refuse a forbidden one."""
    assert _submit(_client(28.0), _order()).sent == ["c-1"]
    assert _submit(_client(0.0), _order()).denied


# ==================================================================================================
# THE REGRESSION: the guard refused ENTRIES. Measured live, 2026-09-01 02:52 SGT.
# ==================================================================================================
def test_an_ENTRY_is_not_an_exit_and_must_not_be_refused():
    """Shipped in 2e4c78f and blocked every entry on paper within minutes.

        OWNERSHIP REFUSED: MOMENTUM-002 holds no open AEM.XNYS; a BUY 9 would OPEN a position
        OWNERSHIP REFUSED: MOMENTUM-002 holds +59 GMAB.XNAS; a BUY 60 adds to it

    Both are legitimate ENTRIES. Two lanes decided, eight orders were produced, and my own guard
    refused all eight — the guard against corrupting the split became the thing that stopped trading.

    THE RULE ALREADY EXISTED. `budget_gate.is_entry(net, side, qty)` decides entry-vs-exit and its
    docstring says: "ONE RULE, ONE IMPLEMENTATION... this codebase has been bitten by exactly that
    often enough to treat a second derivation as a defect rather than a convenience." I wrote a
    second derivation anyway, in a file whose own header cites the same class.
    """
    # A BUY on a lane holding NOTHING: an opening entry.
    assert _submit(_client(0.0), _order(side="BUY", qty=9.0)).sent == ["c-1"], (
        "an opening entry was refused as a broken exit — this blocks all entries"
    )
    # A BUY that ADDS to an existing long: still an entry (absolute exposure grows).
    assert _submit(_client(59.0), _order(side="BUY", qty=60.0)).sent == ["c-1"], (
        "adding to a position was refused as a broken exit"
    )


def test_a_REAL_EXIT_is_still_refused_when_the_lane_holds_nothing():
    """The property the guard exists for must survive the fix. A SELL from a lane holding nothing
    does not close anything — it opens a short on that lane, which is the WHD/CGAU shape."""
    assert _submit(_client(0.0), _order(side="SELL", qty=28.0)).denied


def test_the_guard_uses_the_SHARED_entry_predicate_not_its_own():
    """Pinned structurally as well, because the two answers drifting is the whole defect."""
    from api.exit_ownership import classify_exit as _ce

    # NOT `is_entry`. That predicate was tried and is the WRONG discriminator here: a SELL from a
    # flat lane grows exposure, so it classifies as an entry — and that is exactly the WHD shape this
    # guard exists to refuse. The rule is the RESULTING position on the lane's DECLARED side.
    assert _ce(side="BUY", quantity=9, lane="L", instrument_id="I", lane_signed_qty=0.0).ok
    assert _ce(side="BUY", quantity=60, lane="L", instrument_id="I", lane_signed_qty=59.0).ok
    assert not _ce(side="SELL", quantity=28, lane="L", instrument_id="I", lane_signed_qty=0.0).ok
    assert not _ce(side="SELL", quantity=90, lane="L", instrument_id="I", lane_signed_qty=88.0).ok
