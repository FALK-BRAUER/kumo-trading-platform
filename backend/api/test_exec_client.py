
import pytest


# ==================================================================================================
# THE broker.account SNAPSHOT IS A CONTRACT, AND ITS CONSUMERS LIVE IN ANOTHER REPO (2026-08-21).
#
# QC345-003 submitted ZERO orders for the third consecutive live session. Not a crash this time:
#
#     enter INTC: sizing yielded 0 shares at 90.32     <- a $90 stock, against $103,428 of equity
#
# `_equity_per_position` is `self._broker.equity() or 0.0`, which bottoms out in
# `QC345RotationStrategy.broker_equity` reading the snapshot THIS MODULE publishes:
#
#     qc345_rotation.py:726   value = self._broker_account.get("portfolio_value")
#     momentum_rotation.py:460 equity = float(self._broker_account.get("equity") or 0.0)
#
# The publisher below reads Alpaca's `portfolio_value` and republishes it under the name `equity`.
# There is no `portfolio_value` key on the message. MOMENTUM reads the name that exists; QC345 reads
# the name it was renamed FROM, gets None on every tick, and sizes every entry to zero shares.
#
# This is the second cause of one symptom. Making `broker_equity` a method (kumo-trading-strategies ccea7b4)
# fixed the TypeError that killed the same session earlier the same day; the fix moved the failure one
# step later rather than removing it, because nothing forced the two ends of this topic to agree about
# a field name. The publisher owns the contract, so the test lives here.
# ==================================================================================================
def test_the_account_snapshot_publishes_the_keys_it_is_documented_to_publish():
    """Pins the SHAPE at the publisher. A consumer in another repo cannot be imported reliably from
    this suite, so the guard has two halves: this one fixes the published names, and the one below
    checks the installed consumers against them when they are importable."""
    import inspect
    import re

    from api.providers.alpaca import exec_client

    src = inspect.getsource(exec_client.AlpacaExecutionClient._report_account_state)
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    published = set(re.findall(r'"([a-z_]+)"\s*:', code))
    for required in ("equity", "cash", "buying_power", "last_equity"):
        assert required in published, f"{required!r} is no longer published on {exec_client._ACCOUNT_TOPIC}"
    assert "portfolio_value" not in published, (
        "a `portfolio_value` key appeared on the snapshot — if that is deliberate, QC345's reader is "
        "no longer broken and this test plus its sibling need rewriting rather than silencing"
    )


def test_no_strategy_reads_a_broker_account_KEY_WE_DO_NOT_PUBLISH():
    """AIMED AT THE CLASS, ACROSS THE REPO BOUNDARY.

    LIVE GUARD, no longer an xfail. It was marked xfail(strict) when both QC lanes read
    `portfolio_value`; kumo-trading-strategies b0593af corrected them and the marker XPASSed, which is the
    whole point of strict — it demanded its own removal instead of quietly going green.

    The question is not "is QC345 fixed" but "which other consumer is reading a name that does not
    exist on this message". Both ends passed their own suites for months while disagreeing about a
    dictionary key, because nothing tested the pair.
    """
    import inspect
    import pathlib
    import re

    from api.providers.alpaca import exec_client

    src = inspect.getsource(exec_client.AlpacaExecutionClient._report_account_state)
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    published = set(re.findall(r'"([a-z_]+)"\s*:', code))

    # SCAN WHAT SHIPS, AND NEVER FALL BACK TO THE VENV (2026-08-22, third pass).
    #
    # First version read the venv's `kumo_strategies` — a snapshot frozen at install time. When the
    # upstream fix landed (b0593af) the venv still held the old files, so this kept failing and would
    # have failed identically had nothing been fixed at all.
    #
    # Second version preferred the sibling checkout but STILL FELL BACK to the venv, which put the same
    # defect back one level down: run from a detached worktree the sibling path does not resolve, the
    # fallback fires, and it fails on a stale copy nobody ships. That is exactly what turned main red
    # the moment these branches merged and the suite ran somewhere other than the working checkout.
    #
    # `deploy/Dockerfile.backend` builds with `COPY --from=strategies .` off the SIBLING CHECKOUT, so
    # that tree is the only one whose contents mean anything. If it cannot be found, SKIP. Asserting
    # against the venv is asserting against something that is not the artifact, and a check that fails
    # for environmental reasons teaches people to ignore it.
    here = pathlib.Path(__file__).resolve()
    root = next(
        (c for c in (p.parent / "kumo-trading-strategies" / "src" / "kumo_strategies" for p in here.parents)
         if c.is_dir()),
        None,
    )
    if root is None:
        pytest.skip(
            "the kumo-trading-strategies checkout is not beside this repo; the installed venv copy is "
            "deliberately NOT used as a fallback — it is not what the image bakes"
        )
    source = f"sibling checkout {root}"
    offenders = []
    for path in root.rglob("*.py"):
        for n, raw in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            ln = raw.split("#")[0]
            for key in re.findall(r'_broker_account\.get\(\s*"([a-z_]+)"', ln):
                if key not in published:
                    offenders.append(f"{path.name}:{n} reads {key!r}")
    # The fixture's own property first: if nothing reads the snapshot at all, this proves nothing.
    reads_any = any("_broker_account.get(" in p.read_text(errors="ignore") for p in root.rglob("*.py"))
    assert reads_any, "no installed strategy reads _broker_account — this test no longer describes the code"
    assert not offenders, (
        f"in {source}: strategies read broker.account keys that are never published "
        f"{sorted(published)}: {offenders}"
    )
