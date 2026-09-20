"""The account frame must reach the STRATEGIES, not just the UI.

`_publish` enqueues to the Redis writer for the UI plane. The rotation strategies read their
`_broker_account` from the MSGBUS topic `broker.account`, whose only publisher is
`providers/alpaca/exec_client.py:460`. On an IBKR node nothing publishes it, so:

    _broker_account stays None
      -> broker_equity() raises "no broker account snapshot on 'broker.account' yet"
      -> the session dies before forming an order

Measured on ibkr-paper-retired 2026-08-24, AFTER the account frame itself was fixed: `GET /account` returned
a real equity while the boot gate reported both lanes DEGRADED with
`equity: raised: AttributeError("'NoneType' object has no attribute 'equity'")`. The UI had the number
and the strategies did not.
"""
from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.currencies import USD

from api.engine_node import UiFeedStrategy

CASH = 990_214.30
NET_LIQ = 1_000_015.96


class _IBAccount:
    id = SimpleNamespace(get_issuer=lambda: "INTERACTIVE_BROKERS")

    def balances_total(self):
        return {USD: NET_LIQ}

    def balances_free(self):
        return {USD: CASH}

    def balance_free(self, ccy=None):
        return CASH


def _strat():
    published: list[tuple[str, dict]] = []
    bus: list[tuple[str, dict]] = []
    s = SimpleNamespace(
        _run_boot_gate_once=lambda _e: None,
        _broker_account=None,
        _equity_estimate_warned=False,
        _foreign_equity_warned=False,
        portfolio=SimpleNamespace(equity=lambda *_a, **_k: {}),
        cache=SimpleNamespace(accounts=lambda: [_IBAccount()], positions_open=lambda: [object()]),
        clock=SimpleNamespace(timestamp_ns=lambda: 1),
        log=SimpleNamespace(warning=lambda *a, **k: None),
        msgbus=SimpleNamespace(publish=lambda topic, payload: bus.append((topic, payload))),
    )
    s._publish = lambda kind, payload, **kw: published.append((kind, payload))
    return s, published, bus


def test_the_fixture_publishes_a_ui_frame_so_a_bus_failure_is_not_a_setup_failure():
    """Fixture property first: the UI half already works, and this file is only about the bus half."""
    s, published, _bus = _strat()
    UiFeedStrategy._publish_account(s)
    assert [p for kind, p in published if kind == "account"], "the UI frame is the precondition"


def test_the_derived_account_is_published_on_the_MSGBUS_for_the_strategies():
    s, _published, bus = _strat()
    UiFeedStrategy._publish_account(s)
    topics = [t for t, _p in bus]
    assert "broker.account" in topics, (
        "strategies read `broker.account` off the bus; a UI-only frame leaves broker_equity() raising")
    payload = dict(next(p for t, p in bus if t == "broker.account"))
    assert payload["equity"] == NET_LIQ


def test_our_own_snapshot_is_NOT_ingested_back_into_the_fallback():
    """Otherwise equity FREEZES at the first value, and the daily-loss anchor never moves again.

    `_on_broker_account` caches whatever arrives on the topic, and `_publish_account` returns early
    from that cache. Publishing our own derivation onto the same topic would make the next tick read
    it back, take the cached branch, and never recompute — so a changing account would report its
    first snapshot forever. Live, that is a daily-loss halt anchored to a number from boot.
    """
    s, _published, bus = _strat()
    UiFeedStrategy._publish_account(s)
    payload = next(p for t, p in bus if t == "broker.account")
    UiFeedStrategy._on_broker_account(s, payload)
    assert s._broker_account is None, (
        "the node ingested its own derived snapshot — equity will freeze at the first value")


def test_a_REAL_broker_snapshot_is_still_ingested():
    """The discriminating half: ignoring everything on that topic would break the Alpaca stack, whose
    exec client is the legitimate publisher."""
    s, _published, _bus = _strat()
    real = {"equity": 103_500.35, "cash": 73_393.33, "buying_power": 4.0, "multiplier": 4.0}
    UiFeedStrategy._on_broker_account(s, real)
    assert s._broker_account == real
