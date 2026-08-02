"""repair commission position uniqueness

Revision ID: e1cf20bebec8
Revises: ae712522b0d1
Create Date: 2026-08-01 22:36:39.461375

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e1cf20bebec8'
down_revision = 'ae712522b0d1'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    target_columns = {"commission_id", "position_index"}
    existing_constraints = inspector.get_unique_constraints(
        "commission_position"
    )

    constraint_exists = any(
        set(constraint.get("column_names") or []) == target_columns
        for constraint in existing_constraints
    )

    if not constraint_exists:
        with op.batch_alter_table(
            "commission_position",
            schema=None,
        ) as batch_op:
            batch_op.create_unique_constraint(
                "uq_commission_position_index",
                ["commission_id", "position_index"],
            )


def downgrade():
    pass
