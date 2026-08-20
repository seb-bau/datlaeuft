"""add safety inspection selection mode

Revision ID: a03f7c1d2e4b
Revises: 9929ee9e496f
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


revision = "a03f7c1d2e4b"
down_revision = "9929ee9e496f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "safety_inspection_group",
        sa.Column(
            "selection_mode",
            sa.String(length=30),
            server_default="whitelist",
            nullable=False,
        ),
    )


def downgrade():
    with op.batch_alter_table(
        "safety_inspection_group",
        schema=None,
    ) as batch_op:
        batch_op.drop_column(
            "selection_mode"
        )