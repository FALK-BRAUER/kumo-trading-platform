"""The running stack must not be allowed to disagree with the compose file in silence (#419).

2026-08-21: "We have that problem again and again."

WHAT KEEPS HAPPENING. Editing `compose.paper.yml` — even merging it to main — changes nothing about
what is already running. Docker does not reconcile a live stack against a file. So the code says one
thing and the machine does another, and nothing reports it:

  * 2026-08-21: the `rotation` sidecar was deleted from compose and merged. The container went on
    running and serving the Market tab off a host mount. Discovered only because its bind mount ALSO
    went stale and the tile went blank — i.e. by luck, on a second unrelated failure.
  * The same day: the engine gained a `ledger-tool/tools` mount and the api lost `KUMO_ROTATION_PATH` and
    its data mount. Neither container knew.

WHY `verify-stamp-paper` CANNOT SEE IT. That check reads the revision label off each image and compares
it to the last commit touching that image's sources. It is a good check for "was this built from
current code", and it is structurally blind here for two reasons: it iterates a HARDCODED list
(`engine api ui`), so a service outside that list is invisible whatever it does; and an image label
says nothing about the compose config a container was CREATED with.

A list that must be kept in step is the thing that went out of step. This compares SETS instead.
"""

from __future__ import annotations

from scripts.verify_stack import compare

# The real stack, 2026-08-21. `rotation` had been deleted from compose and was still running; `engine`
# and `api` had compose changes on disk that no container had been recreated for.
_RUNNING = {"api": "a1", "engine": "e1", "ui": "u1", "postgres": "p1", "redis": "r1", "rotation": "x1"}
_DECLARED = {"api": "a2", "engine": "e2", "ui": "u1", "postgres": "p1", "redis": "r1"}


def test_the_fixture_reproduces_the_2026_08_21_stack():
    """The fixture's own property first: it must contain a service that is running and undeclared, or
    the orphan assertion below proves nothing."""
    assert set(_RUNNING) - set(_DECLARED) == {"rotation"}


def test_a_SERVICE_RUNNING_THAT_COMPOSE_NO_LONGER_DECLARES_is_an_orphan():
    """The exact failure. `rotation` was gone from the file, merged to main, and still serving."""
    d = compare(_RUNNING, _DECLARED)
    assert d.orphans == ["rotation"]
    assert not d.ok
    assert any("ORPHAN   rotation" in line for line in d.lines())


def test_an_ORPHAN_ALONE_makes_the_stack_not_ok():
    """Isolated on purpose.

    The combined fixture also has stale services, so `not d.ok` there is satisfied whether or not
    `ok()` considers orphans at all — and it duly passed with orphans dropped from `ok()`. That mutation
    would let a stack with a deleted-but-running service report CLEAN, which is precisely the
    2026-08-21 failure. Caught by the harness, not by reading.
    """
    d = compare({"api": "a1", "rotation": "x1"}, {"api": "a1"})
    assert d.orphans == ["rotation"]
    assert d.stale == [] and d.missing == []
    assert not d.ok, "a service running that compose does not declare is not a healthy stack"


def test_a_CHANGED_compose_config_is_reported_as_stale():
    """A mount, env var or command edited on disk and never applied. The engine's `ledger-tool/tools`
    mount and the api's removed `KUMO_ROTATION_PATH`, both live on 2026-08-21."""
    d = compare(_RUNNING, _DECLARED)
    assert d.stale == ["api", "engine"]


def test_a_DECLARED_service_that_is_not_running_is_missing():
    """The other direction: added to compose, never started. Silent today — the tile that needs it just
    renders empty, which is indistinguishable from a quiet market."""
    d = compare({"api": "a1"}, {"api": "a1", "engine": "e1"})
    assert d.missing == ["engine"]
    assert d.stale == []


def test_a_MATCHING_stack_is_silent():
    """An alarm that fires on the healthy case is one an operator learns to scroll past — this repo has
    shipped that twice (#387, #390) and the whole value of this check is being trusted."""
    d = compare({"api": "a1", "engine": "e1"}, {"api": "a1", "engine": "e1"})
    assert d.ok
    assert d.lines() == []


def test_EVERY_service_differing_is_an_ENV_difference_not_stack_wide_drift():
    """The config hash covers interpolated values, so running this without the environment the deploy
    used changes every hash at once.

    Reporting that as five stale services would be five wrong alarms and would train the reader to
    ignore the check on the day one of them is real. It is a distinct finding with a distinct fix.
    """
    d = compare({"a": "1", "b": "1", "c": "1"}, {"a": "2", "b": "2", "c": "2"})
    assert d.env_mismatch is True
    assert d.stale == []
    assert any("ENV" in line for line in d.lines())


