"""Does the RUNNING stack match the compose file? (#419)

WHY THIS EXISTS. `verify-stamp-paper` answers "was this image built from current code" by reading the
revision label off each image. It is a good check and it is blind to a whole class of drift, because it
iterates a hardcoded list — `engine api ui` — and compares only image labels. It therefore cannot see:

  * a service that is RUNNING but no longer in the compose file at all;
  * a service in the compose file that is not running;
  * a container created from an OLDER compose config — a mount, env var or command that changed on
    disk and was never applied.

All three have bitten. On 2026-08-21 the `rotation` sidecar was deleted from `compose.paper.yml` and
merged to main, and the container went on running and serving the Market tab from a host mount. `main`
said the sidecar was gone; the machine said it was serving. Nothing reported the disagreement, and the
only reason it was noticed is that its bind mount had also gone stale and the tile went blank.

Operator: "We have that problem again and again."

HOW IT DETECTS DRIFT. Compose stamps every container it creates with its own labels:

    com.docker.compose.project      which stack
    com.docker.compose.service      which service
    com.docker.compose.config-hash  a hash of THAT SERVICE'S resolved compose config

and `docker compose config --hash='*'` prints the hash each service WOULD get from the file as it is
now. Comparing the two sets answers all three questions mechanically, with no list to keep in step —
which is what made the old check blind in the first place.

ENV SENSITIVITY, STATED RATHER THAN GUESSED AT. The config hash covers interpolated values, so running
this without the same environment the deploy used changes every hash at once. That is reported as its
own finding (`env-mismatch`) instead of as thirteen stale services — an alarm that fires on healthy
state is one an operator learns to scroll past, and this repo has shipped that twice (#387, #390).
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field


@dataclass
class Drift:
    """What is wrong with the running stack, in the operator's terms."""

    #: Running, but the compose file has no such service. The 2026-08-21 rotation sidecar.
    orphans: list[str] = field(default_factory=list)
    #: In the compose file, but nothing is running for it.
    missing: list[str] = field(default_factory=list)
    #: Running from a compose config that is no longer what the file says.
    stale: list[str] = field(default_factory=list)
    #: Every shared service differs — almost certainly a different environment, not thirteen stale
    #: containers. Reported separately so it cannot be read as widespread drift.
    env_mismatch: bool = False

    @property
    def ok(self) -> bool:
        return not (self.orphans or self.missing or self.stale or self.env_mismatch)

    def lines(self) -> list[str]:
        out: list[str] = []
        for s in self.orphans:
            out.append(f"ORPHAN   {s}: running, but no such service in the compose file — `up -d --remove-orphans`")
        for s in self.missing:
            out.append(f"MISSING  {s}: in the compose file, but not running — `up -d`")
        for s in self.stale:
            out.append(f"STALE    {s}: running from an older compose config — `up -d` to recreate it")
        if self.env_mismatch:
            out.append(
                "ENV      every service's config hash differs — this was almost certainly run without "
                "the environment the deploy used (export the APCA pair), NOT a stack-wide drift"
            )
        return out


def compare(running: dict[str, str], declared: dict[str, str],
            all_declared: dict[str, str] | None = None) -> Drift:
    """Pure comparison. `running` and `declared` are service -> config-hash.

    Split from the docker calls so the interesting cases can be tested without a daemon — the shape of
    every deploy bug this is meant to catch is a set difference, not a docker invocation.

    `declared` is the ACTIVE stack; `all_declared` adds services gated behind an inactive profile. A
    profile service is neither an orphan nor missing — it is OUT OF SCOPE, and the distinction is not
    academic. `ib-gateway` carries `profiles: ["ibkr"]`, nothing depends on it, and this stack executes
    on Alpaca. Passing `--profile "*"` to make it stop reading as an ORPHAN (when it happened to be up)
    made it read as MISSING the moment it was not, and `make up-paper` exited non-zero on a completely
    clean deploy. One flag, two false alarms, decided by whether a service nobody needs is running.

    Three-way, not two:

        declared in an ACTIVE profile, not running    MISSING     real
        running, in no profile at all                 ORPHAN      real
        declared in an INACTIVE profile               out of scope, either way

    `all_declared` DEFAULTS to `declared`, so a caller that does not pass it keeps the old two-way
    behaviour rather than silently treating every unknown container as in scope.
    """
    scope = all_declared if all_declared is not None else declared
    d = Drift(
        orphans=sorted(set(running) - set(scope)),
        missing=sorted(set(declared) - set(running)),
    )
    # STALENESS IS STILL CHECKED ON WHAT IS RUNNING, including a profile service that happens to be
    # up — an out-of-scope service running from an older config is still running from an older config.
    shared = sorted(set(running) & set(scope))
    differing = [s for s in shared if running[s] != scope[s]]
    # ALL of them differing is an environment difference, not drift. One or two is drift.
    if shared and len(differing) == len(shared) and len(shared) > 1:
        d.env_mismatch = True
    else:
        d.stale = differing
    return d


def _sh(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout


def running_services(project: str) -> dict[str, str]:
    """service -> config-hash, from the labels compose stamped on each container."""
    ids = _sh(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"]).split()
    out: dict[str, str] = {}
    for cid in ids:
        raw = _sh(["docker", "inspect", cid, "--format", "{{json .Config.Labels}}"]).strip()
        if not raw:
            continue
        labels = json.loads(raw)
        svc = labels.get("com.docker.compose.service")
        if svc:
            out[svc] = labels.get("com.docker.compose.config-hash", "")
    return out


def declared_services(compose_file: str, env_file: str, project: str) -> tuple[dict, dict]:
    """(active, all) service -> config-hash the file would produce NOW.

    ACTIVE is the default profile set — what should be running. ALL includes every profile, and exists
    ONLY so a profile service that happens to be up is not called an orphan. Returning one map and
    using it for both questions is what produced a MISSING on a clean deploy.
    """
    def _hashes(extra: list[str]) -> dict[str, str]:
        raw = _sh(["docker", "compose", "--env-file", env_file, "-f", compose_file, "-p", project,
                   *extra, "config", "--hash=*"])
        out: dict[str, str] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2 and len(parts[1]) == 64:  # "<service> <sha256>"
                out[parts[0]] = parts[1]
        return out

    # TWO READS, because the two questions are different. The default profile set is what SHOULD be
    # running; every profile is what is DECLARED anywhere, which is only used to keep a profile
    # service from reading as an orphan when it is up.
    return _hashes([]), _hashes(["--profile", "*"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare the running stack against the compose file (#419)")
    ap.add_argument("--file", default="compose.paper.yml")
    ap.add_argument("--env-file", default=".env.paper")
    ap.add_argument("--project", default="kumo-paper")
    args = ap.parse_args()

    running = running_services(args.project)
    declared, all_declared = declared_services(args.file, args.env_file, args.project)
    if not declared:
        print("  could not read the compose file — cannot compare")
        return 1

    drift = compare(running, declared, all_declared=all_declared)
    if drift.ok:
        print(f"  stack matches {args.file}: {len(running)} services, no drift")
        return 0
    for line in drift.lines():
        print("  " + line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
