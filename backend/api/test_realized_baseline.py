"""The realized-reconciliation baseline must be a FACT, not a guess (#651 item 7).

`ACCOUNT_BASELINE_EQUITY` defaulted to "100000" — the Alpaca paper default — and only an explicit 0
disabled the check. On any account that did not start at exactly $100,000 (ibkr-paper-retired starts in
SGD, a funded live account starts wherever it starts), the guard #345 asked for compared two
derivations against a constant that was never true and disagreed by that constant forever: an alarm
calibrated by default rather than by fact, which is the alarm nobody reads.

Untold now means DISABLED-and-said-so: the check arms only when the instance declares its baseline
(kumo-trading-platform/instances owns that; paper must set ACCOUNT_BASELINE_EQUITY=100000 to keep it armed).
Absence must not be readable as a number.
"""

from __future__ import annotations

import logging
import os

from api.engine_node import _ACCOUNT_BASELINE, _baseline_from_env


def test_untold_means_disabled_not_100000(caplog):
    """No env var -> the check is OFF, loudly — not armed against a guessed opening balance."""
    with caplog.at_level(logging.WARNING, logger="api.engine_node"):
        assert _baseline_from_env(None) == 0.0
    assert any("ACCOUNT_BASELINE_EQUITY" in r.message for r in caplog.records), (
        "disabling silently is the same trap one level up — the degraded state must announce itself"
    )


def test_compose_empty_string_is_also_untold():
    """docker compose interpolates an unset variable to the EMPTY STRING (#581) — that is 'never
    told us', not a value."""
    assert _baseline_from_env("") == 0.0
    assert _baseline_from_env("   ") == 0.0


def test_a_declared_baseline_travels():
    """A value the old default could not produce, so agreement cannot mask a severed wire."""
    assert _baseline_from_env("12345") == 12345.0


def test_a_malformed_baseline_refuses_at_boot():
    """Neither a guess nor a silent zero: an operator who set the knob to garbage must hear about
    it before the engine trades, and the error must name the input."""
    import pytest

    with pytest.raises(ValueError, match="ACCOUNT_BASELINE_EQUITY"):
        _baseline_from_env("one hundred thousand")


def test_the_module_constant_is_wired_to_the_env_not_to_a_literal():
    """The seam: this test env does not set the var, so the module must have booted DISABLED.
    On main it booted armed at 100,000 — a number nothing had told it."""
    assert "ACCOUNT_BASELINE_EQUITY" not in os.environ, (
        "fixture property: this environment must not set the knob, or this test says nothing"
    )
    assert _ACCOUNT_BASELINE == 0.0, (
        f"_ACCOUNT_BASELINE booted as {_ACCOUNT_BASELINE!r} with no env var set — the realized "
        f"reconciliation is armed against a guessed opening balance"
    )
