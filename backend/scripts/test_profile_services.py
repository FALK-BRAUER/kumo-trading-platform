"""A profile-gated service is neither an orphan nor missing — it is out of scope (#419).

MEASURED on a live stack, 2026-08-23. `make up-paper` exited NON-ZERO on a completely clean deploy:

    engine: 51ff2ea  OK      strategies: 68201a1  OK
    api:    51ff2ea  OK      strategies: 68201a1  OK
    ui:     caa2775  OK
    MISSING  ib-gateway: in the compose file, but not running — `up -d`
    ^^ the running stack does not match compose.paper.yml

`ib-gateway` carries `profiles: ["ibkr"]`. Nothing depends on it — the engine depends only on redis and
postgres — and this stack executes on ALPACA. Not running is CORRECT.

THE CHECK FLIPS FAILURE DIRECTION ON SOMETHING IT SHOULD NOT CARE ABOUT. `declared_services` passes
`--profile "*"`, which was added earlier today for the opposite symptom: ib-gateway WAS running and read
as an ORPHAN ("running, but no such service in the compose file" — a confident lie about a service
declared twelve lines up). Including every profile fixed that and created this. One flag, two false
alarms, depending only on whether a service nobody needs happens to be up.

Both readings are wrong for the same reason: a profile-gated service is not part of the ACTIVE stack, so
its presence is not drift and its absence is not drift. Three-way, not two:

    declared in an ACTIVE profile, not running    MISSING    -- real
    running, in no profile at all                 ORPHAN     -- real
    declared in an INACTIVE profile               out of scope, either way

AND A DEPLOY COMMAND THAT ALWAYS FAILS IS THE POINT. `make up-paper` returning non-zero on every clean
deploy is the same defect as #470 this morning ("verify-stack cried wolf after every clean deploy"),
which I fixed by giving it the same env as the build. Second instance of the class in one day, and the
consequence is worse than noise: Monday morning is when someone needs a red deploy to mean something.
"""

from __future__ import annotations

from scripts.verify_stack import Drift, compare

#: The live stack, 2026-08-23: five services up, `ib-gateway` gated behind the `ibkr` profile.
_RUNNING = {"redis": "h1", "postgres": "h2", "engine": "h3", "api": "h4", "ui": "h5"}
_ACTIVE = {"redis": "h1", "postgres": "h2", "engine": "h3", "api": "h4", "ui": "h5"}
_ALL = {**_ACTIVE, "ib-gateway": "h6"}


def test_the_fixture_actually_contains_a_profile_service() -> None:
    """Assert the fixture's own property first. Without a service in `_ALL` and not in `_ACTIVE`,
    every assertion below passes whatever the code does."""
    assert set(_ALL) - set(_ACTIVE) == {"ib-gateway"}


def test_a_profile_service_that_is_NOT_running_is_not_MISSING() -> None:
    """The live case. This is what made `make up-paper` exit non-zero on a clean deploy."""
    drift = compare(_RUNNING, _ACTIVE, all_declared=_ALL)
    assert drift.missing == [], f"a profile-gated service reported missing: {drift.missing}"
    assert drift.ok, drift.lines()


def test_a_profile_service_that_IS_running_is_not_an_ORPHAN() -> None:
    """The opposite symptom, from earlier the same day. Both must hold at once, or the check simply
    moves its false alarm from one side to the other — which is exactly what happened."""
    drift = compare({**_RUNNING, "ib-gateway": "h6"}, _ACTIVE, all_declared=_ALL)
    assert drift.orphans == [], f"a declared profile service reported as an orphan: {drift.orphans}"
    assert drift.ok, drift.lines()


def test_a_GENUINELY_missing_service_still_fails() -> None:
    """The direction that must not regress. `engine` is in the active stack; if it is not running,
    that is the finding this check exists for."""
    running = {k: v for k, v in _RUNNING.items() if k != "engine"}
    drift = compare(running, _ACTIVE, all_declared=_ALL)
    assert drift.missing == ["engine"] and not drift.ok


def test_a_GENUINE_orphan_still_fails() -> None:
    """The 2026-08-21 case that created this check: the `rotation` sidecar was deleted from compose,
    merged to main, and went on running and serving the Market tab off a host mount."""
    drift = compare({**_RUNNING, "rotation": "h9"}, _ACTIVE, all_declared=_ALL)
    assert drift.orphans == ["rotation"] and not drift.ok


def test_a_STALE_config_hash_still_fails() -> None:
    drift = compare({**_RUNNING, "engine": "OLD"}, _ACTIVE, all_declared=_ALL)
    assert drift.stale == ["engine"] and not drift.ok


def test_all_declared_DEFAULTS_to_the_active_set() -> None:
    """Callers that do not pass it must keep the old two-way behaviour, or an out-of-tree caller
    silently starts treating every unknown container as in-scope."""
    drift = compare({**_RUNNING, "ib-gateway": "h6"}, _ACTIVE)
    assert drift.orphans == ["ib-gateway"]


def test_declared_services_READS_TWICE_and_the_two_reads_differ(monkeypatch) -> None:
    """The two-read split is the whole fix, and `compare` cannot test it — every case above hands
    `declared` and `all_declared` in already made.

    A mutation that made the ACTIVE read pass `--profile "*"` too left all seven green: the pure
    comparison was perfect and both of its inputs were wrong in the same way. That is the same shape as
    the eight unwired mechanisms — a correct acceptor and a caller nothing drives.
    """
    from scripts import verify_stack

    seen: list[list[str]] = []

    def _fake_sh(args: list[str]) -> str:
        seen.append(args)
        # Compose prints "<service> <64-hex>". The profile read yields one extra service.
        rows = ["engine " + "a" * 64, "api " + "b" * 64]
        if "--profile" in args:
            rows.append("ib-gateway " + "c" * 64)
        return "\n".join(rows)

    monkeypatch.setattr(verify_stack, "_sh", _fake_sh)
    active, all_declared = verify_stack.declared_services("compose.paper.yml", ".env.paper", "kumo-paper")

    assert len(seen) == 2, f"expected two compose reads, got {len(seen)}"
    assert "--profile" not in seen[0], "the ACTIVE read must NOT ask for every profile — that is the bug"
    assert seen[1][seen[1].index("--profile") + 1] == "*", "the second read must span every profile"
    assert set(active) == {"engine", "api"}
    assert set(all_declared) == {"engine", "api", "ib-gateway"}
    # And the fixture must actually distinguish them, or the assertions above cannot discriminate.
    assert set(all_declared) - set(active) == {"ib-gateway"}
