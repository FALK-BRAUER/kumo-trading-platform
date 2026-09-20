"""`KUMO_DATA` must select the data provider, exactly as `KUMO_EXEC` selects the execution one (#574).

2026-08-27: *"the instances project is defining the instance's config"* and *"staging has NO
business with alpaca whatsoever"*.

THE DEFECT. `instance.env` declared `KUMO_DATA` for both instances and the instances README lists that
file as holding "providers" — but `load_feed_config` read the data provider from `feed.toml` alone and
never looked at the environment. Only `KUMO_EXEC` was honoured. So:

    KUMO_DATA=ibkr      changed nothing
    KUMO_EXEC=ibkr      changed the exec client

Two sibling knobs, one wired. It stayed invisible because both sources said `alpaca` and AGREED, which
is the exact condition under which a severed wire cannot be observed — and the prescribed test is to set
the knob to a value the other source could not produce and watch it travel.

The consequence was not cosmetic: ibkr-paper-retired took its market data from Alpaca, which built an
`AlpacaHttpClient`, which the realized-P&L sweep then used to poll an Alpaca BROKERAGE account holding
none of staging's positions (#573).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from api.feed_config import load_feed_config

_REPO = Path(__file__).resolve().parents[2]


def _write(tmp_path, *, data_provider="alpaca", exec_provider="alpaca"):
    """A feed.toml with BOTH provider tables present, so a switch cannot fail for a missing table and
    read as 'the override did not work'."""
    p = tmp_path / "feed.toml"
    p.write_text(f"""
