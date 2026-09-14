"""There is ONE mechanism that creates an IB gateway, and it is compose.

WHAT THIS PREVENTS, measured rather than imagined. Between 2026-08-18 and 2026-08-23 two containers
existed for one job:

    nautilus-ib-gateway-paper   created by scripts/ib_gateway.py, outside compose
                                bridge network, host port 127.0.0.1:4002, LOGGED IN
    kumo-paper-ib-gateway-1     declared in compose, broker network, no host port
                                stuck on "Existing session detected" for FIVE DAYS

Only one session may hold the IB login. The ad-hoc container took it, and the declared one sat
unhealthy while `docker compose ps` showed nothing wrong — compose does not list a container it did not
create. Two mechanisms, neither aware of the other, and the one that lost was the only one anybody
could see.

AND IT DOES NOT SURVIVE MORE THAN ONE INSTANCE. The script's container name and host port are fixed, so
`test-alpaca` and `staging-ibkr` would collide on the name, the port and the login. A compose service is
per-project by construction.

kumo-cockpit#486.
"""

from __future__ import annotations

import pathlib

_BACKEND = pathlib.Path(__file__).resolve().parent.parent


def test_nothing_creates_a_gateway_outside_compose():
    """`DockerizedIBGateway` creates `nautilus-ib-gateway-<mode>` — a SINGLETON, invisible to compose.

    Asserted across the whole backend rather than against the one file that had it: the question is
    not "is that script gone", it is "can anything create a second gateway again".
    """
    # OUR code, not our DEPENDENCIES. `rglob` walks `.venv/` too, so this matched Nautilus's own
    # `adapters/interactive_brokers/gateway.py` -- the library we deliberately depend on -- and the
    # test went permanently red with five site-packages paths in the message. A test that can never
    # be green stops being read, which costs more than the defect it was built to catch. The rule is
    # about what THIS repo constructs; the adapter defining the class is not an offender.
    scanned = 0
    offenders = []
    for path in _BACKEND.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        if any(part in (".venv", "site-packages", "node_modules") for part in path.parts):
            continue
        scanned += 1
        text = path.read_text()
        if "DockerizedIBGateway" in text:
            offenders.append(str(path.relative_to(_BACKEND)))
    assert scanned > 50, (
        f"the scan only reached {scanned} files -- if the tree moved, this test proves nothing by "
        f"finding nothing")
    assert not offenders, (
        f"{offenders} construct a gateway outside compose. Two mechanisms for one job means one holds "
        f"the IB login and compose cannot see it — which is exactly how a declared gateway sat "
        f"unhealthy for five days with no visible cause"
    )


def test_compose_still_declares_the_gateway_behind_its_profile():
    """The guard above is only safe because the REPLACEMENT exists.

    Deleting the script without compose declaring a gateway would leave no way to run IBKR at all —
    a guard that removes a capability instead of consolidating it.
    """
    compose = (_BACKEND.parent / "deploy" / "compose.paper.yml").read_text()
    assert "ib-gateway:" in compose, "compose no longer declares a gateway — nothing can run IBKR"
    assert 'profiles: ["ibkr"]' in compose, (
        "the gateway is no longer behind the ibkr profile, so it would start for every instance "
        "including Alpaca-only ones and contend for the IB login again"
    )
    assert "READ_ONLY_API" in compose, (
        "the read-only flag is gone — Nautilus reconciliation fails with IB error 321 without it"
    )


def test_the_gateway_publishes_NO_host_port():
    """Per-project isolation is the property that makes more than one instance possible.

    A host port is a singleton: two instances would collide on it, which is the structural half of why
    the ad-hoc script could never have worked for staging alongside test.
    """
    compose = (_BACKEND.parent / "deploy" / "compose.paper.yml").read_text()
    block = compose[compose.index("  ib-gateway:"):]
    block = block[:block.index("\n  ", 1)] if "\n  " in block[1:] else block
    assert "ports:" not in block, (
        "the gateway publishes a host port — two instances would contend for it, and the point of "
        "moving to compose was that each project gets its own on an isolated network"
    )
