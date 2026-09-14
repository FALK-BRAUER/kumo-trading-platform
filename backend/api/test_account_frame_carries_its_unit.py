"""An account figure must carry its CURRENCY across the seam (#518).

WHAT REACHES THE UI FROM staging-ibkr, whose Nautilus object is unambiguous:

    AccountState(account_id=INTERACTIVE_BROKERS-DUPTEST02, base_currency=None,
                 balances=[AccountBalance(total=999_215.19 SGD, ...)])

    -> {"equity": 999215.3, "cash": 990135.8, "long_market_value": 0.0, "ts": ...}

Rendered beside USD position values. `base_currency=None` means Nautilus treats the account as
multi-currency and converts NOTHING, so 999,215 SGD sits next to dollar positions with no unit on it.

THE SGD DENOMINATION IS INTENDED AND THIS IS NOT ABOUT CHANGING IT. The operator confirmed the paper account
is SGD by design. This is about the number losing its unit on the way to the operator.

WHERE IT IS LOST, precisely: `_single_currency_amount` exists to CHOOSE a currency — USD when
present, otherwise the single unambiguous leg — and then returns the bare float, discarding which one
it picked. A helper whose entire job is selecting a unit is the one place that must not drop it.

`realized_pnl` on the same model is already currency-aware (a `"0.00 USD"` Money string), so the
codebase knows how to do this.

AND THE DTO IS THE OTHER HALF. `AccountDTO`'s own docstring, four lines below the field it warns
about, says "a Pydantic model DROPS keys it does not declare" — written after `last_equity` was
published, reached Redis, and still rendered "unknown". That is now the third field lost to this
exact seam, so a currency the engine publishes and the model does not declare would vanish the same
way and this file would prove nothing.

kumo-strategies' caution, which bounds what this closes: a currency field that nothing COMPARES is a
third number nobody reads. So the frame's unit and the warning the engine already logs are asserted
to be two derivations of ONE fact — if they can disagree, one of them is lying.
"""

from __future__ import annotations

from types import SimpleNamespace

from nautilus_trader.model.currencies import SGD, USD

from api.engine_node import UiFeedStrategy, _single_currency_amount

SGD_TOTAL = 999_215.19


def test_the_fixture_reproduces_the_SGD_ONLY_account():
    """THE FIXTURE'S OWN PROPERTY FIRST. A USD leg anywhere in this map and the whole file is about a
    path production does not take on that stack."""
    balances = {SGD: SGD_TOTAL}
    assert USD not in balances, "the fixture has a USD leg — it cannot represent the IB paper account"
    assert _single_currency_amount(balances, prefer=USD) == SGD_TOTAL, (
        "the single-leg rule no longer applies, so nothing would be published at all here")


def test_the_helper_REPORTS_WHICH_CURRENCY_it_picked():
    """THE DEFECT, at its source. Choosing a unit and returning only the number destroys it."""
    from api.engine_node import _single_currency_amount_and_ccy

    amount, ccy = _single_currency_amount_and_ccy({SGD: SGD_TOTAL}, prefer=USD)
    assert amount == SGD_TOTAL
    assert str(ccy) == "SGD", (
        f"the helper picked a currency and reported {ccy!r}. It selects between legs — USD when "
        f"present, otherwise the single unambiguous one — so it is the ONLY place that knows which "
        f"unit the published float is in"
    )

    amount, ccy = _single_currency_amount_and_ccy({USD: 100.0, SGD: 5.0}, prefer=USD)
    assert (amount, str(ccy)) == (100.0, "USD"), "the USD preference stopped being reported"

    assert _single_currency_amount_and_ccy({USD: 1.0, SGD: 2.0}, prefer=None) == (None, None), (
        "two legs and no preference is a GUESS — it must stay unknown, and unknown has no currency")


def test_the_published_account_frame_NAMES_ITS_CURRENCY():
    """The seam. A float on this channel with no unit is what #518 is."""
    published: list = []

    class _Account:
        id = SimpleNamespace(get_issuer=lambda: "INTERACTIVE_BROKERS")

        def balances_free(self):
            return {SGD: SGD_TOTAL}

        def balance_total(self, _ccy=None):
            return SGD_TOTAL

    node = SimpleNamespace(
        _run_boot_gate_once=lambda _e: None, _broker_account=None,
        _equity_estimate_warned=False, _foreign_equity_warned=False,
        cache=SimpleNamespace(accounts=lambda: [_Account()], positions_open=list),
        clock=SimpleNamespace(timestamp_ns=lambda: 1),
        msgbus=SimpleNamespace(publish=lambda _t, _p: None),
        log=SimpleNamespace(warning=lambda *a, **k: None),
        portfolio=SimpleNamespace(equity=lambda *a, **k: None),
    )
    node._publish = lambda kind, payload, **kw: published.append((kind, payload))

    UiFeedStrategy._publish_account(node)

    assert published, "nothing published — this fixture cannot see the frame"
    _kind, payload = published[-1]
    assert payload.get("currency") == "SGD", (
        f"the account frame carries no currency: {sorted(payload)}. 999,215 SGD renders beside USD "
        f"positions as a bare float, which is #518"
    )


def test_the_DTO_DECLARES_currency_so_pydantic_cannot_drop_it():
    """THE THIRD FIELD LOST TO THIS SEAM would be the point of the exercise.

    `last_equity` was published, reached Redis, and rendered "unknown" because the model did not
    declare it. Publishing a currency the DTO drops would reproduce that exactly, and every other
    assertion in this file would still pass.
    """
    from api.models import AccountDTO

    assert "currency" in AccountDTO.model_fields, (
        "AccountDTO does not declare `currency`, so Pydantic filters it out at this boundary — the "
        "same way it ate `last_equity`, which its own docstring warns about four lines below"
    )

    dto = AccountDTO(equity=SGD_TOTAL, cash=1.0, buying_power=1.0, ts=1, currency="SGD")
    assert dto.model_dump()["currency"] == "SGD", "declared but not carried through a round trip"


def test_currency_is_OPTIONAL_so_a_frame_without_one_is_not_a_crash():
    """Every producer must not have to be updated at once, and unknown is an answer.

    NOT a default of "USD": a wrong unit stated confidently is worse than an absent one, which is the
    same reasoning `last_equity` uses for defaulting to None rather than 0.0.
    """
    from api.models import AccountDTO

    dto = AccountDTO(equity=1.0, cash=1.0, buying_power=1.0, ts=1)
    assert dto.currency is None, (
        f"currency defaults to {dto.currency!r}. A default of USD would label an SGD account as "
        f"dollars — a plausible wrong unit, which is worse than no unit"
    )
