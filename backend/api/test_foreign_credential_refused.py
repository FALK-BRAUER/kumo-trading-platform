"""An instance must not boot holding a credential for a venue it does not trade (#756).

A CREDENTIAL THAT IS PRESENT IS A CREDENTIAL THAT WILL BE USED. That is not a slogan here, it is a
measurement: the moment ibkr-paper-retired had an Alpaca key, the realized-P&L sweep polled an Alpaca
brokerage account holding none of its positions — 45 calls against 12 for market data (#573). Nothing
selected that behaviour; the key being present was enough, because code that gates on "do we have a
client" finds one.

WHY THE EXISTING PROTECTION IS NOT ENOUGH. `bin/resolve-secrets.sh` in the instances repo unsets every
known secret and supplies only what an instance's manifest names, and staging's manifest has no Alpaca
entry. But that runs inside `make up` ALONE, and `compose.paper.yml` interpolates the credential BARE
(`${APCA_API_KEY_ID}`, four places) rather than with a default. So a `docker compose up` from a shell
that happens to carry those keys hands an IBKR instance an Alpaca credential, and nothing refuses.

Not hypothetical: that bypass was performed on the paper stack on 2026-08-31 — the operator did not
know `alpaca-paper` IS the `kumo-paper` project — and it disarmed the instance for ~35 minutes. Aimed
at staging with the same shell, it would have booted holding the key.

THREE STATES, AND THE THIRD IS THE POINT. Configured for this venue / not configured / handed a
credential for a venue it does not trade. The third currently reads as the first.
"""

from __future__ import annotations

import pytest

from api.engine_node import refuse_foreign_credentials


def test_an_IBKR_instance_holding_an_ALPACA_key_is_REFUSED_by_name():
    """The #573 shape. Refusing must name the variable, or an operator sees "misconfigured" and has
    to guess which of forty is wrong."""
    with pytest.raises(RuntimeError) as e:
        refuse_foreign_credentials("ibkr", {"APCA_API_KEY_ID": "PKTEST", "APCA_API_SECRET_KEY": "s"})
    msg = str(e.value)
    assert "APCA_API_KEY_ID" in msg, msg
    assert "ibkr" in msg.lower(), msg


def test_an_ALPACA_instance_holding_its_OWN_key_boots():
    """The guard must not break the instance the credential belongs to — which is the whole book on
    the paper stack."""
    refuse_foreign_credentials("alpaca", {"APCA_API_KEY_ID": "PKTEST", "APCA_API_SECRET_KEY": "s"})


def test_an_IBKR_instance_with_NO_alpaca_key_boots():
    """Staging's correct state, and it must stay cheap — this is every boot."""
    refuse_foreign_credentials("ibkr", {"KUMO_EXEC": "ibkr"})


def test_an_EMPTY_STRING_credential_is_ABSENT_not_present():
    """Compose interpolates an UNSET variable to the EMPTY STRING, so `APCA_API_KEY_ID=""` is what a
    correctly-configured IBKR instance actually looks like from inside the container — measured:
    staging's engine reports len=1 for both, which is the empty value plus a newline.

    Treating that as "present" would refuse every healthy IBKR boot, which is the failure that gets a
    guard deleted rather than fixed."""
    refuse_foreign_credentials("ibkr", {"APCA_API_KEY_ID": "", "APCA_API_SECRET_KEY": ""})


def test_a_WHITESPACE_credential_is_also_absent():
    """A value that is only whitespace is not a credential, and a guard that refuses on it fails the
    same healthy boot one character over."""
    refuse_foreign_credentials("ibkr", {"APCA_API_KEY_ID": "   \n"})


def test_the_provider_NONE_case_is_still_guarded():
    """`exec_provider` can be "none" — a display-only node. It trades no venue at all, so an Alpaca
    credential is foreign there too, and "none" must not read as "anything goes"."""
    with pytest.raises(RuntimeError):
        refuse_foreign_credentials("none", {"APCA_API_KEY_ID": "PKTEST"})


def test_the_REFUSAL_IS_WIRED_INTO_build_node_not_merely_available():
    """A guard nothing calls is the class this repo keeps paying for — an orphan that reads as
    coverage. It must run on the path a container actually boots through, and `build_node` is the one
    seam every boot crosses regardless of how the container was started."""
    import ast
    import inspect
    import pathlib

    from api import engine_node

    tree = ast.parse(pathlib.Path(inspect.getfile(engine_node)).read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "build_node"), None)
    assert fn is not None, "build_node moved — this test is blind"
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "refuse_foreign_credentials" in calls, (
        "build_node does not call the guard, so a container started outside `make up` boots with "
        "whatever credentials the shell happened to carry"
    )