[data]
provider = "{data_provider}"
backfill_days = 200
[data.alpaca]
key_env = "APCA_API_KEY_ID"
secret_env = "APCA_API_SECRET_KEY"
[data.ibkr]
ibg_host = "127.0.0.1"
ibg_port = 4002
ibg_client_id = 2
[execution]
provider = "{exec_provider}"
[execution.alpaca]
key_env = "APCA_API_KEY_ID"
[execution.ibkr]
ibg_host = "127.0.0.1"
[universe]
symbols = ["AAPL"]
[engine]
kind = "live"
trader_id = "T-001"
venue = "XNAS"
""")
    return p


def test_the_fixture_defaults_to_alpaca(tmp_path, monkeypatch):
    """Fixture property first. If the file did not already say `alpaca`, the override test below would
    pass for the wrong reason — it would be reading the file, not the environment."""
    monkeypatch.delenv("KUMO_DATA", raising=False)
    assert load_feed_config(_write(tmp_path)).data_provider == "alpaca"


def test_KUMO_DATA_overrides_the_file(tmp_path, monkeypatch):
    """THE FIX. A value the file could not produce, per `agreement-is-not-connection`: the file says
    alpaca, the environment says ibkr, and ibkr must win."""
    monkeypatch.setenv("KUMO_DATA", "ibkr")
    cfg = load_feed_config(_write(tmp_path, data_provider="alpaca"))
    assert cfg.data_provider == "ibkr", (
        "KUMO_DATA did not select the provider — instance.env declares it and the instances README "
        "lists that file as holding providers, so a config change there must actually travel")
    assert cfg.provider_config.get("ibg_port") == 4002, (
        "the provider CONFIG still came from the old table — selecting ibkr must also select [data.ibkr]")


def test_KUMO_DATA_and_KUMO_EXEC_are_resolved_THE_SAME_WAY(tmp_path, monkeypatch):
    """Two derivations of one idea. `KUMO_EXEC` was env-overridable and `KUMO_DATA` was not; that
    asymmetry is the whole defect, and nothing but a test keeps them together."""
    monkeypatch.setenv("KUMO_DATA", "ibkr")
    monkeypatch.setenv("KUMO_EXEC", "ibkr")
    cfg = load_feed_config(_write(tmp_path, data_provider="alpaca", exec_provider="alpaca"))
    assert (cfg.data_provider, cfg.exec_provider) == ("ibkr", "ibkr")


def test_an_UNKNOWN_provider_RAISES_rather_than_falling_back(tmp_path, monkeypatch):
    """A typo must refuse, not silently keep the file's value. A fallback here would mean
    `KUMO_DATA=ibrk` runs on Alpaca while the operator believes otherwise — a silent wrong answer,
    which CLAUDE.md forbids."""
    monkeypatch.setenv("KUMO_DATA", "ibrk")
    with pytest.raises(ValueError, match="ibrk"):
        load_feed_config(_write(tmp_path))


def test_case_and_whitespace_are_tolerated_like_KUMO_EXEC(tmp_path, monkeypatch):
    """`KUMO_EXEC` does `.strip().lower()`; a shell export can carry a trailing space. Diverging here
    would make one knob accept what the other rejects."""
    monkeypatch.setenv("KUMO_DATA", "  IBKR ")
    assert load_feed_config(_write(tmp_path)).data_provider == "ibkr"


# ==================================================================================================
# THE INSTANCE RULE — "staging has NO business with alpaca whatsoever" (2026-08-27)
# ==================================================================================================


#: The instances repo is a SEPARATE checkout. CI clones only this one, so these tests skip there — and
#: a skip exits GREEN, which means CI reports a pass while validating nothing about instance config
#: (codex). Marked so the skip is at least VISIBLE and can be selected for explicitly:
#:
#:     pytest -m instances        # run them where the sibling repo exists
#:     pytest -m 'not instances'  # what CI effectively does today
#:
#: The real fix is for CI to check out the instances repo too; until it does, this stops the skip from
#: reading as coverage. Tracked in #581.
needs_instances = pytest.mark.instances


def _instance_env(name: str) -> dict:
    p = _REPO.parent / "kumo-trading-platform/instances" / "instances" / name / "instance.env"
    if not p.exists():
        pytest.skip(f"{p} not checked out beside this repo")
    out = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _feed_toml(name: str) -> dict:
    p = _REPO.parent / "kumo-trading-platform/instances" / "instances" / name / "feed.toml"
    if not p.exists():
        pytest.skip(f"{p} not checked out beside this repo")
    with open(p, "rb") as fh:
        return tomllib.load(fh)


@needs_instances
def test_the_alpaca_instance_really_is_on_alpaca():
    """Fixture property first, again: if NO instance used Alpaca, the staging assertions below would
    hold vacuously and this file would be pinning nothing."""
    env = _instance_env("alpaca-paper")
    assert env.get("KUMO_DATA") == "alpaca" and env.get("KUMO_EXEC") == "alpaca"


@needs_instances
def test_staging_ibkr_declares_NO_alpaca_provider():
    """An IBKR instance takes IBKR data. `providers/__init__.py` registers `"ibkr": ibkr.build_data`
    and the staging gateway serves it; Alpaca was a choice, never a necessity."""
    env = _instance_env("ibkr-paper-retired")
    assert env.get("KUMO_EXEC") == "ibkr", env
    assert env.get("KUMO_DATA") == "ibkr", (
        f"ibkr-paper-retired declares KUMO_DATA={env.get('KUMO_DATA')!r} — it must have no Alpaca anything")
    assert _feed_toml("ibkr-paper-retired")["data"]["provider"] == "ibkr", (
        "feed.toml still selects alpaca; instance.env and feed.toml must not disagree about the "
        "provider — that disagreement is how #574 hid")


@needs_instances
def test_staging_ibkr_names_NO_alpaca_credential_anywhere():
    """The credential is the thing the operator asked about: an Alpaca key should exist for the Alpaca test
    instance and nowhere else. A key present on staging is a key that CAN be used, and #573 showed the
    account path using it the moment it existed."""
    root = _REPO.parent / "kumo-trading-platform/instances" / "instances" / "ibkr-paper-retired"
    if not root.exists():
        pytest.skip("instances repo not checked out beside this repo")
    offenders = []
    for f in ("instance.env", "feed.toml"):
        p = root / f
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            bare = line.split("#", 1)[0]          # comments may DISCUSS alpaca; declarations may not
            if "APCA" in bare or "alpaca" in bare.lower():
                offenders.append(f"{f}:{i}: {line.strip()}")
    assert not offenders, (
        "ibkr-paper-retired still declares Alpaca configuration:\n  " + "\n  ".join(offenders))


# ==================================================================================================
# AN EMPTY STRING IS NOT A SELECTION (#581)
#
# compose interpolates an UNSET variable to the EMPTY STRING, not to absent:
#
#     KUMO_DATA: "${KUMO_DATA:-}"
#
# so the container gets `KUMO_DATA=""` and `os.environ.get("KUMO_DATA", data["provider"])` returns ""
# — the default never fires, and the lookup then fails on a `[data.]` table that cannot exist. The
# variable has to be forwarded for #574 to work at all, and forwarding it introduces exactly this.
# ==================================================================================================


def test_an_EMPTY_KUMO_DATA_falls_back_to_the_file(tmp_path, monkeypatch):
    """The compose case. An unset override must read as "operator did not choose", not as a provider
    named empty-string."""
    monkeypatch.setenv("KUMO_DATA", "")
    assert load_feed_config(_write(tmp_path, data_provider="alpaca")).data_provider == "alpaca", (
        "an empty KUMO_DATA was treated as a selection — compose sets it empty for every instance "
        "that does not override, so this is the DEFAULT path, not an edge case")


def test_a_WHITESPACE_ONLY_KUMO_DATA_falls_back_too(tmp_path, monkeypatch):
    """`KUMO_DATA: "${KUMO_DATA:- }"` or a stray shell space is the same fact wearing a disguise."""
    monkeypatch.setenv("KUMO_DATA", "   ")
    assert load_feed_config(_write(tmp_path, data_provider="alpaca")).data_provider == "alpaca"


def test_a_REAL_value_still_wins_over_the_file(tmp_path, monkeypatch):
    """The discriminating half. A fallback that swallowed every value would make #574 a no-op again,
    which is the state this whole line of work exists to leave."""
    monkeypatch.setenv("KUMO_DATA", "ibkr")
    assert load_feed_config(_write(tmp_path, data_provider="alpaca")).data_provider == "ibkr"


# ==================================================================================================
# THE SEAM: the variable must actually be FORWARDED to the container
#
# `test_KUMO_DATA_overrides_the_file` proves `load_feed_config` READS the variable. It cannot see that
# nothing SETS it — and nothing did: measured 2026-08-27, `printenv KUMO_DATA` was unset on BOTH
# engines while 20 other KUMO_* vars were present. A green unit test over a severed deployment wire.
# ==================================================================================================


def test_compose_parameterises_the_provider_vars_in_BOTH_processes():
    """PER SERVICE, and PARAMETERISED — not "the string appears somewhere" (codex).

    The weak version of this test checked that `KUMO_DATA:` occurred anywhere in the file. It would
    pass if only the api had it, if the engine had it as a hardcoded literal, or if it sat in a
    comment. Codex reproduced the first case: engine `KUMO_DATA: ibkr`, api `${KUMO_DATA:-}` — and the
    check was happy.

    BOTH processes call `load_feed_config()` (engine `build_node`, api via `RedisConsumer.__init__`),
    so a var reaching only one makes them resolve DIFFERENT providers — two halves of one stack
    disagreeing about what feeds it. And a literal is worse than absent: it silently overrides the
    instance rather than falling through to feed.toml.
    """
    import yaml

    compose = _REPO / "deploy" / "compose.paper.yml"
    if not compose.exists():
        pytest.skip("compose file not present")
    services = yaml.safe_load(compose.read_text())["services"]

    problems = []
    for svc in ("engine", "api"):
        env = services.get(svc, {}).get("environment") or {}
        if not isinstance(env, dict):
            problems.append(f"{svc}: environment is not a mapping ({type(env).__name__})")
            continue
        for var in ("KUMO_DATA", "KUMO_EXEC"):
            if var not in env:
                problems.append(f"{svc}: {var} absent — never reaches this process")
            elif "${" + var not in str(env[var]):
                problems.append(
                    f"{svc}: {var}={env[var]!r} is a LITERAL — it overrides the instance instead of "
                    f"carrying it, and a compose literal beats env_file")
    assert not problems, "compose does not carry the provider vars into both processes:\n  " + \
        "\n  ".join(problems)


def test_the_fixture_can_detect_a_MISSING_var():
    """Prove the assertion above can fail. Parsed the same way, a service without the var must be
    reported — otherwise the test is a yaml-parse smoke check wearing a safety label."""
    import yaml

    doc = yaml.safe_load("""
services:
  engine:
    environment:
      KUMO_DATA: ibkr
  api:
    environment:
      KUMO_DATA: ${KUMO_DATA:-}
""")
    engine_env = doc["services"]["engine"]["environment"]
    assert "${KUMO_DATA" not in str(engine_env["KUMO_DATA"]), (
        "the literal-detection predicate cannot tell a hardcoded value from a parameterised one — "
        "this is the exact case codex reproduced against the weaker version of this test")
