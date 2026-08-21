"""add safety regex and use unit exit

Revision ID: c25f9e3a4b6d
Revises: b14e8d2f3a5c
Create Date: 2026-08-21
"""

from alembic import op
import sqlalchemy as sa


revision = "c25f9e3a4b6d"
down_revision = "b14e8d2f3a5c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "wowi_use_unit",
        sa.Column(
            "exit_date",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "safety_inspection_group",
        sa.Column(
            "id_num_regex",
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "safety_inspection_group",
        sa.Column(
            "id_num_regex_action",
            sa.String(length=20),
            nullable=True,
        ),
    )


def downgrade():
    with op.batch_alter_table(
        "safety_inspection_group",
        schema=None,
    ) as batch_op:
        batch_op.drop_column(
            "id_num_regex_action"
        )
        batch_op.drop_column(
            "id_num_regex"
        )

    with op.batch_alter_table(
        "wowi_use_unit",
        schema=None,
    ) as batch_op:
        batch_op.drop_column(
            "exit_date"
        )