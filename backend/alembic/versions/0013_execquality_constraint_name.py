"""Rename the execution-quality unique constraint to the name the model declares (#210).

0012 built the name with an f-string — `f"uq_{_TABLE}_leg"` where `_TABLE` was already
`execution_quality_leg` — so the database got `uq_execution_quality_leg_leg` while the ORM model
declares `uq_execution_quality_leg`. Upserts were unaffected (`on_conflict_do_update` infers the
constraint from its index columns, not its name), but alembic autogenerate compares NAMES, so every
future unrelated migration would propose dropping and recreating this one.

Renaming rather than matching the model to the typo, so a fresh database and this one end up
identical instead of merely both working.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0013_execquality_constraint_name"
down_revision: Union[str, None] = "0012_execution_quality"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE execution_quality_leg "
               "RENAME CONSTRAINT uq_execution_quality_leg_leg TO uq_execution_quality_leg")


def downgrade() -> None:
    op.execute("ALTER TABLE execution_quality_leg "
               "RENAME CONSTRAINT uq_execution_quality_leg TO uq_execution_quality_leg_leg")
