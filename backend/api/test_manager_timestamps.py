"""A manager row must say WHEN, or a stale failure is indistinguishable from a live one (#400).

WHAT HAPPENED. `GET /managers` returned no timestamp of any kind:

    ['cycle_id', 'error', 'instrument_id', 'kind', 'leash', 'manager_id', 'params', 'state', 'strategy_id']

so a `peak_watch` that FAILED on 2026-08-11 with `'dict' object has no attribute 'project'` rendered
identically to `A.XNYS`'s `stop_reenter_rearm` that FAILED at 18:20 on 2026-08-20. Reading that undated
list produced a report that PEAK was currently broken — it was not; the bug named in that row was fixed
38 minutes after the row was written, and five later `peak_watch` rows are APPLIED. Meanwhile the
two-hour-old failure that DID matter went unnoticed until Postgres was queried directly.

THE COLUMNS WERE ALWAYS THERE. `Manager.created_at` and `Manager.updated_at` have existed since the
model was written, with `onupdate=func.now()`. Only the response model failed to declare them.

That is the FOURTH time a published field has been dropped at a DTO boundary here — #233, #322, #336,
and this. A Pydantic response model silently discards keys it does not declare: the engine writes it,
the database stores it, the API drops it, and the screen goes on showing the older, wrong thing while
everything compiles and every test passes.

`updated_at` is the terminal-state timestamp for FAILED/CANCELLED: a terminal row is not touched again,
so its `updated_at` is when it died.
"""

from __future__ import annotations

from datetime import UTC, datetime

from api.models import ManagerDTO


def _dto(**over):
    base = dict(
        manager_id="47c7c332-0e5c-4527-8ff9-8f9718ceb167",
        kind="stop_reenter_rearm",
        instrument_id="A.XNYS",
        strategy_id="MANUAL-001",
        cycle_id=None,
        leash="AUTO",
        state="FAILED",
        params={"floor_price": 131.44614999999993},
        error="price 131.44614999999993 is not a valid tick for A.XNYS",
    )
    base.update(over)
    return ManagerDTO(**base)


def test_the_DTO_DECLARES_the_timestamps():
    """The whole defect. An undeclared field is not an error — Pydantic drops it silently."""
    assert "created_at" in ManagerDTO.model_fields
    assert "updated_at" in ManagerDTO.model_fields


def test_a_timestamp_SURVIVES_the_response_model():
    """Construct it the way the route does and read it back out.

    Asserting on `model_fields` alone would pass with the field declared as the wrong type or excluded
    from serialisation, so this round-trips through `model_dump` — the hop that actually drops things.
    """
    stamped = datetime(2026, 8, 20, 18, 20, 41, tzinfo=UTC)
    dto = _dto(created_at=stamped, updated_at=stamped)

    dumped = dto.model_dump()
    assert dumped["created_at"] == stamped
    assert dumped["updated_at"] == stamped


def test_the_TWO_REAL_ROWS_are_distinguishable():
    """The actual point, with the actual rows, both read from Postgres on 2026-08-21.

    Before this, these two were the same object as far as any consumer could tell. One describes a bug
    fixed nine days ago; the other is a live defect that cost a re-entry this afternoon.
    """
    stale = _dto(
        kind="peak_watch",
        instrument_id="WDAY.XNAS",
        error="'dict' object has no attribute 'project'",
        created_at=datetime(2026, 8, 11, 18, 3, 26, tzinfo=UTC),
        updated_at=datetime(2026, 8, 11, 18, 3, 34, tzinfo=UTC),
    )
    fresh = _dto(
        created_at=datetime(2026, 8, 20, 18, 20, 41, tzinfo=UTC),
        updated_at=datetime(2026, 8, 20, 18, 20, 41, tzinfo=UTC),
    )

    assert stale.state == fresh.state == "FAILED", "same state — the state alone never separated them"
    assert stale.created_at != fresh.created_at
    assert (fresh.created_at - stale.created_at).days == 9


