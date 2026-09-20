"""The gateway's healthcheck must gate on the port the ENGINE uses (#815).

MEASURED ON ibkr-paper. Two boots, two outcomes, and the difference is a third of a second:

    2026-09-08 23:45:06.827  engine: Connected to Interactive Brokers ... client id: 1
    2026-09-08 23:45:06.915  engine: Paper trading disclaimer must first be accepted (10141)  x4
    2026-09-08 23:45:07.156  gateway IBC: Click button: I understand and accept

    2026-09-09 05:36:31.070  gateway IBC: Click button: I understand and accept
    2026-09-09 05:36:37.291  engine: Connected to Interactive Brokers ... client id: 2

The engine won the race on 09-08 by 0.33s and lost the next five minutes to it: 4 disclaimer errors,
then 91 `Not connected (code: 504)`, 139 `disconnected during req*`, 43 reconnect attempts, and
**83 venue order reads failed with 0 successes** — `exec_read_guard` correctly refusing to treat 20
cached open orders as missing at a venue it could not read. On 09-09 the same stack, same config,
same image booted clean. Nothing changed but the ordering.

`AcceptNonBrokerageAccountWarning=yes` is ALREADY set in the container's live `config.ini`, and IBC
does click the dialog — so this is not a missing option. It is a readiness signal that fires before
the thing it claims to signal.

WHAT THIS FILE CAN PROVE, AND WHAT IT CANNOT. It cannot prove IBC has finished its dialogs; that
needs a marker the gnzsnz image does not emit (`launcher.log` switches to an ENCRYPTED log partway
through login — checked in the running container, 2026-09-09). What it CAN prove is that the
healthcheck probes the port the engine actually connects to. It did not:

    healthcheck   bash -c 'echo > /dev/tcp/localhost/4002'      <- the gateway's inner API port
    engine        KUMO_IBG_PORT=4004  ->  ib-gateway:4004       <- the socat forwarder

socat's listener and the Java gateway's listener come up independently, so `service_healthy` could
be satisfied by a port no client uses while the one they do use was not yet accepting. That is the
detector-aimed-one-level-away shape this repo has now paid for three times: a guard that recognises
its subject by a neighbouring property.

Narrowing the race is not the same as closing it, and this file does not claim to close it. #815
carries the remaining half: a readiness signal that means "IBC has finished", which has to be
validated against a real gateway recreate rather than asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DEPLOY = Path(__file__).resolve().parent.parent.parent / "deploy"
_COMPOSE = _DEPLOY / "compose.paper.yml"


def _service_block(src: str, service: str) -> str:
    m = re.search(rf"^  {re.escape(service)}:$", src, re.M)
    assert m, f"compose has no `{service}` service"
    rest = src[m.end():]
    nxt = re.search(r"^  [a-z0-9-]+:$", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _healthcheck_ports(block: str) -> set[str]:
    """Every TCP port the service's healthcheck probes, with `${VAR:-default}` resolved.

    RESOLVING THE DEFAULT IS THE POINT, not parser convenience. The healthcheck names the port the
    same way the engine does — through `${KUMO_IBG_PORT:-4004}` — so a parser that only understood
    bare digits would report "probes nothing" for a correct healthcheck and "probes 4002" for the
    broken one, i.e. it would pass the fix and fail the fix's absence for the wrong reason. It has to
    read both sides the way compose does.
    """
    m = re.search(r"^\s+healthcheck:\s*$", block, re.M)
    assert m, "the service declares no healthcheck"
    tail = block[m.end():]
    stop = re.search(r"^\s{4}[a-z_]+:", tail, re.M)
    hc = tail[: stop.start()] if stop else tail
    probed = hc
    #: `${NAME:-1234}` -> `1234`, exactly as compose interpolates it when NAME is unset.
    probed = re.sub(r"\$\{[A-Z0-9_]+:-(\d+)\}", r"\1", probed)
    return set(re.findall(r"/dev/tcp/[^/\s]+/(\d+)", probed))


def _engine_gateway_port_default(block: str) -> str:
    m = re.search(r"KUMO_IBG_PORT:\s*\"?\$\{KUMO_IBG_PORT:-(\d+)\}", block)
    assert m, "the engine does not declare a KUMO_IBG_PORT default"
    return m.group(1)


# -------------------------------------------------------------------------------------------------
# VACUITY GUARDS
# -------------------------------------------------------------------------------------------------

def test_the_parsers_actually_reach_their_subjects():
    """Both sides return sets; an empty one on either side makes the comparison below vacuous in one
    direction or the other, and it would pass forever."""
    src = _COMPOSE.read_text()
    gw = _service_block(src, "ib-gateway")
    assert "healthcheck" in gw, "the ib-gateway block parsed without its healthcheck"
    ports = _healthcheck_ports(gw)
    assert ports, f"no port parsed out of the ib-gateway healthcheck: {gw[:200]!r}"
    assert _engine_gateway_port_default(_service_block(src, "engine")).isdigit()


# -------------------------------------------------------------------------------------------------
# THE RULE
# -------------------------------------------------------------------------------------------------

def test_the_gateway_healthcheck_probes_the_PORT_THE_ENGINE_CONNECTS_TO():
    """A readiness check on a port nobody uses is a check aimed one level away from what it protects.

    The gateway serves the engine on the socat port (`KUMO_IBG_PORT`, 4004 on ibkr-paper), while the
    healthcheck probed the Java gateway's inner API port (4002). Those two listeners come up
    independently, so `service_healthy` could be true while the engine's port was not yet accepting.
    """
    src = _COMPOSE.read_text()
    engine_port = _engine_gateway_port_default(_service_block(src, "engine"))
    probed = _healthcheck_ports(_service_block(src, "ib-gateway"))
    assert engine_port in probed, (
        f"the gateway healthcheck probes {sorted(probed)} but the engine connects on {engine_port}. "
        f"`depends_on: service_healthy` therefore releases the engine against a port that may not be "
        f"listening — and a gateway that is 'healthy' on a port no client uses is not a readiness "
        f"signal, it is a neighbouring property."
    )


def test_the_engine_still_WAITS_for_the_gateway_at_all():
    """The fix must tighten the gate, not remove it. Deleting the dependency would make the race
    permanent while making every healthcheck assertion here pass."""
    block = _service_block(_COMPOSE.read_text(), "engine")
    m = re.search(r"ib-gateway:\s*\n\s+condition:\s*(\S+)", block)
    assert m, "the engine no longer waits for ib-gateway at all"
    assert m.group(1) == "service_healthy", (
        f"the engine waits on `{m.group(1)}` rather than service_healthy — `service_started` is "
        f"satisfied the instant the container exists, which is the race with no gate at all"
    )


def test_the_start_period_still_covers_an_IBC_login():
    """IBC took 24s from launch to `Configuration tasks completed` on ibkr-paper's 2026-09-09 boot and
    5s on 09-08. `start_period` is what stops the healthcheck's early failures from being counted, so
    shrinking it below a real login turns a slow-but-healthy boot into a restart loop."""
    block = _service_block(_COMPOSE.read_text(), "ib-gateway")
    m = re.search(r"start_period:\s*(\d+)s", block)
    assert m, "the gateway healthcheck has no start_period"
    assert int(m.group(1)) >= 60, (
        f"start_period is {m.group(1)}s; the measured IBC login took 24s and the gateway's own "
        f"comment budgets 60s. Below that a slow login reads as an unhealthy container."
    )


@pytest.mark.parametrize("setting", ["AcceptNonBrokerageAccountWarning", "ALLOW_BLIND_TRADING",
                                     "BYPASS_WARNING"])
def test_the_dialog_settings_are_not_QUIETLY_dropped(setting):
    """PINNED BECAUSE THEY LOOK LIKE THE FIX AND ARE NOT.

    `AcceptNonBrokerageAccountWarning=yes` is already in the container's live config.ini and IBC does
    click the dialog — the 10141 errors happened anyway, because the engine arrived first. Anyone
    reading #815 will reach for these; the test records that they were checked and were already
    correct, so the next person does not "fix" a setting that was never wrong and conclude the race
    is closed.

    ALLOW_BLIND_TRADING / BYPASS_WARNING are compose-level; AcceptNonBrokerageAccountWarning is set
    by the image from TRADING_MODE=paper, so it is asserted as a comment reference rather than a key.
    """
    src = _COMPOSE.read_text()
    assert setting in src, (
        f"{setting} no longer appears in compose.paper.yml — if it was removed deliberately, say so "
        f"where it was, because #815's investigation turns on it having been present and correct"
    )
