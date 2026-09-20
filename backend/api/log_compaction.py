"""Rotated Nautilus log files -> Parquet, and retention BY AGE, which Nautilus cannot express (#758).

Operator: "can we extend the nautilus log mechanism to not keep all logs forever and maybe even convert
finished files into parquet?"

TWO GAPS, ONE PASS.

`LoggingConfig` rotates on `log_file_max_size` and keeps `log_file_max_backup_count` files. There is
no notion of days, so "keep 10 days" is only ever APPROXIMATED by sizing — a quiet fortnight keeps
more than ten days and a loud afternoon keeps fewer. And the files are large: measured on an Alpaca paper instance
2026-08-31, 1,805,706 bytes in 13 minutes, 161 bytes/line, order of 200 MB/day.

NAUTILUS KEEPS WRITING JSONL. Parquet cannot be appended to cheaply, so this is COMPACTION of files
the logger has finished with — never a replacement for the live writer.

THE FILE CURRENTLY BEING WRITTEN MUST NEVER BE TOUCHED, and "finished" is decided by two independent
conditions rather than one, because either alone is wrong at the boundary:

  - it is not the NEWEST file, and
  - nothing has written to it for `quiet_seconds`.

Measured naming, from a probe in the running container:

    probe_2026-08-31_143610:955.jsonl   1799     <- finished
    probe_2026-08-31_143610:956.jsonl   3598     <- finished
    probe_2026-08-31_143610:957.jsonl      0     <- ACTIVE, currently open

Three files can be created inside the same second, so a date-based or lexical rule alone would
misidentify the active one under load — which is exactly when logs matter.

LOSING A LOG TO A LOG-TIDYING JOB IS THE WORST VERSION OF THIS. So the JSON is deleted only after the
Parquet exists AND its row count matches, and any failure leaves the original in place.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A finished file must have been untouched this long. Belt and braces with the newest-file rule:
#: rotation can produce several files within one second, so neither condition is safe alone.
QUIET_SECONDS = 60

#: What Nautilus writes. Measured, not assumed — the extension is `.jsonl`, not `.json`.
LIVE_SUFFIX = ".jsonl"
COMPACTED_SUFFIX = ".parquet"


@dataclass(frozen=True)
class FileInfo:
    """Just the two facts the decision needs, so it can be tested without a filesystem."""

    name: str
    mtime: float


def finished_files(files, *, now: float, quiet_seconds: float = QUIET_SECONDS) -> list[str]:
    """Names of rotated files safe to compact — never the one being written.

    BOTH CONDITIONS, AND THE NEWEST IS EXCLUDED BY MTIME, NOT BY NAME. The names carry a timestamp
    and a sequence number, which sorts correctly today; but a rule that depends on parsing a log
    library's filename format breaks silently on upgrade, and this decision deletes files.
    """
    live = [f for f in files if f.name.endswith(LIVE_SUFFIX)]
    if not live:
        return []
    # The active file is the most recently touched. Ties broken by name so the choice is stable and
    # a tie can never leave TWO files looking active (which would compact neither) or NONE.
    newest = max(live, key=lambda f: (f.mtime, f.name))
    return sorted(
        f.name for f in live
        if f.name != newest.name and (now - f.mtime) >= quiet_seconds
    )


def expired(files, *, now: float, keep_days: float) -> list[str]:
    """Compacted files past the retention window — the age-based rule Nautilus has no field for.

    ONLY `.parquet`. A `.jsonl` past the window has not been compacted yet, and deleting it would
    destroy the only copy: the retention pass must never be the thing that loses a log. It ages out
    after compaction, or not at all.
    """
    cutoff = now - keep_days * 86400.0
    return sorted(f.name for f in files if f.name.endswith(COMPACTED_SUFFIX) and f.mtime < cutoff)


def compact_file(path, *, read_json, write_parquet, count_parquet, unlink) -> str | None:
    """JSONL -> Parquet, deleting the original ONLY once the replacement is proven.

    The IO is injected so the ordering below — the part that can lose data — is testable without a
    filesystem, and so a test can make each step fail in turn.

    THE ORDER IS THE SAFETY PROPERTY:
      1. read the JSON
      2. write the Parquet
      3. COUNT THE ROWS BACK OFF DISK and require they match
      4. only then delete the JSON

    Step 3 is not ceremony. A truncated or partially-flushed write produces a readable Parquet with
    fewer rows, and without the count the original would be deleted against it. Verifying by reading
    back is the same discipline as `_await_reducing_orders_clear`: proving the thing happened, rather
    than assuming it did because no exception was raised.
    """
    rows = read_json(path)
    if rows is None:
        return None
    target = str(path)[: -len(LIVE_SUFFIX)] + COMPACTED_SUFFIX
    write_parquet(rows, target)
    written = count_parquet(target)
    if written != len(rows):
        # Leave BOTH in place and say so. Deleting the source here is the data-loss path, and
        # deleting the bad parquet would hide that compaction is broken.
        raise ValueError(
            f"compaction wrote {written} rows for {len(rows)} input lines in {path} — the source is "
            f"kept and the parquet is left for inspection"
        )
    unlink(path)
    return target


# ==================================================================================================
# The filesystem layer. Everything above is pure so the decisions can be tested without one.
# ==================================================================================================
def _read_jsonl(path) -> list | None:
    """Rows from a JSONL file, or None if it cannot be read.

    A MALFORMED LINE DOES NOT DISCARD THE FILE. The live file is flushed as the process runs, so the
    last line of a rotated file can be a partial write; dropping the whole file for one bad line
    would lose a session's history to a truncated tail. Bad lines are skipped and counted into the
    output as a record of themselves, so the count still reconciles.
    """
    import json

    rows: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                obj = {"_unparsed": line[:2000], "_line_no": i}
            rows.append(obj if isinstance(obj, dict) else {"_value": obj, "_line_no": i})
    return rows


def _write_parquet(rows, target) -> None:
    import pandas as pd

    # `object` dtype on a mixed schema is fine and deliberate: log records have varying keys, and
    # forcing a schema would drop the fields that only appear on the rare lines worth finding.
    pd.DataFrame(rows).to_parquet(target, index=False, compression="zstd")


def _count_parquet(target) -> int:
    import pyarrow.parquet as pq

    # METADATA ONLY — the row count without materialising the file, so verification of a large
    # compaction cannot itself be the thing that runs the container out of memory.
    return pq.ParquetFile(target).metadata.num_rows


def run_compaction(directory, *, keep_days: float, now: float | None = None) -> dict:
    """Compact finished files and drop compacted ones past `keep_days`. Returns what it did.

    RUN THROUGH `Observations`, never bare: a compaction that quietly stops is a disk that fills and
    a retention policy that silently lapses, which is the same silence this whole ticket is about.
    """
    import os
    import time

    now = time.time() if now is None else now
    d = str(directory)
    if not os.path.isdir(d):
        return {"compacted": 0, "expired": 0, "skipped": "no such directory"}

    infos = []
    for name in os.listdir(d):
        full = os.path.join(d, name)
        try:
            infos.append(FileInfo(name=name, mtime=os.path.getmtime(full)))
        except OSError:
            continue          # vanished between listing and stat — a rotation in flight

    compacted = 0
    for name in finished_files(infos, now=now):
        out = compact_file(
            os.path.join(d, name),
            read_json=_read_jsonl,
            write_parquet=_write_parquet,
            count_parquet=_count_parquet,
            unlink=os.unlink,
        )
        compacted += 1 if out else 0

    # RE-STAT after compaction: the files that just became .parquet are new, and expiring against a
    # stale listing could drop one the same pass created.
    fresh = []
    for name in os.listdir(d):
        try:
            fresh.append(FileInfo(name=name, mtime=os.path.getmtime(os.path.join(d, name))))
        except OSError:
            continue
    dropped = 0
    for name in expired(fresh, now=now, keep_days=keep_days):
        try:
            os.unlink(os.path.join(d, name))
            dropped += 1
        except OSError:
            continue
    return {"compacted": compacted, "expired": dropped}
