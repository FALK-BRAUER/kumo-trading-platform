"""A node that cannot reconcile must have a way back that is not a hand-edited cache (#363).

WHAT HAPPENED, 2026-08-21. A routine restart failed reconciliation and the engine ran on with an EMPTY
BOOK while reporting RUNNING — `/positions` returned 0 against 9 held at the broker, and no strategy
started.

    Rejecting fill that would cause overfill for kumo-cc1c64396a2a6cba243b:
      order.quantity=136, order.filled_qty=136, fill.last_qty=28, would result in filled_qty=164
    Reconciliation for ALPACA failed
    Execution state could not be reconciled

Nautilus is RIGHT to refuse: guessing which side is correct could double a position. The defect is that
there is no path back. Every subsequent restart hits the same state and fails identically — the trap
re-arms itself.

IT WAS NOT CACHE CORRUPTION, WHICH IS WHY #363's RECORDED REMEDY DID NOT APPLY. Both cached orders were
verified correct against Alpaca: the 136-share sell recorded venue `4d22c75d` with three fills totalling
136, the 28-share buy recorded venue `51822980` with one fill of 28, and the broker agreed with both.
Nothing was corrupt. A fill was being attributed to the wrong order INSIDE reconciliation, and the
"decode msgpack and delete the row" procedure had no row to delete.

THE LEVER WAS ALREADY IN NAUTILUS. `LiveExecEngineConfig.filtered_client_order_ids` skips an order
report AND its fills (`live/execution_engine.py:1889`). Reading the config object's fields found in one
minute what an evening of cache archaeology did not — the CLAUDE.md rule, again, and it was not
consulted first.

WHY AN ENV VAR RATHER THAN A CODE EDIT. The whole failure is that recovery required a deploy or a
hand-edited Redis while the book was invisible. #363: "None of that is a procedure anyone should have to
invent at 23:00 while a session is pending." The log already NAMES the offending order, so unblocking a
dead node is now: read the id, set the var, restart.

WHAT IT DOES NOT DO. It does not fix the attribution bug, and it must not be left set — a stale entry
would silently stop reconciling a live order. Skipping an order's reconciliation does NOT skip the
position: `/v2/positions` still reconciles, which is why the book came back whole (15 held) on the boot
that used it.
"""

from __future__ import annotations

import pytest

from api.engine_node import _reconcile_skip_coids


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KUMO_RECONCILE_SKIP_COIDS", raising=False)


def test_unset_means_NO_filtering():
    """The correct default, and the important one. This is an escape hatch, not a setting: a node that
    filters something by default is a node that silently stops reconciling part of the book."""
    assert _reconcile_skip_coids() == []


def test_empty_and_whitespace_are_also_no_filtering():
    """`KUMO_RECONCILE_SKIP_COIDS=` in a compose file is how this looks 99% of the time."""
    import os

    for value in ("", "   ", ",", " , "):
        os.environ["KUMO_RECONCILE_SKIP_COIDS"] = value
        assert _reconcile_skip_coids() == [], f"{value!r} must not filter anything"
    del os.environ["KUMO_RECONCILE_SKIP_COIDS"]


def test_the_REAL_ids_from_the_2026_08_21_outage_parse(monkeypatch):
    """The two orders that actually blocked the boot. Pinned as the values, not as placeholders, so the
    next person meets the shape the log prints."""
    monkeypatch.setenv(
        "KUMO_RECONCILE_SKIP_COIDS",
        "kumo-cc1c64396a2a6cba243b,kumo-d0c9f0db9c7d2caeae01",
    )
    out = [str(c) for c in _reconcile_skip_coids()]
    assert out == ["kumo-cc1c64396a2a6cba243b", "kumo-d0c9f0db9c7d2caeae01"]


def test_whitespace_around_ids_is_tolerated(monkeypatch):
    """Copied out of a log line at 23:00, this will have spaces in it."""
    monkeypatch.setenv("KUMO_RECONCILE_SKIP_COIDS", " kumo-aaa , kumo-bbb ")
    assert [str(c) for c in _reconcile_skip_coids()] == ["kumo-aaa", "kumo-bbb"]


def test_it_returns_ClientOrderId_not_strings(monkeypatch):
    """`filtered_client_order_ids` is compared with `in` against `order_report.client_order_id`, which is
    a `ClientOrderId`. A list of strings would never match and would filter NOTHING — a fix that looks
    applied, changes no behaviour, and leaves the node dead. That is the shape of half the defects in
    this repo."""
    from nautilus_trader.model.identifiers import ClientOrderId

    monkeypatch.setenv("KUMO_RECONCILE_SKIP_COIDS", "kumo-aaa")
    out = _reconcile_skip_coids()
    assert all(isinstance(c, ClientOrderId) for c in out)
    assert ClientOrderId("kumo-aaa") in out


def test_the_config_ACTUALLY_passes_them_to_nautilus():
    """THE SEAM. A correct parser wired to nothing leaves the node exactly as dead. `_durable_configs`
    is what Nautilus reads."""
    import inspect

    from api import engine_node

    src = inspect.getsource(engine_node._durable_configs)
    assert "filtered_client_order_ids=_reconcile_skip_coids()" in src


def test_the_operator_is_TOLD_when_a_filter_is_active(caplog, monkeypatch):
    """A silent filter is worse than no filter: it stops reconciling an order and says nothing, which is
    the same class of quiet-wrong-state the outage was. It must be loud, and it must say the book still
    reconciles so nobody reads it as data loss."""
    monkeypatch.setenv("KUMO_RECONCILE_SKIP_COIDS", "kumo-aaa")
    with caplog.at_level("DEBUG"):
        _reconcile_skip_coids()

    # AT WARNING, not debug. Captured at DEBUG deliberately so the assertion is about the RECORD'S
    # LEVEL rather than about what the capture happened to let through — a first version used
    # `at_level("WARNING")` and passed with the call downgraded to `_log.debug`, which is exactly the
    # silent filter this test exists to forbid.
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "SKIPPING" in r.getMessage()]
    assert warnings, f"the filter must announce itself at WARNING; saw {[(r.levelname, r.getMessage()[:40]) for r in caplog.records]}"
    text = warnings[0].getMessage()
    assert "kumo-aaa" in text
    assert "positions still reconcile" in text
