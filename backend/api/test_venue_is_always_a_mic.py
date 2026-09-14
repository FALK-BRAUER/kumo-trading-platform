def test_the_adapter_falls_back_to_the_RAW_EXCHANGE_and_that_is_why_a_symbol_override_is_needed():
    """FIXTURE PROPERTY for everything below. If Nautilus stopped falling back to the raw string,
    the symbol override would be unnecessary and this file would be carrying a workaround for a
    fixed bug. Read from the installed package, which cannot be wrong about our pinned version."""
    import inspect

    from nautilus_trader.adapters.interactive_brokers.providers import (
        InteractiveBrokersInstrumentProvider,
    )

    src = inspect.getsource(InteractiveBrokersInstrumentProvider._resolve_venue_from_exchange)
    assert "return exchange" in src, (
        "nautilus no longer falls back to the raw exchange string — the symbol override may be "
        "unnecessary now; re-measure before keeping it"
    )


def test_the_symbol_override_reaches_BOTH_ibkr_clients(monkeypatch):
    """THE SEAM, and the half that is easy to get wrong: the DATA client and the EXEC client have
    SEPARATE provider objects. #606 shipped a fix wired into only one of them and it changed nothing
    observable. A venue that differs between the two is a split identity — worse than the defect.
    """
    import api.providers.ibkr as mod

    monkeypatch.setenv("KUMO_IBKR_SYMBOL_MIC", "TRT=XASE")
    monkeypatch.setenv("KUMO_IBG_HOST", "127.0.0.1")
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DU0")
    data = mod.build_data({}).config.instrument_provider.symbol_to_mic_venue
    exec_ = mod.build({"account_id_env": "IBKR_ACCOUNT_ID"}).config.instrument_provider.symbol_to_mic_venue
    assert data == {"TRT": "XASE"} == exec_, (
        f"the symbol->MIC override does not reach both clients (data={data}, exec={exec_}) — a venue "
        f"that differs between them splits the instrument's identity (#622)"
    )


def test_an_UNSET_symbol_override_is_EMPTY_not_a_guess(monkeypatch):
    """Nothing is overridden by default. The override exists for an instrument an operator has SEEN;
    shipping a default would be the unverified-guess trap this file already refuses elsewhere.

    Empty and unset are the same, per #581 — compose interpolates an unset variable to "".
    """
    import api.providers.ibkr as mod

    monkeypatch.delenv("KUMO_IBKR_SYMBOL_MIC", raising=False)
    assert mod._symbol_mic_overrides() == {}
    monkeypatch.setenv("KUMO_IBKR_SYMBOL_MIC", "")
    assert mod._symbol_mic_overrides() == {}


def test_a_SHORT_override_key_warns_because_nautilus_matches_it_as_a_PREFIX(caplog, monkeypatch):
    """THE TRAP IN NAUTILUS'S OWN SEAM, pinned by BEHAVIOUR.

    `providers.py:836` matches `contract.symbol.startswith(symbol_prefix)`. So an entry for `TR`
    ALSO captures TRT, TRTX, TRTN — silently re-pointing the venue of instruments the operator never
    intended to touch. That is the same second-identity failure the override exists to fix, arriving
    from the other direction. Nautilus's matching is not ours to change, so the read must warn.

    ASSERTED BY DRIVING IT, not by grepping the source. The first version checked for the word
    "warning" in the function body and DID NOT BITE when the guard was deleted — the malformed-entry
    warning also contains it, so the assertion could not tell the two apart. Same family as matching
    a docstring: the check was satisfied by something adjacent to what it meant.
    """
    import api.providers.ibkr as mod

    monkeypatch.setenv("KUMO_IBKR_SYMBOL_MIC", "TR=XASE")
    with caplog.at_level("WARNING"):
        assert mod._symbol_mic_overrides() == {"TR": "XASE"}      # still honoured — a warning, not a refusal
    assert "PREFIX" in caplog.text.upper(), (
        "a short override key was accepted silently; Nautilus matches it as a prefix, so it also "
        "re-points every symbol starting with those letters (#622)"
    )


def test_a_FULL_LENGTH_key_does_not_warn(caplog, monkeypatch):
    """The other half, or the warning is unconditional noise and an operator learns to ignore it —
    which is how the account-frame warning became wallpaper."""
    import api.providers.ibkr as mod

    monkeypatch.setenv("KUMO_IBKR_SYMBOL_MIC", "TRT=XASE")
    with caplog.at_level("WARNING"):
        mod._symbol_mic_overrides()
    assert "PREFIX" not in caplog.text.upper()


def test_the_hazard_is_documented_where_the_value_is_READ():
    """A future reader must meet the prefix trap at the point of use, not in a ticket."""
    import inspect

    import api.providers.ibkr as mod

    doc = inspect.getdoc(mod._symbol_mic_overrides) or ""
    assert "prefix" in doc.lower(), "the prefix-matching hazard is not documented where it is read"
