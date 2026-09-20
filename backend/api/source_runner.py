"""Run an instance's pool-source refreshers, and be observable when they do NOTHING.

WHY THIS EXISTS. #486 makes the ORIGIN of a pool source an instance's business: it supplies
`sources/<name>.sh`, which emits symbols from a folder, a Google Sheet, a CSV in GitHub, whatever it
has. Both reviews of that plan caught the same omission independently — an endpoint accepts rows, and
NOTHING RUNS THE SCRIPT. That is the eighth instance this weekend of a mechanism that would have
existed, been correct, and never been called.

THE FAILURE IS NOT SYMMETRIC, AND THAT SHAPES THIS MODULE:

    a refresher that RUNS AND FAILS      calls `mark_source_failed`; the previous set is kept and the
                                         health row says so. Degrades honestly and loudly.
    a refresher that NEVER RUNS AT ALL   leaves a health row ageing quietly until it crosses the stale
                                         threshold, at which point EVERY LANE STOPS DECIDING AT ONCE,
                                         and nothing anywhere says why.

So the second case is the one to design for. `last_attempt` is recorded on EVERY tick — success,
failure, and the case where a source has no script at all — because a runner that is silent when idle
is indistinguishable from a runner that is dead, and the difference only becomes visible at the moment
it is most expensive.

WHAT IT DOES NOT DO. It does not write `exec_pool_source`. It calls the endpoint's own wrapper, which
delegates to `PgSymbolPool.refresh_source` — the single writer. Two writers with replace semantics do
not race; the loser's rows vanish.

kumo-trading-platform issue 486 step 5.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger(__name__)

#: Where an instance's refresher scripts live. Absent is NORMAL — an instance may supply none.
SOURCES_DIR = os.environ.get("KUMO_SOURCES_DIR", "")

#: A refresher gets this long before it is abandoned. A hung script must not become a hung runner:
#: the next tick is more valuable than this one finishing.
TIMEOUT_S = float(os.environ.get("KUMO_SOURCE_TIMEOUT_S", "60"))


class RunnerState:
    """What the runner has actually done, so "idle" and "dead" are distinguishable.

    Every field is written on EVERY tick, including ticks that do nothing. A counter that only moves
    on success cannot tell you the runner stopped.
    """

    def __init__(self) -> None:
        self.last_attempt_ts: float = 0.0
        self.last_success_ts: float = 0.0
        self.attempts: int = 0
        self.failures: int = 0
        self.per_source: dict[str, str] = {}

    def snapshot(self) -> dict:
        return {
            "last_attempt_ts": self.last_attempt_ts,
            "last_success_ts": self.last_success_ts,
            "attempts": self.attempts,
            "failures": self.failures,
            "per_source": dict(self.per_source),
        }


STATE = RunnerState()


def discover(sources_dir: str = "") -> list[Path]:
    """Executable refreshers in the instance's `sources/` directory, sorted.

    An empty or missing directory returns `[]` and is NOT an error — an instance with no refreshers is
    a legitimate deployment. The runner still ticks, so its silence stays measurable.
    """
    root = Path(sources_dir or SOURCES_DIR or "")
    if not root or not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.sh") if p.is_file() and os.access(p, os.X_OK))


def parse_output(raw: str):
    """A refresher's stdout: JSON list of symbols, or an object of symbol -> meta.

    Anything else RAISES rather than being coerced. A script that prints a warning line and then valid
    JSON is a script whose output we do not understand, and guessing produces a plausible symbol set
    that nobody chose — which is worse than refusing, because a refusal keeps the previous set.
    """
    data = json.loads(raw)
    if isinstance(data, list) and all(isinstance(x, str) for x in data):
        return data
    if isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()):
        return data
    raise ValueError(
        "a refresher must print a JSON list of symbols or an object of symbol -> meta; "
        f"got {type(data).__name__}"
    )


async def run_once(sources_dir: str = "", state: RunnerState | None = None) -> dict[str, str]:
    """One tick over every discovered refresher. Returns `{source: outcome}`.

    Records the attempt BEFORE doing any work, so a tick that dies partway is still visible as an
    attempt rather than vanishing.
    """
    from api import pool

    st = state or STATE
    st.attempts += 1
    st.last_attempt_ts = time.time()

    scripts = discover(sources_dir)
    if not scripts:
        # NOT an error, and still recorded. An instance with no refreshers must be distinguishable
        # from a runner that has stopped ticking.
        st.per_source = {}
        _log.debug("source runner: no refreshers to run")
        return {}

    outcomes: dict[str, str] = {}
    for script in scripts:
        name = script.stem
        try:
            proc = await asyncio.create_subprocess_exec(
                str(script), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
            except TimeoutError:
                proc.kill()
                raise TimeoutError(f"{name} exceeded {TIMEOUT_S}s") from None
            if proc.returncode != 0:
                raise RuntimeError(f"{name} exited {proc.returncode}: {err.decode()[:200]}")
            symbols = parse_output(out.decode())
            n = await pool.refresh_source(name, symbols, detail=f"refreshed by {script.name}")
            outcomes[name] = f"ok:{n}"
            st.last_success_ts = time.time()
        except Exception as exc:  # noqa: BLE001 — one bad refresher must not stop the others
            # The previous set is KEPT upstream, so this degrades honestly: the pool is stale and
            # says so, rather than being replaced by whatever a broken script emitted.
            st.failures += 1
            outcomes[name] = f"failed:{type(exc).__name__}"
            _log.warning("source refresher %s failed, keeping the previous set: %s", name, exc)

    st.per_source = outcomes
    return outcomes


#: How often the runner ticks. Well under the staleness threshold, so a single missed tick is not an
#: outage and a persistent failure is visible long before a lane stops deciding.
INTERVAL_S = float(os.environ.get("KUMO_SOURCE_INTERVAL_S", "900"))


async def run_forever(sources_dir: str = "", state: RunnerState | None = None) -> None:
    """Tick until cancelled. THE THING WHOSE ABSENCE WAS THE DEFECT.

    Both reviews of #486 caught the same omission independently: `sources/<name>.sh` would have
    existed, been correct, and never been called — with `source_health()` reporting stale and nobody
    able to say why. So this loop is the point, and its properties are chosen for the asymmetric
    failure rather than for the happy path:

      it never exits on error      one bad tick must not end the runner; that converts a recoverable
                                   failure into the unrecoverable one
      it ticks even with no work   `run_once` records the attempt regardless, so a runner that is
                                   idle stays distinguishable from a runner that is dead
    """
    while True:
        try:
            await run_once(sources_dir, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a tick that dies must not end the loop
            _log.warning("source runner tick failed: %s", exc)
        await asyncio.sleep(INTERVAL_S)
