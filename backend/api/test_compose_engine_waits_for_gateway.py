"""The engine must wait for the IB gateway when there is one (2026-08-25).

WHAT HAPPENED. On a cold deploy of ibkr-paper-retired the engine started immediately — its `depends_on`
named only redis and postgres — while the gateway took ~5 minutes to log in and open its API port.
The IB client retried with exponential backoff and reached:

    Attempt 7: attempting to reconnect in 163 seconds...
    Attempt 8: attempting to reconnect in 300 seconds...

By the time the gateway went `healthy`, the engine had backed off to a five-minute sleep. A ready
gateway sat unused, no lane registered, and `verify` failed a deploy that was otherwise fine. The
only way out was restarting the engine by hand — after which every lane registered and contracts
qualified normally.

Every redeploy makes this worse, because the backoff restarts from wherever the gateway's slowness
takes it. It is not a transient: it is guaranteed on any instance whose gateway is slower than the
engine's first few retries, which is every IBKR cold start.

WHY `depends_on` WAS NOT SIMPLY MISSING. `ib-gateway` sits behind `profiles: ["ibkr"]`, so an Alpaca
instance never starts it. A plain `condition: service_healthy` would make every Alpaca deploy wait
for — or fail on — a service that is deliberately absent. `required: false` is the form that means
"wait for it when it exists, ignore it when the profile is off", which is exactly the semantics
needed and is why this was not a one-line oversight.

WHAT THIS PINS. Both halves. The condition must be `service_healthy` — `service_started` would be
worse than nothing here, because the gateway container starts in a second and spends the next five
minutes logging in, so "started" is precisely the lie that made this hard to see.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest
import yaml

COMPOSE = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "compose.paper.yml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_the_fixture_can_see_both_services():
    """THE FIXTURE'S OWN PROPERTY FIRST. Every assertion below is about two named services; if either
    were renamed the checks would pass over a file that no longer says what they think."""
    svc = _compose().get("services") or {}
    assert "engine" in svc, "no `engine` service — this test is blind"
    assert "ib-gateway" in svc, "no `ib-gateway` service — this test is blind"
    assert "ibkr" in (svc["ib-gateway"].get("profiles") or []), (
        "ib-gateway is no longer profile-gated, so `required: false` may no longer be the right "
        "form — re-read this test before changing the compose file"
    )


def test_the_engine_WAITS_FOR_THE_GATEWAY_TO_BE_HEALTHY():
    """THE DEFECT. Without this the engine races a five-minute login and loses."""
    dep = (_compose()["services"]["engine"].get("depends_on") or {})
    assert isinstance(dep, dict), (
        "engine.depends_on is a bare list, which cannot express a condition — the engine will start "
        "before the gateway has logged in and back off to 300s while a ready gateway sits unused"
    )
    assert "ib-gateway" in dep, (
        "the engine does not wait for ib-gateway. On 2026-08-25 it reached `Attempt 8: reconnect in "
        "300 seconds` against a gateway that went healthy at minute five, no lane registered, and "
        "the deploy failed verification until the engine was restarted by hand"
    )

    cond = dep["ib-gateway"].get("condition")
    assert cond == "service_healthy", (
        f"condition is {cond!r}. `service_started` is worse than nothing here: the gateway container "
        f"starts in about a second and then spends five minutes logging in, so 'started' is exactly "
        f"the lie that made this hard to diagnose"
    )


def test_the_wait_is_NOT_REQUIRED_so_an_ALPACA_deploy_is_unaffected():
    """The regression this fix could easily introduce.

    `ib-gateway` is `profiles: ["ibkr"]`. A required dependency on a service in an inactive profile
    makes every Alpaca deploy fail or hang on something deliberately absent — trading one broken
    tenant for the other.
    """
    dep = _compose()["services"]["engine"]["depends_on"]["ib-gateway"]
    assert dep.get("required") is False, (
        "the gateway dependency is REQUIRED. alpaca-paper never starts ib-gateway (profiles: ibkr), "
        "so this would break the tenant that currently works"
    )


@pytest.mark.parametrize("profile", ["", "ibkr"])
def test_docker_compose_ITSELF_accepts_the_file_with_and_without_the_ibkr_profile(profile):
    """THE SEAM, not the YAML. A file that parses as a dict can still be rejected by compose.

    This is the check that would have caught a `required: false` unsupported by the installed
    compose, or a typo in the condition — neither of which a `yaml.safe_load` can see. Skipped rather
    than failed where docker is unavailable, because a missing daemon is not a defect in the file.
    """
    if subprocess.run(["docker", "compose", "version"],
                      capture_output=True).returncode != 0:  # pragma: no cover
        pytest.skip("docker compose unavailable")

    cmd = ["docker", "compose", "-f", str(COMPOSE)]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["config", "--quiet"]

    env_stub = {
        "KUMO_VOLUME_PREFIX": "t", "COMPOSE_PROJECT_NAME": "t",
        "APCA_API_KEY_ID": "x", "APCA_API_SECRET_KEY": "x",
        "TWS_USERID": "x", "TWS_PASSWORD": "x", "IBKR_ACCOUNT_ID": "x",
        # The ledger mount is `${KUMO_LEDGER_DIR:?}` (#1045): compose REFUSES the file without it,
        # which is the point — so the stub declares one, as every instance.env does.
        "KUMO_LEDGER_DIR": "/tmp",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    r = subprocess.run(cmd, capture_output=True, text=True, env=env_stub, timeout=90)
    assert r.returncode == 0, (
        f"docker compose rejects the file with profile={profile!r}:\n{r.stderr}\n\n"
        f"A depends_on the installed compose cannot express breaks EVERY deploy, not just IBKR ones."
    )
