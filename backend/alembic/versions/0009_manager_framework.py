"""manager / manager_event — generic automation framework (#55, first slice)

The foundation #55 specified but never had a concrete shape: a registry of automation "managers" — an
attach->watch->trigger->act lifecycle with a leash (AUTO/CONFIRM/ALERT), replacing yesterday's flatten-specific
`queued_flatten` table with the general primitive it was one instance of.

Two tables, matching the outbox lesson already learned today on `position_transfer_event`: `manager` is
current state (fast query), `manager_event` is the append-only audit/recovery log. A manager's `apply()` has
the identical partial-failure shape a transfer's leg-apply does (order submitted, DB write crashes) — an
INTENT_RECORDED event lands before the effect, so a crash between "order sent" and "row updated" is
recoverable by checking the durable Nautilus cache for the deterministic client_order_id, not guessed at.

`cycle_id` is load-bearing: without it, a manager attached to a position could apply against a DIFFERENT
cycle if the position closed and reopened while it sat ARMED — same instrument/strategy/side/qty by
coincidence, wrong intent (code review finding).

Deliberately does NOT drop `queued_flatten` in this migration (code review finding: this repo's compose
starts the engine independently of the api's migration step, so dropping a table an old process might still
reference mid-rollover is unsafe). Existing rows migrate into `manager`/`manager_event`; the old table is
left in place, unused, for a follow-up migration once the new path is proven.

Revision ID: 0009_manager_framework
Revises: 0008_queued_flatten
Create Date: 2026-07-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0009_manager_framework"
down_revision: Union[str, None] = "0008_queued_flatten"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "manager",
        sa.Column("manager_id", sa.String(length=36), primary_key=True),  # own UUID — NOT a command_id
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("kind_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        # The cycle this manager was attached against — an apply() whose live cycle no longer matches this
        # is refused, not blindly executed (a coincidental same instrument/strategy/side/qty match on a NEW
        # cycle is not the intent that was confirmed).
        sa.Column("cycle_id", sa.String(length=280), nullable=True),
        sa.Column("leash", sa.String(length=8), nullable=False),  # AUTO | CONFIRM | ALERT
        sa.Column("params", JSONB, nullable=False),  # kind-specific config
        # ARMED | PROPOSED | APPROVED | APPLYING | APPLIED | FAILED | CANCELLED
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column("last_event_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_manager_state", "manager", ["state"])
    op.create_index("ix_manager_strategy_id", "manager", ["strategy_id"])
    op.create_index("ix_manager_instrument_id", "manager", ["instrument_id"])
    op.create_index("ix_manager_kind", "manager", ["kind"])

    op.create_table(
        "manager_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("manager_id", sa.String(length=36), nullable=False),
        sa.Column(
            "event_type", sa.String(length=16), nullable=False
        ),  # ATTACHED|PROPOSED|APPROVED|REJECTED|INTENT_RECORDED|APPLIED|FAILED|CANCELLED
        # Transport idempotency for human-initiated events (ATTACHED/APPROVED/REJECTED) — reuses the SAME
        # CommandLedgerStore (#78) pattern as order commands rather than a second mechanism; command_id here
        # is for readability/audit joins, not the dedup guard itself (that's the ledger's job).
        sa.Column("command_id", sa.String(length=64), nullable=True),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column("event_ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_manager_event_manager_id", "manager_event", ["manager_id"])
    op.create_index("ix_manager_event_event_type", "manager_event", ["event_type"])

    # Carry forward any live queued_flatten rows — do not silently drop real user intent.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT command_id, instrument_id, strategy_id, expected_side, expected_qty, status, error "
            "FROM queued_flatten"
        )
    ).fetchall()
    manager_tbl = sa.table(
        "manager",
        sa.column("manager_id", sa.String),
        sa.column("kind", sa.String),
        sa.column("kind_version", sa.Integer),
        sa.column("account_id", sa.String),
        sa.column("client_id", sa.String),
        sa.column("instrument_id", sa.String),
        sa.column("strategy_id", sa.String),
        sa.column("cycle_id", sa.String),
        sa.column("leash", sa.String),
        sa.column("params", JSONB),
        sa.column("state", sa.String),
    )
    event_tbl = sa.table(
        "manager_event",
        sa.column("manager_id", sa.String),
        sa.column("event_type", sa.String),
        sa.column("command_id", sa.String),
        sa.column("detail", JSONB),
    )
    # QUEUED migrates to ARMED — untouched by any prior attempt, matching how a fresh startup reconcile
    # would resolve it. SUBMITTED/REJECTED are terminal and carry no live risk, but are preserved as
    # APPLIED/FAILED for audit continuity.
    # SUBMITTING is deliberately NOT mapped to ARMED (code review): that legacy state means the old code may
    # have already submitted an order (deterministic id `FL-{command_id[:20]}`) before crashing, and this
    # migration has no INTENT_RECORDED event to give the new recovery path — the one thing it needs to check
    # the cache instead of guessing. Landing it on ARMED would let a manager resubmit under a DIFFERENT id
    # (MGR-derived) without ever knowing the old order might already be live. FAILED is the honest "we
    # genuinely don't know" outcome; a human re-reads the position and re-confirms if it's still open.
    # Any status this map has never seen fails the SAME way, not silently to ARMED.
    _STATE_MAP = {"QUEUED": "ARMED", "SUBMITTED": "APPLIED", "REJECTED": "FAILED"}
    for r in rows:
        manager_id = f"MIGRATED-{r.command_id}"[:36]
        state = _STATE_MAP.get(r.status, "FAILED")
        unsafe_reason = "legacy SUBMITTING/unknown status cannot be safely resumed" if r.status not in _STATE_MAP else None
        # account_id/client_id were not columns on queued_flatten (it never needed them — the engine derived
        # them live at replay). Placeholder here is safe: this manager_id's OWN apply() re-derives them from
        # the live position exactly as it always did; these columns exist for future kinds, unused by replay.
        conn.execute(
            manager_tbl.insert().values(
                manager_id=manager_id,
                kind="deferred_flatten",
                kind_version=1,
                account_id="",
                client_id="",
                instrument_id=r.instrument_id,
                strategy_id=r.strategy_id,
                cycle_id=None,  # unknown for pre-migration rows — apply() treats null as "no cycle check"
                leash="AUTO",
                params={
                    "expected_side": r.expected_side,
                    "expected_qty": float(r.expected_qty) if r.expected_qty is not None else None,
                },
                state=state,
            )
        )
        conn.execute(
            event_tbl.insert().values(
                manager_id=manager_id,
                event_type="ATTACHED",
                command_id=r.command_id,
                detail={
                    "migrated_from": "queued_flatten",
                    "original_status": r.status,
                    "original_error": r.error,
                    **({"reason": unsafe_reason} if unsafe_reason else {}),
                },
            )
        )
        # `GET /managers` surfaces a FAILED reason by reading the most recent FAILED event's `detail.detail`
        # (api/app.py) — the ATTACHED event above is migration audit trail, not what that endpoint reads.
        # Without this, a migrated FAILED row would show up with no reason in the UI (code review).
        if state == "FAILED":
            conn.execute(
                event_tbl.insert().values(
                    manager_id=manager_id,
                    event_type="FAILED",
                    command_id=r.command_id,
                    detail={"detail": unsafe_reason or r.error or "migrated as failed from queued_flatten"},
                )
            )


def downgrade() -> None:
    op.drop_index("ix_manager_event_event_type", table_name="manager_event")
    op.drop_index("ix_manager_event_manager_id", table_name="manager_event")
    op.drop_table("manager_event")
    op.drop_index("ix_manager_kind", table_name="manager")
    op.drop_index("ix_manager_instrument_id", table_name="manager")
    op.drop_index("ix_manager_strategy_id", table_name="manager")
    op.drop_index("ix_manager_state", table_name="manager")
    op.drop_table("manager")
