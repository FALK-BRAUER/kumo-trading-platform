"""One place where an OBSERVATION may fail without taking its subject down — and says that it did.

WHY THIS EXISTS (#758). Observability code rides on live paths: the protection tick, the fill
handler, the health read. It must never break them. The obvious answer is a `try/except` at each
site, and this repo has now paid for that answer twice in one day:

  - `_record_book_truth`'s except branch called `self.clock.timestamp_ns()`. On any object without a
    clock the HANDLER ITSELF raised, aborting the whole protection pass. 39 tests caught it; on a live
    stack it would have looked like a protection outage rather than a broken probe.
  - Earlier the same day, two shipped detectors were inert for two deploys because `Notifier.send`
    got an f-string instead of an `Alert`, and the `except Exception` that existed so a broken check
    could not take down healthy ones meant "no alarms" read as "nothing wrong".

Operator: "think of a general logging mechanism that prevents interfering with actual functionality.
best a generic mechanism rather than try catch everywhere."

THE TWO PROPERTIES, AND THEY ARE INSEPARABLE.

1. It absorbs. The subject runs to completion whatever the observer does.
2. **It reports ITSELF.** A swallowed exception on every poll is indistinguishable from a clean poll,
   which is the exact failure observability exists to end. So absorbing without recording is not
   allowed by construction: there is one code path, and it records.

`BaseException` IS NOT CAUGHT. `KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` are the
runtime asking the process to stop; swallowing those turns a shutdown into a hang, and a cancelled
task into a silently wedged one. Only `Exception`.

THE RECORDER CANNOT RAISE. That is the bug this module was written after, so the recording path
touches nothing but built-in types — no clock, no logger, no config, no attribute on the subject.
A timestamp is passed IN by whoever has one, and its absence is a `0`, not an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: A registry is not allowed to grow without bound — a runaway observer minting a new name per call
#: would otherwise consume memory on the live path this module exists to protect. Past the cap,
#: failures are COUNTED as dropped rather than discarded: silence about what was lost is the same
#: defect one level up.
MAX_OBSERVERS = 256

#: Reported when an observer has failed and nothing else can say so. Named after the rule in
#: CLAUDE.md: a check that fails must eventually report ITSELF, or the fail-soft is just off.
BROKEN_OBSERVER = "observer raised and was absorbed"


@dataclass
class _Failure:
    name: str
    count: int = 0
    last_error: str = ""
    last_ts_ns: int = 0


@dataclass
class Observations:
    """The registry every absorbed observation failure lands in.

    Deliberately NOT a logger. A log line is what these failures already were, and it is why four of
    them ran for days: the information existed and nothing carried it forward. This is standing
    state, so a broken observer is a condition on `/health` for as long as it is broken.
    """

    _failures: dict[str, _Failure] = field(default_factory=dict)
    #: Observers that have run at least once WITHOUT raising. Distinguishes "never ran" from "ran
    #: clean" — an observer that was never invoked is not evidence of anything, and reporting it as
    #: healthy is how a dead detector passes for a working one.
    _ran_ok: set[str] = field(default_factory=set)
    #: Observers that DECLARED themselves at wiring time. This is what makes absence readable:
    #: without it, an observer that never ran and an observer that does not exist are the same empty
    #: space. Operator: "missing logs can be confusing." `0 of 0` is not `0 of 4`.
    _declared: set[str] = field(default_factory=set)
    #: Failures that could not be recorded because the registry was full. Counted, never dropped
    #: silently — the cap is a defence, and a defence that hides what it discarded is a second bug.
    dropped: int = 0
    #: Set when the recorder itself failed. It is written to touch only built-in types precisely so
    #: this stays zero; if it is ever non-zero, the mechanism that reports failures is itself broken
    #: and that is the most important thing on the page.
    recorder_failures: int = 0

    def declare(self, *names: str) -> None:
        """Register observers that SHOULD run, so one that never does is visible as such."""
        for n in names:
            self._declared.add(str(n))

    def run(self, name: str, fn, *args, ts_ns: int = 0, **kwargs):
        """Call `fn`, returning its value, or None if it raised. The raise is RECORDED, never lost.

        `ts_ns` is passed in rather than read: the recorder must not depend on a clock, because
        depending on one inside an error handler is the defect this module exists to prevent.
        """
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — absorbing IS the contract; see the module docstring
            # THE RECORDER IS ITSELF GUARDED, and this is not paranoia repeated: the defect that
            # produced this module was an error handler that raised. If even this fails, the failure
            # is COUNTED — `recorder_failures` is a plain integer increment on a dataclass field,
            # which is the smallest thing that can still be true.
            try:
                self._record(name, exc, ts_ns)
            except Exception:  # noqa: BLE001
                try:
                    self.recorder_failures += 1
                except Exception:  # noqa: BLE001
                    pass
            return None
        # Only on success, and only after: an observer that raised has not "run ok" this time, but a
        # single later success does not erase the failure count either — both facts are kept.
        self._ran_ok.add(str(name))
        return out

    def _record(self, name: str, exc: BaseException, ts_ns: int) -> None:
        key = str(name)
        row = self._failures.get(key)
        if row is None:
            if len(self._failures) >= MAX_OBSERVERS:
                self.dropped += 1
                return
            row = self._failures[key] = _Failure(key)
        row.count += 1
        # `repr` of the exception, not `str`: an exception whose message is empty — and several
        # common ones are — would otherwise record as a blank, which reads as no error at all.
        row.last_error = f"{type(exc).__name__}: {exc}" if str(exc) else repr(exc)
        row.last_ts_ns = int(ts_ns or 0)

    @property
    def total_failures(self) -> int:
        return sum(f.count for f in self._failures.values())

    def state_of(self, name: str) -> str:
        """`ok`, `failing`, or `never ran` — three states, because the third is not a variant of the
        others. An observer nothing has invoked cannot vouch for anything."""
        key = str(name)
        if key in self._failures:
            return "failing"
        return "ok" if key in self._ran_ok else "never ran"

    def as_rows(self) -> list[dict]:
        return [
            {
                "observer": f.name,
                "failures": f.count,
                "last_error": f.last_error,
                "last_ts_ns": f.last_ts_ns,
                "note": BROKEN_OBSERVER,
            }
            for f in sorted(self._failures.values(), key=lambda f: -f.count)
        ]

    def summary(self) -> dict:
        """What ran, what failed, and WHAT NEVER RAN AT ALL.

        The third is the one that is easy to leave out and is the reason this method exists. An
        observer that produced nothing looks exactly like a healthy quiet one on every surface that
        reports only failures — and four defects on 2026-08-31 hid in precisely that shape.
        """
        never = sorted(self._declared - self._ran_ok - set(self._failures))
        return {
            "declared": len(self._declared),
            "ok": len(self._ran_ok - set(self._failures)),
            "failing": len(self._failures),
            "never_ran": len(never),
            "never_ran_names": never[:20],
            "dropped": self.dropped,
            "recorder_failures": self.recorder_failures,
            "rows": self.as_rows()[:20],
        }

    def log_lines(self) -> list[str]:
        """One line per condition, for the places that emit text.

        Operator: "logging errors might get logged too." A failure absorbed here must still be able to
        reach the log and the alert path — it is standing state INSTEAD OF a lost exception, not
        instead of an operator ever hearing about it. Rendering is separate from recording so that a
        broken log sink cannot lose the record, which is the whole ordering these two defects had
        backwards.
        """
        out = [f"observer {f.name} failed {f.count}x: {f.last_error}"
               for f in sorted(self._failures.values(), key=lambda f: -f.count)]
        if self.dropped:
            out.append(f"{self.dropped} observer failures were DROPPED: the registry hit its "
                       f"{MAX_OBSERVERS}-observer cap, so some failures are counted but unnamed")
        if self.recorder_failures:
            out.append(f"the failure recorder ITSELF failed {self.recorder_failures}x — the "
                       f"mechanism that reports broken observers is broken")
        for name in sorted(self._declared - self._ran_ok - set(self._failures)):
            out.append(f"observer {name} has NEVER RUN — that is not the same as running clean")
        return out
