"""Compacting rotated logs must never touch the live file, and must never lose one (#758).

Operator: "can we extend the nautilus log mechanism to not keep all logs forever and maybe even convert
finished files into parquet?"

MEASURED NAMING, from a probe run inside the running engine container:

    probe_2026-08-31_143610:955.jsonl   1799     finished
    probe_2026-08-31_143610:956.jsonl   3598     finished
    probe_2026-08-31_143610:957.jsonl      0     ACTIVE

Three files inside one second. Any rule that identifies the active file by parsing that name is
guessing at a log library's format under exactly the load where logs matter most.
"""

from __future__ import annotations

import pytest

from api.log_compaction import (
    COMPACTED_SUFFIX,
    QUIET_SECONDS,
    FileInfo,
    compact_file,
    expired,
    finished_files,
)

NOW = 1_000_000.0


def _f(name, age_seconds):
    return FileInfo(name=name, mtime=NOW - age_seconds)


# ==================================================================================================
# Never touch the live file
# ==================================================================================================
def test_the_fixture_can_express_the_bug():
    """Vacuity guard: the set must contain a file that IS eligible, or 'nothing was compacted' passes
    for the wrong reason in every test below."""
    got = finished_files([_f("a.jsonl", 9999), _f("b.jsonl", 0)], now=NOW)
    assert got == ["a.jsonl"]


def test_the_ACTIVE_file_is_never_compacted():
    """The measured three-in-one-second case. The newest is open and being written."""
    files = [_f("probe_2026-08-31_143610:955.jsonl", 9999),
             _f("probe_2026-08-31_143610:956.jsonl", 9998),
             _f("probe_2026-08-31_143610:957.jsonl", 0)]
    got = finished_files(files, now=NOW)
    assert "probe_2026-08-31_143610:957.jsonl" not in got
    assert got == ["probe_2026-08-31_143610:955.jsonl", "probe_2026-08-31_143610:956.jsonl"]


def test_a_RECENTLY_TOUCHED_file_is_left_alone_even_if_it_is_not_the_newest():
    """Both conditions, independently. Rotation can create several files in one second, so 'not the
    newest' alone would hand a file to the compactor while the logger still had it open."""
    files = [_f("old.jsonl", 1), _f("newest.jsonl", 0)]
    assert finished_files(files, now=NOW) == []


def test_QUIESCENCE_ALONE_IS_NOT_ENOUGH_either():
    """The mirror case: an idle engine's active file goes quiet, and compacting it would take the
    live log away from a running process."""
    assert finished_files([_f("only.jsonl", 9999)], now=NOW) == []


def test_a_TIE_on_mtime_still_leaves_exactly_one_active():
    """A tie must not make two files look active (compacting neither) or none (compacting the live
    one). Broken by name, so the choice is stable across calls."""
    files = [_f("a.jsonl", 0), _f("b.jsonl", 0), _f("c.jsonl", 9999)]
    got = finished_files(files, now=NOW)
    # Only the genuinely old file. The tie-loser is 0 seconds old, so QUIESCENCE protects it even
    # though the newest-file rule did not — which is the point of having both conditions.
    assert got == ["c.jsonl"], got
    # And the choice is stable: the same input gives the same answer, so a tie cannot make a file
    # eligible on one pass and not the next.
    assert finished_files(list(reversed(files)), now=NOW) == got


def test_ONLY_jsonl_is_considered():
    """A .parquet is already compacted; anything else is not ours."""
    files = [_f("done.parquet", 9999), _f("notes.txt", 9999),
             _f("a.jsonl", 9999), _f("live.jsonl", 0)]
    assert finished_files(files, now=NOW) == ["a.jsonl"]


@pytest.mark.parametrize("age", [0, QUIET_SECONDS - 1])
def test_the_quiet_threshold_binds(age):
    assert finished_files([_f("x.jsonl", age), _f("live.jsonl", 0)], now=NOW) == []


# ==================================================================================================
# Retention by AGE — the rule Nautilus has no field for
# ==================================================================================================
def test_old_PARQUET_expires():
    files = [_f("old.parquet", 11 * 86400), _f("new.parquet", 1 * 86400)]
    assert expired(files, now=NOW, keep_days=10) == ["old.parquet"]


def test_an_UNCOMPACTED_jsonl_NEVER_EXPIRES_however_old():
    """THE ONE THAT PREVENTS DATA LOSS. A .jsonl past the window has not been compacted yet, so it is
    the ONLY copy. The retention pass must never be the thing that loses a log — it ages out after
    compaction or not at all."""
    files = [_f("ancient.jsonl", 400 * 86400)]
    assert expired(files, now=NOW, keep_days=10) == []


def test_retention_is_not_off_by_a_day():
    """Pinned deliberately: `keep_days=10` keeps a file from nine days ago and drops one from eleven."""
    assert expired([_f("a.parquet", 9 * 86400)], now=NOW, keep_days=10) == []
    assert expired([_f("a.parquet", 11 * 86400)], now=NOW, keep_days=10) == ["a.parquet"]


# ==================================================================================================
# The ordering that is the safety property
# ==================================================================================================
def _io(rows, written_rows=None, fail=None):
    state = {"wrote": None, "unlinked": []}

    def read_json(p):
        if fail == "read":
            raise OSError("cannot read")
        return rows

    def write_parquet(r, target):
        if fail == "write":
            raise OSError("disk full")
        state["wrote"] = target

    def count_parquet(target):
        return len(rows) if written_rows is None else written_rows

    def unlink(p):
        state["unlinked"].append(p)

    return state, dict(read_json=read_json, write_parquet=write_parquet,
                       count_parquet=count_parquet, unlink=unlink)


def test_the_source_is_deleted_ONLY_after_the_parquet_is_verified():
    state, io = _io([{"a": 1}, {"a": 2}])
    out = compact_file("x.jsonl", **io)
    assert out == "x" + COMPACTED_SUFFIX
    assert state["unlinked"] == ["x.jsonl"]


def test_a_SHORT_WRITE_keeps_the_source_and_RAISES():
    """A truncated or partially flushed write produces a READABLE parquet with fewer rows. Without
    reading the count back, the original would be deleted against it — silently, and only noticed
    when someone needed the log."""
    state, io = _io([{"a": 1}, {"a": 2}, {"a": 3}], written_rows=2)
    with pytest.raises(ValueError, match="2 rows for 3"):
        compact_file("x.jsonl", **io)
    assert state["unlinked"] == [], "the source was deleted against a short write"


def test_a_FAILED_WRITE_keeps_the_source():
    state, io = _io([{"a": 1}], fail="write")
    with pytest.raises(OSError):
        compact_file("x.jsonl", **io)
    assert state["unlinked"] == []


def test_a_FAILED_READ_deletes_nothing():
    state, io = _io([{"a": 1}], fail="read")
    with pytest.raises(OSError):
        compact_file("x.jsonl", **io)
    assert state["unlinked"] == []


def test_an_UNREADABLE_file_returning_None_is_skipped_not_deleted():
    state, io = _io(None)
    assert compact_file("x.jsonl", **io) is None
    assert state["unlinked"] == []


def test_an_EMPTY_file_compacts_without_special_casing():
    """A rotated file can legitimately be empty — the measured probe produced one at 0 bytes."""
    state, io = _io([])
    assert compact_file("x.jsonl", **io) == "x" + COMPACTED_SUFFIX
    assert state["unlinked"] == ["x.jsonl"]