def test_ONE_service_differing_is_REAL_DRIFT_not_an_env_excuse():
    """The boundary in the other direction. The env heuristic must not swallow a genuine single-service
    drift — which is the common case, since a compose edit usually touches one service."""
    d = compare({"a": "1", "b": "2"}, {"a": "9", "b": "2"})
    assert d.env_mismatch is False
    assert d.stale == ["a"]


def test_a_SINGLE_service_stack_differing_is_drift_not_env():
    """With one shared service, "all of them differ" is trivially true and would excuse every real
    drift on a one-service stack. Guarded by requiring more than one."""
    d = compare({"a": "1"}, {"a": "2"})
    assert d.env_mismatch is False
    assert d.stale == ["a"]


def test_every_finding_names_the_COMMAND_that_fixes_it():
    """The check exists to be acted on at 3am. A finding that says what is wrong and not what to do
    gets postponed, and this one has already been postponed by not existing."""
    d = compare(_RUNNING, _DECLARED)
    text = " ".join(d.lines())
    assert "--remove-orphans" in text
    assert "up -d" in text


def test_the_declared_read_includes_EVERY_PROFILE():
    """`ib-gateway` carries `profiles: ["ibkr"]` and runs on this stack. Without every profile it was
    reported as "running, but no such service in the compose file" — a confident lie about a service
    declared twelve lines above the one that was actually missing.

    Caught on the first run against the live stack, which is why this is pinned rather than trusted.
    """
    import inspect

    from scripts import verify_stack

    src = inspect.getsource(verify_stack.declared_services)
    assert '"--profile", "*"' in src


# ==================================================================================================
# DEPLOY CONTRACT: compose CONSUMES credentials; the Makefile must SUPPLY them (2026-08-21).
#
# The Telegram alerts had never worked. Not one message, while `notifications.enabled` was true and
# every per-alert switch was on. compose.paper.yml passes `${LEDGER_TOOL_TELEGRAM_BOT_TOKEN:-}`, and that
# `:-` default is an EMPTY STRING rather than an error — so a bring-up from a shell without the
# variable exported silently handed the container four values of length 0.
#
# Two derivations of one fact, in two files, that nothing forced to agree. This is the seam test.
# ==================================================================================================
import re
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _compose_env_vars(name: str) -> set:
    """Every ${VAR} compose interpolates, whatever default it carries."""
    text = (_DEPLOY / name).read_text()
    return set(re.findall(r"\$\{([A-Z0-9_]+)(?::-[^}]*)?\}", text))


def test_the_fixture_can_actually_see_the_compose_file():
    # Assert the fixture's own property first. If the path were wrong, every test below would pass
    # vacuously on an empty set and report a contract that was never checked.
    assert (_DEPLOY / "compose.paper.yml").is_file()
    assert "LEDGER_TOOL_TELEGRAM_BOT_TOKEN" in _compose_env_vars("compose.paper.yml")


def test_every_telegram_var_compose_reads_is_supplied_by_the_makefile():
    """The bug, pinned at the seam that produced it.

    Not "the Makefile mentions telegram" — that would pass against a Makefile naming one variable of
    the four. Every var compose interpolates must appear on the line that actually exports them.
    """
    makefile = (_DEPLOY / "Makefile").read_text()
    telegram_block = re.search(r"^TELEGRAM\s*=(.*?)(?=^\s*$)", makefile, re.S | re.M)
    assert telegram_block, "no TELEGRAM export block in deploy/Makefile"
    supplied = set(re.findall(r"([A-Z0-9_]*TELEGRAM[A-Z0-9_]*)=", telegram_block.group(1)))

    consumed = {v for v in _compose_env_vars("compose.paper.yml") if "TELEGRAM" in v}
    assert consumed, "compose no longer reads any telegram var — update or delete this test"
    missing = consumed - supplied
    assert not missing, (
        f"compose.paper.yml passes {sorted(missing)} to the containers but the Makefile never exports "
        "them; compose substitutes a missing var as an EMPTY STRING, so alerts are dropped silently"
    )


def test_the_bringup_actually_uses_the_telegram_block():
    """Defining TELEGRAM and forgetting to reference it is the same outage with extra steps — exactly
    the shape of the UI_BUILD_ID miss already recorded in this Makefile (#374)."""
    makefile = (_DEPLOY / "Makefile").read_text()
    up = re.search(r"^up-paper:\n(.*?)(?=^\S)", makefile, re.S | re.M)
    assert up, "up-paper target not found"
    assert "$(TELEGRAM)" in up.group(1), "up-paper does not pass $(TELEGRAM) to compose"
