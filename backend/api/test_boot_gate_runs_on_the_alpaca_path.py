"""The boot gate must run on the tenant that TRADES, not only on the one with no broker frame (#515).

WHAT THIS PINS, and why the existing wiring test could not.

`test_boot_gate_is_wired.py` asserts, via AST, that `run_boot_gate` has a production caller and that
the caller is reached from `_publish_account`. Every one of those assertions is TRUE. The gate is
still dead on Alpaca, because `_publish_account` opens with:

    broker = self._broker_account
    if broker is not None:
        self._publish("account", {...})
        return                          # <-- the gate is 120 lines BELOW this

On an Alpaca node the exec client publishes `broker.account` to the msgbus, so `_broker_account` is
populated and the publisher returns here on every single tick. On an IBKR node NOTHING publishes that
topic — `engine_node.py` says so in its own comment — so `_broker_account` stays None, the fallback
derivation runs, and the gate fires. The gate was wired into the FALLBACK branch.

So the boot gate ran only where there was no broker account frame. alpaca-paper, the tenant that does
most of the trading, has never run it. ibkr-paper-retired ran it and reported DEGRADED twice, and those
staging verdicts were quoted as evidence during the 2026-08-24 outage — a gate covering one tenant
was read as covering both.

**AN AST TEST CANNOT SEE A `return`.** It walks the syntax tree, so a call that is lexically present
and statically reachable reads as wired no matter what control flow does before it. That is the class
here, not this one gate: any static reachability test in this package is blind the same way. This
file DRIVES the publisher instead, on both branches, and asks whether the gate actually ran.

It is also `agreement-is-not-connection` exactly: the gate exists, is enabled, is unit-tested, is
mutation-bitten, and has a green test named `..._IS_WIRED`. Every one of those agreed with "the gate
runs". None of them was connected to the branch that matters.

WHY NO EXISTING TEST CAUGHT IT: every `_publish_account` test in this package sets
`_broker_account=None`. The broker branch — the production path on Alpaca — had no test at all.
"""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.currencies import USD

from api.engine_node import UiFeedStrategy

BROKER_EQUITY = 103_500.35
FALLBACK_CASH = 50_000.00


class _Account:
    """The Nautilus account the FALLBACK branch reads, in the shape cockpit's Alpaca client builds it.

    Flat (`positions_open() == []`), so the #382 refusal does not fire and the fallback actually
    reaches the gate. A double that returned no accounts would make `_publish_account` return three
    lines in and the fallback assertion below would pass for the wrong reason — it would be measuring
    an early return, not the gate.
    """

    id = SimpleNamespace(get_issuer=lambda: "ALPACA")

    def balances_free(self):
        return {USD: FALLBACK_CASH}

    def balance_free(self, _ccy=None):
        return FALLBACK_CASH

    def balance_total(self, _ccy=None):
        return FALLBACK_CASH


def _node(*, broker_account):
    """A host in the shape production hands `_publish_account`, recording boot-gate calls.

    THE DOUBLE MUST REJECT WHAT PRODUCTION REJECTS. `_run_boot_gate_once` records rather than
    no-ops, because a double that silently accepts the call cannot tell "ran" from "never reached" —
    which is the entire question here.
    """
    gate_calls: list = []
    published: list = []

    node = SimpleNamespace(
        _run_boot_gate_once=lambda equity: gate_calls.append(equity),
        _broker_account=broker_account,
        _equity_estimate_warned=False,
        _foreign_equity_warned=False,
        # Reached only on the fallback branch; on the broker branch it must never be consulted.
        cache=SimpleNamespace(accounts=lambda: [_Account()], positions_open=list),
        clock=SimpleNamespace(timestamp_ns=lambda: 1),
        msgbus=SimpleNamespace(publish=lambda _t, _p: None),
        log=SimpleNamespace(warning=lambda *a, **k: None),
    )
    node._publish = lambda kind, payload, **kw: published.append((kind, payload))
    return node, gate_calls, published


def _alpaca_frame() -> dict:
    """What the Alpaca exec client puts on `broker.account`, which is what makes `_broker_account`
    non-None on alpaca-paper and None on ibkr-paper-retired."""
    return {
        "equity": BROKER_EQUITY,
        "cash": 73_393.33,
        "buying_power": 146_786.66,
        "multiplier": 2.0,
        "long_market_value": 30_107.02,
        "last_equity": 102_900.00,
        "ts": 1,
    }


def test_the_fixture_actually_takes_the_broker_branch():
    """THE FIXTURE'S OWN PROPERTY FIRST.

    If this double fell through to the fallback derivation, the assertion below would be measuring
    the branch that was never broken, and would pass with the defect fully present. Prove the broker
    frame is what got published before trusting any verdict about the gate.
    """
    node, _gate, published = _node(broker_account=_alpaca_frame())

    UiFeedStrategy._publish_account(node)

    assert published, "nothing published — the double does not reach either branch"
    kind, payload = published[-1]
    assert kind == "account"
    assert payload["equity"] == BROKER_EQUITY, (
        "the published frame did not come from the broker account — this fixture is on the fallback "
        "branch and cannot see the defect"
    )
    assert payload["long_market_value"] == 30_107.02, "not the broker's own fields"


def test_the_boot_gate_RUNS_when_a_broker_account_frame_exists():
    """THE DEFECT. Alpaca populates `_broker_account`, so the publisher returns before the gate.

    Without the fix this list is empty: the gate has never run on alpaca-paper, for its whole life.
    """
    node, gate_calls, _pub = _node(broker_account=_alpaca_frame())

    UiFeedStrategy._publish_account(node)

    assert gate_calls, (
        "the boot gate did not run on a node WITH a broker account frame. `_publish_account` returns "
        "inside `if broker is not None:` and the gate sits below that return, so on Alpaca — the "
        "tenant that trades — preflight has never run at boot (#515)"
    )
    assert gate_calls[0] == BROKER_EQUITY, (
        f"the gate ran with {gate_calls[0]!r}, not the broker's equity. Preflight sizes its probes "
        f"off this number; handing it the wrong one degrades lanes for a reason about the caller"
    )


def test_the_boot_gate_STILL_runs_on_a_node_with_no_broker_frame():
    """The other direction — ibkr-paper-retired, where the gate already worked. Hoisting the call must not
    cost the branch that had it, and `should_run`'s once-guard must not be spent twice per tick."""
    node, gate_calls, _pub = _node(broker_account=None)

    UiFeedStrategy._publish_account(node)

    assert gate_calls, "the fallback branch lost the boot gate"


def test_the_gate_is_not_invoked_twice_for_one_account_update():
    """`should_run` makes the gate fire once per NODE, but it is a guard, not a licence to call the
    helper twice per tick: each call is a broker probe per lane. One update, one invocation."""
    node, gate_calls, _pub = _node(broker_account=_alpaca_frame())

    UiFeedStrategy._publish_account(node)

    assert len(gate_calls) == 1, (
        f"the gate was invoked {len(gate_calls)} times for a single account update — the call was "
        f"added to the broker branch without removing it from the path below"
    )
