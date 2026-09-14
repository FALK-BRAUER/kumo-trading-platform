"""Re-derive every SOURCE fact from the installed package, on every run.

A venue catalogue that is only ever READ rots into a comment nobody trusts. These tests make an
upgrade that changes the venue underneath us fail loudly and say which fact moved.

They read the INSTALLED package rather than documentation or memory, because the installed package
cannot be wrong about our pinned version — the same rule that found `reduce_only` missing from the IB
adapter while ten other adapters implement it.
"""
from __future__ import annotations

import pathlib

import nautilus_trader
import pytest

from api.venues.facts import ALPACA, IBKR, VENUES, Provenance

_NAUTILUS = pathlib.Path(nautilus_trader.__file__).parent
_IB = _NAUTILUS / "adapters" / "interactive_brokers"
_COCKPIT = pathlib.Path(__file__).resolve().parents[1]


def _read(path: pathlib.Path) -> str:
    return path.read_text(errors="ignore")


# ---------------------------------------------------------------------------------------------
# The catalogue's own shape. A scan that cannot see anything passes every assertion below.
# ---------------------------------------------------------------------------------------------

def test_the_installed_adapter_is_actually_present():
    """Fixture property first: without these files every grep below matches nothing and passes."""
    assert _IB.exists(), f"IB adapter not installed at {_IB} — every IBKR fact here is unchecked"
    assert (_NAUTILUS / "risk" / "engine.pyx").exists()
    assert (_COCKPIT / "providers" / "alpaca" / "exec_client.py").exists()


@pytest.mark.parametrize("venue", sorted(VENUES))
def test_every_fact_carries_provenance_and_evidence(venue):
    """A fact with no evidence cannot be rechecked, which makes it an opinion."""
    for f in VENUES[venue].facts:
        assert isinstance(f.how, Provenance)
        assert f.evidence.strip(), f"{venue}.{f.key} has no evidence — how would anyone recheck it?"


# ---------------------------------------------------------------------------------------------
# IBKR
# ---------------------------------------------------------------------------------------------

def test_ib_still_maps_NetLiquidation_onto_balance_total():
    """If this moves, `_TOTAL_IS_NET_LIQUIDATION` in engine_node is publishing the wrong number as
    equity — on the channel the daily-loss halt anchors on."""
    src = _read(_IB / "execution.py")
    assert 'self._account_summary[currency]["NetLiquidation"]' in src, (
        "IB no longer maps NetLiquidation into AccountBalance.total — recheck IBKR.balance_total_is "
        "and the allow-list in engine_node._TOTAL_IS_NET_LIQUIDATION")
    assert IBKR["balance_total_is"] == "net_liquidation"


def test_ib_still_maps_FullAvailableFunds_onto_balance_free():
    src = _read(_IB / "execution.py")
    assert 'self._account_summary[currency]["FullAvailableFunds"]' in src
    assert IBKR["balance_free_is"] == "full_available_funds"


def test_ib_STILL_does_not_support_reduce_only():
    """The fact with the sharpest consequence: not supported AND not refused.

    If IB ever gains it, the naked-short exposure closes and `position_id` on submit stops being the
    only protection — which is worth knowing the day it happens rather than a year later.
    """
    hits = [p.name for p in _IB.rglob("*.py") if "reduce_only" in _read(p)]
    assert hits == [], (
        f"IB now references reduce_only in {hits} — IBKR.reduce_only_supported says False. Recheck "
        f"whether it is honoured or merely parsed.")
    assert IBKR["reduce_only_supported"] is False


def test_other_adapters_DO_implement_reduce_only():
    """The discriminating half. Without it, a grep that matches nothing anywhere would 'prove' the
    fact above while actually proving the search is broken."""
    others = {}
    for name in ("okx", "bybit", "kraken"):
        d = _NAUTILUS / "adapters" / name
        if d.exists():
            others[name] = sum(1 for p in d.rglob("*.py") if "reduce_only" in _read(p))
    assert others, "no comparison adapters installed — this test cannot discriminate"
    assert any(n > 0 for n in others.values()), (
        f"no adapter implements reduce_only ({others}) — the search itself is broken, and the IB fact "
        f"above is therefore unproven rather than true")


def test_ib_still_truncates_order_ref_at_the_last_colon():
    src = _read(_IB / "execution.py")
    assert 'rsplit(":", 1)[0]' in src, (
        "IB no longer truncates orderRef — any client_order_id format carrying meaning after a colon "
        "should be rechecked before it is trusted")
    assert IBKR["order_ref_truncated_at_last_colon"] is True


# ---------------------------------------------------------------------------------------------
# ALPACA — our own client, which is the one that can change without an upgrade
# ---------------------------------------------------------------------------------------------

def test_alpaca_now_puts_NET_LIQUIDATION_in_balance_total():
    """CHANGED BY #588. This assertion used to read `total=cash` and was the premise of the 28.0%
    understatement argument. The premise moved: our connector now writes `portfolio_value`, matching
    Nautilus's IBKR adapter, because this field is what the cockpit reads back as equity.

    Source-pinned rather than remembered, and pinned on the SEMANTICS not the spelling — the
    behavioural assertion lives in `providers/alpaca/test_account_state_reports_equity.py`, which
    drives the real `_report_account_state`. This one only guarantees the fact table stays honest.
    """
    src = _read(_COCKPIT / "providers" / "alpaca" / "exec_client.py")
    assert "AccountBalance(total=cash," not in src, (
        "Alpaca is writing CASH into AccountBalance.total again — that is #588, and it makes the "
        "Home equity curve plot the cash balance while staging stays correct")
    assert 'raise ValueError(' in src and "portfolio_value" in src, (
        "the equity read lost its no-fallback guard; reporting cash as equity must RAISE, not degrade")
    assert ALPACA["balance_total_is"] == "net_liquidation"


# ---------------------------------------------------------------------------------------------
# Nautilus itself — the behaviours our doubles kept getting wrong
# ---------------------------------------------------------------------------------------------

def test_portfolio_equity_still_returns_a_mapping_not_a_scalar():
    """The lie that made a whole branch dead on every venue: `float(dict)` raises into a debug log."""
    from nautilus_trader.portfolio.portfolio import Portfolio

    doc = Portfolio.equity.__doc__ or ""
    assert "dict[Currency, Money]" in doc, (
        "Portfolio.equity's return contract moved — every double returning a scalar is now lying")


def test_reduce_only_is_enforced_only_when_a_position_id_is_supplied():
    """Why `reduce_only=True` alone protects nothing: the RiskEngine's check is conditional.

    This is what made IB's missing adapter support fatal rather than merely untidy — nothing else was
    going to catch an oversized exit.
    """
    src = _read(_NAUTILUS / "risk" / "engine.pyx")
    i = src.find("Check reduce only")
    assert i != -1, "the reduce-only guard moved in risk/engine.pyx — re-derive this"
    window = src[i : i + 600]
    assert "command.position_id is not None" in window, (
        "the RiskEngine no longer gates its reduce-only check on position_id — if it now applies "
        "unconditionally, kumo-strategies' submit no longer needs it and that changes the exposure")
