"""What commit is this process running (#329).

Exists because on 2026-08-18 a reconciliation outage was misdiagnosed twice over, and both wrong theories
rested on an assumption nobody could check: that the running engine matched the repo. It did not — the
container was 59 lines behind `main`, so a fix that had been written, reviewed, mutation-tested and merged
had never once executed. Establishing that required copying the source out of the container and diffing it
against three candidate commits.

The stamp is set by `deploy/Dockerfile.backend` from a build arg (`make build-paper` / `up-paper` supply it).

DELIBERATELY NOT DERIVED AT RUNTIME. Reading `git rev-parse` from inside the process would report the commit
of whatever checkout happens to be mounted — or nothing at all, since the image ships no `.git`. The only
honest answer is the one baked in at BUILD time, because that is the thing that produced the code actually
loaded. A runtime guess that is usually right is exactly the failure mode this replaces.

`unknown` is a first-class answer, not an error. An ad-hoc `docker build` must still produce a working
image, and "I don't know" is worth more than a plausible-but-wrong sha — the whole point is that the stamp
can be TRUSTED when it does say something.
"""

from __future__ import annotations

import os

#: Value used when the image was built without a stamp (a hand-run `docker build`).
UNKNOWN = "unknown"


def _clean(raw: str | None) -> str:
    """Env var → stamp. Blank/whitespace-only reads as UNKNOWN.

    Compose passes `${KUMO_GIT_SHA:-unknown}`, but a hand-written `.env` or a `docker run -e KUMO_GIT_SHA=`
    yields an EMPTY string rather than an absent var — and an empty stamp rendered into the UI is an empty
    box, which reads as "no build info available" instead of "this build did not record one". Same answer
    for both, so the two cases cannot be confused.
    """
    return (raw or "").strip() or UNKNOWN


def build_stamp() -> dict[str, str]:
    """The commits baked into this image: the cockpit tree and the kumo-trading-strategies checkout.

    Both, because they move independently — the strategies package is installed from a separate build
    context, so a cockpit sha alone leaves "which strategies code?" unanswerable, which was half the
    ambiguity in the incident above.
    """
    return {
        "git_sha": _clean(os.environ.get("KUMO_GIT_SHA")),
        "strategies_sha": _clean(os.environ.get("KUMO_STRATEGIES_SHA")),
    }


def build_stamp_line() -> str:
    """One-line form for the startup log — the first thing to look at when a container misbehaves."""
    s = build_stamp()
    return f"build: cockpit={s['git_sha']} strategies={s['strategies_sha']}"


def is_stamped() -> bool:
    """True when this image can name its own cockpit commit.

    Callers use this to decide whether to DISPLAY the stamp as authoritative. An unstamped image must not
    render as though it knew — silence is the correct output when the answer is unknown.
    """
    return build_stamp()["git_sha"] != UNKNOWN