def test_a_row_with_NO_timestamp_is_None_not_a_crash_and_not_a_default():
    """Pre-migration rows exist. `None` means unknown, and must not be dressed up as a date — a
    consumer that read a defaulted `now()` would call a nine-day-old row current, which is the exact
    misreading this issue is about."""
    dto = _dto()
    assert dto.created_at is None
    assert dto.updated_at is None


def test_the_ROUTE_passes_them_through():
    """THE SEAM. Declaring the field on the DTO does nothing if the route never sets it.

    That is the shape all four of these defects share: the field exists at one end and at the other, and
    the hop between them drops it. Executed rather than scanned would need a live Postgres session; this
    reads the construction site, which is the one line that can silently omit a keyword argument.
    """
    import inspect

    from api import app as app_module

    src = inspect.getsource(app_module.get_managers)
    assert "created_at=r.created_at" in src
    assert "updated_at=r.updated_at" in src


def test_the_model_still_carries_the_columns():
    """Guards the ORM end: a migration that dropped the columns would make the projection lose them."""
    from api.db.models import Manager

    assert hasattr(Manager, "created_at")
    assert hasattr(Manager, "updated_at")


def test_THE_ROW_THE_ROUTE_ACTUALLY_READS_carries_them():
    """THE SEAM, AND THE ONE THIS FILE ORIGINALLY MISSED.

    The route does not read a `Manager`. It reads a `ManagerRow` — the projection `all_managers()`
    returns — and the first version of this suite asserted `hasattr(Manager, "updated_at")`, which is
    true and is about a different class. `/managers` then 500'd on EVERY call in production with
    `AttributeError: 'ManagerRow' object has no attribute 'updated_at'`, while the suite stayed green.

    A green test one class away from the code under test is the exact shape CLAUDE.md warns about, and
    it shipped anyway. This asserts the class the route dereferences.
    """
    import dataclasses

    from api.managers import ManagerRow

    fields = {f.name for f in dataclasses.fields(ManagerRow)}
    assert "created_at" in fields
    assert "updated_at" in fields


def test_the_projection_COPIES_the_timestamps_off_the_model():
    """A field on the dataclass that `_to_row` never fills is a field that is always None — the endpoint
    works and every row reads as undated, which is the defect #400 exists to remove, arriving quietly."""
    from datetime import datetime

    from api.managers import _to_row

    created = datetime(2026, 8, 11, 18, 3, 26, tzinfo=UTC)
    updated = datetime(2026, 8, 20, 18, 20, 41, tzinfo=UTC)

    class _M:
        manager_id = "m1"; kind = "peak_watch"; kind_version = 1
        account_id = "acct"; client_id = "ALPACA"; instrument_id = "WDAY.XNAS"
        strategy_id = "MANUAL-001"; cycle_id = None; leash = "AUTO"
        params: dict = {}; state = "FAILED"

    m = _M(); m.created_at = created; m.updated_at = updated
    row = _to_row(m)

    assert row.created_at == created
    assert row.updated_at == updated


def test_the_route_can_BUILD_a_dto_from_a_real_row():
    """Executed end to end: projection -> DTO, the exact hop that raised.

    `test_the_ROUTE_passes_them_through` scans the route's source for `updated_at=r.updated_at` and
    passed — because the line was there. What was missing was the attribute on `r`. Source-scanning the
    caller cannot see whether the callee has the field.
    """
    from datetime import datetime

    from api.managers import ManagerRow
    from api.models import ManagerDTO

    row = ManagerRow(
        manager_id="m1", kind="peak_watch", kind_version=1, account_id="acct", client_id="ALPACA",
        instrument_id="WDAY.XNAS", strategy_id="MANUAL-001", cycle_id=None, leash="AUTO",
        params={}, state="FAILED",
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
        updated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )

    dto = ManagerDTO(
        manager_id=row.manager_id, kind=row.kind, instrument_id=row.instrument_id,
        strategy_id=row.strategy_id, cycle_id=row.cycle_id, leash=row.leash, state=row.state,
        params=row.params, error=None,
        created_at=row.created_at, updated_at=row.updated_at,
    )
    assert dto.updated_at == row.updated_at
